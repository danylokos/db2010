from support.guarded import run as guarded_run
import sys
from support.paths import RESOURCES
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
import os
from pathlib import Path
import select
import struct
import subprocess
import time
import threading

import pytest

from db2010.execution import runner
from db2010.execution.debug import DebugOptions
from db2010.paths import ROOT, DEFAULT_EXPERIMENT
from test_runner import synthetic_preparation


def arm_thumb_preparation(root, spec, output):
    path, _ = synthetic_preparation(root, spec, output)
    code = struct.pack('<5I3H', 0xE3A00001, 0xE59F1004, 0xE12FFF11, 0xE1A00000, 0x1015,
                       0x2207, 0x3201, 0x7823)
    (artifact_path(output, 'code.bin', create=True)).write_bytes(code)
    state = json.loads(path.read_text())
    state['executable_ranges'] = [{'start':0x1000, 'end':0x100C}, {'start':0x1014, 'end':0x101A}]
    state['regions'].append({'name':'fixture.ram', 'address':0x2000, 'size':4096, 'readonly':False, 'seeds':[]})
    path.write_text(json.dumps(state)+'\n')
    return path, {'seconds': 5}


def assert_reaped(result):
    assert result['qemu_returncode'] is not None
    with pytest.raises(ProcessLookupError):
        os.kill(result['debugger']['pid'], 0)


def debug_loop_preparation(root, spec, output):
    path, _ = synthetic_preparation(root, spec, output)
    return path, {'seconds': 5}


def test_gdb_script_breakpoint_arm_thumb_steps_and_guest_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'prepare', arm_thumb_preparation)
    script = tmp_path/'steps with spaces.gdb'
    script.write_text('''hbreak *0x1004
continue
printf "ARM_HIT:%x:%u\\n", $pc, $r0
delete breakpoints
stepi
stepi
printf "THUMB_ENTER:%x:%x\\n", $pc, $cpsr
stepi
stepi
printf "THUMB_RESULT:%x:%u\\n", $pc, $r2
set $r4=0x2000
x/1xb 0x1014
continue
printf "GUARD_STOP:%x\\n", $pc
''')
    debug = DebugOptions(interactive=False, actions=[('command','printf "BEFORE_SCRIPT\\n"'),
                       ('script',script), ('command','printf "AFTER_SCRIPT\\n"')])
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run with spaces', diagnostics=True, debug=debug, trace=True)
    assert failure is None and result['success']
    log = (artifact_path((tmp_path) / 'run with spaces', 'gdb-console.log')).read_text()
    assert 'ARM_HIT:1004:1' in log and 'THUMB_ENTER:1014:f3' in log and 'THUMB_RESULT:1018:8' in log
    assert log.index('BEFORE_SCRIPT') < log.index('ARM_HIT') < log.index('AFTER_SCRIPT')
    assert result['snapshot']['stop']['reason'] == 'read_unknown_ram'
    assert result['snapshot']['stop']['address'] == 0x2000
    assert (artifact_path((tmp_path) / 'run with spaces', 'gdb-output.log')).exists()
    assert (artifact_path((tmp_path) / 'run with spaces', 'gdb-script-001.gdb')).read_bytes() == script.read_bytes()
    assert result['debugger']['intervention'] and result['session_end'] == 'debugger_exit'
    assert_reaped(result)


def test_gdb_error_preserves_diagnostics_and_reaps(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'prepare', debug_loop_preparation)
    debug = DebugOptions(interactive=False, actions=[('command','not_a_gdb_command')])
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, debug=debug, trace=True)
    assert failure is not None and not result['success']
    assert result['debugger']['returncode'] == 1
    assert 'Undefined command' in (artifact_path((tmp_path) / 'run', 'gdb-console.log')).read_text()
    assert_reaped(result)


def test_gdb_session_deadline_reaps_both_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'prepare', debug_loop_preparation)
    debug = DebugOptions(interactive=False, actions=[('command','continue')])
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, debug=debug, seconds=1, trace=True)
    assert isinstance(failure, TimeoutError) and not result['success']
    assert result['debugger']['terminated_by_runner']
    assert_reaped(result)


