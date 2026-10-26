from support.guarded import run as guarded_run
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
from pathlib import Path
import struct

import pytest
import pyarrow.parquet as pq

from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation

PROFILE={'profile':'tick1-upcounter-irq-assumption','frequency_hz':131072,'max_irqs':8}


def setup(output,path,code,*,masks=0,limit=20):
    (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<'+str(len(code))+'I',*code))
    state=json.loads(path.read_text());state['instruction_limit']=limit
    state['executable_ranges'][0]['end']=0x1000+4*len(code)
    state['timer_irq']=PROFILE
    state['device_registers']=[{'address':address,'width':4,'initial_value':masks if address in (0xFB020104,0xFB020204) else None,
                               'read_or_mask':0,'name':'fixture.irq-latch'} for address in (0xFB020104,0xFB020204,0xFB020118,0xFB020218)]
    return state


def test_native_wfi_irq_entry_ack_and_return(tmp_path,monkeypatch):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        state=setup(output,path,[0xE5832000,0xE5810000,0xE121F00C,0xEE070F90,0xE3A0702A,0xEAFFFFFE],limit=14)
        state['registers'][:7]=[1,0xF9010000,5,0xF9010004,0,0,0xFB020218]
        state['registers'][12]=0x53
        vector=artifact_path(output, 'irq.bin', create=True);vector.write_bytes(struct.pack('<7I',0xE14F8000,0xE1A0900E,0xE1A0A00D,0xE3A05001,0xE5865000,0xE5065100,0xE25EF004))
        state['regions'].append({'name':'fixture.irq','address':0,'size':4096,'readonly':True,'seeds':[{'address':0x18,'file':str(vector)}]})
        state['executable_ranges'].append({'start':0x18,'end':0x34})
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    assert result['snapshot']['registers'][7:11]==[42,0x53,0x1014,0]
    assert result['snapshot']['cpsr']==0x53
    timer=result['snapshot']['timer_irq']
    assert timer['irqs']==1 and timer['idle_cycles']==0 and not timer['pending'] and not timer['selected']
    assert result['snapshot']['irq_events'][0]['rejected_candidate'] is False
    rows=pq.read_table(artifact_path((tmp_path) / 'run', 'trace.parquet')).to_pylist()
    assert next(r for r in rows if r['pc']==0x18)['registers']['cpsr']==0xD2
    assert timer['last_cycles'] >= 6
    assert result['snapshot']['virtual_ns'] >= 45000



@pytest.mark.parametrize('address,size,write,value,reason',[
    (0xF9010004,4,False,0,'timer_load_unknown'),
    (0xF9010000,4,True,2,'timer_other_channel_unmodeled'),
    (0xF901001C,4,True,1,'timer_irq_register_write_unmodeled'),
    (0xFB020110,4,False,0,'irq_selection_without_pending'),
    (0xF9010000,2,True,1,'timer_irq_access_width_unmodeled')])
def test_timer_negative_access_preserves_effects(tmp_path,monkeypatch,address,size,write,value,reason):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        instruction=(0xE5810000 if write else 0xE5912000) if size==4 else 0xE1C100B0
        state=setup(output,path,[instruction]);state['registers'][:3]=[value,address,77]
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']==reason
    assert result['snapshot']['registers'][2]==77
    assert result['snapshot']['timer_irq']['control']==0
    assert result['memory_producer']['events']==0


def test_unmodeled_wfi_wake_stops(tmp_path,monkeypatch):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        state=setup(output,path,[0xEE070F90]);path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='wfi_without_modeled_wake'


@pytest.mark.parametrize('ack_order,reason',[(True,'instruction_limit'),(False,'irq_primary_ack_before_secondary')])
def test_native_virtual_clock_irq_and_ack_order(tmp_path,monkeypatch,ack_order,reason):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        state=setup(output,path,[0xE5832000,0xE5810000,0xE121F00C,0xEAFFFFFE],limit=140)
        state['registers'][:7]=[1,0xF9010000,0,0xF9010004,0,0,0xFB020218]
        state['registers'][12]=0x53
        stores=[0xE5865000,0xE5065100] if ack_order else [0xE5065100,0xE5865000]
        vector=artifact_path(output, 'irq.bin', create=True);vector.write_bytes(struct.pack('<5I',0xE14F8000,0xE3A05001,*stores,0xE25EF004))
        state['regions'].append({'name':'fixture.irq','address':0,'size':4096,'readonly':True,'seeds':[{'address':0x18,'file':str(vector)}]})
        state['executable_ranges'].append({'start':0x18,'end':0x2C})
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']==reason
    event=result['snapshot']['irq_events'][0]
    assert not event['rejected_candidate'] and event['virtual_ns'] >= 7629
    rows=pq.read_table(artifact_path((tmp_path) / 'run', 'trace.parquet')).to_pylist()
    assert result['snapshot']['timer_irq']['irqs']==1
    if not ack_order:
        assert result['snapshot']['timer_irq']['pending'] and result['snapshot']['timer_irq']['selected']
