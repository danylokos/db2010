"""Bounded fourth spare pulse: known cells, native readback, reset and fifth guard."""
from db2010.diagnostics.artifacts import artifact as artifact_path
import pytest
import pyarrow.parquet as pq

from test_nand import execute, write, address, program, read, spare_read, block_erase


def startup_metadata(page=0):
    return [*program(page,3,0xbf),*program(page,14,0x54),*program(page,0,0x0b)]


def values(tmp_path):
    return [x['value'] for x in pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist() if not x['write']]


def test_fourth_spare_state_program_and_native_readback(tmp_path,monkeypatch):
    ops=[write(0x10,0xff),*block_erase(0),*startup_metadata(),*program(0,3,0xaf),
         write(0x10,0x70),write(0x20,0),read(),*spare_read(0,3)]
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True,spare_limit=4)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    assert values(tmp_path)==[0xc0,0xaf]
    p=result['snapshot']['nand']['spare_program']
    assert p['page_program_counts']=={'0':4}
    assert p['operations'][-1]=={'page':0,'column':3,'old_hex':'bf','new_hex':'af'}
    assert p['partial_program_limit']==4 and p['manufacturer_reference_limit']==3
    assert p['physical_reliability_established'] is False


@pytest.mark.parametrize('extra,reason',[
    (program(0,3,0xa7),'nand_spare_partial_program_limit'),
    ([write(0x10,0xff),*program(0,3,0xa7)],'nand_spare_partial_program_limit'),
])
def test_fifth_spare_pulse_remains_guarded_without_cell_effects(tmp_path,monkeypatch,extra,reason):
    ops=[write(0x10,0xff),*block_erase(0),*startup_metadata(),*program(0,3,0xaf),*extra]
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True,spare_limit=4)
    assert failure is None and result['snapshot']['stop']['reason']==reason
    p=result['snapshot']['nand']['spare_program']
    assert p['page_program_counts']=={'0':4} and len(p['operations'])==4
    assert p['operations'][-1]['new_hex']=='af' and p['buffer']==[0xa7]


def test_erase_resets_four_pulse_history_and_keeps_neighbor_independent(tmp_path,monkeypatch):
    ops=[write(0x10,0xff),*block_erase(0),*startup_metadata(),*program(0,3,0xaf),
         *program(32,3,0),*block_erase(31),*spare_read(0,3),*startup_metadata(),
         *program(0,3,0xaf),*spare_read(0,3),*spare_read(32,3)]
    result,failure=execute(tmp_path,monkeypatch,ops,'all-spare-ff-valid-footers',erase=True,spare_limit=4)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    assert values(tmp_path)==[255,0xaf,0]
    assert result['snapshot']['nand']['spare_program']['page_program_counts']=={'0':4,'32':1}


def test_reset_abandons_pending_fourth_pulse(tmp_path,monkeypatch):
    ops=[write(0x10,0xff),*block_erase(0),*startup_metadata(),write(0x10,0x50),
         write(0x10,0x80),*address(0,3),write(0,0xaf),write(0x10,0xff),
         *spare_read(0,3),*program(0,3,0xaf),*spare_read(0,3)]
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True,spare_limit=4)
    assert failure is None and values(tmp_path)==[0xbf,0xaf]
    assert result['snapshot']['nand']['spare_program']['page_program_counts']=={'0':4}


@pytest.mark.parametrize('ops,reason',[
    ([write(0x10,0xff),*program(32,3,0)],'nand_program_unknown_spare'),
    ([write(0x10,0xff),*program(0,3,0),*program(0,3,255)],'nand_program_requires_erase'),
])
def test_four_pulse_profile_retains_known_cell_and_transition_guards(tmp_path,monkeypatch,ops,reason):
    result,failure=execute(tmp_path,monkeypatch,ops,spare_limit=4)
    assert failure is None and result['snapshot']['stop']['reason']==reason
    assert len(result['snapshot']['nand']['spare_program']['operations'])==(reason=='nand_program_requires_erase')
