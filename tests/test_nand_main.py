"""Native command fixtures for known NAND cells, status resume and page bounds."""
from db2010.diagnostics.artifacts import artifact as artifact_path
import pytest
import pyarrow.parquet as pq

from test_nand import execute, write, read, address, block_erase, spare_read, program


def main_program(page, data, column=0):
    return [write(0x10,0),write(0x10,0x80),*address(page,column),
            *(write(0,b) for b in data),write(0x10,0x10)]


def main_read(page, n, column=0, status=False):
    ops=[write(0x10,0),*address(page,column),write(0x20,0)]
    if status:
        ops += [write(0x10,0x70),read(),write(0x10,0),write(0x10,0)]
    return [*ops,*(read() for _ in range(n))]


def reads(tmp_path):
    return [e['value'] for e in pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist() if not e['write']]


def test_native_page_program_readback_spare_and_status_resume(tmp_path,monkeypatch):
    data=bytes(range(256))*2+b'\xff'*6+b'\x12\x34\x56\x00'
    ops=[write(0x10,0xff),*block_erase(17),*main_program(0,data),
         write(0x10,0x70),write(0x20,0),read(),write(0x24,0),
         *main_read(0,528,status=True),write(0x24,0),write(0x10,0xff),
         *main_read(0,3,250),*spare_read(0,6),read(),read(),read()]
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True,main=True)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    assert reads(tmp_path)==[0xc0,0xc0,*data,*([255]*6),250,251,252,0x12,0x34,0x56,0]
    nand=result['snapshot']['nand'];main=nand['main_array']
    assert main['known_blocks']==[0] and main['known_bytes']==16384
    assert main['data_reads']==515 and main['page_program_counts']=={'0':1}
    assert main['program_operations']==[{'page':0,'column':0,'old_hex':'ff'*522,'new_hex':data.hex(),'input_hex':data.hex()}]
    assert nand['block_erase']['known_main_ff_blocks']==[]
    assert nand['spare_program']['page_program_counts']=={}


def test_main_erase_reset_and_two_partial_pulses(tmp_path,monkeypatch):
    ops=[write(0x10,0xff),*block_erase(0),*main_program(0,b'\x7f'),
         *main_program(0,b'\x3f'),*block_erase(31),*main_read(0,1),
         *main_program(0,b'\x1f'),write(0x10,0xff),*main_read(0,1)]
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True,main=True)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    assert reads(tmp_path)==[255,31]
    main=result['snapshot']['nand']['main_array']
    assert main['page_program_counts']=={'0':1}
    assert [o['old_hex'] for o in main['program_operations']]==['ff','7f','ff']


@pytest.mark.parametrize('ops,reason',[
    ([write(0x10,0)],'nand_handoff_state_unmodeled'),
    ([write(0x10,0xff),*main_read(32,1)],'nand_read_unknown_main'),
    ([write(0x10,0xff),*main_program(32,b'\x00')],'nand_program_unknown_main'),
    ([write(0x10,0xff),write(0x10,0),write(0x20,0),read()],'nand_incomplete_main_address'),
    ([write(0x10,0xff),write(0x10,0),write(8,0),write(0x10,0x70)],'nand_incomplete_main_address'),
    ([write(0x10,0xff),write(0x10,0),*address(0),write(8,0)],'nand_address_sequence_unmodeled'),
    ([write(0x10,0xff),write(0x10,0),write(0x10,0x80),write(8,0),write(0x10,0x10)],'nand_program_confirm_unmodeled'),
    ([write(0x10,0xff),write(0x10,0),write(0x10,0x80),write(0x10,0x70)],'nand_program_pending_unmodeled'),
    ([write(0x10,0xff),*block_erase(0),*main_program(0,b'\x00'),*main_program(0,b'\x00'),*main_program(0,b'\x00')],'nand_main_partial_program_limit'),
    ([write(0x10,0xff),*block_erase(0),*main_read(0,529)],'nand_main_sequential_page_unmodeled'),
    ([write(0x10,0xff),write(0x10,0),write(0x10,0x80),*address(0),*(write(0,255) for _ in range(529))],'nand_program_page_bounds'),
    ([write(0x10,0xff),write(0x10,1)],'nand_array_operation_unmodeled'),
    ([write(0x10,0xff),write(0x10,0x8a)],'nand_array_operation_unmodeled'),
])
def test_main_guards_preserve_confirmed_cells(tmp_path,monkeypatch,ops,reason):
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True,main=True)
    assert failure is None and result['snapshot']['stop']['reason']==reason
    main=result['snapshot']['nand']['main_array']
    expected=sum(o==write(0x10,0x10) for o in ops)
    if reason in ('nand_program_unknown_main','nand_program_confirm_unmodeled','nand_main_partial_program_limit'):
        expected-=1
    assert len(main['program_operations'])==expected
    if reason=='nand_read_unknown_main':
        assert result['snapshot']['stop']['page']==32 and result['snapshot']['stop']['column']==0
        assert main['data_reads']==0 and not main['known_blocks']


def test_main_reset_abandons_unconfirmed_data(tmp_path,monkeypatch):
    ops=[write(0x10,0xff),*block_erase(0),write(0x10,0),write(0x10,0x80),
         *address(0),write(0,0),write(0x10,0xff),*main_read(0,1)]
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True,main=True)
    assert failure is None and reads(tmp_path)==[255]
    assert result['snapshot']['nand']['main_array']['program_operations']==[]


def test_main_program_ff_preserves_prior_spare_metadata(tmp_path,monkeypatch):
    data=b'\x00'*512+b'\xff'*6+b'\x12\x34\x56\x00'
    ops=[write(0x10,0xff),*block_erase(0),*program(0,3,0xbf),
         *program(0,14,0x54),*program(0,0,0x0b),*main_program(0,data),
         *main_read(0,522),*main_program(0,b'\x01'),*main_read(0,1)]
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True,main=True)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    expected=bytearray(data);expected[512]=0x0b;expected[515]=0xbf
    assert reads(tmp_path)==[*expected,0]
    nand=result['snapshot']['nand']
    assert nand['spare_program']['page_program_counts']=={'0':3}
    assert nand['main_array']['page_program_counts']=={'0':2}
    assert nand['main_array']['program_operations'][0]['new_hex']==expected.hex()
