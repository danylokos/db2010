"""Bounded native Vincenne2 ID and explicitly declared register-read hypotheses."""
from db2010.diagnostics.artifacts import artifact as artifact_path
import pyarrow.parquet as pq
import pytest
from test_i2c import execute, STATUS_PROFILE, FULL_POWER_OPERATIONS, status_read, STATUS_READ_PREFIX


def profile(reads):
    return {**STATUS_PROFILE, 'profile':'vincenne2-startup-register-reads', 'register_reads':reads}


STATUS_OPERATIONS = FULL_POWER_OPERATIONS+status_read(0x21)+status_read(0x22)+status_read(0x23)


@pytest.mark.parametrize('value',[0xe3,0xd3,0xc7,0,None])
def test_native_pmic_id_and_unknown_byte(tmp_path,monkeypatch,value):
    result,failure=execute(tmp_path,monkeypatch,STATUS_OPERATIONS+status_read(0x20),
                           profile([{'register':0x20,'value':value}]))
    assert failure is None
    snap=result['snapshot'];state=snap['i2c']
    assert snap['stop']['reason']==('i2c_async_power_input_unknown' if value is None else 'instruction_limit')
    assert state['status_reads']==3 and state['register_reads']==(0 if value is None else 1)
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    received=[e['value'] for e in events if not e['write'] and e['address']==0xf9040000]
    assert received==[0,0,0x80]+([] if value is None else [value])
    assert not state['pending_operation']
    if value is not None:assert state['bus_state']==0xf8 and state['transactions']==32


def test_native_pmic_last_write_returns_final_value_and_bounds_next_operation(tmp_path,monkeypatch):
    result,failure=execute(tmp_path,monkeypatch,STATUS_OPERATIONS+status_read(0xb0)+[('write',2,0x84)],
                           profile([{'register':0xb0,'value':'last-write'}]))
    assert failure is None and result['snapshot']['stop']['reason']=='i2c_async_power_next_dependency_unmodeled'
    state=result['snapshot']['i2c'];assert state['register_reads']==1 and state['received']==0xf4
    assert state['power_writes']==27 and len(state['captured_writes'])==27


def test_native_pmic_unwritten_last_write_stays_unknown(tmp_path,monkeypatch):
    result,failure=execute(tmp_path,monkeypatch,STATUS_OPERATIONS+status_read(0x24),
                           profile([{'register':0x24,'value':'last-write'}]))
    assert failure is None and result['snapshot']['stop']['reason']=='i2c_async_power_input_unknown'
    # Rejected receive preserves the previous successful USB/status byte.
    assert result['snapshot']['i2c']['register_reads']==0 and result['snapshot']['i2c']['received']==0x80


def test_native_pmic_wrong_register_keeps_order_guard(tmp_path,monkeypatch):
    result,failure=execute(tmp_path,monkeypatch,STATUS_OPERATIONS+status_read(0x24),
                           profile([{'register':0x20,'value':0xe3}]))
    assert failure is None and result['snapshot']['stop']['reason']=='i2c_async_power_byte_unmodeled'
    assert result['snapshot']['i2c']['register_reads']==0


@pytest.mark.parametrize('tail',[
    [('write',2,0x90)],
    [('read',0,0)],
])
def test_native_pmic_receive_and_stop_order_stays_guarded(tmp_path,monkeypatch,tail):
    prefix=list(STATUS_READ_PREFIX);prefix[7]=('write',0,0x20)
    result,failure=execute(tmp_path,monkeypatch,STATUS_OPERATIONS+prefix+tail,
                           profile([{'register':0x20,'value':0xe3}]))
    assert failure is None and result['snapshot']['stop']['reason'] in ('i2c_async_read_unmodeled','i2c_async_power_sequence_unmodeled')
    assert result['snapshot']['i2c']['register_reads']==0
