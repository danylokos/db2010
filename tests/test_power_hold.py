"""Original startup/shutdown hold RMWs under a narrowly scoped release hypothesis."""
from support.guarded import run as guarded_run
from db2010.diagnostics.artifacts import artifact as artifact_path
import json,struct
from pathlib import Path
import pyarrow.parquet as pq
import pytest
from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation


@pytest.mark.parametrize('caller,enabled,initial,expected',[
    ('startup',True,0x8d,'instruction_limit'),
    ('startup',False,0x8d,'power_hold_release_unmodeled'),
    ('shutdown',True,0x8d,'power_hold_release_unmodeled'),
    ('startup',True,0x89,'power_hold_release_unmodeled'),
    ('startup',True,None,'power_hold_initial_state_unknown'),
])
def test_original_hold_release_scope(tmp_path,monkeypatch,caller,enabled,initial,expected):
    if caller=='startup':start=0x44aceab0;raw='2a492b4a13880b401380';mask=0x44aceb5c;pointer=0x44aceb60
    else:start=0x44a949b2;raw='0b480b490a8802400a80';mask=0x44a949e0;pointer=0x44a949e4
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        code=artifact_path(output, 'original.bin', create=True);code.write_bytes(bytes.fromhex(raw))
        literals=artifact_path(output, 'literals.bin', create=True);literals.write_bytes(struct.pack('<II',0xfffe,0xf9050058))
        state=json.loads(path.read_text());state['entry']=start;state['cpsr']=0xf3;state['instruction_limit']=5
        state['regions']=[{'name':'fixture.original','address':start&~4095,'size':4096,'readonly':True,
            'seeds':[{'address':start,'file':str(code)},{'address':mask,'file':str(literals)}]}]
        state['executable_ranges']=[{'start':start,'end':start+10}]
        state['power_hold']={'address':0xf9050058,'initial_value':initial,'rtc_init_mask':0x8c}
        if enabled:state['power_hold']['startup_release']='capture-once-without-supply-effect'
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']==expected
    snap=result['snapshot'];events=[e for e in pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist() if e['address']==0xf9050058]
    if expected=='instruction_limit':
        assert [(e['pc'],e['value'],e['write']) for e in events]==[(0x44aceab4,0x8d,False),(0x44aceab8,0x8c,True)]
        assert snap['power_hold']==0x8c and snap['power_startup_releases']==1
    else:
        assert snap['power_hold']==initial and not any(e['write'] for e in events)
        if enabled:assert snap['power_startup_releases']==0


def test_second_release_after_reassertion_is_guarded(tmp_path,monkeypatch):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        # Original startup RMW then a fixture reassertion/branch back to it.
        native=artifact_path(output, 'native.bin', create=True);native.write_bytes(bytes.fromhex('2a492b4a13880b401380'))
        tail=artifact_path(output, 'tail.bin', create=True);tail.write_bytes(bytes.fromhex('1388012003431380f5e7'))
        literals=artifact_path(output, 'literals.bin', create=True);literals.write_bytes(struct.pack('<II',0xfffe,0xf9050058))
        state=json.loads(path.read_text());state['entry']=0x44aceab0;state['cpsr']=0xf3;state['instruction_limit']=30
        state['regions']=[{'name':'fixture.original','address':0x44ace000,'size':4096,'readonly':True,'seeds':[
            {'address':0x44aceab0,'file':str(native)},{'address':0x44aceaba,'file':str(tail)},{'address':0x44aceb5c,'file':str(literals)}]}]
        state['executable_ranges']=[{'start':0x44aceab0,'end':0x44aceac4}]
        state['power_hold']={'address':0xf9050058,'initial_value':0x8d,'rtc_init_mask':0x8c,'startup_release':'capture-once-without-supply-effect'}
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='power_hold_release_unmodeled'
    assert result['snapshot']['power_startup_releases']==1 and result['snapshot']['power_hold']==0x8d
