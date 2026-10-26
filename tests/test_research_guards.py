"""Explicit fast/guarded contracts; these are deliberate off-mode controls."""
import json
import struct
import time

import pytest

from db2010 import cli
from db2010.execution import runner
from db2010.paths import ROOT
from support.fixtures import base_state, words, qemu_run
from test_native_checkpoint import engine, command, migration, stopped


def loop_state(thumb=False):
    state = base_state(struct.pack('<2H', 0x3001, 0xe7fd) if thumb else
                       words(0xe2800001, 0xeafffffd), cpsr=0xf3 if thumb else 0xd3)
    state.pop('instruction_limit')
    return state


def run_briefly(vm):
    command(vm, 'cont')
    until = time.monotonic()+3
    while time.monotonic() < until:
        progress = json.loads(command(vm, 'qom-get', {'path':'/machine', 'property':'progress'}))
        if progress['observations'] > 1000:
            break
    command(vm, 'stop')
    return vm.snapshot(time.monotonic()+5)


@pytest.mark.parametrize('thumb', [False, True])
def test_fast_native_count_and_reset_without_validation(tmp_path, thumb):
    with engine(loop_state(thumb), tmp_path/'run', research_guards=False) as vm:
        result = run_briefly(vm)
        assert result['research_guards'] is False
        assert result['counter_kind'] == 'qemu-icount'
        assert result['observations'] > 1000 and result['registers'][0] > 0
        assert 'stop' not in result
        command(vm, 'system_reset')
        reset = vm.summary(time.monotonic()+5)
        assert reset['research_guards'] is False
        assert reset['observations'] == 0 and reset['counter_kind'] == 'qemu-icount'


def test_mode_immutable_after_realization(tmp_path):
    with engine(loop_state(), tmp_path/'run', research_guards=False) as vm:
        with pytest.raises(RuntimeError, match='immutable'):
            command(vm, 'qom-set', {'path':'/machine', 'property':'research-guards', 'value':True})


@pytest.mark.parametrize('guarded', [False, True])
@pytest.mark.parametrize('thumb', [False, True])
def test_generated_blocks_exclude_optional_helpers(tmp_path, guarded, thumb):
    state = loop_state(thumb)
    state['regions'].append({'name':'data','address':0x2000,'size':4096,
        'seeds':[{'address':0x2000,'data':bytes(4)}]})
    state['regions'][0]['seeds'][0]['data'] = (
        struct.pack('<4H', 0x6811, 0x3101, 0x6011, 0xe7fb) if thumb else
        words(0xe5921000, 0xe2811001, 0xe5821000, 0xeafffffb))
    state['registers'][2] = 0x2000
    if guarded:
        state['instruction_limit'] = 16
    log = tmp_path/'tcg.log'
    with engine(state, tmp_path/'run', research_guards=guarded,
                extra_args=['-d','op','-D',str(log)]) as vm:
        result = stopped(vm) if guarded else run_briefly(vm)
        assert result['registers'][1] > 0
    generated = log.read_text()
    assert ('research_instruction' in generated) is guarded
    assert ('research_memory' in generated) is guarded
    assert 'board_instruction' not in generated


def test_fast_mmio_preserves_pre_effect_rejection(tmp_path):
    state = base_state(words(0xe5810000, 0xeafffffe))
    state['pl080_dma'] = {'profile':'cold-disabled-init','address':0xf2000000,'channels':8}
    state['registers'][:2] = [1, 0xf2000110]
    guarded = qemu_run(state, tmp_path/'guarded', trace=False)
    state.pop('instruction_limit')
    fast = qemu_run(state, tmp_path/'fast', trace=False, research_guards=False)
    assert fast['stop']['reason'] == guarded['stop']['reason']
    assert fast['stop']['address'] == guarded['stop']['address']
    assert fast['pl080'] == guarded['pl080']


