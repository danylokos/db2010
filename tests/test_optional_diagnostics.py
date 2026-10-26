"""Optional recording must not change execution or resource ownership."""
from support.guarded import run as guarded_run
from db2010.paths import DEFAULT_EXPERIMENT
import sys
from support.paths import RESOURCES
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
import os
import struct
import subprocess
from pathlib import Path

import pytest

from db2010 import cli
from db2010.execution import runner
from db2010.execution.debug import DebugOptions
from db2010.paths import ROOT
from db2010.preparation.inputs import prepare
from test_runner import synthetic_preparation
from support.controlled_clock import controlled_clock


@pytest.mark.parametrize('output', [False, True])
@pytest.mark.parametrize('trace,disassemble', [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize('debug', [False, True])
def test_cli_recording_options(tmp_path, monkeypatch, output, trace, disassemble, debug):
    calls = []

    def run(root, experiment, destination, **options):
        calls.append((destination, options))
        return {'success': True, 'uart': {}, 'phases': {}}, None

    arguments = ['db2010', '--research-guards']
    if output:
        arguments += ['--diagnostics', '--artifacts-dir', str(tmp_path), '--label', 'run']
    if trace:
        arguments += ['--trace']
    if disassemble:
        arguments += ['--disassemble']
    if debug:
        arguments += ['--gdb-command', 'info registers']
    monkeypatch.setattr(cli, 'run', run)
    monkeypatch.setattr('sys.argv', arguments)
    cli.main()
    destination, options = calls[0]
    assert destination is None
    assert options['diagnostics'] == output
    assert options['label'] == ('run' if output else None)
    assert options['trace'] == (trace or disassemble)
    assert (options['debug'] is not None) == debug
    assert options['config_limits'] is False
    assert not (tmp_path/'run').exists()


def test_removed_no_trace_flag_is_rejected(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['db2010', '--research-guards', '--no-trace'])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert 'unrecognized arguments: --no-trace' in capsys.readouterr().err


def test_cli_without_output_keeps_json_summary_and_creates_no_capture(tmp_path):
    process = subprocess.run([sys.executable, '-B', '-m', 'db2010', '--research-guards',
        '--experiment', str(DEFAULT_EXPERIMENT), '--limit', '1', '--json-summary'], cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(ROOT/'src')}, capture_output=True, text=True, timeout=20)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stderr)
    assert result['success'] and result['stop']['observations'] == 1
    assert result['trace_rows'] is None
    assert not list(tmp_path.iterdir())


@pytest.fixture
def temporary_runs(monkeypatch):
    directories = []
    original = runner.tempfile.TemporaryDirectory

    def temporary(*args, **options):
        context = original(*args, **options)
        directories.append(Path(context.name))
        return context

    monkeypatch.setattr(runner.tempfile, 'TemporaryDirectory', temporary)
    yield directories
    assert directories and all(not path.exists() for path in directories)


def uart_preparation(root, experiment, output, **options):
    path, _ = synthetic_preparation(root, experiment, output)
    # Four stores through the real UART owner, one byte on each host port.
    (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<5I', 0xE1C100B2, 0xE1C200B2,
        0xE1C300B2, 0xE1C400B2, 0xEAFFFFFE))
    state = json.loads(path.read_text())
    state['registers'][:5] = [65, 0xFB060000, 0xFC040000, 0xFC030000, 0xFB040000]
    state['executable_ranges'][0]['end'] = 0x1014
    state['uart_transmit'] = {'profile': 'polled-tx-immediate-drain', 'free_slots': 1}
    state['instruction_limit'] = 5
    path.write_text(json.dumps(state)+'\n')
    return path, {'seconds': 5}


