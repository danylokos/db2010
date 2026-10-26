from support.guarded import run as guarded_run
from support.paths import BASELINE_MODELS
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
from pathlib import Path
import struct
import pytest
import pyarrow.parquet as pq

from db2010.execution import runner
from db2010.paths import ROOT
from db2010.preparation.inputs import nand_spare_seeds
from oracles.nand import Nand,NandFault
from test_runner import synthetic_preparation

PROFILE=json.loads((BASELINE_MODELS).read_text())['nand']


def execute(tmp_path,monkeypatch,operations,spare_profile='first-erase-block-ff-valid-footer',*,erase=False,main=False,spare_limit=3,copy=False):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output);code=[]
        for kind,off,value in operations:
            if kind=='write':
                code += ([0xE59F0000,0xEA000000,value] if value>255 else [0xE3A00000|value])
                code += [0xE5C10000|off]
            else:code += [0xE5D12000|off]
        code.append(0xEAFFFFFE);(artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<'+str(len(code))+'I',*code))
        seeds=artifact_path(output, 'seeds.bin', create=True);seeds.write_bytes(nand_spare_seeds(spare_profile))
        state=json.loads(path.read_text());state['instruction_limit']=len(code);state['executable_ranges'][0]['end']=0x1000+4*len(code)
        state['regions'][0]['size']=max(state['regions'][0]['size'],(4*len(code)+4095)&~4095)
        state['registers'][1]=0x50000000;state['nand']={**PROFILE,'prepared_spare':str(seeds)}
        state['nand']['synthetic_spare']={'profile':spare_profile}
        if erase:
            state['nand']['block_erase']={'profile':'samsung-x8-block-erase-immediate'}
        if main:
            state['nand']['main_array']={'profile':'samsung-x8-known-main-read-program'}
        if copy:
            state['nand']['copy_back']={'profile':'samsung-x8-known-page-copyback-no-wear' if copy=='no-wear' else 'samsung-x8-known-page-copyback'}
        if spare_limit == 4:
            state['nand']['spare_program']={'profile':'samsung-x8-known-spare-four-pulse-startup'}
        elif spare_limit is None:
            state['nand']['spare_program']={'profile':'samsung-x8-known-spare-lifecycle-no-wear'}
        elif spare_limit != 3:
            raise ValueError('unsupported fixture spare program limit')
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    return guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)


def write(off,value):return ('write',off,value)
def read():return ('read',0,0)
def address(page,column=0):return [write(8,column),write(8,page&255),write(8,page>>8)]
def program(page,column,value):return [write(0x10,0x50),write(0x10,0x80),*address(page,column),write(0,value),write(0x10,0x10)]
def block_erase(page):return [write(0x10,0x60),write(8,page&255),write(8,page>>8),write(0x10,0xd0)]
def spare_read(page,column):return [write(0x10,0x50),*address(page,column),write(0x20,0),read()]


def test_nand_erase_resets_target_block_and_program_history(tmp_path,monkeypatch):
    ops=[write(0x10,0xff),*program(0,3,0),*program(0,3,0),*program(0,3,0),
         *program(32,3,0),*block_erase(17),write(0x10,0x70),write(0x20,0),read(),
         *spare_read(0,3),*spare_read(0,14),*spare_read(32,3),
         *program(0,3,0),write(0x10,0xff),*spare_read(0,3)]
    result,failure=execute(tmp_path,monkeypatch,ops,'all-spare-ff-valid-footers',erase=True)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    nand=result['snapshot']['nand']
    assert nand['block_erase']['operations']==[{'page':17,'block':0}]
    assert nand['block_erase']['known_main_ff_blocks']==[0]
    assert nand['block_erase']['known_main_bytes']==16384
    assert nand['spare_program']['page_program_counts']=={'0':1,'32':1}
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert [e['value'] for e in events if not e['write']]==[0xc0,255,255,0,0]
    assert nand['spare_program']['operations'][-1]['old_hex']=='ff'