def test_mmio_does_not_change_pc_relative_translation_baseline(tmp_path):
    # A successful I/O callback must leave the translator's cached PC baseline
    # intact. The branch skips one instruction after the MMIO write.
    state = base_state(words(0xe3a03001, 0xe5810030, 0xea000000,
                             0xe3a03063, 0xe2833001, 0xe5942000))
    state['pl080_dma'] = {'profile':'cold-disabled-init','address':0xf2000000,'channels':8}
    state['registers'][1] = 0xf2000000
    state['registers'][4] = 0x60000000
    guarded = qemu_run(state, tmp_path/'guarded', trace=False)
    state.pop('instruction_limit')
    fast = qemu_run(state, tmp_path/'fast', trace=False, research_guards=False)
    assert fast['stop']['reason'] == guarded['stop']['reason'] == 'read_unmapped'
    assert fast['registers'] == guarded['registers']
    assert fast['registers'][3] == 2
    assert fast['pl080'] == guarded['pl080']


@pytest.mark.parametrize('guarded', [False, True])
@pytest.mark.parametrize('kind', ['tcm', 'wfi', 'kgen', 'etx'])
def test_functional_hooks_survive_without_instruction_guards(tmp_path, guarded, kind):
    if kind == 'tcm':
        state = base_state(words(0xee090f11, 0xe3a0102a, 0xe5821000,
                                 0xe5923000, 0xeafffffe))
        state['registers'][0] = 0x10001d
        state['registers'][2] = 0x100000
    elif kind == 'wfi':
        from test_native_time import sleeping_peripheral_state
        state = sleeping_peripheral_state()
    elif kind == 'kgen':
        from support.kgen_fixtures import state as kgen_state
        state = kgen_state('init')
    else:
        from support.etx_fixtures import state as etx_state
        state = etx_state()
    state.pop('instruction_limit')
    with engine(state, tmp_path/'run', research_guards=guarded) as vm:
        result = run_briefly(vm)
        assert 'stop' not in result
        if kind == 'tcm':
            assert result['registers'][3] == 42
        elif kind == 'wfi':
            assert result['registers'][2] == 1
            assert result['f500_command_port']['completions'] == 1
            assert result['timer_irq']['pending'] and result['timer_irq']['irqs'] == 0
        elif kind == 'kgen':
            from support.kgen_fixtures import CTX, context_from
            from support.kgen_candidate import initialize
            memory = command(vm, 'human-monitor-command',
                             {'command-line':f'xp /23wx {CTX:#x}'})
            assert context_from({'memory_inspection':memory}) == initialize()
            assert result['synthetic_kgen']['init_calls'] == 1
        else:
            assert result['synthetic_etx']['completed_calls'] == 1
            assert result['device_registers'][str(0xf9030004)] == 1
        checkpoint = tmp_path/'functional.vmstate'
        migration(vm, 'migrate', checkpoint)
        layout = json.loads(command(vm, 'qom-get',
                                    {'path':'/machine', 'property':'checkpoint-layout'}))
    state['checkpoint_tcm'] = layout
    with engine(state, tmp_path/'restored', incoming=True, research_guards=guarded) as vm:
        migration(vm, 'migrate-incoming', checkpoint)
        restored = vm.snapshot(time.monotonic()+5)
        assert restored['registers'] == result['registers']
        assert restored['observations'] == result['observations']
        for key in ('tcm', 'synthetic_kgen', 'synthetic_etx', 'f500_command_port', 'timer_irq'):
            assert restored.get(key) == result.get(key)


def test_fast_ram_does_not_claim_unknown_backing_is_validated(tmp_path):
    state = base_state(words(0xe5910000, 0xe5923000))
    state['regions'].append({'name':'data', 'address':0x2000, 'size':4096, 'seeds':[]})
    state['registers'][1:3] = [0x2000, 0x60000000]
    guarded = qemu_run(state, tmp_path/'guarded', trace=False)
    state.pop('instruction_limit')
    fast = qemu_run(state, tmp_path/'fast', trace=False, research_guards=False)
    assert guarded['stop']['reason'] == 'read_unknown_ram'
    assert fast['stop']['reason'] == 'read_unmapped'
    assert fast['registers'][0] == 0 and fast['research_guards'] is False


@pytest.mark.parametrize('fetch', [False, True])
def test_fast_flash_holes_still_stop_in_device_model(tmp_path, fetch):
    from test_flash import storage_state
    state = storage_state(tmp_path)
    state.pop('instruction_limit')
    state['regions'][0]['seeds'][0]['data'] = words(0xe12fff10 if fetch else 0xe5d02000)
    state['registers'][0] = 0x44001000
    result = qemu_run(state, tmp_path/'fast', trace=False, research_guards=False)
    assert 'unknown' in result['stop']['reason']
    assert result['stop']['address'] == 0x44001000


