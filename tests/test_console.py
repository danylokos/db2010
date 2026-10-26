"""Terminal coordination and byte-stream behavior without losing UART evidence."""
from io import StringIO

import pytest

from db2010.ui.console import Console


class Terminal(StringIO):
    def isatty(self):
        return True


def sample(count=1000000):
    return {'observations': count, 'pc': 0x44b3c714, 'running': True,
            'uart_bytes': [100, 0, 0, 0], 'lcd_frames': 2,
            'timer_ticks': 131072, 'timer_hz': 131072, 'timer_irqs': 300}


def test_progress_and_final_summary_omit_cpu_frequency_and_lcd_counter(monkeypatch):
    monkeypatch.setenv('TERM', 'xterm')
    status = Terminal()
    console = Console(StringIO(), status)
    console.phase('run')
    for hz in (None, 13000000, 52000000, 104000000):
        console.progress(dict(sample(), cpu_hz=hz))
        text = status.getvalue().split('\033[K')[-1]
        assert 'PC 44B3C714' in text
        assert 'CPU' not in text and 'MHz' not in text and 'LCD' not in text
    console.finish({'snapshot':{'cpu_hz':104000000}})
    assert 'CPU' not in status.getvalue().splitlines()[-1]


def test_redirected_summary_omits_unknown_cpu():
    status = StringIO()
    Console(StringIO(), status).finish({'snapshot':{'cpu_hz':None}})
    assert 'CPU' not in status.getvalue()


def test_partial_uart_hides_ticker_until_newline_and_utf8_fragments_survive(monkeypatch):
    monkeypatch.setenv('TERM', 'xterm')
    terminal = Terminal()
    console = Console(terminal, terminal, uart=True)
    console.phase('run')
    console.progress(sample())
    assert 'PC 44B3C714' in terminal.getvalue()
    console.uart(0, b'partial \xe2')
    index = terminal.tell()
    console.progress(sample(2000000))
    assert terminal.tell() == index  # No status overwrites the partial line.
    console.uart(0, b'\x82\xac\n')
    assert terminal.getvalue()[index:].startswith('\u20ac\n\r\033[Krun')
    assert console.visible and not console.partial


def test_redirected_stdout_is_streamed_once_without_status_or_final_newline():
    output, status = StringIO(), StringIO()
    console = Console(output, status, uart=True)
    console.phase('run')
    console.progress(sample())
    console.uart(0, b'hello\nlast \xff\xe2')
    assert output.getvalue() == 'hello\nlast \\xff'
    console.finish({'snapshot': {'stop': {'reason': 'instruction_limit', 'pc': 0x1000, 'observations': 12}}})
    assert output.getvalue() == 'hello\nlast \\xff\\xe2'
    assert '\033' not in status.getvalue() and 'Stopped: instruction_limit' in status.getvalue()
    assert '12 insn' in status.getvalue()


def test_separate_stderr_terminal_keeps_status_with_redirected_partial_uart(monkeypatch):
    monkeypatch.setenv('TERM', 'xterm')
    output, status = StringIO(), Terminal()
    console = Console(output, status, uart=True)
    console.uart(0, b'partial')
    console.progress(sample())
    assert console.visible and 'PC 44B3C714' in status.getvalue()
    assert output.getvalue() == 'partial'


def test_port_switch_labels_are_not_repeated_per_fragment():
    output = StringIO()
    console = Console(output, StringIO(), uart=True)
    console.uart(0, b'zero\n')
    console.uart(1, b'one')
    console.uart(1, b' tail\n')
    console.uart(0, b'again\n')
    assert output.getvalue() == 'zero\n[UART1] one tail\n[UART0] again\n'


def test_dumb_terminal_and_debug_mode_have_no_ticker(monkeypatch):
    monkeypatch.setenv('TERM', 'dumb')
    assert not Console(Terminal(), Terminal()).show_status
    monkeypatch.setenv('TERM', 'xterm')
    assert not Console(Terminal(), Terminal(), progress=False).show_status


