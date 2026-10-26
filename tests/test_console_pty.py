"""Exercise actual terminal descriptors, line discipline, routing and resizing."""
import errno
import os
import select
import subprocess
import sys
import termios
import time

import pytest

from db2010.paths import ROOT
from support.terminal import rendered_text


def terminal_process(script, *, stdout_terminal=True, stderr_terminal=True, term='xterm'):
    master, slave = os.openpty()
    termios.tcsetwinsize(slave, (24, 100))
    process = subprocess.Popen([sys.executable, '-B', '-c', script], cwd=ROOT,
        stdout=slave if stdout_terminal else subprocess.PIPE,
        stderr=slave if stderr_terminal else subprocess.PIPE,
        env={**os.environ, 'TERM': term})
    os.close(slave)
    readers = {master: bytearray()}
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            readers[stream.fileno()] = bytearray()
    active = set(readers)
    deadline = time.monotonic()+15
    try:
        while active:
            assert time.monotonic() < deadline, 'terminal subprocess stalled'
            for fd in select.select(list(active), [], [], 0.1)[0]:
                try:
                    data = os.read(fd, 65536)
                except OSError as exc:
                    if fd == master and exc.errno == errno.EIO:
                        data = b''
                    else:
                        raise
                if not data:
                    active.remove(fd)
                readers[fd].extend(data)
        assert process.wait(timeout=2) == 0
        return (bytes(readers[master]),
                bytes(readers[process.stdout.fileno()]) if process.stdout else b'',
                bytes(readers[process.stderr.fileno()]) if process.stderr else b'')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()


@pytest.mark.parametrize('uart,progress', [(False, True), (True, True), (True, False), (False, False)])
def test_real_terminal_modes_resize_fragments_and_control_characters(uart, progress):
    script = f'''
import os, sys, termios
from db2010.ui.console import Console
c = Console(uart={uart!r}, progress={progress!r})
assert c.shared_terminal
c.phase('run')
c.uart(0, b'firmware ready\\r')
c.phase('run')
c.uart(0, b'\\nnext stage\\n')
c.uart(0, b'partial \\xe2')
c.phase('run')
c.uart(1, b'other\\r')
c.uart(0, b'\\x82\\xac\\x1b[2K\\b\\x00\\t\\n')
termios.tcsetwinsize(sys.stderr.fileno(), (24, 35))
c.phase('capture')
c.finish({{'snapshot': {{'stop': {{'reason': 'complete'}}}}}})
c.finish({{}})  # No duplicate replay/summary on repeated cleanup.
'''
    raw, _, _ = terminal_process(script)
    assert (b'\x1b[K' in raw) == progress
    assert raw.count(b'Stopped: complete') == 1
    text = rendered_text(raw)
    if uart:
        assert text.startswith('firmware ready\nnext stage\npartial \n[UART1] other\n[UART0] \u20ac\\x1b[2K\\x08\\x00\\x09\n')
        assert raw.count(b'firmware ready') == 1
        assert b'\x1b[2K' not in raw and b'\x08' not in raw
    else:
        assert text.startswith('Stopped: complete')
    # Actual ioctl resize must shorten the next ticker, never wrapping it.
    if progress:
        final_ticker = raw.split(b'\x1b[K')[-1].split(b'\r')[0]
        assert final_ticker.startswith(b'capture ') and len(final_ticker) < 35


@pytest.mark.parametrize('stdout_terminal,stderr_terminal,term', [
    (False, False, 'xterm'), (False, True, 'xterm'), (True, False, 'xterm'),
    (True, True, 'dumb')])
def test_real_stream_routing_and_dumb_terminals(stdout_terminal, stderr_terminal, term):
    raw, output, status = terminal_process('''
from db2010.ui.console import Console
c = Console(uart=True)
c.phase('run')
c.uart(0, b'hello\\r\\npartial\\x1b[2K\\xff\\xe2')
c.phase('capture')
c.finish({})
''', stdout_terminal=stdout_terminal, stderr_terminal=stderr_terminal, term=term)
    if not stdout_terminal:
        assert output == b'hello\r\npartial\x1b[2K\\xff\\xe2'
    else:
        assert b'partial\\x1b[2K\\xff\\xe2\r\n' in raw
    assert (b'\x1b[K' in raw) == (stderr_terminal and term != 'dumb')
    if not stderr_terminal:
        assert status.startswith(b'Stopped: complete') and b'\x1b' not in status


def test_real_terminal_concurrent_uart_ports_and_progress():
    raw, _, _ = terminal_process('''
from concurrent.futures import ThreadPoolExecutor
from db2010.ui.console import Console
c = Console(uart=True)
def send(port):
    for n in range(20):
        c.uart(port, f'p{port}-{n}\\n'.encode())
        c.phase('run')
with ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(send, range(4)))
c.finish({})
''')
    text = rendered_text(raw)
    import re
    messages = re.findall(r'p\d-\d+', text)
    assert sorted(messages) == sorted(f'p{port}-{n}' for port in range(4) for n in range(20))


def test_broken_standard_pipes_exit_cleanly():
    script = '''
from db2010.ui.console import Console
c = Console(uart=True)
c.uart(0, b'hello')
c.finish({})
c.write_summary('{}')
'''
    for destination in ('stdout', 'stderr'):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        try:
            process = subprocess.run([sys.executable, '-B', '-c', script], cwd=ROOT,
                stdout=write_fd if destination == 'stdout' else subprocess.PIPE,
                stderr=write_fd if destination == 'stderr' else subprocess.PIPE, timeout=10)
            assert process.returncode == 0, process.stderr
        finally:
            os.close(write_fd)
