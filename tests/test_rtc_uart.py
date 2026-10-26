from support.guarded import run as guarded_run
from support.paths import RESOURCES
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
from pathlib import Path
import struct

import pytest
from support.controlled_clock import controlled_clock

pytestmark = pytest.mark.usefixtures("controlled_clock")

from db2010.execution import runner
from db2010.paths import ROOT
from oracles.serial_lines import SerialLines
from test_runner import synthetic_preparation

RTC=json.loads((RESOURCES/'experiments/0191.json').read_text())['rtc_counter']


@pytest.mark.parametrize('offset',[0,2,4,6,8,12,14,16,18,20,30])
def test_rtc_reads_match_retained_calendar(tmp_path,monkeypatch,offset):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<2I',0xE1D120B0,0xEAFFFFFE))
        state=json.loads(path.read_text());state['instruction_limit']=2;state['executable_ranges'][0]['end']=0x1008
        state['registers'][1:3]=[0xF9050000+offset,77];state['rtc_counter']=RTC
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None
    expected={0:0,2:0,4:0,6:0,8:0,12:1,14:1,16:0,18:20,20:0,30:192}[offset]
    assert result['snapshot']['registers'][2]==expected


def test_rtc_samples_completed_instructions_at_tick_boundary(tmp_path,monkeypatch):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        code=[0xE1A00000]*475+[0xE1D120B0,0xE1D130B0,0xEAFFFFFE]
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<478I',*code))
        state=json.loads(path.read_text());state['instruction_limit']=478;state['executable_ranges'][0]['end']=0x1000+4*478
        state['registers'][1]=0xF9050000;state['rtc_counter']=RTC
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['registers'][2:4]==[0,1]


@pytest.mark.parametrize('direction',[0,0x11,1,0x10,None])
def test_direction_aware_serial_inputs(tmp_path,monkeypatch,direction):
    profile={'profile':'port1-rx-levels','uart0_rx_level':1,'uart1_rx_level':0}
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<2I',0xE1D120B0,0xEAFFFFFE))
        state=json.loads(path.read_text());state['instruction_limit']=2;state['executable_ranges'][0]['end']=0x1008
        state['registers'][1:3]=[0xF900000A,77];state['serial_lines']=profile
        state['device_registers']=[{'address':a,'width':2,'initial_value':v,'read_or_mask':0,'name':'fixture.gpio'}
                                  for a,v in ((0xF900000A,0x10),(0xF900000C,direction))]
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None
    if direction is None:
        assert result['snapshot']['stop']['reason']=='serial_gpio_direction_unknown'
        assert result['snapshot']['registers'][2]==77
    else:
        assert result['snapshot']['registers'][2]==SerialLines(profile).sample(0x10,direction)['value']


@pytest.mark.parametrize('port,base',list(enumerate((0xFB060000,0xFC040000,0xFC030000,0xFB040000))))
def test_all_uart_ports_capture_only_native_byte_writes(tmp_path,monkeypatch,port,base):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<3I',0xE1D122BC,0xE1C100B2,0xEAFFFFFE))
        state=json.loads(path.read_text());state['instruction_limit']=3;state['executable_ranges'][0]['end']=0x100C
        state['registers'][:3]=[65+port,base,77];state['uart_transmit']={'profile':'polled-tx-immediate-drain','free_slots':1}
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['registers'][2]==1
    assert (artifact_path((tmp_path) / 'run', f'uart{port}.bin')).read_bytes()==bytes([65+port])
    assert result['snapshot']['uart_bytes']==[int(i==port) for i in range(4)]


@pytest.mark.parametrize('kind,value,reason',[('rtc',1,'rtc_counter_write_unmodeled'),('uart',256,'uart_access_unmodeled')])
def test_peripheral_rejected_writes_have_no_bus_or_serial_effect(tmp_path,monkeypatch,kind,value,reason):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<I',0xE1C100B0))
        state=json.loads(path.read_text());state['registers'][:2]=[value,0xF9050000 if kind=='rtc' else 0xFB060002]
        state['rtc_counter']=RTC;state['uart_transmit']={'profile':'polled-tx-immediate-drain','free_slots':1}
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']==reason
    assert result['memory_producer']['events']==0
    assert (artifact_path((tmp_path) / 'run', 'uart0.bin')).read_bytes()==b''
