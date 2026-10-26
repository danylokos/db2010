"""Persistent storage bytes are cold inputs, never execution checkpoints."""
from support.guarded import run as guarded_run
import copy
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest

from db2010 import cli
from db2010.diagnostics.artifacts import RunArtifacts, artifact, identity, seal
from db2010.diagnostics.retention import prepare as prune, regeneration_command, verify as retained_verify
from db2010.diagnostics.verify import verify
from db2010.execution import flash, runner
from db2010.paths import ROOT, RESOURCES
from db2010.preparation.inputs import prepare
from support.controlled_clock import controlled_clock
from support.fixtures import base_state, words
from support.nor_fixtures import program as nor_program
from support.paths import BASELINE_MODELS, FIRMWARE_MANIFEST
from test_native_checkpoint import engine, command, stopped
from test_nand import write, block_erase, program, spare_read
from test_nand_main import main_program, main_read

EXPERIMENT = RESOURCES/'boot.json'


def test_main_expansion_authentication_tail_and_execution_bounds(tmp_path):
    path, _ = prepare(ROOT, EXPERIMENT, tmp_path/'original')
    state = json.loads(path.read_text())
    image = Path(state['regions'][0]['seeds'][0]['file']).read_bytes()
    payload_size = 0x451C4BB8-0x44020000
    assert len(image) == 20840448
    assert image[payload_size:] == b'\xff'*(len(image)-payload_size)
    assert all(seed['address'] >= 0x44020000 for seed in state['regions'][0]['seeds'])
    manifest = json.loads(FIRMWARE_MANIFEST.read_text())
    original = next(e for e in manifest['entries'] if e['id'] == 'main')
    short = tmp_path/'short.bin'; short.write_bytes(image[:payload_size])
    tail = tmp_path/'tail.bin'; tail.write_bytes(image[:-4]+b'tail')
    for supplied in (short, tail):
        output = tmp_path/supplied.stem
        path, _ = prepare(ROOT, EXPERIMENT, output, images={'main': supplied})
        loaded = json.loads(path.read_text())
        assert loaded['executable_ranges'] == state['executable_ranges']
        assert loaded['executable_ranges'][0]['end'] == original['end']
        data = Path(loaded['regions'][0]['seeds'][0]['file']).read_bytes()
        assert data == (image if supplied == short else tail.read_bytes())
    corrupt = bytearray(image); corrupt[100] ^= 1
    tail.write_bytes(corrupt)
    with pytest.raises(ValueError, match='identity'):
        prepare(ROOT, EXPERIMENT, tmp_path/'bad', images={'main': tail})


def storage_state(tmp_path, operations=()):
    code, limit, _ = nor_program(operations)
    state = base_state(code, limit=limit)
    profiles = json.loads(BASELINE_MODELS.read_text())
    state['nor_model'] = copy.deepcopy(profiles['nor_model'])
    state['nor_model']['initial_locked'] = False
    state['regions'].append({'name': 'fixture.nor', 'address': 0x44000000, 'size': 0x2000000,
        'readonly': True, 'seeds': [{'address': 0x44000000, 'data': b'prefix'},
                                  {'address': 0x453ffffe, 'data': b'\x12\x34'},
                                  {'address': 0x45f00000, 'data': b'\xff\xff'}]})
    seed = tmp_path/'spare-seeds.bin'; seed.write_bytes(b'\x00\x00\x0e\x54')
    state['nand'] = {**profiles['nand'], 'prepared_spare': str(seed),
        'block_erase': {'profile': 'samsung-x8-block-erase-immediate'},
        'main_array': {'profile': 'samsung-x8-known-main-read-program'}}
    return state


def export(vm, path):
    path.mkdir()
    command(vm, 'qom-set', {'path': '/machine', 'property': 'flash-byte-export', 'value': str(path)})
    return {name: (path/(name+'.bin')).read_bytes() for name in flash.SIZES}


def nand_ops(ops):
    return [('write' if kind == 'write' else 'read', 1, 0x50000000+off, value)
            for kind, off, value in ops]