def test_synthetic_decode_placeholder_does_not_supply_data(tmp_path):
    from support.kgen_fixtures import state as kgen_state
    state = kgen_state('init')
    state.pop('instruction_limit')
    state['regions'][0]['seeds'][0]['data'] = struct.pack('<H', 0x880a)  # LDRH r2,[r1]
    state['registers'][1] &= ~1
    result = qemu_run(state, tmp_path/'fast', trace=False, research_guards=False)
    assert result['stop']['reason'] == 'read_unmapped'
    assert result['synthetic_kgen']['init_calls'] == 0


def test_fast_nand_preserves_command_and_read_effects(tmp_path):
    from test_flash import storage_state, nand_ops
    from test_nand import write, read
    from support.nor_fixtures import program
    # Three ID reads, then a forbidden command; preserve the complete accepted
    # prefix and reject before changing NAND state, in both execution modes.
    operations = nand_ops([write(0x10,0xff), write(0x10,0x90), write(8,0),
                           write(0x20,0), read(), read(), read(), write(0x10,0x34)])
    code, limit, _ = program(operations)
    state = storage_state(tmp_path)
    state['regions'][0]['seeds'][0]['data'] = code
    state['instruction_limit'] = limit
    guarded = qemu_run(state, tmp_path/'guarded', trace=False)
    state.pop('instruction_limit')
    fast = qemu_run(state, tmp_path/'fast', trace=False, research_guards=False)
    assert fast['stop']['reason'] == guarded['stop']['reason']
    assert fast['nand'] == guarded['nand']
    assert fast['registers'] == guarded['registers']


def test_fast_nor_program_and_array_read_match_guarded(tmp_path):
    from test_flash import storage_state
    from support.nor_fixtures import program
    address = 0x45f00000
    code, limit, _ = program([
        ('write',2,address,0x40), ('write',2,address,0x1234),
        ('write',2,address,0xff), ('read',2,address,0), ('write',2,address,0x34)])
    state = storage_state(tmp_path)
    state['regions'][0]['seeds'][0]['data'] = code
    state['instruction_limit'] = limit
    guarded = qemu_run(state, tmp_path/'guarded', trace=False)
    state.pop('instruction_limit')
    fast = qemu_run(state, tmp_path/'fast', trace=False, research_guards=False)
    assert fast['stop']['reason'] == guarded['stop']['reason']
    assert fast['nor'] == guarded['nor']
    assert fast['registers'] == guarded['registers']
    assert fast['registers'][2] == 0x1234


def test_fast_display_does_not_claim_ram_provenance(tmp_path):
    from test_display_first_burst import prepare_channel5
    from db2010.diagnostics.artifacts import artifact
    prepared = tmp_path/'prepared'
    prepared.mkdir()
    path, _ = prepare_channel5(ROOT, None, prepared, 'stream_nonzero', frame=True, stream=True)
    state = json.loads(path.read_text())
    state.pop('instruction_limit')
    path.write_text(json.dumps(state))
    with engine(state, tmp_path/'run', prepared=path, research_guards=False) as vm:
        command(vm, 'cont')
        until = time.monotonic()+5
        while json.loads(command(vm, 'qom-get', {'path':'/machine', 'property':'progress'}))['lcd_frames'] < 1:
            assert time.monotonic() < until
        command(vm, 'stop')
        frame = vm.snapshot(time.monotonic()+5)['pdi']['frames'][0]
    assert frame['source_all_known'] is False
    assert 'provenance unchecked' in frame['transport']
    assert bytes.fromhex(frame['pixels_hex']) == artifact(prepared, 'source.bin').read_bytes()


def test_native_fast_checkpoint_and_cross_mode_rejection(tmp_path):
    path = tmp_path/'fast.vmstate'
    with engine(loop_state(), tmp_path/'first', research_guards=False) as vm:
        first = run_briefly(vm)
        migration(vm, 'migrate', path)
    with engine(loop_state(), tmp_path/'restored', incoming=True, research_guards=False) as vm:
        migration(vm, 'migrate-incoming', path)
        restored = vm.snapshot(time.monotonic()+5)
        assert restored['registers'] == first['registers']
        assert restored['observations'] == first['observations']
        assert run_briefly(vm)['observations'] > first['observations']
    with engine(loop_state(), tmp_path/'wrong-mode', incoming=True, research_guards=True) as vm:
        with pytest.raises((AssertionError, ConnectionError, EOFError)):
            migration(vm, 'migrate-incoming', path)
    assert 'research guard mode differs' in (tmp_path/'wrong-mode/engine.log').read_text()


