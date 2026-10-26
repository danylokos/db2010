"""Versioned runs, family paths, allocation and integrity ownership."""
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

SCHEMA = 2
FAMILIES = frozenset(('diagnostics', 'trace', 'snapshot', 'audio', 'flash'))
GROUPS = frozenset(('config', 'inputs', 'provenance', 'diagnostics', 'traces', 'snapshots', 'audio', 'flash'))
CONFIG = frozenset(('experiment.json', 'baseline.json', 'manifest.json', 'state.json', 'probe-experiment.json'))
TRACES = frozenset(('trace.parquet', 'memory.parquet', 'trace.bin',
    'stream-error-tail.bin', 'memory-error-tail.bin'))
METRICS = frozenset(('producer.json', 'collector.json', 'memory-producer.json', 'memory-collector.json'))
AUDITS = frozenset(('display-audit.json', 'memory-audit-queries.json'))
PROVENANCE = frozenset(('inputs.json', 'execution-provenance.json',
    'ram_execution_evidence.json', 'additional_ram_execution_evidence.json',
    'qemu-build.json', 'trace-build.json', 'source-lock.json', 'dependencies.json', 'command.json', 'debug-control.json',
    'emulator-source.json', 'runner-sources.zip', 'db2010.bundle', 'trace-producer.c', 'observation-method.json',
    'f500-helper.bin', 'provenance.json', 'trace.so'))


def relative_artifact(root, name):
    """Validate containment before any read, write or delete (including symlinks)."""
    root = Path(root).resolve()
    if not isinstance(name, str):
        raise ValueError('artifact path must be a relative string')
    parts = PurePosixPath(name).parts
    if (not parts or PurePosixPath(name).is_absolute() or '..' in parts or '\\' in name
            or str(PurePosixPath(name)) != name
            or (name not in ('run.json', 'artifacts.sha256.json', 'retention.json')
                and (len(parts) < 2 or parts[0] not in GROUPS))):
        raise ValueError(f'unsupported run artifact path: {name}; older layouts require frozen tooling')
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError(f'artifact path escapes run directory: {name}')
    return path


def artifact_name(name):
    if not isinstance(name, str) or not name or '/' in name or '\\' in name or name in ('.', '..'):
        raise ValueError(f'invalid artifact name: {name}')
    plain = name.removesuffix('.gz')
    if name in ('run.json', 'artifacts.sha256.json', 'retention.json'):
        return name
    if plain == 'result.json':
        return 'diagnostics/state.json'+('.gz' if name.endswith('.gz') else '')
    if plain in CONFIG:
        group = 'config'
    elif plain in TRACES:
        return 'traces/'+('instructions.parquet' if name == 'trace.parquet' else name)
    elif plain in METRICS:
        group = 'traces/metrics'
    elif plain in AUDITS:
        group = 'traces/audits'
    elif plain in PROVENANCE or plain.startswith('execution-proof-') or plain.endswith(('.zip', '.bundle')):
        group = 'provenance'
    elif plain.startswith('gdb-') or plain in ('debug-session.json', 'event-probe.json') or plain.endswith('.gdb'):
        group = 'diagnostics/debug'
    elif (plain.startswith('uart') and plain.endswith('.bin')) or plain.endswith('.log'):
        group = 'diagnostics/logs'
    elif plain.endswith('.bin'):
        group = 'inputs'
    else:
        group = 'diagnostics'
    return f'{group}/{name}'


def artifact(root, name, *, create=False):
    path = relative_artifact(root, artifact_name(name))
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def files(root):
    root = Path(root)
    result = set()
    for path in root.rglob('*'):
        name = path.relative_to(root).as_posix()
        if path.is_symlink() or path.is_file():
            relative_artifact(root, name)
        if path.is_file() and name != 'artifacts.sha256.json':
            result.add(name)
    return result


def identity(path):
    with Path(path).open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'size_bytes': Path(path).stat().st_size, 'sha256': digest}