def test_native_committed_bytes_unknown_fill_staging_and_no_mutation(tmp_path):
    ops = [('write', 2, 0x45f00000, 0x40), ('write', 2, 0x45f00000, 0x1234)]
    ops += nand_ops([write(0x10, 0xff), *block_erase(32), *main_program(33, b'\x7f'),
                    *program(33, 5, 0x3f), *main_program(34, b'\x00')[:-1]])
    state = storage_state(tmp_path, ops)
    with engine(state, tmp_path/'engine') as vm:
        before = stopped(vm)
        images = export(vm, tmp_path/'flash')
        after = vm.snapshot(time.monotonic()+5)
        assert after == before
    assert {p.name for p in (tmp_path/'flash').iterdir()} == {name+'.bin' for name in flash.SIZES}
    assert images['main'] == b'\xff'*(flash.SIZES['main']-2)+b'\x12\x34'
    assert images['fs'] == b'\xff'*flash.SIZES['fs']
    assert images['gdfs'] == b'\x34\x12'+b'\xff'*(flash.SIZES['gdfs']-2)
    assert images['nand-main'][33*512:33*512+2] == b'\x7f\xff'
    assert images['nand-main'][34*512:35*512] == b'\xff'*512
    assert images['nand-main'][64*512:] == b'\xff'*(flash.SIZES['nand-main']-64*512)
    assert images['nand-spare'][33*16+5] == 0x3f
    assert images['nand-spare'][14] == 0x54
    # Fresh controllers and bookkeeping; every loaded byte is now known input.
    loaded = storage_state(tmp_path, nand_ops([write(0x10, 0xff), *main_read(33, 1),
        *main_program(33, b'\xff'), *main_read(33, 1), *spare_read(33, 5)]))
    loaded['nand'].update(nand_main_image=str(tmp_path/'flash/nand-main.bin'),
                          nand_spare_image=str(tmp_path/'flash/nand-spare.bin'))
    with engine(loaded, tmp_path/'loaded') as vm:
        initial = vm.snapshot(time.monotonic()+5)
        nand = initial['nand']
        assert nand['spare_program']['page_program_counts'] == {}
        assert nand['main_array']['page_program_counts'] == {}
        assert nand['block_erase']['operations'] == []
        assert nand['main_array']['known_bytes'] == 32*1024*1024
        assert initial['observations'] == 0
        assert export(vm, tmp_path/'again')['nand-main'] == images['nand-main']
        final = stopped(vm)
        assert final['registers'][2] == 0x3f
        assert final['nand']['main_array']['page_program_counts'] == {'33': 1}
        assert export(vm, tmp_path/'programmed')['nand-main'][33*512] == 0x7f


def test_pending_nor_program_is_not_storage(tmp_path):
    state = storage_state(tmp_path, [('write', 2, 0x45f00000, 0x40)])
    with engine(state, tmp_path/'engine') as vm:
        stopped(vm)
        assert export(vm, tmp_path/'flash')['gdfs'] == b'\xff'*flash.SIZES['gdfs']


@pytest.mark.parametrize('name', ['nand-main', 'nand-spare'])
def test_independent_nand_overrides_and_geometry(tmp_path, name):
    supplied = tmp_path/'image.bin'; supplied.write_bytes(b'\xa5'*flash.SIZES[name])
    path, _ = prepare(ROOT, EXPERIMENT, tmp_path/'prepared', images={name: supplied})
    state = json.loads(path.read_text())
    assert state['nand'][name.replace('-', '_')+'_image'] == str(supplied)
    other = 'nand_spare' if name == 'nand-main' else 'nand_main'
    default = json.loads(EXPERIMENT.read_text())['firmware_images'][other.replace('_', '-')]
    assert state['nand'][other+'_image'] == str(ROOT/default['input_path'])
    supplied.write_bytes(b'bad')
    with pytest.raises(ValueError, match='exactly'):
        prepare(ROOT, EXPERIMENT, tmp_path/'bad', images={name: supplied})
    spec = json.loads(EXPERIMENT.read_text()); spec.pop('nand')
    spec['firmware_images'] = {name: entry for name, entry in spec['firmware_images'].items()
                               if not name.startswith('nand-')}
    config = tmp_path/'spec.json'; config.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match='storage model'):
        prepare(ROOT, config, tmp_path/'unsupported', images={name: supplied})


def save_run(tmp_path, **options):
    result, error = guarded_run(ROOT, EXPERIMENT, artifacts_dir=tmp_path, label='saved',
        save_flash=True, instruction_limit=1, config_limits=False, seconds=20, **options)
    assert error is None, result.get('error')
    return Path(result['run_directory']), result


