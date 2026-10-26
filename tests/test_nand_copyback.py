"""Native known-page copy-back, same-plane constraints and partial-program policy."""
import pytest
import json
from pathlib import Path
from support.fixtures import qemu_run
from db2010.diagnostics.artifacts import artifact

from test_nand import execute, write, read, address, block_erase, program
from support.controlled_clock import controlled_clock
from test_nand_main import main_program, main_read, reads


def copyback(source,target,column=0):
    return [write(0x10,0),*address(source),write(0x20,0),write(0x10,0x70),read(),
            write(0x10,0x8a),*address(target,column)]


def setup():
    data=bytes(range(256))*2+bytes(range(16))
    return data,[write(0x10,0xff),*block_erase(0),*block_erase(64),*main_program(0,data)]


def test_all_528_source_bytes_copy_without_data_reload_and_reset_preserves_cells(tmp_path,monkeypatch):
    data,ops=setup();ops += [*copyback(0,64),write(0x10,0x70),read(),write(0x10,0xff),*main_read(64,528)]
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True,main=True,copy=True)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    assert reads(tmp_path)==[0xc0,0xc0,*data]
    n=result['snapshot']['nand'];c=n['copy_back']
    assert c['operations']==[{'source_page':0,'page':64,'old_hex':'ff'*528,'input_hex':data.hex(),'new_hex':data.hex()}]
    assert n['main_array']['page_program_counts']=={'0':1,'64':1}
    assert c['copied_pages']==[64] and c['partial_programs_after_copy']==0


def test_omitting_histories_preserves_nand_cells_counts_and_guards(tmp_path,monkeypatch,controlled_clock):
    _,ops=setup()
    ops += [*program(0,3,0),*copyback(0,64),*program(64,3,0),
            *main_program(64,b'\x00'),*main_read(64,8),*main_program(64,b'\x00')]
    reference,failure=execute(tmp_path,monkeypatch,ops,erase=True,main=True,
        copy='no-wear',spare_limit=None)
    assert failure is None
    from db2010.diagnostics.artifacts import prepared_references
    state=prepared_references(json.loads(artifact(tmp_path/'run','state.json').read_text()),tmp_path/'run',absolute=True)
    state['record_diagnostics']=False
    for region in state['regions']:
        for seed in region.get('seeds',[]):seed['data']=Path(seed.pop('file')).read_bytes()
    actual=qemu_run(state,tmp_path/'without-history',trace=False)
    expected=reference['snapshot']
    for key in ('registers','observations','stop'):
        assert actual[key]==expected[key]
    assert actual['stop']['reason']=='nand_main_partial_program_limit'
    for section,history in [('spare_program','operations'),('main_array','program_operations'),
                            ('block_erase','operations'),('copy_back','operations')]:
        wanted=dict(expected['nand'][section])
        assert wanted.pop(history)
        observed=dict(actual['nand'][section])
        assert observed.pop(history)==[]
        assert observed==wanted


@pytest.mark.parametrize('no_wear',[False,True])
def test_partial_after_copy_is_strict_or_explicitly_diagnosed(tmp_path,monkeypatch,no_wear):
    data,ops=setup();ops += [*copyback(0,64),*program(64,3,1)]
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True,main=True,
                           copy='no-wear' if no_wear else True,spare_limit=None if no_wear else 3)
    assert failure is None
    stop=result['snapshot']['stop']
    assert stop['reason']==('instruction_limit' if no_wear else 'nand_copyback_partial_program_unmodeled')
    c=result['snapshot']['nand']['copy_back'];assert c['partial_programs_after_copy']==int(no_wear)
    assert c['physical_reliability_established'] is False


@pytest.mark.parametrize('target,column,reason',[
    (32,0,'nand_copyback_cross_plane'),(96,0,'nand_copyback_cross_plane'),
    (128,0,'nand_copyback_unknown_destination'),(64,1,'nand_copyback_column_unmodeled'),
    (0,0,'nand_copyback_requires_erased_main'),
])
def test_destination_and_plane_guards_leave_no_copy_effect(tmp_path,monkeypatch,target,column,reason):
    data,ops=setup();ops += copyback(0,target,column)
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True,main=True,copy=True)
    assert failure is None and result['snapshot']['stop']['reason']==reason
    assert result['snapshot']['nand']['copy_back']['operations']==[]
    assert result['snapshot']['nand']['main_array']['page_program_counts']=={'0':1}


@pytest.mark.parametrize('source,prefix,reason',[
    (128,[write(0x10,0xff)],'nand_copyback_unknown_source'),
    (0,[write(0x10,0xff),write(0x10,0x70)],'nand_copyback_source_unmodeled'),
])
def test_source_must_be_a_known_main_page_register(tmp_path,monkeypatch,source,prefix,reason):
    ops=[*prefix,*copyback(source,64)] if source else [*prefix,write(0x10,0x8a)]
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True,main=True,copy=True)
    assert failure is None and result['snapshot']['stop']['reason']==reason
    assert result['snapshot']['nand']['copy_back']['operations']==[]


def test_erase_clears_copied_page_restriction_and_other_page_survives(tmp_path,monkeypatch):
    data,ops=setup();ops += [*copyback(0,64),*block_erase(64),*program(64,3,0xbf),*main_read(0,528)]
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True,main=True,copy=True)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    assert result['snapshot']['nand']['copy_back']['copied_pages']==[]
    assert reads(tmp_path)==[0xc0,*data]
