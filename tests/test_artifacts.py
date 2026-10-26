"""Grouped captures, retained integrity and containment before destructive work."""
from support.guarded import run as guarded_run
from db2010.paths import DEFAULT_EXPERIMENT
from support.paths import RESOURCES
from support.controlled_clock import controlled_clock
import gzip
import json
from pathlib import Path
import shlex
import subprocess

import pytest

from db2010.execution import runner
from db2010.diagnostics.artifacts import artifact, files, identity, read_json, relative_artifact, seal, RunArtifacts
from db2010.paths import ROOT
from db2010.diagnostics.verify import verify
from db2010.diagnostics.retention import prepare, regeneration_command, verify as retained_verify
from db2010.diagnostics import render_lcd_frames


def capture(path):
    context = RunArtifacts(ROOT, families=['diagnostics'], destination=path)
    artifact(path, 'result.json', create=True).write_text(json.dumps({
        'machine': 'db2010', 'success': True, 'trace_enabled': False,
        'execution_limits': {'instruction_limit': 5, 'seconds': None},
        'snapshot': {'stop': {'reason': 'instruction_limit'}}})+'\n')
    artifact(path, 'experiment.json', create=True).write_text('{}\n')
    artifact(path, 'main.bin', create=True).write_bytes(b'authenticated prepared input')
    artifact(path, 'uart0.bin', create=True).write_bytes(b'exact UART \r\n')
    context.finish(json.loads(artifact(path, 'result.json').read_text()))
    return path


@pytest.mark.parametrize('name', ['../outside', '/tmp/outside', 'logs/../../outside',
    'logs/../results/result.json', 'logs//uart0.bin', './logs/uart0.bin',
    'result.json', 'logs\\uart0.bin'])
def test_unsafe_inventory_names_are_rejected_before_io(tmp_path, name):
    with pytest.raises(ValueError):
        relative_artifact(tmp_path, name)


@pytest.mark.parametrize('change', ['modified', 'missing', 'extra'])
def test_recursive_integrity_failures(tmp_path, change):
    path = capture(tmp_path/'capture')
    assert verify(path)['integrity'] == 'passed'
    if change == 'modified':
        artifact(path, 'uart0.bin').write_bytes(b'changed')
    elif change == 'missing':
        artifact(path, 'main.bin').unlink()
    else:
        nested = path/'diagnostics/nested/extra.json'
        nested.parent.mkdir()
        nested.write_text('{}')
    with pytest.raises(ValueError):
        verify(path)
    with pytest.raises(ValueError):
        prepare(path, tmp_path, {})
    assert not artifact(path, 'retention.json').exists()


@pytest.mark.parametrize('directory', [False, True])
def test_symlink_escape_is_rejected_without_modifying_target(tmp_path, directory):
    path = capture(tmp_path/'capture')
    outside = tmp_path/'outside'
    outside.mkdir()
    target = outside/'main.bin'
    target.write_bytes(b'outside bytes')
    if directory:
        artifact(path, 'main.bin').unlink()
        (path/'inputs').rmdir()
        (path/'inputs').symlink_to(outside, target_is_directory=True)
    else:
        artifact(path, 'main.bin').unlink()
        artifact(path, 'main.bin').symlink_to(target)
    for operation in (verify, lambda p: prepare(p, tmp_path, {})):
        with pytest.raises(ValueError):
            operation(path)
    assert target.read_bytes() == b'outside bytes'
    assert not artifact(path, 'retention.json').exists()


def test_flat_capture_requires_frozen_tooling(tmp_path):
    (tmp_path/'result.json').write_text('{}')
    (tmp_path/'artifacts.sha256.json').write_text(json.dumps({'result.json': identity(tmp_path/'result.json')}))
    with pytest.raises(ValueError, match='unsupported'):
        verify(tmp_path)
    with pytest.raises(ValueError, match='frozen tooling'):
        read_json(tmp_path)
    with pytest.raises(ValueError, match='unsupported'):
        prepare(tmp_path, tmp_path, {})


@pytest.mark.parametrize('disassemble', [False, True])
def test_grouped_trace_retention_and_regeneration_with_spaces(tmp_path, disassemble, controlled_clock):
    path = tmp_path/'capture with spaces'
    result, failure = guarded_run(ROOT, DEFAULT_EXPERIMENT,
        path, diagnostics=True, trace=True, disassemble=disassemble, instruction_limit=1, seconds=20,
        config_limits=False, bounded_history=True)
    assert failure is None, result.get('error')
    assert verify(path)['integrity'] == 'passed'
    assert {p.name for p in path.iterdir() if p.is_file()} == {'run.json', 'artifacts.sha256.json'}
    assert artifact(path, 'trace.parquet').exists() and artifact(path, 'memory.parquet').exists()
    original_seal = (path/'artifacts.sha256.json').read_bytes()
    prepare(path, tmp_path, {})
    assert retained_verify(path)['status'] == 'bulk-pruned'
    assert (path/'artifacts.sha256.json').read_bytes() == original_seal
    assert read_json(path)['snapshot'] == result['snapshot']
    command = shlex.split(regeneration_command(path))
    assert ('--disassemble' in command) == disassemble
    assert '--trace' in command
    regenerated = Path(command[command.index('--artifacts-dir')+1])/command[command.index('--label')+1]
    process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stderr
    assert verify(regenerated)['integrity'] == 'passed'
    assert read_json(regenerated)['snapshot'] == result['snapshot']


def test_compressed_lcd_result_renders_without_flat_fallback(tmp_path, monkeypatch, capsys):
    path = tmp_path/'capture with spaces'
    RunArtifacts(ROOT, families=['diagnostics'], destination=path)
    frame = {'pixels_hex': ('f800'*38720), 'source': 0x4C000000, 'started': 1, 'completed': 2}
    result = {'snapshot': {'pdi': {'frames': [frame]}}}
    artifact(path, 'result.json.gz', create=True).write_bytes(gzip.compress(json.dumps(result).encode()))
    output = tmp_path/'rendered frames'
    monkeypatch.setattr('sys.argv', ['render', '--run', str(path), '--output', str(output)])
    render_lcd_frames.main()
    capsys.readouterr()
    assert (output/'frame-0000.png').read_bytes().startswith(b'\x89PNG')
    assert (output/'frame-0000.rgb565').read_bytes() == bytes.fromhex('f800')*38720


def test_nested_seal_with_same_basename_is_not_omitted(tmp_path):
    nested = tmp_path/'diagnostics/nested/artifacts.sha256.json'
    nested.parent.mkdir(parents=True)
    nested.write_text('{}')
    assert files(tmp_path) == {'diagnostics/nested/artifacts.sha256.json'}
    assert 'diagnostics/nested/artifacts.sha256.json' in seal(tmp_path)
