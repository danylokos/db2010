"""Execute the original F500 bit14 RMW and preserve unknown-state/width guards."""
from support.guarded import run as guarded_run
from db2010.diagnostics.artifacts import artifact as artifact_path
import json,struct
from pathlib import Path
import pyarrow.parquet as pq
import pytest
from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation


@pytest.mark.parametrize('initial,width,expected',[
    (0,2,'instruction_limit'),
    (None,2,'device_register_initial_state_unknown'),
    (1,2,'device_register_command_unmodeled'),
    (0,1,'device_register_access_unmodeled'),
])
def test_original_power_control_rmw(tmp_path,monkeypatch,initial,width,expected):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        # Authenticated MAIN44ACE98E..44ACE99A; literal44ACEA64=F5000800.
        code=artifact_path(output, 'original-rmw.bin', create=True);code.write_bytes(bytes.fromhex('8020c00134490a8802430a80'))
        literal=artifact_path(output, 'literal.bin', create=True);literal.write_bytes(struct.pack('<I',0xf5000800))
        state=json.loads(path.read_text());state['entry']=0x44ace98e;state['cpsr']=0xf3;state['instruction_limit']=6
        state['regions']=[{'name':'fixture.original','address':0x44ace000,'size':4096,'readonly':True,'seeds':[
            {'address':0x44ace98e,'file':str(code)},{'address':0x44acea64,'file':str(literal)}]}]
        state['executable_ranges']=[{'start':0x44ace98e,'end':0x44ace99a}]
        state['device_registers']=[{'address':0xf5000800,'width':width,'name':'explicit.bit14.latch',
            'initial_value':initial,'read_or_mask':0,'allowed_writes':[0,0x4000] if width==2 else [0]}]
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']==expected
    events=[x for x in pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist() if x['address']==0xf5000800]
    if initial==0 and width==2:
        assert [(x['pc'],x['size'],x['value'],x['write']) for x in events]==[(0x44ace994,2,0,False),(0x44ace998,2,0x4000,True)]
        assert result['snapshot']['device_registers'][str(0xf5000800)]==0x4000
    else:
        assert not any(x['write'] for x in events)
