from support.guarded import run as guarded_run
from support.paths import RESOURCES
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
from pathlib import Path
import struct
import pytest
import pyarrow.parquet as pq
from db2010.execution import runner
from db2010.paths import ROOT
from oracles.i2c import PolledI2C,I2CFault
from test_runner import synthetic_preparation

PROFILE={'profile':'marita-polled-byte','address':0xF9040000,'completion_ns':128,
         'power_inputs':{'usb_connected':True,'charger_connected':False},'capture_boot_writes':True}


def execute(tmp_path,monkeypatch,operations,profile=PROFILE):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        code=[]
        for kind,offset,value in operations:
            if kind=='write':code += [0xE3A00000|value,0xE1C100B0|offset]
            else:code += [0xE1D120B0|offset]
            code += [0xE1A00000,0xE1A00000]
        code.append(0xEAFFFFFE)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<'+str(len(code))+'I',*code))
        state=json.loads(path.read_text());state['instruction_limit']=len(code);state['executable_ranges'][0]['end']=0x1000+4*len(code)
        state['regions'][0]['size']=((4*len(code)+4095)//4096)*4096
        state['registers'][1]=0xF9040000;state['i2c_controller']=profile
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    return guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)


def init():return [('write',2,4),('write',4,0x60),('write',0,0),('write',2,0)]


@pytest.mark.parametrize('reg,value',[(0xB0,0),(0xB1,0xAA),(0x23,None)])
def test_i2c_complete_recovered_transactions_match_oracle(tmp_path,monkeypatch,reg,value):
    ops=init()+[('write',2,0x24),('write',0,0x94),('write',2,0),('write',0,reg),('write',2,0)]
    if value is None:ops += [('write',2,0x24),('write',0,0x95),('write',2,0),('write',2,0),('read',0,0)]
    else:ops += [('write',0,value),('write',2,0)]
    ops += [('write',2,0x10),('read',2,0)]
    result,failure=execute(tmp_path,monkeypatch,ops)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    oracle=PolledI2C({**{k:v for k,v in PROFILE.items() if k!='completion_ns'}, 'completion_instructions':2})
    for event in pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist():
        value=oracle.access('write' if event['write'] else 'read',event['address'],event['size'],event['index'],event['value'] if event['write'] else None)
        assert value==event['value']
    actual=result['snapshot']['i2c'];expected=oracle.summary()
    assert actual['phase']==expected['phase'] and actual['control']==expected['control']
    assert actual['transactions']==len(expected['transactions'])==1
    assert actual['captured_writes']==expected['captured_writes']
    if reg==0x23:assert actual['received']==0x80


@pytest.mark.parametrize('ops,reason',[
    ([('read',2,0)],'i2c_initial_state_unknown'),
    ([('write',4,0x61)],'i2c_clock_value_unmodeled'),
    (init()+[('write',2,0x25)],'i2c_control_value_unmodeled'),
    (init()+[('write',2,0x24),('write',0,0x92),('write',2,0)],'i2c_slave_unmodeled'),
    (init()+[('read',0,0)],'i2c_data_read_unmodeled')])
def test_i2c_unknown_inputs_and_commands_stop(tmp_path,monkeypatch,ops,reason):
    result,failure=execute(tmp_path,monkeypatch,ops)
    assert failure is None and result['snapshot']['stop']['reason']==reason


ASYNC_INIT_PROFILE={**PROFILE,'profile':'marita-polled-byte-async-init-capture'}
REINITIALIZE=[('write',0,0),('write',2,0),('write',2,0x84),('write',4,0x60)]


def test_i2c_async_setup_stops_before_start_effects(tmp_path,monkeypatch):
    result,failure=execute(tmp_path,monkeypatch,init()+REINITIALIZE+
                           [('write',2,0xA4)],ASYNC_INIT_PROFILE)
    assert failure is None
    snap=result['snapshot'];state=snap['i2c']
    assert snap['stop']['reason']=='i2c_async_start_unmodeled'
    assert (state['async_init_step'],state['control'],state['clock'])==(3,0x84,0x60)
    assert state['pending_operation']==0 and state['transactions']==0
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert not any(e['write'] and e['value']==0xA4 for e in events)


