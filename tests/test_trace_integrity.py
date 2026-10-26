"""Bounded ledger validation across batch boundaries and malformed ledgers."""
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from db2010.diagnostics import integrity


def ledger(path, indices, pcs, group=2):
    pq.write_table(pa.table({'index':pa.array(indices,pa.uint64()), 'pc':pa.array(pcs,pa.uint32())}),path,row_group_size=group)
    return pq.ParquetFile(path)


def test_merge_handles_sparse_repeated_events_and_empty_streams(tmp_path, monkeypatch):
    monkeypatch.setattr(integrity,'BATCH',2)
    t,m=tmp_path/'t.parquet',tmp_path/'m.parquet'
    ledger(t,list(range(1,8)),list(range(101,108)))
    ledger(m,[2,2,5,7],[102,102,105,107],group=3)
    integrity.memory_identity(t,m)
    ledger(m,[],[])
    integrity.memory_identity(t,m)
    ledger(t,[],[])
    integrity.memory_identity(t,m)


@pytest.mark.parametrize('ti,mi,mp,message',[
    ([1,1,3],[1],[101],'trace ordinal'),
    ([2,1,3],[1],[101],'trace ordinal'),
    ([1,2,3],[0],[101],'identity differs'),
    ([1,2,3],[4],[104],'identity differs'),
    ([1,2,3],[1,2],[101,999],'identity differs'),
    ([1,2,3],[2,1],[102,101],'instruction order'),
    ([1,2,3],[2,2,1],[102,102,101],'instruction order'),
])
def test_merge_rejects_invalid_indices_order_and_pc(tmp_path,monkeypatch,ti,mi,mp,message):
    monkeypatch.setattr(integrity,'BATCH',2)
    t,m=tmp_path/'t.parquet',tmp_path/'m.parquet'
    ledger(t,ti,[100+i for i in ti]);ledger(m,mi,mp)
    with pytest.raises(ValueError,match=message):integrity.memory_identity(t,m)


def test_reference_comparison_accepts_different_row_groups(tmp_path):
    def write(path,group,pcs):
        pq.write_table(pa.table(dict(index=[1,2,3,4],pc=pcs,size=[2]*4,thumb=[True]*4,
                                     bytes=[b'xx']*4,registers=[[0]*16]*4)),path,row_group_size=group)
        return pq.ParquetFile(path)
    a=write(tmp_path/'a',2,[1,2,3,4]);b=write(tmp_path/'b',3,[1,2,3,4])
    integrity.reference_instructions(a,b)
    b=write(tmp_path/'b',3,[1,2,9,4])
    with pytest.raises(ValueError,match='reference instruction'):integrity.reference_instructions(a,b)