def test_nand_erase_last_block_establishes_only_target_unknown_spare(tmp_path,monkeypatch):
    ops=[write(0x10,0xff),*block_erase(65535),*spare_read(65535,15),
         write(0x10,0xff),*spare_read(65504,5),*spare_read(32,5)]
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True)
    assert failure is None and result['snapshot']['stop']['reason']=='nand_read_unknown_spare'
    nand=result['snapshot']['nand']
    assert nand['spare_read']['known_bytes']==1024
    assert nand['spare_read']['page']==32 and nand['spare_read']['column']==5
    assert nand['block_erase']['operations']==[{'page':65535,'block':2047}]
    assert nand['block_erase']['known_main_ff_blocks']==[2047]
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert [e['value'] for e in events if not e['write']]==[255,255]


@pytest.mark.parametrize('ops,reason',[
    ([write(0x10,0x60)],'nand_handoff_state_unmodeled'),
    ([write(0x10,0xff),write(0x10,0xd0)],'nand_erase_confirm_unmodeled'),
    ([write(0x10,0xff),write(0x10,0x60),write(8,0),write(0x10,0xd0)],'nand_erase_confirm_unmodeled'),
    ([write(0x10,0xff),write(0x10,0x60),write(8,0),write(8,0),write(8,0)],'nand_address_sequence_unmodeled'),
    ([write(0x10,0xff),write(0x10,0x60),write(0x10,0x70)],'nand_erase_pending_unmodeled'),
    ([write(0x10,0xff),write(0x10,0x60),write(8,0),write(0x10,0xff),write(0x10,0xd0)],'nand_erase_confirm_unmodeled'),
    ([write(0x10,0xff),write(0x10,0x60),write(0,0)],'nand_data_write_unmodeled'),
])
def test_nand_erase_rejects_incomplete_or_unsupported_sequences(tmp_path,monkeypatch,ops,reason):
    result,failure=execute(tmp_path,monkeypatch,ops,erase=True)
    assert failure is None and result['snapshot']['stop']['reason']==reason
    initial=result['initial_snapshot']['nand']['spare_read']
    final=result['snapshot']['nand']['spare_read']
    assert initial['known_contents_sha256']==final['known_contents_sha256']
    assert initial['known_bytes']==final['known_bytes']
    assert result['snapshot']['nand']['block_erase']['operations']==[]
    assert result['snapshot']['nand']['block_erase']['known_main_bytes']==0


@pytest.mark.parametrize('profile,footer',[
    ('all-spare-ff-first-valid-footer',[255,255]),
    ('all-spare-ff-valid-footers',[0x54,0x12])])
def test_full_spare_inputs_preserve_baseline_and_reach_last_block(tmp_path,monkeypatch,profile,footer):
    seeds=nand_spare_seeds(profile)
    records=list(struct.iter_unpack('<HBB',seeds))
    assert len(records)==65536*16
    assert len({(p,c) for p,c,v in records})==len(records)
    assert seeds[:32*16*4]==nand_spare_seeds('first-erase-block-ff-valid-footer')
    ops=[write(0x10,0xff),write(0x10,0x50),*address(65504,5),write(0x20,0),read(),
         write(0x10,0x50),*address(65504,14),read(),read(),write(0x10,0)]
    result,failure=execute(tmp_path,monkeypatch,ops,profile)
    assert failure is None
    assert result['snapshot']['stop']['reason']=='nand_array_operation_unmodeled'
    assert result['snapshot']['nand']['spare_read']['known_bytes']==65536*16
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert [e['value'] for e in events if not e['write']]==[255,*footer]
    assert not result['snapshot']['nand']['spare_program']['operations']