def test_uart_and_snapshots_match_with_recording_disabled(tmp_path, monkeypatch, temporary_runs, controlled_clock):
    monkeypatch.setattr(runner, 'prepare', uart_preparation)
    recorded, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True)
    assert failure is None

    def forbidden(*args, **options):
        raise AssertionError('diagnostic collection without --output')

    monkeypatch.setattr(runner, 'file_identity', forbidden)
    monkeypatch.setattr(runner, 'save_diagnostics', forbidden)
    monkeypatch.setattr(runner, 'TraceCollector', forbidden)
    monkeypatch.setattr(runner, 'MemoryCollector', forbidden)
    monkeypatch.setattr(runner.QMP, 'snapshot', forbidden)
    unrecorded, failure = guarded_run(ROOT, Path('unused'))
    assert failure is None and unrecorded['success']
    for key in ('stop','observations','virtual_ns','uart_bytes'):
        assert unrecorded['snapshot'][key] == recorded['snapshot'][key]
    assert 'registers' not in unrecorded['snapshot']
    assert unrecorded['diagnostics_enabled'] is False
    assert recorded['diagnostics_enabled'] is True
    assert len(json.dumps(unrecorded['snapshot']))<4096
    assert [unrecorded['uart'][str(i)]['size_bytes'] for i in range(4)] == [1]*4
    assert all((artifact_path((tmp_path) / 'run', f'uart{i}.bin')).read_bytes() == b'A' for i in range(4))
    assert all('sha256' not in entry for entry in unrecorded['uart'].values())
    assert not (artifact_path((tmp_path) / 'run', 'trace.parquet')).exists()


def test_temporary_preparation_keeps_only_engine_inputs(tmp_path):
    state_path, _ = prepare(ROOT, DEFAULT_EXPERIMENT,
                            tmp_path, recording=False)
    state = json.loads(state_path.read_text())
    required = {state_path.relative_to(tmp_path).as_posix()}
    required.update(Path(proof['file']).relative_to(tmp_path).as_posix() for proof in state['execution_proofs'])
    from db2010.diagnostics.artifacts import input_references, input_path
    for name in input_references(state):
        path = input_path(tmp_path, name)
        assert path.is_file()
        if path.is_relative_to(tmp_path):
            required.add(path.relative_to(tmp_path).as_posix())
    assert {path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob('*') if path.is_file()} == required
    assert len(state['execution_proofs']) == 2


def test_preparation_failure_cleans_temporary_inputs(monkeypatch, temporary_runs):
    def rejected(root, experiment, output, **options):
        (artifact_path(output, 'partial.bin', create=True)).write_bytes(b'partial')
        raise ValueError('input authentication rejected')

    monkeypatch.setattr(runner, 'prepare', rejected)
    result, failure = guarded_run(ROOT, Path('unused'))
    assert isinstance(failure, ValueError)
    assert result['error']['message'] == 'input authentication rejected'
    assert not result['success'] and 'qemu_returncode' not in result


def test_engine_failure_keeps_bounded_context(tmp_path, monkeypatch, temporary_runs):
    monkeypatch.setattr(runner, 'prepare', uart_preparation)
    engine = tmp_path/'rejected-engine'
    engine.write_text('#!/bin/sh\nhead -c 200000 /dev/zero >&2\nprintf "ENGINE_REJECTED" >&2\nexit 1\n')
    engine.chmod(0o755)
    result, failure = guarded_run(ROOT, Path('unused'), engine_binary=engine)
    assert failure and not result['success'] and result['qemu_returncode'] == 1
    assert result['engine_error_context'].endswith('ENGINE_REJECTED')
    assert len(result['engine_error_context']) <= 4096


@pytest.mark.parametrize('failure_kind', ['interrupt', 'uart'])
def test_failure_reaps_engine_without_recording(monkeypatch, temporary_runs, failure_kind):
    monkeypatch.setattr(runner, 'prepare', uart_preparation)
    if failure_kind == 'interrupt':
        def interrupted(*args, **options):
            raise KeyboardInterrupt()
        monkeypatch.setattr(runner.QMP, 'wait_stop', interrupted)
        console = None
    else:
        class RejectedConsole:
            show_status = False

            def phase(self, name):
                pass

            def uart(self, port, data):
                raise OSError('UART consumer rejected bytes')
        console = RejectedConsole()
    result, failure = guarded_run(ROOT, Path('unused'), console=console)
    assert failure and not result['success'] and result['qemu_returncode'] is not None
    if failure_kind == 'interrupt':
        assert isinstance(failure, KeyboardInterrupt)
    else:
        assert 'capture' in str(failure) or result['capture_errors']


