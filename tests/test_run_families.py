"""Independent capture families, portable inputs and atomic run identity."""
from support.guarded import run as guarded_run
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil

import pytest

from db2010.diagnostics.artifacts import RunArtifacts, artifact, load_run, prepared_references
from db2010.diagnostics.verify import verify
from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation


@pytest.mark.parametrize('snapshot', [False, True])
@pytest.mark.parametrize('diagnostics', [False, True])
@pytest.mark.parametrize('trace', [False, True])
def test_independent_families(tmp_path, monkeypatch, diagnostics, trace, snapshot):
    monkeypatch.setattr(runner, 'prepare', lambda root, experiment, output, **kw:
                        synthetic_preparation(root, experiment, output))
    requests = []
    original = runner.QMP.execute
    def execute(self, name, deadline, arguments=None):
        if name == 'qom-get': requests.append(arguments.get('property'))
        return original(self, name, deadline, arguments)
    monkeypatch.setattr(runner.QMP, 'execute', execute)
    result, failure = guarded_run(ROOT, Path('unused'), diagnostics=diagnostics, trace=trace,
        artifacts_dir=tmp_path, label='saved' if diagnostics or trace or snapshot else None, snapshot=snapshot,
        instruction_limit=2, config_limits=False)
    assert failure is None, result.get('error')
    assert result['diagnostics_enabled'] == diagnostics
    assert ('snapshot-chunk' in requests) == diagnostics
    if not (diagnostics or trace or snapshot):
        assert not list(tmp_path.iterdir())
        return
    path = tmp_path/'saved'
    assert (path/'diagnostics').exists() == diagnostics
    assert (path/'traces').exists() == trace
    assert (path/'snapshots/final/snapshot.json').exists() == snapshot
    state = json.loads(artifact(path, 'state.json').read_text())
    assert state['record_diagnostics'] == diagnostics
    assert state['regions'][0]['seeds'][0]['file'] == 'inputs/code.bin'
    assert verify(path)['integrity'] == 'passed'
    if not diagnostics:
        from db2010.diagnostics.artifacts import read_json
        with pytest.raises(ValueError, match='not collected'): read_json(path)
    moved = tmp_path/'moved'
    shutil.move(path, moved)
    assert verify(moved)['integrity'] == 'passed'
    restored = prepared_references(state, moved, absolute=True)
    assert Path(restored['regions'][0]['seeds'][0]['file']).is_file()


def test_atomic_label_allocation_and_automatic_names(tmp_path):
    def allocate(_):
        try: return RunArtifacts(ROOT, families=['trace'], artifacts_dir=tmp_path, label='same')
        except FileExistsError: return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        contexts = list(pool.map(allocate, range(8)))
    assert sum(c is not None for c in contexts) == 1
    automatic = [RunArtifacts(ROOT, families=['trace'], artifacts_dir=tmp_path) for _ in range(5)]
    assert len({c.path.name for c in automatic}) == 5
    assert all(c.path.name.endswith('Z') for c in automatic)


@pytest.mark.parametrize('label', ['../escape', '/tmp/absolute', 'a/b', '', '..'])
def test_invalid_labels(tmp_path, label):
    with pytest.raises(ValueError):
        RunArtifacts(ROOT, families=['trace'], artifacts_dir=tmp_path, label=label)


def test_unsaved_label_rejected_before_allocation(tmp_path):
    with pytest.raises(ValueError, match='saving flag'):
        RunArtifacts(ROOT, artifacts_dir=tmp_path, label='unused')
    assert not list(tmp_path.iterdir())
