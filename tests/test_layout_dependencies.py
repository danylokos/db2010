"""Preparation must survive removal of disposable research directories."""
import io
from pathlib import Path

from db2010.paths import ROOT, DEFAULT_EXPERIMENT
from db2010.preparation.inputs import prepare


def test_preparation_never_reads_research_tools_or_derived_data(tmp_path, monkeypatch):
    original = io.open
    reads = []
    def guarded(file, mode='r', *args, **kwargs):
        if isinstance(file, (str, bytes, Path)) and 'r' in mode:
            path = Path(file).resolve()
            assert not any(path.is_relative_to(ROOT/name) for name in ('tools', 'derived', 'references'))
            reads.append(path)
        return original(file, mode, *args, **kwargs)
    monkeypatch.setattr(io, 'open', guarded)
    state, _ = prepare(ROOT, DEFAULT_EXPERIMENT, tmp_path)
    assert state.is_file() and reads
