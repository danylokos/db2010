from support.guarded import run as guarded_run
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
from pathlib import Path
import struct
import pytest
from db2010.execution import runner
from db2010.paths import ROOT
from oracles.pl080 import Pl080,DmaFault
from test_runner import synthetic_preparation

PROFILE={'profile':'cold-disabled-init','address':0xF2000000,'channels':8}


@pytest.mark.parametrize('offset,value',[(8,255),(16,255),(0x30,1),(0x100,0x1234),(0x108,0x1000),(0x10C,0x480000),(0x110,0xC000),(0x1F0,0)])
def test_native_dma_initialization_matches_oracle(tmp_path,monkeypatch,offset,value):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<2I',0xE5810000,0xEAFFFFFE))
        state=json.loads(path.read_text());state['executable_ranges'][0]['end']=0x1008;state['instruction_limit']=2
        state['registers'][:2]=[value,0xF2000000+offset];state['pl080_dma']=PROFILE
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    oracle=Pl080(PROFILE);oracle.access('write',0xF2000000+offset,4,value)
    expected=oracle.summary();expected.pop('config');assert result['snapshot']['pl080']==expected


@pytest.mark.parametrize('offset,value',[(0x110,1),(0x110,0x800000),(0x108,2),(0x10C,0xC0000),(0x20,1),(8,256),(0,1),(0x114,0)])
def test_dma_rejected_operations_preserve_state(tmp_path,monkeypatch,offset,value):
    oracle=Pl080(PROFILE)
    with pytest.raises(DmaFault) as caught:oracle.access('write',0xF2000000+offset,4,value)
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output);(artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<I',0xE5810000))
        state=json.loads(path.read_text());state['registers'][:2]=[value,0xF2000000+offset];state['pl080_dma']=PROFILE
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']==caught.value.reason
    assert result['memory_producer']['events']==0