def test_flash_only_cold_load_explicit_inputs_retention_and_replay(tmp_path, controlled_clock):
    source, result = save_run(tmp_path)
    assert not (source/'diagnostics').exists()
    assert not (source/'snapshots').exists()
    assert verify(source)['integrity'] == 'passed'
    images = flash.load_flash_images(source)
    runs = []
    for name, options in [('load', {'load_flash': 'saved'}), ('explicit', {'images': images})]:
        current, error = guarded_run(ROOT, EXPERIMENT, artifacts_dir=tmp_path, label=name,
            diagnostics=True, save_flash=True, instruction_limit=20, config_limits=False,
            seconds=20, **options)
        assert error is None, current.get('error')
        runs.append(current)
    assert runs[0]['initial_snapshot'] == runs[1]['initial_snapshot']
    assert runs[0]['snapshot'] == runs[1]['snapshot']
    assert runs[0]['snapshot']['observations'] == 20
    nand = runs[0]['initial_snapshot']['nand']
    assert nand['mode'] == 'unknown' and nand['reset_count'] == 0 and not nand['read_open']
    assert nand['copy_back']['copied_pages'] == [] and nand['copy_back']['operations'] == []
    assert {n: identity(p) for n,p in images.items()} == {
        n: identity(p) for n,p in flash.load_flash_images(tmp_path/'load').items()}
    prune(tmp_path/'load', tmp_path, {})
    assert retained_verify(tmp_path/'load')['status'] == 'bulk-pruned'
    command_line = shlex.split(regeneration_command(tmp_path/'load'))
    assert '--save-flash' in command_line and '--nand-main' in command_line and '--nand-spare' in command_line
    completed = subprocess.run(command_line, cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert flash.load_flash_images(tmp_path/'load-regenerated')


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'geometry'])
def test_load_rejects_missing_corrupt_and_incompatible_images(tmp_path, controlled_clock, damage):
    source, _ = save_run(tmp_path)
    if damage == 'missing':
        (source/'flash/nand-main.bin').unlink()
    elif damage == 'corrupt':
        with (source/'flash/gdfs.bin').open('r+b') as stream: stream.write(b'bad')
    else:
        manifest = source/'flash/flash.json'
        spec = json.loads(manifest.read_text()); spec['geometry']['nand']['pages'] = 1
        manifest.write_text(json.dumps(spec)); seal(source)
    with pytest.raises((ValueError, OSError)):
        flash.load_flash_images(source)


@pytest.mark.parametrize('failed_family', [None, 'snapshot', 'flash'])
def test_combined_saves_once_with_independent_failures(tmp_path, monkeypatch, controlled_clock, failed_family):
    calls = {'snapshot': 0, 'flash': 0}
    for family, attr in [('snapshot', 'save_checkpoint'), ('flash', 'save_flash_images')]:
        original = getattr(runner, attr)
        def save(*args, family=family, original=original):
            calls[family] += 1
            if family == failed_family: raise OSError('injected save failure')
            return original(*args)
        monkeypatch.setattr(runner, attr, save)
    result, error = guarded_run(ROOT, EXPERIMENT, artifacts_dir=tmp_path, label='combined',
        save_flash=True, snapshot=True, diagnostics=True, trace=True, record_audio=True,
        instruction_limit=1, config_limits=False, seconds=20)
    assert (error is not None) == (failed_family is not None)
    assert calls == {'snapshot': 1, 'flash': 1}
    assert set(result.get('artifact_errors', {})) == ({failed_family} if failed_family else set())
    if failed_family in (None, 'snapshot'):
        assert flash.load_flash_images(tmp_path/'combined')
    else:
        assert (tmp_path/'combined/snapshots/final/snapshot.json').exists()


def test_interrupted_publication_has_no_final_directory(tmp_path, monkeypatch):
    context = RunArtifacts(ROOT, families=['flash'], destination=tmp_path/'saved')
    class Export:
        def execute(self, name, deadline, arguments):
            for image, size in flash.SIZES.items():
                with (Path(arguments['value'])/(image+'.bin')).open('wb') as stream: stream.truncate(size)
    def interrupted(source, destination): raise KeyboardInterrupt()
    monkeypatch.setattr(flash.os, 'rename', interrupted)
    with pytest.raises(KeyboardInterrupt):
        flash.save_flash_images(Export(), context, {'phases': {}},
            {'nor_model': {}, 'nand': {'main_array': {'profile': 'samsung-x8-known-main-read-program'}}})
    assert not (context.path/'flash').exists()
    assert not list(context.path.glob('.flash-*'))


@pytest.mark.parametrize('arguments', [
    ['--load-flash', 'saved', '--main', 'image'], ['--load-flash', 'saved', '--nand-spare', 'image'],
    ['--load-flash', 'saved', '--restore', 'saved'], ['--flash-state', 'image'], ['--cold-boot', 'saved']])
def test_conflicting_and_retired_cli_inputs(monkeypatch, arguments):
    monkeypatch.setattr(sys, 'argv', ['db2010', '--research-guards', *arguments])
    with pytest.raises(SystemExit) as exc: cli.main()
    assert exc.value.code == 2