def test_closed_output_pipe_does_not_abort_uart_capture():
    class ClosedPipe(StringIO):
        def write(self, text):
            raise BrokenPipeError()
    console = Console(ClosedPipe(), StringIO(), uart=True)
    console.uart(0, b'first')
    console.uart(0, b'second')
    assert not console.output_open


def test_status_never_wraps_and_clears_before_uart(monkeypatch):
    monkeypatch.setenv('TERM', 'xterm')
    monkeypatch.setattr('db2010.ui.console.os.get_terminal_size', lambda fd: type('Size', (), {'columns': 55})())
    class NarrowTerminal(Terminal):
        def fileno(self):
            return 42
    terminal = NarrowTerminal()
    console = Console(terminal, terminal, uart=True)
    console.progress(sample())
    rendered = terminal.getvalue().split('\033[K')[-1].rstrip('\r')
    assert len(rendered) < 55 and '\n' not in rendered
    mark = terminal.tell()
    console.uart(0, b'new\n')
    assert terminal.getvalue()[mark:].startswith('\r\033[Jnew\n')


def test_default_console_does_not_decode_or_display_uart():
    output, status = StringIO(), StringIO()
    console = Console(output, status)
    console.uart(0, b'firmware\xff\xe2')
    console.finish({})
    assert output.getvalue() == ''
    assert status.getvalue().startswith('Stopped: complete')


def test_bare_cr_crlf_and_nonprinting_characters_are_safe_on_terminal(monkeypatch):
    monkeypatch.setenv('TERM', 'xterm')
    terminal = Terminal()
    console = Console(terminal, terminal, uart=True, progress=False)
    console.uart(0, b'first\r')
    console.uart(0, b'\nsecond\rthird\n\x1b[2K\b\t\x00')
    console.uart(0, '\u009b\u202e\u200d'.encode())
    console.finish({}, summary=False)
    assert terminal.getvalue() == 'first\nsecond\nthird\n\\x1b[2K\\x08\\x09\\x00\\x9b\\u202e\\u200d\n'


def test_distinct_terminals_do_not_hide_each_others_progress(monkeypatch):
    monkeypatch.setenv('TERM', 'xterm')
    output, status = Terminal(), Terminal()
    console = Console(output, status, uart=True)
    assert not console.shared_terminal
    console.uart(0, b'partial')
    console.phase('run')
    assert console.visible and output.getvalue() == 'partial'


@pytest.mark.parametrize('stream_kind', ['output', 'status'])
@pytest.mark.parametrize('exception', [BrokenPipeError, OSError, ValueError])
def test_console_io_failures_disable_only_the_failed_stream(monkeypatch, stream_kind, exception):
    monkeypatch.setenv('TERM', 'xterm')
    class Failed(Terminal):
        def write(self, text):
            raise exception('closed console')
    output = Failed() if stream_kind == 'output' else StringIO()
    status = Failed() if stream_kind == 'status' else Terminal()
    console = Console(output, status, uart=True)
    console.phase('run')
    console.uart(0, b'bytes\xe2')
    console.finish({}, OSError('test failure'))
    assert console.output_open == (stream_kind != 'output')
    assert console.status_open == (stream_kind != 'status')
    if stream_kind == 'status':
        assert output.getvalue() == 'bytes\\xe2'


def test_finish_reports_safe_bounded_error_and_interruption():
    status = StringIO()
    console = Console(StringIO(), status)
    console.finish({'engine_error_context': 'a\n'*8+'\x1b[2K'}, ValueError('bad\r\x1b[2K'))
    assert status.getvalue().startswith('Failed: bad\\x0d\\x1b[2K')
    assert '\x1b' not in status.getvalue() and len(status.getvalue().splitlines()) == 5
    status = StringIO()
    Console(StringIO(), status).finish({}, KeyboardInterrupt())
    assert status.getvalue().startswith('Interrupted')