def test_cli_rejects_limit_without_explicit_guards(monkeypatch, tmp_path):
    monkeypatch.setattr('sys.argv', ['db2010','--limit','1','--diagnostics',
                                   '--artifacts-dir',str(tmp_path),'--label','never'])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and not list(tmp_path.iterdir())


def test_public_api_rejects_fast_limit_before_preparation(tmp_path):
    result, failure = runner.run(ROOT, output=tmp_path/'never', instruction_limit=1)
    assert isinstance(failure, ValueError) and 'research-guards' in str(failure)
    assert not (tmp_path/'never').exists()


def test_public_api_default_is_uncapped_fast_and_records_native_count(tmp_path, monkeypatch):
    from test_runner import synthetic_preparation
    monkeypatch.setattr(runner, 'prepare', lambda *a, **kw: synthetic_preparation(*a))
    result, failure = runner.run(ROOT, seconds=.03)
    assert isinstance(failure, TimeoutError)
    assert result['research_guards'] is False
    assert result['capture_baseline']['research_guards'] is False
    assert result['snapshot']['counter_kind'] == 'qemu-icount'
    assert result['snapshot']['observations'] > 8
    assert result['execution_limits']['instruction_limit'] is None


def test_fast_tracing_and_diagnostics_do_not_enable_guards(tmp_path, monkeypatch):
    from support.fixtures import freeze_state
    state = base_state(words(0xe3a0002a, 0xe5923000))
    state['registers'][2] = 0x60000000
    monkeypatch.setattr(runner, 'prepare',
        lambda root, spec, output: (freeze_state(state, output), {'seconds':5}))
    output = tmp_path/'trace'
    result, failure = runner.run(ROOT, output=output, trace=True, diagnostics=True)
    assert failure is None
    assert result['snapshot']['research_guards'] is False
    assert result['snapshot']['stop']['reason'] == 'read_unmapped'
    assert result['snapshot']['registers'][0] == 42
    assert result['producer']['observations'] == result['collector']['observations'] > 0
    from db2010.diagnostics.artifacts import artifact
    argv = json.loads(artifact(output, 'command.json').read_text())
    assert 'research-guards=off' in argv[argv.index('-machine')+1]


def test_fast_host_checkpoint_mode_is_enforced_even_with_engine_override(tmp_path, monkeypatch):
    from test_runner import synthetic_preparation
    from db2010.execution.checkpoint import load_checkpoint
    monkeypatch.setattr(runner, 'prepare', lambda *a, **kw: synthetic_preparation(*a))
    output = tmp_path/'first'
    result, failure = runner.run(ROOT, output=output, seconds=.03,
                                 diagnostics=True, snapshot=True)
    assert isinstance(failure, TimeoutError)
    assert result['checkpoint'] == 'snapshots/final/snapshot.json'
    spec = json.loads((output/result['checkpoint']).read_text())
    assert spec['research_guards'] is False
    with pytest.raises(ValueError, match='guard mode differs'):
        load_checkpoint(output, ROOT/'qemu/build/qemu-system-arm',
                        research_guards=True, allow_engine_mismatch=True)
    resumed, failure = runner.run(ROOT, output=tmp_path/'resumed', restore=output,
                                  seconds=.03, diagnostics=True)
    assert isinstance(failure, TimeoutError)
    assert resumed['capture_baseline']['observations'] == result['snapshot']['observations']
    assert resumed['snapshot']['observations'] > result['snapshot']['observations']


def test_public_cli_defaults_off_and_passes_explicit_opt_in(monkeypatch):
    selected = []
    def capture(*args, **options):
        selected.append(options['research_guards'])
        return {'success':True,'phases':{},'uart':{}}, None
    monkeypatch.setattr(cli, 'run', capture)
    for extra in ([], ['--research-guards']):
        monkeypatch.setattr('sys.argv', ['db2010','--no-progress',*extra])
        cli.main()
    assert selected == [False, True]