@pytest.mark.parametrize('tail',[
    [('write',2,0x84)], # Staged zero/control zero must precede this setup.
    [('write',0,0),('write',2,0),('write',4,0x60)], # Wrong order.
    REINITIALIZE+[('read',2,0)], # No asynchronous readback contract.
    REINITIALIZE+[('write',2,0x90)], # STOP completion is also unsupported.
])
def test_i2c_async_setup_keeps_unknown_order_and_read_guards(tmp_path,monkeypatch,tail):
    result,failure=execute(tmp_path,monkeypatch,init()+tail,ASYNC_INIT_PROFILE)
    assert failure is None
    assert result['snapshot']['stop']['reason'] in (
        'i2c_control_value_unmodeled','i2c_async_init_sequence_unmodeled')


def test_i2c_legacy_profile_rejects_async_reinitialization(tmp_path,monkeypatch):
    result,failure=execute(tmp_path,monkeypatch,init()+REINITIALIZE)
    assert failure is None
    assert result['snapshot']['stop']['reason']=='i2c_transfer_sequence_unmodeled'


ASYNC_COMPLETE_PROFILE={**PROFILE,'profile':'marita-polled-byte-async-init-completion'}


def test_i2c_start_byte_nack_and_stop_without_slave_data(tmp_path,monkeypatch):
    ops=init()+REINITIALIZE+[('write',2,0xA4),('read',6,0),
        ('write',0,1),('write',2,0x80),('read',6,0),
        ('write',2,0x90),('read',2,0),('read',6,0)]
    result,failure=execute(tmp_path,monkeypatch,ops,ASYNC_COMPLETE_PROFILE)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    state=result['snapshot']['i2c']
    assert state['async_phase']==6 and state['async_completions']==3
    assert state['transactions']==1 and not state['irq_pending'] and state['control']==0x80
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert [e['value'] for e in events if not e['write'] and e['address']==0xf9040006]==[8,0x48,0xf8]


@pytest.mark.parametrize('tail',[
    [('write',0,0x95)], # Only reserved START byte01 is supplied by this contract.
    [('write',2,0x90)], # Cannot stop before the byte-completion phase.
    [('write',0,1),('write',2,0x84)], # Expected SI-clear command is80.
])
def test_i2c_completion_keeps_unmodeled_sequence_guard(tmp_path,monkeypatch,tail):
    result,failure=execute(tmp_path,monkeypatch,init()+REINITIALIZE+
                           [('write',2,0xA4)]+tail,ASYNC_COMPLETE_PROFILE)
    assert failure is None
    state=result['snapshot']['i2c']
    assert result['snapshot']['stop']['reason']=='i2c_async_init_sequence_unmodeled'
    assert state['bus_state']==8 and state['irq_pending'] and state['async_phase']==2


def test_native_i2c_irq_cascade_consumes_edge_but_keeps_si(tmp_path,monkeypatch):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        code=[]
        for _,offset,value in init()+REINITIALIZE+[('write',2,0xA4)]:
            code += [0xE3A00000|value,0xE1C100B0|offset,0xE1A00000,0xE1A00000]
        code += [0xE121F00C,0xEAFFFFFE]
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<'+str(len(code))+'I',*code))
        state=json.loads(path.read_text());state['instruction_limit']=100
        state['executable_ranges'][0]['end']=0x1000+4*len(code)
        state['i2c_controller']=ASYNC_COMPLETE_PROFILE
        state['timer_irq']={'profile':'tick1-upcounter-irq-assumption',
            'frequency_hz':131072,'max_irqs':8}
        state['registers'][1]=0xf9040000;state['registers'][2]=0xfb020318
        state['registers'][6]=0xfb020110;state['registers'][7]=0xfb020310;state['registers'][12]=0x53
        state['device_registers']=[{'address':a,'width':4,'initial_value':0 if a in (0xfb020104,0xfb020204,0xfb020304) else None,
            'read_or_mask':0,'name':'fixture.irq-latch'} for a in (0xfb020104,0xfb020204,0xfb020304,0xfb020118,0xfb020218,0xfb020318)]
        handler=[0xE5964000,0xE5975000,0xE3A08001,0xE5828000,
                 0xE3A08001,0xE5868008,0xE25EF004]
        vector=artifact_path(output, 'irq.bin', create=True);vector.write_bytes(struct.pack('<7I',*handler))
        state['regions'].append({'name':'fixture.irq','address':0,'size':4096,'readonly':True,
            'seeds':[{'address':0x18,'file':str(vector)}]})
        state['executable_ranges'].append({'start':0x18,'end':0x34})
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    snap=result['snapshot'];assert snap['registers'][4:6]==[30,10]
    assert snap['timer_irq']['i2c_irqs']==snap['timer_irq']['irqs']==1
    assert not snap['i2c']['irq_pending'] and snap['i2c']['control']==0x8c
    assert not snap['timer_irq']['selected'] and snap['i2c']['bus_state']==8