@pytest.mark.parametrize('reject', [False, True])
def test_gdb_without_output_uses_only_temporary_setup(tmp_path, monkeypatch, temporary_runs, reject):
    monkeypatch.setattr(runner, 'prepare', uart_preparation)
    script = tmp_path/'commands with spaces.gdb'
    script.write_text('info registers\n')
    action = ('command', 'not_a_gdb_command') if reject else ('script', script)
    original_close = runner.DebugSession.close

    def close(session):
        assert not any(path.suffix == '.log' or path.name in ('debug-session.json', 'gdb-history')
                       for path in session.output.rglob('*') if path.is_file())
        original_close(session)

    monkeypatch.setattr(runner.DebugSession, 'close', close)
    debug = DebugOptions(interactive=False, actions=[action])
    result, failure = guarded_run(ROOT, Path('unused'), debug=debug)
    assert bool(failure) == reject
    assert result['qemu_returncode'] is not None
    assert result['debugger']['returncode'] == (1 if reject else 0)
    assert 'sha256' not in result['debugger']
    if reject:
        assert 'Undefined command' in result['debugger']['error_context']
    else:
        assert result['snapshot']['observations'] == 0


def test_finalization_failure_is_returned_and_not_sealed(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'prepare', uart_preparation)

    def failed(*args):
        raise OSError('diagnostic destination full')

    monkeypatch.setattr(runner, 'save_diagnostics', failed)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True)
    assert isinstance(failure, OSError) and not result['success']
    assert result['qemu_returncode'] == 0
    assert not (tmp_path/'run/artifacts.sha256.json').exists()
    assert not json.loads((artifact_path((tmp_path) / 'run', 'result.json')).read_text())['success']


def test_existing_output_is_not_modified(tmp_path):
    marker = artifact_path(tmp_path, 'result.json', create=True)
    marker.write_text('existing capture')
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path, diagnostics=True)
    assert isinstance(failure, FileExistsError) and not result['success']
    assert marker.read_text() == 'existing capture'


@pytest.mark.parametrize('recording', [False, True])
def test_console_modes_preserve_qemu_uart_bytes_and_device_state(tmp_path, monkeypatch, temporary_runs, recording, controlled_clock):
    from db2010.ui.console import Console
    from test_console import Terminal
    payload = b'A\r\n\x1b[2K\xe2\x82\xac\xff\x00\b\t'
    def prepared(root, experiment, output, **options):
        path, spec = uart_preparation(root, experiment, output)
        words = []
        for byte in payload:
            words += [0xE3A00000 | byte, 0xE1C100B2, 0xE1C200B2, 0xE1C300B2, 0xE1C400B2]
        words.append(0xEAFFFFFE)
        artifact_path(output, 'code.bin').write_bytes(struct.pack('<'+'I'*len(words), *words))
        state = json.loads(path.read_text())
        state['executable_ranges'][0]['end'] = 0x1000+4*len(words)
        state['instruction_limit'] = len(words)
        path.write_text(json.dumps(state)+'\n')
        return path, spec
    monkeypatch.setattr(runner, 'prepare', prepared)
    monkeypatch.setenv('TERM', 'xterm')
    snapshots = []
    for uart, progress in [(False, True), (True, True), (True, False), (False, False)]:
        terminal = Terminal()
        console = Console(terminal, terminal, uart=uart, progress=progress)
        destination = tmp_path/f'capture-{uart}-{progress}' if recording else None
        result, failure = guarded_run(ROOT, Path('unused'), destination, diagnostics=recording, console=console)
        console.finish(result, failure)
        assert failure is None and result['qemu_returncode'] == 0
        assert result['diagnostics_enabled']==recording
        assert (result['run_directory'] is not None)==recording
        assert [result['uart'][str(port)]['size_bytes'] for port in range(4)] == [len(payload)]*4
        if recording:
            for port in range(4):
                assert artifact_path(destination, f'uart{port}.bin').read_bytes() == payload
        assert ('A\n' in terminal.getvalue()) == uart
        snapshots.append(result['snapshot'])
    assert all(snapshot == snapshots[0] for snapshot in snapshots)


def test_broken_console_streams_do_not_interrupt_capture_or_leave_resources(tmp_path, monkeypatch, temporary_runs):
    from io import StringIO
    from db2010.ui.console import Console
    class Broken(StringIO):
        def isatty(self):
            return True
        def write(self, value):
            raise BrokenPipeError()
    monkeypatch.setenv('TERM', 'xterm')
    monkeypatch.setattr(runner, 'prepare', uart_preparation)
    console = Console(Broken(), Broken(), uart=True)
    result, failure = guarded_run(ROOT, Path('unused'), console=console)
    console.finish(result, failure)
    assert failure is None and result['qemu_returncode'] == 0
    assert [result['uart'][str(port)]['size_bytes'] for port in range(4)] == [1]*4
