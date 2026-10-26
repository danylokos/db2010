"""Debug-cable selection, exclusive UART ownership and reproducible CLI runs."""
from support.guarded import run as guarded_run
import json
from pathlib import Path
import shlex
import struct

import pytest

from db2010 import cli
from db2010.diagnostics.artifacts import artifact
from db2010.diagnostics.retention import regeneration_command
from db2010.execution import runner
from db2010.execution.serial import SerialLink
from db2010.paths import ROOT
from test_runner import synthetic_preparation


@pytest.mark.parametrize('flags,debug,visible', [([], False, False),
    (['--uart'], False, True), (['--uart-debug'], True, True)])
def test_cli_mode_and_live_output(monkeypatch, capsys, flags, debug, visible):
    def run(*args, **options):
        assert options['uart_debug'] is debug
        assert args[1] == cli.DEFAULT_EXPERIMENT.resolve()
        options['console'].uart(0, b'boot log\n')
        return {'success': True, 'phases': {}, 'uart': {}}, None
    monkeypatch.setattr(cli, 'run', run)
    monkeypatch.setattr('sys.argv', ['db2010', '--research-guards', *flags])
    cli.main()
    assert capsys.readouterr().out == ('boot log\n' if visible else '')


def test_help_explains_exclusive_modes(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['db2010', '--research-guards', '--help'])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    help_text = ' '.join(capsys.readouterr().out.split())
    assert 'debug logging and default OBEX file transfer are mutually exclusive' in help_text


def test_restore_rejects_mode_override_before_launch(monkeypatch, capsys):
    monkeypatch.setattr(cli, 'run', lambda *a, **kw: pytest.fail('launched restore'))
    monkeypatch.setattr('sys.argv', ['db2010', '--research-guards', '--restore', 'saved', '--uart-debug'])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert '--restore preserves its saved serial mode' in capsys.readouterr().err


@pytest.mark.parametrize('debug', [False, True])
def test_native_pad_selection_transport_owner_and_regeneration(tmp_path, monkeypatch, debug):
    def prepare(root, experiment, output):
        path, _ = synthetic_preparation(root, experiment, output)
        # Native LDRH samples the input pads, then stays in original fixture code.
        artifact(output, 'code.bin').write_bytes(struct.pack('<2I', 0xE1D120B0, 0xEAFFFFFE))
        state = json.loads(path.read_text())
        state['executable_ranges'][0]['end'] = 0x1008
        state['registers'][1] = 0xF900000A
        state['serial_lines'] = {'profile': 'port1-rx-levels',
                                 'uart0_rx_level': 1, 'uart1_rx_level': 1}
        state['serial_transport'] = {'profile': 'uart0-9600-fifo-assumption'}
        state['uart_transmit'] = {'profile': 'polled-tx-immediate-drain', 'free_slots': 1}
        state['device_registers'] = [dict(address=a, width=2, initial_value=0,
            read_or_mask=0, name='fixture.gpio') for a in (0xF900000A, 0xF900000C)]
        path.write_text(json.dumps(state)+'\n')
        return path, {'seconds': 5}
    class Service:
        link = None
        def attach(self, link): self.link = link
        def detach(self): pass
        def progress(self, sample): pass
    service = Service()
    monkeypatch.setattr(runner, 'prepare', prepare)
    output = tmp_path/'run'
    result, failure = guarded_run(ROOT, Path('unused'), output, diagnostics=True,
        instruction_limit=2, uart_debug=debug, serial_service=service, snapshot=debug)
    assert failure is None and result['success']
    assert result['snapshot']['registers'][2] == (1 if debug else 0x11)
    assert (service.link is None) is debug
    effective = json.loads(artifact(output, 'state.json').read_text())
    assert ('serial_transport' not in effective) is debug
    # The frozen experiment stays intact; replay carries the explicit override.
    assert json.loads(artifact(output, 'experiment.json').read_text()) == {'seconds': 5}
    assert ('--uart-debug' in shlex.split(regeneration_command(output))) is debug
    if debug:
        restored_service = Service()
        restored, error = guarded_run(ROOT, output=tmp_path/'restored', restore=output,
            diagnostics=True, instruction_limit=2, serial_service=restored_service)
        assert error is None and restored['success']
        assert restored_service.link is None
        assert restored['snapshot']['registers'][2] == 1
        saved = json.loads(artifact(tmp_path/'restored', 'state.json').read_text())
        assert saved['serial_lines'] == effective['serial_lines']
        assert 'serial_transport' not in saved


@pytest.mark.parametrize('options', [{'restore': 'unused'}, {'replay': 'unused'},
                                  {'serial_link': SerialLink()}])
def test_runner_rejects_conflicting_start_or_peer_before_preparing(tmp_path, monkeypatch, options):
    monkeypatch.setattr(runner, 'prepare', lambda *a, **kw: pytest.fail('prepared conflicting mode'))
    result, failure = guarded_run(ROOT, output=tmp_path/'run', diagnostics=True,
                                 uart_debug=True, **options)
    assert isinstance(failure, ValueError) and not result['success']
    assert not (tmp_path/'run').exists()
