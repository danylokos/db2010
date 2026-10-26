"""The FA owner commits guest transactions once and reports only real bus work."""
from support.guarded import run as guarded_run
from db2010.diagnostics.artifacts import artifact as artifact_path
from pathlib import Path

import pyarrow.parquet as pq

from db2010.execution import runner
from db2010.paths import ROOT
from support.fixtures import freeze_state
from support.fa_transfer_fixtures import small


def test_busy_completion_readback_and_bus_provenance(tmp_path,monkeypatch):
    def prepare(root,spec,output):
        state=small([0xe5814000,0xe5812004,0xe5830000,0xe5936000,0xe1a07007,
                     0xe5937000,0xe5835000,0xe1a08008,0xe1a08008,0xe5938000,0xe5919004])
        return freeze_state(state,output),{'seconds':10}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None
    fa=result['snapshot']['fa_transfer']
    assert (fa['write_commands'],fa['read_commands'],fa['completions'])==(1,1,2)
    assert fa['memory']==[{'start_halfword':0x8000,'halfwords':2,'little_endian_hex':'31ec008e'}]
    assert result['snapshot']['registers'][6:10]==[1,0,0,0xec318e00]
    assert result['memory_producer']['events']==8
    rows=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert all(0xfa040024<=r['address']<=0xfa04002c and r['size']==4 for r in rows)
