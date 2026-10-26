"""Native GPIO42 pull-up sampling for the explicitly empty MemoryStick slot."""
from support.guarded import run as guarded_run
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
from pathlib import Path
import struct

import pytest

from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation


def fixture(monkeypatch, words, *, direction=0, data=0, enabled=True, address=0xf9000028):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<'+'I'*len(words),*words))
        state=json.loads(path.read_text())
        state['instruction_limit']=len(words)
        state['executable_ranges'][0]['end']=0x1000+4*len(words)
        state['registers'][:3]=[address,0xf900002a,0x77]
        state['device_registers']=[{'address':a,'width':2,'initial_value':v,
                                   'read_or_mask':0,'name':n}
                                  for a,v,n in [(0xf9000028,data,'port4 data'),
                                                (0xf900002a,direction,'port4 direction')]]
        if enabled:state['memorystick_slot']={'profile':'gpio42-empty-slot'}
        path.write_text(json.dumps(state)+'\n')
        return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)


def test_pullup_respects_direction_and_guest_output_latch(tmp_path,monkeypatch):
    # Input high; output-low; output-high; input high despite a low data store.
    fixture(monkeypatch,[0xe1d030b0,0xe3a04004,0xe1c140b0,0xe1d050b0,
                         0xe1c040b0,0xe1d060b0,0xe3a04000,0xe1c140b0,
                         0xe1c040b0,0xe1d080b0,0xeafffffe])
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True)
    assert failure is None and result['success']
    regs=result['snapshot']['registers']
    assert [regs[i] for i in (3,5,6,8)]==[4,0,4,4]
    assert result['snapshot']['device_registers'][str(0xf9000028)]==0
    assert result['snapshot']['memorystick_slot']['card_present'] is False


@pytest.mark.parametrize('enabled,direction,data,expected',[
    (False,0,0,0), (True,4,0x80,0x80), (True,0,0x80,0x84),
])
def test_empty_slot_only_samples_its_input_bit(tmp_path,monkeypatch,enabled,direction,data,expected):
    fixture(monkeypatch,[0xe1d020b0,0xeafffffe],enabled=enabled,direction=direction,data=data)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True)
    assert failure is None and result['success']
    assert result['snapshot']['registers'][2]==expected


@pytest.mark.parametrize('direction,data,word,address,reason',[
    (None,0,0xe1d020b0,0xf9000028,'memorystick_detect_direction_unknown'),
    (0,None,0xe1d020b0,0xf9000028,'device_register_initial_state_unknown'),
    (0,0,0xe5902000,0xf9000028,'device_register_access_unmodeled'),
    (0,0,0xe1d020b0,0xf900002c,'device_register_access_unmodeled'),
])
def test_empty_slot_retains_unknown_width_and_neighbor_guards(tmp_path,monkeypatch,
                                                            direction,data,word,address,reason):
    fixture(monkeypatch,[word],direction=direction,data=data,address=address)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True)
    assert failure is None
    assert result['snapshot']['stop']['reason']==reason
    assert result['snapshot']['registers'][2]==0x77