ASYNC_POWER_PROFILE={**PROFILE,'profile':'marita-polled-byte-async-power-startup'}
START_BYTE=init()+REINITIALIZE+[('write',2,0xA4),('write',0,1),
    ('write',2,0x80),('write',2,0x90)]


def power_write(register,value):
    return [('write',2,0x84),('write',4,0x60),('write',2,0xA4),('read',6,0),
            ('write',0,0x94),('write',2,0x80),('read',6,0),
            ('write',0,register),('write',2,0x80),('read',6,0),
            ('write',0,value),('write',2,0x80),('read',6,0),
            ('write',2,0x90),('read',6,0)]


def test_native_power_startup_writes_capture_and_completion(tmp_path,monkeypatch):
    result,failure=execute(tmp_path,monkeypatch,START_BYTE+
        power_write(0x60,0x88)+power_write(0x61,0x13),ASYNC_POWER_PROFILE)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    state=result['snapshot']['i2c']
    assert state['captured_writes']==[
        {'slave':0x4a,'register':0x60,'value':0x88},
        {'slave':0x4a,'register':0x61,'value':0x13}]
    assert (state['power_writes'],state['power_step'],state['transactions'])==(2,0,3)
    assert state['async_completions']==13 and not state['pending_operation']
    assert state['bus_state']==0xf8 and not state['irq_pending']
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert [e['value'] for e in events if not e['write'] and e['address']==0xf9040006]==[8,0x18,0x28,0x28,0xf8]*2


@pytest.mark.parametrize('prefix,tail,reason',[
    ([],[('write',2,0xA4)],'i2c_async_power_sequence_unmodeled'),
    (power_write(0x60,0x88)[:4],[('write',0,0x95)],'i2c_async_power_byte_unmodeled'),
    (power_write(0x60,0x88)[:7],[('write',0,0x61)],'i2c_async_power_byte_unmodeled'),
    (power_write(0x60,0x88)[:10],[('write',0,0x89)],'i2c_async_power_byte_unmodeled'),
    (power_write(0x60,0x88)[:10],[('write',2,0x90)],'i2c_async_power_sequence_unmodeled'),
    (power_write(0x60,0x88)+power_write(0x61,0x13),[('write',2,0x84)],'i2c_async_power_next_transaction_unmodeled'),
])
def test_native_power_startup_keeps_strict_guards(tmp_path,monkeypatch,prefix,tail,reason):
    result,failure=execute(tmp_path,monkeypatch,START_BYTE+prefix+tail,ASYNC_POWER_PROFILE)
    assert failure is None and result['snapshot']['stop']['reason']==reason
    if len(prefix)<11:assert result['snapshot']['i2c']['captured_writes']==[]
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    last=tail[-1]
    assert not (events[-1]['write'] and events[-1]['address']==0xf9040000+last[1] and events[-1]['value']==last[2])


FULL_POWER_PROFILE={**PROFILE,'profile':'marita-polled-byte-power-init-sequence'}
FULL_POWER_WRITES=json.loads((RESOURCES/'power-init-writes.json').read_text())['writes']
FULL_POWER_OPERATIONS=START_BYTE+[operation for row in FULL_POWER_WRITES
    for operation in power_write(row['register'],row['value'])]
STATUS_READ_PREFIX=[('write',2,0x84),('write',4,0x60),('write',2,0xa4),('read',6,0),
    ('write',0,0x94),('write',2,0x80),('read',6,0),('write',0,0x21),
    ('write',2,0x80),('read',6,0),('write',2,0xa4),('read',6,0),
    ('write',0,0x95),('write',2,0x80),('read',6,0)]


def test_full_power_initializer_captures_all_ordered_writes(tmp_path,monkeypatch):
    result,failure=execute(tmp_path,monkeypatch,FULL_POWER_OPERATIONS,FULL_POWER_PROFILE)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    state=result['snapshot']['i2c']
    assert len(state['captured_writes'])==27
    assert [(x['register'],x['value']) for x in state['captured_writes']]==[(x['register'],x['value']) for x in FULL_POWER_WRITES]
    assert (state['power_writes'],state['power_step'],state['transactions'],state['async_completions'])==(27,0,28,138)
    assert state['bus_state']==0xf8 and not state['pending_operation'] and not state['irq_pending']