def load_run(root, *, family=None):
    path = Path(root)/'run.json'
    if not path.is_file():
        raise ValueError('unsupported run layout: use its frozen tooling')
    manifest = json.loads(path.read_text())
    if manifest.get('schema_version') != SCHEMA:
        raise ValueError('unsupported run schema: use its frozen tooling')
    if not isinstance(manifest.get('families'), list) or not set(manifest['families']) <= FAMILIES:
        raise ValueError('invalid artifact families')
    if family and family not in manifest['families']:
        raise ValueError(f'{family} artifacts were not collected')
    return manifest


def stored_artifact(root, name, *, family='diagnostics'):
    load_run(root, family=family)
    path = artifact(root, name)
    if path.is_file():
        return path
    compressed = artifact(root, name+'.gz')
    if compressed.is_file():
        return compressed
    raise ValueError(f'missing {family} artifact {path}')


def read_artifact(root, name, *, family='diagnostics'):
    path = stored_artifact(root, name, family=family)
    raw = path.read_bytes()
    return gzip.decompress(raw) if path.suffix == '.gz' else raw


def read_json(root, name='result.json', *, family='diagnostics'):
    return json.loads(read_artifact(root, name, family=family))


def write_json(path, value):
    descriptor, name = tempfile.mkstemp(prefix='.'+path.name+'-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(json.dumps(value, indent=2)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def seal(root):
    identities = {name: identity(relative_artifact(root, name)) for name in sorted(files(root))}
    write_json(artifact(root, 'artifacts.sha256.json'), identities)
    return identities


def verify_seal(root):
    load_run(root)
    seals = json.loads(artifact(root, 'artifacts.sha256.json').read_text())
    for name in seals:
        relative_artifact(root, name)
    if files(root) != set(seals):
        raise ValueError('artifact inventory differs')
    for name, expected in seals.items():
        if identity(relative_artifact(root, name)) != expected:
            raise ValueError(f'artifact identity differs: {name}')
    return seals


def prepared_references(value, root, *, absolute):
    """Only prepared engine file references are runnable; provenance is descriptive."""
    if isinstance(value, list):
        return [prepared_references(item, root, absolute=absolute) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in ('file', 'prepared_spare', 'nand_main_image', 'nand_spare_image') and isinstance(item, str):
            path = Path(item)
            if path.is_absolute() and not path.is_relative_to(Path(root).resolve()):
                result[key] = str(path)
            elif absolute:
                result[key] = str(relative_artifact(root, path.relative_to(Path(root).resolve()).as_posix() if path.is_absolute() else item))
            else:
                path = Path(item)
                name = path.relative_to(Path(root).resolve()).as_posix() if path.is_absolute() else item
                relative_artifact(root, name)
                result[key] = name
        else:
            result[key] = prepared_references(item, root, absolute=absolute)
    return result


def input_references(value):
    """Enumerate engine dependencies without traversing descriptive metadata."""
    if isinstance(value, list):
        for item in value: yield from input_references(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in ('file', 'prepared_spare', 'nand_main_image', 'nand_spare_image') and isinstance(item, str):
                yield item
            else:
                yield from input_references(item)


def input_path(root, name):
    path = Path(name)
    return path if path.is_absolute() and not path.is_relative_to(Path(root).resolve()) else relative_artifact(
        root, path.relative_to(Path(root).resolve()).as_posix() if path.is_absolute() else name)


def external_inputs(state, root):
    return {name: identity(input_path(root, name)) for name in sorted(set(input_references(state)))
            if Path(name).is_absolute() and not Path(name).is_relative_to(Path(root).resolve())}


def verify_external_inputs(references):
    if not isinstance(references, dict):
        raise ValueError('invalid external input identities')
    for name, expected in references.items():
        if not isinstance(name, str) or not Path(name).is_absolute():
            raise ValueError(f'external input path must be absolute: {name}')
        try:
            actual = identity(name)
        except OSError as exc:
            raise ValueError(f'missing external input: {name}') from exc
        if actual != expected:
            raise ValueError(f'external input identity differs: {name}')


class RunArtifacts:
    """Allocate once; own explicit families and publish one final sealed manifest."""
    def __init__(self, root, *, families=(), destination=None, artifacts_dir=None, label=None, lineage=None):
        self.families = frozenset(families)
        if not self.families <= FAMILIES:
            raise ValueError('unsupported artifact family')
        if label and not self.families:
            raise ValueError('--label requires an artifact saving flag')
        if label is not None and (not re.fullmatch(r'[^/\\\x00]+', label) or label in ('.', '..')):
            raise ValueError('label must be a nonempty directory name without path separators')
        self.temporary = None
        if self.families:
            base = Path(artifacts_dir or Path(root)/'artifacts').resolve()
            generated = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
            self.path = Path(destination).resolve() if destination is not None else base/(label or generated)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # mkdir is the cross-process collision barrier; never use exist_ok.
            self.path.mkdir()
            self.manifest = {'schema_version': SCHEMA, 'id': self.path.name,
                'created_utc': datetime.now(timezone.utc).isoformat(), 'families': sorted(self.families),
                'lineage': lineage, 'status': 'preparing', 'artifacts': {
                    'configuration': 'config', 'inputs': 'inputs', 'provenance': 'provenance',
                    **{f: {'diagnostics': 'diagnostics', 'trace': 'traces', 'snapshot': 'snapshots', 'audio': 'audio', 'flash': 'flash'}[f]
                       for f in self.families}}}
            write_json(self.path/'run.json', self.manifest)
        else:
            if destination is not None:
                raise ValueError('persistent destination requires an artifact family')
            self.temporary = tempfile.TemporaryDirectory(prefix='db2010-inputs-')
            self.path = Path(self.temporary.name)
            self.manifest = None

    def require(self, family):
        if family not in self.families:
            raise ValueError(f'{family} artifacts were not collected')
        return self.path/{'diagnostics': 'diagnostics', 'trace': 'traces', 'snapshot': 'snapshots', 'audio': 'audio', 'flash': 'flash'}[family]

    def status(self, value, result=None):
        if self.manifest is None:
            return
        self.manifest['status'] = value
        if result is not None:
            self.manifest.update(limits=result.get('execution_limits'), engine=result.get('binary'))
            if result.get('checkpoint_compatibility'):
                self.manifest['checkpoint_compatibility'] = result['checkpoint_compatibility']
        write_json(self.path/'run.json', self.manifest)

    def finish(self, result):
        if self.manifest is None:
            return
        self.manifest.update(status='complete' if result['success'] else 'failed',
            limits=result.get('execution_limits'), outcome={
                'success': result['success'], 'stop': result.get('snapshot', {}).get('stop'),
                'error': result.get('error'), 'uart': result.get('uart', {}),
                'observations': result.get('snapshot', {}).get('observations'),
                'virtual_ns': result.get('snapshot', {}).get('virtual_ns'),
                'trace_rows': result.get('collector', {}).get('observations')})
        self.manifest['research_guards'] = result.get('research_guards', True)
        self.manifest['counter_kind'] = result.get('snapshot', {}).get('counter_kind')
        self.manifest['phases'] = result.get('phases', {})
        if 'checkpoint_size_bytes' in result:
            self.manifest['checkpoint_size_bytes'] = result['checkpoint_size_bytes']
        self.manifest['debugger_controlled'] = bool(result.get('debug_session') or result.get('debugger_lineage'))
        if result.get('checkpoint_compatibility'):
            self.manifest['checkpoint_compatibility'] = result['checkpoint_compatibility']
        if result.get('binary'):
            self.manifest['engine'] = result['binary']
        if result.get('audio'):
            self.manifest['audio'] = result['audio']
        if result.get('flash'):
            self.manifest['flash'] = result['flash']
        if result.get('artifact_errors'):
            self.manifest['artifact_errors'] = result['artifact_errors']
        if result.get('checkpoint'):
            self.manifest['checkpoint'] = result['checkpoint']
        if result.get('collector'):
            self.manifest['trace'] = {k: result[k] for k in (
                'collector', 'producer', 'memory_collector', 'memory_producer') if k in result}
        write_json(self.path/'run.json', self.manifest)
        seal(self.path)

    def close(self):
        if self.temporary:
            self.temporary.cleanup()
