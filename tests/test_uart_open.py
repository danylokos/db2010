"""Original UART status branch and read-only status-input guards."""
from support.guarded import run as guarded_run
from support.paths import RESOURCES
from db2010.diagnostics.artifacts import artifact as artifact_path
import json,struct
from pathlib import Path
import pyarrow.parquet as pq
import pytest
from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation

ORIGINAL=json.loads((RESOURCES/'uart-open-original.json').read_text())


@pytest.mark.parametrize('value,target',[(16,0x44a97932),(0,0x44a9790a),(0xffff,0x44a97932),(None,None)])
@pytest.mark.parametrize('base',[0xfb060000,0xfc030000])
def test_original_status_bit4_branch(tmp_path,monkeypatch,value,target,base):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        code=artifact_path(output, 'original.bin', create=True);code.write_bytes(bytes.fromhex(ORIGINAL['uart_open']['hex']))
        state=json.loads(path.read_text());state['entry']=0x44a97904;state['cpsr']=0xf3;state['instruction_limit']=3
        state['registers'][2]=base
        state['regions']=[{'name':'fixture.original','address':0x44a97000,'size':4096,'readonly':True,
                          'seeds':[{'address':0x44a97800,'file':str(code)}]}]
        state['executable_ranges']=[{'start':0x44a97800,'end':0x44a979aa}]
        state['device_registers']=[{'address':base+14,'width':2,'initial_value':value,'read_or_mask':0,'access':'read-only','name':'fixture.status'}]
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True);assert failure is None
    snap=result['snapshot']
    assert snap['stop']['reason']==('device_register_initial_state_unknown' if target is None else 'instruction_limit')
    if target is not None:
        assert snap['registers'][15]==target
        events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
        assert [(x['pc'],x['address'],x['value'],x['write']) for x in events]==[(0x44a97904,base+14,value,False)]


@pytest.mark.parametrize('value',[0,16,255])
@pytest.mark.parametrize('base',[0xfb060000,0xfc030000])
def test_status_input_rejects_all_native_writes(tmp_path,monkeypatch,value,base):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        code=artifact_path(output, 'code.bin');code.write_bytes(struct.pack('<3I',0xe3a00000|value,0xe1c100b0,0xeafffffe))
        state=json.loads(path.read_text());state['regions'][0]['seeds']=[{'address':0x1000,'file':str(code)}]
        state['executable_ranges']=[{'start':0x1000,'end':0x100c}]
        state['registers'][1]=base+14
        state['device_registers']=[{'address':base+14,'width':2,'initial_value':16,'read_or_mask':0,'access':'read-only','name':'fixture.status'}]
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True);assert failure is None
    assert result['snapshot']['stop']['reason']=='device_register_write_unmodeled'
    assert result['snapshot']['device_registers'][str(base+14)]==16
