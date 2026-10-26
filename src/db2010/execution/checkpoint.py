"""Atomic native checkpoints with authenticated execution dependencies."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from ..diagnostics.artifacts import (
    artifact, identity, input_path, input_references, external_inputs, load_run,
    prepared_references, relative_artifact, verify_external_inputs, write_json,
)

SCHEMA = 2
PREFIX = 'snapshots/final'


def model_identity(state, root):
    model = {k: v for k, v in state.items() if k not in (
        'instruction_limit', 'record_diagnostics', 'recording_history', 'checkpoint_model_identity')}
    # Legacy guarded identities did not include this explicit invocation field.
    if model.get('research_guards') is True:
        model.pop('research_guards')
    references = {name: identity(input_path(root, name)) for name in input_references(model)}
    return hashlib.sha256(json.dumps({'model': model, 'inputs': references},
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def required_files(state, root):
    names = {'config/state.json', 'config/experiment.json'}
    names.update(path.relative_to(root).as_posix() for path in (root/'config').iterdir() if path.is_file())
    names.update('provenance/'+name for name in ('inputs.json', 'execution-provenance.json', 'f500-helper.bin')
                 if (root/'provenance'/name).is_file())
    for name in input_references(state):
        path = input_path(root, name)
        if path.is_relative_to(root): names.add(path.relative_to(root).as_posix())
    return {name: identity(relative_artifact(root, name)) for name in sorted(names)}


def verify_required(root, inventory, seals):
    for name, expected in inventory.items():
        path = relative_artifact(root, name)
        if seals.get(name) != expected:
            raise ValueError(f'checkpoint dependency differs from run seal: {path}')
        try:
            actual = identity(path)
        except OSError as exc:
            raise ValueError(f'missing checkpoint dependency: {path}') from exc
        if actual != expected:
            raise ValueError(f'checkpoint dependency identity differs: {path}')


def starting_source(source):
    """Replay needs its parent; final restore never follows this reference."""
    manifest = load_run(source)
    seals = json.loads((source/'artifacts.sha256.json').read_text())
    verify_required(source, {'run.json': identity(source/'run.json')}, seals)
    reference = manifest.get('starting_checkpoint')
    if not isinstance(reference, dict) or not Path(reference.get('path', '')).is_absolute():
        raise ValueError('run has no absolute starting checkpoint reference')
    path = Path(reference['path'])
    try:
        actual = identity(path)
    except OSError as exc:
        raise ValueError(f'missing starting checkpoint: {path}') from exc
    if actual != {k: v for k, v in reference.items() if k != 'path'}:
        raise ValueError(f'starting checkpoint identity differs: {path}')
    if path.parts[-3:] != ('snapshots', 'final', 'snapshot.json'):
        raise ValueError(f'invalid starting checkpoint location: {path}')
    return path.parents[2]


def source_run(source, artifacts_dir):
    source = Path(source).expanduser()
    if not source.is_absolute() and len(source.parts) == 1 and not source.is_dir():
        source = Path(artifacts_dir)/source
    return source.resolve(strict=True)


def load_checkpoint(source, binary, *, starting=False, allow_engine_mismatch=False, research_guards=None):
    source = Path(source).resolve()
    if starting:
        source = starting_source(source)
    manifest = load_run(source, family='snapshot')
    checkpoint = relative_artifact(source, PREFIX+'/snapshot.json')
    spec = json.loads(checkpoint.read_text())
    if type(spec.get('schema_version')) is not int or spec['schema_version'] != SCHEMA:
        raise ValueError(f'unsupported checkpoint format version: {spec.get("schema_version")}; requires schema2/native21 or newer; use historical tooling')
    source_guards = spec.get('research_guards', True)
    if type(source_guards) is not bool:
        raise ValueError('invalid checkpoint research guard mode')
    if research_guards is not None and research_guards != source_guards:
        required = 'on (--research-guards)' if source_guards else 'off (omit --research-guards)'
        raise ValueError(f'checkpoint research guard mode differs; this checkpoint requires guards {required}')
    if spec.get('status') != 'complete':
        raise ValueError('incomplete checkpoint')
    seals = json.loads((source/'artifacts.sha256.json').read_text())
    verify_required(source, {'run.json': identity(source/'run.json'),
                            PREFIX+'/snapshot.json': identity(checkpoint)}, seals)
    if not isinstance(spec.get('engine'), dict) or manifest.get('engine') != spec['engine']:
        raise ValueError('checkpoint engine metadata differs from run provenance')
    if spec['engine'] != identity(binary) and not allow_engine_mismatch:
        raise ValueError('checkpoint engine differs from installed QEMU build; '
                         'use --allow-engine-mismatch to attempt a compatible native load')
    if spec.get('vmstate') != PREFIX+'/state.vmstate' or set(spec.get('files', {})) != {spec['vmstate']}:
        raise ValueError('invalid checkpoint artifact locations/inventory')
    if {path.name for path in checkpoint.parent.iterdir()} != {'snapshot.json', 'state.vmstate'}:
        raise ValueError('checkpoint file inventory differs')
    verify_required(source, spec['files'], seals)
    dependencies = spec.get('required_files', {})
    if not {'config/state.json', 'config/experiment.json'} <= dependencies.keys():
        raise ValueError('missing required checkpoint configuration')
    verify_required(source, dependencies, seals)
    state = json.loads(artifact(source, 'state.json').read_text())
    local_names = {path.relative_to(source).as_posix() for name in input_references(state)
                   if (path := input_path(source, name)).is_relative_to(source)}
    if not local_names <= dependencies.keys():
        raise ValueError('missing required checkpoint input/proof')
    references = spec.get('external_inputs')
    if references != manifest.get('external_inputs'):
        raise ValueError('checkpoint external inputs differ from run provenance')
    verify_external_inputs(references)
    if references != external_inputs(state, source):
        raise ValueError('checkpoint external input inventory differs')
    if spec.get('model_identity') != model_identity(state, source):
        raise ValueError('checkpoint model configuration differs')
    prepared_references(state, source, absolute=True)
    return spec


def copy_starting_inputs(source, destination, spec):
    # Copy only required compact configuration/proofs, never images/history.
    for name in spec['required_files']:
        target = relative_artifact(destination, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(relative_artifact(source, name), target)
    return artifact(destination, 'state.json'), json.loads(artifact(destination, 'experiment.json').read_text())


def migrate_file(qmp, path, *, incoming=False, seconds=60):
    deadline = time.monotonic()+seconds
    qmp.execute('migrate-incoming' if incoming else 'migrate', deadline,
        {'channels': [{'channel-type': 'main', 'addr': {
            'transport': 'file', 'filename': str(path), 'offset': 0}}]})
    while True:
        status = qmp.execute('query-migrate', deadline)
        if status['status'] == 'completed':
            return
        if status['status'] in ('failed', 'cancelled'):
            raise RuntimeError(f'native migration failed: {status}')
        if time.monotonic() >= deadline:
            raise TimeoutError('native migration deadline exceeded')
        time.sleep(0.01)


def save_checkpoint(qmp, artifacts, result, canonical):
    started = time.perf_counter()
    artifacts.status('saving')
    family = artifacts.require('snapshot')
    family.mkdir(parents=True, exist_ok=True)
    final = family/'final'
    if final.exists():
        raise FileExistsError('final checkpoint already exists')
    pending = Path(tempfile.mkdtemp(prefix='.final-', dir=family))
    try:
        # VM is stopped and keypad release handling has already completed.
        topology = json.loads(qmp.execute('qom-get', time.monotonic()+5,
            {'path': '/machine', 'property': 'checkpoint-layout'}))
        migrating = time.perf_counter()
        migrate_file(qmp, pending/'state.vmstate')
        result['phases']['checkpoint_migration_seconds'] = time.perf_counter()-migrating
        inventory = {}
        for path in sorted(pending.rglob('*')):
            if path.is_file():
                inventory['snapshots/final/'+path.relative_to(pending).as_posix()] = identity(path)
        spec = {'schema_version': SCHEMA, 'status': 'complete',
            'engine': result['binary'], 'research_guards': result['research_guards'],
            'model_identity': model_identity(canonical, artifacts.path),
            'vmstate': 'snapshots/final/state.vmstate',
            'required_files': required_files(canonical, artifacts.path),
            'external_inputs': external_inputs(canonical, artifacts.path),
            'tcm_layout': topology, 'summary': qmp.summary(time.monotonic()+5),
            'files': inventory}
        write_json(pending/'snapshot.json', spec)
        # Flush every component before publishing the directory entry. Failed or
        # interrupted writes can leave no apparent complete final checkpoint.
        for path in pending.rglob('*'):
            if path.is_file():
                with path.open('rb') as stream: os.fsync(stream.fileno())
        descriptor = os.open(pending, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
        os.rename(pending, final)
        descriptor = os.open(family, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
        result['phases']['checkpoint_save_seconds'] = time.perf_counter()-started
        result['checkpoint'] = 'snapshots/final/snapshot.json'
        result['checkpoint_size_bytes'] = sum(row['size_bytes'] for row in inventory.values())+(final/'snapshot.json').stat().st_size
    finally:
        if pending.exists(): shutil.rmtree(pending)