def test_full_power_initializer_rejects_receive_before_unknown_byte(tmp_path,monkeypatch):
    result,failure=execute(tmp_path,monkeypatch,FULL_POWER_OPERATIONS+
        STATUS_READ_PREFIX+[('write',2,0x80)],FULL_POWER_PROFILE)
    assert failure is None and result['snapshot']['stop']['reason']=='i2c_async_power_input_unknown'
    state=result['snapshot']['i2c']
    assert (state['power_writes'],state['power_step'],state['bus_state'],state['received'])==(27,15,0x40,-1)
    assert not state['pending_operation'] and state['control']==0x88
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert [x['value'] for x in events if not x['write'] and x['address']==0xf9040006][-5:]==[8,0x18,0x28,0x10,0x40]
    assert events[-1]['write'] is False # Rejected receive trigger has no successful bus effect.


@pytest.mark.parametrize('prefix,tail',[
    (START_BYTE+power_write(0x60,0x88)+power_write(0x61,0x13)+power_write(0x40,0x2a)[:7],[('write',0,0x41)]),
    (FULL_POWER_OPERATIONS+STATUS_READ_PREFIX[:7],[('write',0,0x22)]),
    (FULL_POWER_OPERATIONS+STATUS_READ_PREFIX[:12],[('write',0,0x97)]),
])
def test_full_power_initializer_keeps_byte_and_read_address_guards(tmp_path,monkeypatch,prefix,tail):
    result,failure=execute(tmp_path,monkeypatch,prefix+tail,FULL_POWER_PROFILE)
    assert failure is None and result['snapshot']['stop']['reason'] in ('i2c_async_power_byte_unmodeled','i2c_async_power_sequence_unmodeled')
    assert not result['snapshot']['i2c']['pending_operation']


STATUS_PROFILE={**PROFILE,'profile':'marita-polled-byte-power-status-inputs',
    'status_inputs':{'register_21':0,'register_22':0}}


def status_read(register):
    prefix=list(STATUS_READ_PREFIX);prefix[7]=('write',0,register)
    return prefix+[('write',2,0x80),('read',6,0),('read',0,0),('write',2,0x90),('read',6,0)]


@pytest.mark.parametrize('byte21,byte22',[(0,0),(8,0),(0,32),(8,32)])
def test_native_power_status_inputs_complete_once_with_final_nack(tmp_path,monkeypatch,byte21,byte22):
    profile={**STATUS_PROFILE,'status_inputs':{'register_21':byte21,'register_22':byte22}}
    result,failure=execute(tmp_path,monkeypatch,FULL_POWER_OPERATIONS+
        status_read(0x21)+status_read(0x22)+status_read(0x23),profile)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    state=result['snapshot']['i2c'];assert state['status_reads']==3 and state['transactions']==31
    assert state['power_writes']==27 and not state['pending_operation'] and not state['irq_pending']
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert [x['value'] for x in events if not x['write'] and x['address']==0xf9040000]==[byte21,byte22,0x80]
    assert [x['value'] for x in events if not x['write'] and x['address']==0xf9040006][-21:]==[8,0x18,0x28,0x10,0x40,0x58,0xf8]*3


@pytest.mark.parametrize('byte21,byte22,completed',[(None,0,0),(0,None,1)])
def test_native_power_status_unknown_input_keeps_guard(tmp_path,monkeypatch,byte21,byte22,completed):
    profile={**STATUS_PROFILE,'status_inputs':{'register_21':byte21,'register_22':byte22}}
    ops=list(FULL_POWER_OPERATIONS)
    if completed:ops+=status_read(0x21)
    prefix=list(STATUS_READ_PREFIX);prefix[7]=('write',0,0x21+completed)
    result,failure=execute(tmp_path,monkeypatch,ops+prefix+[('write',2,0x80)],profile)
    assert failure is None and result['snapshot']['stop']['reason']=='i2c_async_power_input_unknown'
    state=result['snapshot']['i2c'];assert state['status_reads']==completed and state['power_step']==15 and not state['pending_operation']


def test_native_power_status_extra_transaction_is_guarded(tmp_path,monkeypatch):
    result,failure=execute(tmp_path,monkeypatch,FULL_POWER_OPERATIONS+
        status_read(0x21)+status_read(0x22)+status_read(0x23)+[('write',2,0x84)],STATUS_PROFILE)
    assert failure is None and result['snapshot']['stop']['reason']=='i2c_async_power_next_dependency_unmodeled'
    assert result['snapshot']['i2c']['status_reads']==3
