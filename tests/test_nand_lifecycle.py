"""Explicit ideal-cell spare policy records history without inventing wear failures."""
from db2010.diagnostics.artifacts import artifact as artifact_path
import pytest
import pyarrow.parquet as pq

from test_nand import execute, write, read, program, spare_read, block_erase
from test_nand_spare_state import startup_metadata


def lifecycle(page):
    return [*startup_metadata(page),*program(page,3,0xaf),
            *program(page,3,0xab),*program(page,3,0xaa),*program(page,3,0)]


def test_native_monotonic_slot_lifecycle_and_reference_diagnostics(tmp_path,monkeypatch):
    ops=[write(0x10,0xff),*block_erase(0),*lifecycle(0),*spare_read(0,3)]
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True,spare_limit=None)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    p=result['snapshot']['nand']['spare_program']
    assert p['page_program_counts']=={'0':7} and p['pages_over_reference']==[0]
    assert p['partial_program_limit'] is None and p['manufacturer_reference_limit']==3
    assert p['physical_reliability_established'] is False
    assert [o['new_hex'] for o in p['operations'] if o['column']==3]==['bf','af','ab','aa','00']
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert [e['value'] for e in events if not e['write']]==[0]


def test_history_is_not_truncated_after_255_and_reset_preserves_it(tmp_path,monkeypatch):
    ops=[write(0x10,0xff),*(op for _ in range(260) for op in program(0,3,0)),
         write(0x10,0xff),*spare_read(0,3)]
    result,failure=execute(tmp_path,monkeypatch,ops,spare_limit=None)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    p=result['snapshot']['nand']['spare_program']
    assert p['page_program_counts']=={'0':260} and len(p['operations'])==260
    assert p['pages_over_reference']==[0]


def test_erase_resets_all_32_page_counts_and_preserves_neighbor(tmp_path,monkeypatch):
    ops=[write(0x10,0xff),*lifecycle(0),*lifecycle(31),*lifecycle(32),
         *block_erase(17),*program(31,3,0xbf),*spare_read(0,3),*spare_read(32,3)]
    result,failure=execute(tmp_path,monkeypatch,ops,'all-spare-ff-valid-footers',erase=True,spare_limit=None)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    p=result['snapshot']['nand']['spare_program']
    assert p['page_program_counts']=={'31':1,'32':7} and p['pages_over_reference']==[32]
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert [e['value'] for e in events if not e['write']]==[255,0]


@pytest.mark.parametrize('ops,reason',[
    ([write(0x10,0xff),*program(32,3,0)],'nand_program_unknown_spare'),
    ([write(0x10,0xff),*program(0,3,0),*program(0,3,1)],'nand_program_requires_erase'),
    ([write(0x10,0xff),write(0x10,0x80)],'nand_program_pointer_unmodeled'),
])
def test_no_wear_profile_retains_cell_and_sequence_guards(tmp_path,monkeypatch,ops,reason):
    result,failure=execute(tmp_path,monkeypatch,ops,spare_limit=None)
    assert failure is None and result['snapshot']['stop']['reason']==reason