def test_nand_strb_truncates_full_register_at_guard_and_bus(tmp_path,monkeypatch):
    ops=[write(0x10,0x100ff),write(0x10,0x150),write(8,0x105),
         write(8,0x100),write(8,0x101),write(0x20,0x100),read()]
    result,failure=execute(tmp_path,monkeypatch,ops,'all-spare-ff-first-valid-footer')
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    spare=result['snapshot']['nand']['spare_read']
    assert spare['page']==256 and spare['column']==6 and spare['address_cycles']==[5,0,1]
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert [e['value'] for e in events if e['address']>=0x50000000]==[255,80,5,0,1,0,255]


@pytest.mark.parametrize('ops',[
    [write(0x10,0xff),write(0x10,0x90),write(8,0),write(0x20,0),read(),read()],
    [write(0x10,0xff),write(0x10,0x70),write(0x20,0),read()],
    [write(0x10,0xff),write(0x10,0x50),*address(0,0xFE),write(0x20,0),read(),read()],
    [write(0x10,0xff),write(0x10,0x50),*address(0,3),write(0x10,0x70),write(0x20,0),read(),write(0x10,0x50),read()],
    [write(0x10,0xff),*program(0,3,0),write(0x10,0x50),*address(0,3),write(0x20,0),read()]])
def test_native_nand_protocol_and_contents_match_oracle(tmp_path,monkeypatch,ops):
    result,failure=execute(tmp_path,monkeypatch,ops);assert failure is None
    assert result['snapshot']['stop']['reason']=='instruction_limit'
    oracle=Nand(PROFILE)
    for event in pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist():
        value=oracle.access('write' if event['write'] else 'read',event['address'],event['size'],event['value'] if event['write'] else None)
        assert value==event['value']
    expected=oracle.summary();expected.pop('config');assert result['snapshot']['nand']==expected


@pytest.mark.parametrize('ops,reason',[
    ([write(0x10,0x90)],'nand_handoff_state_unmodeled'),
    ([write(0x10,0xff),read()],'nand_data_gate_closed'),
    ([write(0x10,0xff),write(0x10,0)],'nand_array_operation_unmodeled'),
    ([write(0x10,0xff),write(0x10,0x50),write(0x10,0x70)],'nand_incomplete_spare_address'),
    ([write(0x10,0xff),write(0x10,0x50),*address(32,5),write(0x20,0),read()],'nand_read_unknown_spare'),
    ([write(0x10,0xff),*program(32,0,0)],'nand_program_unknown_spare'),
    ([write(0x10,0xff),*program(0,3,0),*program(0,3,255)],'nand_program_requires_erase'),
    ([write(0x10,0xff),*program(0,3,0),*program(0,3,0),*program(0,3,0),*program(0,3,0)],'nand_spare_partial_program_limit'),
    ([write(0x10,0xff),write(0x20,1)],'nand_gate_value_unmodeled')])
def test_native_nand_unsupported_inputs_preserve_known_contents(tmp_path,monkeypatch,ops,reason):
    result,failure=execute(tmp_path,monkeypatch,ops)
    assert failure is None and result['snapshot']['stop']['reason']==reason
    oracle=Nand(PROFILE)
    try:
        for kind,off,value in ops:oracle.access(kind,0x50000000+off,1,value if kind=='write' else None)
    except NandFault as fault:assert fault.reason==reason
    expected=oracle.summary();expected.pop('config');assert result['snapshot']['nand']==expected


def test_f500_zero_command_profile_and_guard(tmp_path,monkeypatch):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<3I',0xE1C100B0,0xE3A00001,0xE1C100B0))
        state=json.loads(path.read_text());state['executable_ranges'][0]['end']=0x100C;state['registers'][1]=0xF500081C
        state['f500_command_capture']={'profile':'081c-halfword-zero-command-capture'}
        state['device_registers']=[{'address':0xF500081C,'width':2,'initial_value':None,'read_or_mask':0,'name':'fixture.f500','access':'write-only','allowed_writes':[0]}]
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='device_register_command_unmodeled'
    assert result['snapshot']['f500_zero_commands']==1 and result['memory_producer']['events']==1
