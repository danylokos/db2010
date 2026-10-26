"""Separate firmware roots never relocate emulator-generated caches or weaken identity."""
import json
from pathlib import Path
import shutil

import pytest

from db2010.paths import ROOT, DEFAULT_EXPERIMENT
from db2010.preparation.inputs import prepare
from db2010.preparation.filesystem import Fat16
from db2010.provision_fs import customization_members
import io
import zipfile


def test_external_input_root_keeps_cache_in_emulator_and_authenticates(tmp_path):
    emulator = tmp_path/'emulator'
    inputs = tmp_path/'research'
    (inputs/'fw').mkdir(parents=True)
    spec = json.loads(DEFAULT_EXPERIMENT.read_text())
    for image in spec['firmware_images'].values():
        (inputs/image['input_path']).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/image['input_path'], inputs/image['input_path'])
    state, _ = prepare(emulator, DEFAULT_EXPERIMENT, tmp_path/'prepared',
                       input_root=inputs, recording=False)
    seeds = json.loads(state.read_text())['regions'][0]['seeds']
    assert Path(seeds[0]['file']).is_relative_to(inputs/'fw')
    assert Path(json.loads(state.read_text())['nand']['prepared_spare']).is_relative_to(emulator/'build/inputs')
    assert Path(seeds[1]['file']).is_relative_to(inputs/'fw')
    assert not (inputs/'build').exists()
    fs = inputs/spec['firmware_images']['fs']['input_path']
    raw = fs.read_bytes(); fs.write_bytes(bytes([raw[0]^1])+raw[1:])
    with pytest.raises(ValueError, match='configured firmware image identity differs: fs'):
        prepare(emulator, DEFAULT_EXPERIMENT, tmp_path/'changed', input_root=inputs)
    fs.unlink()
    with pytest.raises(FileNotFoundError):
        prepare(emulator, DEFAULT_EXPERIMENT, tmp_path/'missing', input_root=inputs)


def test_bundled_fs_customization_writer_preserves_existing_file(tmp_path):
    original = (ROOT/'fw/non-provisioned/w800i-r1l002-cid49-fs.bin').read_bytes()
    disk = Fat16(original)
    xml = disk.find('tpa/preset/custom/customize.xml')
    expected = disk.chain(xml['cluster'])[:xml['size_bytes']]
    disk.install('tpa/preset/custom/standalone.txt', b'fixture data')
    reopened = Fat16(disk.render())
    existing = reopened.find('tpa/preset/custom/customize.xml')
    new = reopened.find('tpa/preset/custom/standalone.txt')
    assert reopened.chain(existing['cluster'])[:existing['size_bytes']] == expected
    assert reopened.chain(new['cluster'])[:new['size_bytes']] == b'fixture data'
    assert (ROOT/'fw/non-provisioned/w800i-r1l002-cid49-fs.bin').read_bytes() == original


@pytest.mark.parametrize('name', ['../customize.xml', '/customize.xml', 'tpa\\customize.xml'])
def test_customization_archive_paths_are_guarded(name):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr(name, b'<customization/>')
    with zipfile.ZipFile(io.BytesIO(stream.getvalue())) as archive:
        with pytest.raises(ValueError, match='unsafe customization archive path'):
            customization_members(archive)


def test_regeneration_retains_explicit_checkout(tmp_path):
    from db2010.diagnostics.artifacts import RunArtifacts, artifact
    from db2010.diagnostics.retention import regeneration_command
    import shlex
    checkout = tmp_path/'explicit emulator'
    run = RunArtifacts(checkout, families=['snapshot'], destination=tmp_path/'saved')
    manifest = run.manifest
    manifest.update(emulator_root=str(checkout), limits={'instruction_limit': 1, 'seconds': 5})
    (run.path/'run.json').write_text(json.dumps(manifest))
    artifact(run.path, 'experiment.json', create=True).write_text('{}')
    command = shlex.split(regeneration_command(run.path))
    assert command[command.index('--repo')+1] == str(checkout)