def test_loaded_nor_programming_rules_and_prefix_are_preserved(tmp_path):
    operations = [('write', 2, 0x45f00000, 0x40), ('write', 2, 0x45f00000, 0xffff), ('read', 2, 0x45f00000, 0)]
    state = storage_state(tmp_path, operations)
    state['regions'][1]['seeds'][-1]['data'] = b'\x34\x12'
    with engine(state, tmp_path/'engine') as vm:
        stopped(vm)
        after = vm.snapshot(time.monotonic()+5)
        assert after['stop']['reason'] == 'instruction_limit'
        assert after['registers'][2] & 0x10  # ready with program-error status
        assert export(vm, tmp_path/'flash')['gdfs'][:2] == b'\x34\x12'
    # The excluded prefix retains its ordinary supplied/unknown behavior.
    code, limit, _ = nor_program([('read', 1, 0x44000000, 0)])
    state['regions'][0]['seeds'][0]['data'] = code
    state['instruction_limit'] = limit
    with engine(state, tmp_path/'prefix') as vm:
        assert stopped(vm)['registers'][2] == ord('p')


def test_api_rejects_load_conflicts_before_execution(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs): raise AssertionError('must not launch')
    monkeypatch.setattr(runner.subprocess, 'Popen', unexpected)
    for options in ({'images': {'main': 'input'}}, {'restore': 'run'}, {'replay': 'run'}, {'nand_main': 'input'}):
        result, error = guarded_run(ROOT, load_flash='saved', **options)
        assert isinstance(error, ValueError)
        assert 'cannot combine' in str(error)


@pytest.mark.parametrize('mode', ['load', 'images'])
def test_cli_forwards_flash_flags_and_current_default(tmp_path, monkeypatch, mode):
    captured = []
    def run(*args, **options):
        captured.append((args, options))
        return {'success': True, 'phases': {}, 'uart': {}}, None
    monkeypatch.setattr(cli, 'run', run)
    options = ['--load-flash', 'source'] if mode == 'load' else [
        '--nand-main', 'data.bin', '--nand-spare', 'spare.bin']
    monkeypatch.setattr(sys, 'argv', ['db2010', '--research-guards', '--save-flash', '--label', 'saved',
                                     '--limit', '25', '--no-progress', *options])
    cli.main()
    args, kwargs = captured[0]
    assert args[1] == EXPERIMENT.resolve()
    assert kwargs['save_flash'] and kwargs['instruction_limit'] == 25
    assert kwargs['config_limits'] is False
    if mode == 'load':
        assert kwargs['load_flash'] == 'source' and kwargs['images'] is None
    else:
        assert kwargs['images'] == {'nand-main': Path('data.bin').resolve(),
                                    'nand-spare': Path('spare.bin').resolve()}


def test_unsupported_flash_model_is_rejected_before_launch(tmp_path, monkeypatch):
    spec = json.loads(EXPERIMENT.read_text()); spec.pop('nand')
    spec['firmware_images'] = {name: entry for name, entry in spec['firmware_images'].items()
                               if not name.startswith('nand-')}
    config = tmp_path/'spec.json'; config.write_text(json.dumps(spec))
    def unexpected(*args, **options): raise AssertionError('must not launch')
    monkeypatch.setattr(runner.subprocess, 'Popen', unexpected)
    result, error = guarded_run(ROOT, config, save_flash=True, artifacts_dir=tmp_path, label='bad')
    assert isinstance(error, ValueError)
    assert 'storage models' in str(error)


@pytest.mark.parametrize('name', ['nand-main', 'nand-spare'])
def test_native_independent_images_last_page_index_and_default_other_area(tmp_path, name):
    state = storage_state(tmp_path)
    raw = bytearray(b'\xff'*flash.SIZES[name]); raw[-1] = 0x42
    supplied = tmp_path/(name+'.bin'); supplied.write_bytes(raw)
    state['nand'][name.replace('-', '_')+'_image'] = str(supplied)
    with engine(state, tmp_path/'engine') as vm:
        nand = vm.snapshot(time.monotonic()+5)['nand']
        assert nand['mode'] == 'unknown' and not nand['read_open'] and nand['reset_count'] == 0
        assert nand['spare_program']['page_program_counts'] == {}
        assert nand['main_array']['page_program_counts'] == {}
        images = export(vm, tmp_path/'flash')
        assert images[name] == raw
        if name == 'nand-main':
            assert nand['main_array']['known_bytes'] == flash.SIZES[name]
            assert nand['spare_read']['known_bytes'] == 1
            assert images['nand-spare'][14] == 0x54
        else:
            assert nand['spare_read']['known_bytes'] == flash.SIZES[name]
            assert nand['main_array']['known_bytes'] == 0
            assert images['nand-spare'][14] == 0xff


def test_raw_fs_storage_bytes_are_supplied_without_footer_edits(tmp_path):
    raw = b'\xff'*flash.SIZES['fs']
    image = tmp_path/'fs.bin'; image.write_bytes(raw)
    path, _ = prepare(ROOT, EXPERIMENT, tmp_path/'prepared', images={'fs': image})
    state = json.loads(path.read_text())
    assert state['regions'][0]['seeds'][1] == {'address': 0x45400000, 'file': str(image)}
    assert image.read_bytes() == raw