def test_gdb_unknown_execution_stays_guarded(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'prepare', debug_loop_preparation)
    debug = DebugOptions(interactive=False, actions=[('command','set $pc=0x2000'), ('command','continue')])
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, debug=debug, trace=True)
    assert failure is None and result['success']
    assert result['snapshot']['stop']['reason'] == 'fetch_outside_executable_bytes'
    assert result['snapshot']['stop']['address'] == 0x2000
    assert_reaped(result)


@pytest.mark.parametrize('command', ['quit', 'disconnect'])
def test_debugger_exit_does_not_resume_guest(tmp_path, monkeypatch, command):
    monkeypatch.setattr(runner, 'prepare', debug_loop_preparation)
    debug = DebugOptions(interactive=False, actions=[('command', command)])
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, trace=True, debug=debug)
    assert failure is None and result['success']
    assert result['snapshot']['observations'] == 0
    assert result['producer']['observations'] == 0
    assert_reaped(result)


def test_missing_debugger_rejected_before_engine_start(tmp_path):
    debug = DebugOptions(executable=str(tmp_path/'missing-gdb'))
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, debug=debug, trace=True)
    assert isinstance(failure, FileNotFoundError) and not result['success']
    assert not (artifact_path((tmp_path) / 'run', 'command.json')).exists()


def test_debugger_crash_stops_and_reaps_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'prepare', debug_loop_preparation)
    original = subprocess.Popen
    timers = []
    def popen(command, **kwargs):
        process = original(command, **kwargs)
        if kwargs.get('start_new_session') and Path(command[0]).name == 'gdb-multiarch':
            timer = threading.Timer(0.5, lambda: process.kill() if process.poll() is None else None)
            timer.start();timers.append(timer)
        return process
    monkeypatch.setattr(subprocess, 'Popen', popen)
    debug = DebugOptions(interactive=False, actions=[('command','continue')])
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, debug=debug, trace=True)
    for timer in timers:
        timer.join()
    assert failure is not None and not result['success']
    assert result['debugger']['returncode'] == -9
    assert result['snapshot']['observations'] > 0
    assert_reaped(result)


@pytest.mark.parametrize('recording', [False, True])
def test_interactive_gdb_inherited_terminal(tmp_path, monkeypatch, recording):
    # Exercise the real interactive process and shared capture supervisor through
    # a PTY, without requiring this pytest process to own a terminal.
    import pty
    master, slave = pty.openpty()
    output = tmp_path/'interactive'
    command = [sys.executable, '-B', '-m', 'db2010', '--research-guards', '--debug', '--experiment', str(DEFAULT_EXPERIMENT)]
    if recording:
        command.extend(['--diagnostics', '--artifacts-dir', str(output.parent), '--label', output.name])
    process = subprocess.Popen(command, cwd=ROOT, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    text = bytearray()
    def until(marker):
        deadline = time.monotonic()+15
        while marker not in text:
            if time.monotonic() > deadline:
                raise AssertionError(text.decode(errors='replace'))
            if select.select([master], [], [], 0.1)[0]:
                text.extend(os.read(master, 65536))
    try:
        until(b'(gdb)')
        os.write(master, b'printf "INTERACTIVE_PC:%x\\n", $pc\n')
        until(b'INTERACTIVE_PC:44020114')
        os.write(master, b'quit\n')
        process.wait(timeout=15)
        assert process.returncode == 0, text.decode(errors='replace')
        if recording:
            result = json.loads((artifact_path(output, 'result.json')).read_text())
            assert result['success'] and result['debugger']['wall_limit_seconds'] is None
            assert 'INTERACTIVE_PC' in (artifact_path(output, 'gdb-history')).read_text()
            assert_reaped(result)
        else:
            assert not output.exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9);process.wait()
        os.close(master)
