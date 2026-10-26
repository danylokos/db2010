"""Protocol failure and process ownership checks for the production host."""
from support.guarded import run as guarded_run
from db2010.diagnostics.artifacts import artifact as artifact_path
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import struct
import signal
import time
import threading

import pyarrow.parquet as pq
import pytest

from db2010.execution import runner
from db2010.paths import ROOT
from db2010.preparation.inputs import load_json
from db2010.execution.qmp import QMP
from db2010.execution import qmp as qmp_module
from db2010.diagnostics.trace import BATCH, HEADER, SCHEMA, TraceCollector


def frame(index, *, pc=0x1000):
    regs = [0]*16+[0xD3]
    regs[15] = pc
    return struct.pack('<Q21I', index, pc, 4, 0, 0xEAFFFFFE, *regs)


@pytest.mark.parametrize('suffix,reason', [(b'bad', 'truncated'), (frame(4), 'unordered')])
def test_partial_trace_preserves_valid_prefix(tmp_path, suffix, reason):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = tmp_path/'socket'
    listener.bind(str(path));listener.listen(1);listener.settimeout(2)
    collector = TraceCollector(tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(collector.collect, listener, [], threading.Lock(), 2)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(path))
                client.sendall(HEADER+frame(1)+frame(2)+suffix)
            with pytest.raises(ValueError, match=reason):
                future.result(timeout=3)
        table = pq.read_table(artifact_path(tmp_path, 'trace.parquet'))
        assert table.schema.equals(SCHEMA, check_metadata=True)
        assert table.column('index').to_pylist() == [1, 2]
        assert collector.metrics['complete'] is False and collector.metrics['parquet_finalized']
        assert (artifact_path(tmp_path, 'stream-error-tail.bin')).read_bytes() == suffix
    finally:
        listener.close()


def test_fragmented_stream_and_batch_boundary(tmp_path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = tmp_path/'socket'
    listener.bind(str(path));listener.listen(1);listener.settimeout(2)
    collector = TraceCollector(tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(collector.collect, listener, [], threading.Lock(), 2)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(path))
                data = HEADER+b''.join(frame(i) for i in range(1, BATCH+4))
                for i in range(0, len(data), 137):
                    client.sendall(data[i:i+137])
            metrics = future.result(timeout=5)
        parquet = pq.ParquetFile(artifact_path(tmp_path, 'trace.parquet'))
        assert metrics['observations'] == BATCH+3 and parquet.metadata.num_row_groups == 2
        assert pq.read_table(artifact_path(tmp_path, 'trace.parquet')).column('index').to_pylist() == list(range(1, BATCH+4))
    finally:
        listener.close()


def test_qmp_error_is_reported(tmp_path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = tmp_path/'qmp'
    listener.bind(str(path));listener.listen(1)
    def server():
        connection, _ = listener.accept()
        with connection:
            connection.sendall(b'{"QMP":{}}\n')
            request = json.loads(connection.makefile('rb').readline())
            connection.sendall(json.dumps({'id': request['id'], 'error': {'class': 'CommandNotFound'}}).encode()+b'\n')
    try:
        import time
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(server)
            with pytest.raises(RuntimeError, match='QMP error'):
                QMP(path, time.monotonic()+2, lambda: None)
            future.result(timeout=2)
    finally:
        listener.close()


@pytest.mark.parametrize('oversize',[False,True])
def test_qmp_large_snapshot_and_bounded_frame(tmp_path,monkeypatch,oversize):
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    path=tmp_path/'qmp-large';listener.bind(str(path));listener.listen(1)
    payload='x'*(1024*1024+4096)
    if oversize:
        monkeypatch.setattr(qmp_module,'MAX_FRAME_BYTES',1024)
        payload='x'*2048
    def server():
        connection,_=listener.accept()
        with connection:
            stream=connection.makefile('rb')
            connection.sendall(b'{"QMP":{}}\n')
            capabilities=json.loads(stream.readline())
            connection.sendall(json.dumps({'id':capabilities['id'],'return':{}}).encode()+b'\n')
            request=json.loads(stream.readline())
            response=json.dumps({'id':request['id'],'return':payload}).encode()+b'\n'
            try:
                for offset in range(0,len(response),8192):
                    connection.sendall(response[offset:offset+8192])
            except (BrokenPipeError,ConnectionResetError):
                assert oversize
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future=executor.submit(server)
            client=QMP(path,time.monotonic()+5,lambda:None)
            try:
                if oversize:
                    with pytest.raises(ValueError,match='QMP frame exceeds limit'):
                        client.execute('qom-get',time.monotonic()+5)
                else:
                    assert client.execute('qom-get',time.monotonic()+5)==payload
            finally:
                client.close()
            future.result(timeout=5)
    finally:
        listener.close()


def synthetic_preparation(root, spec, output):
    code = artifact_path(output, 'code.bin', create=True);code.write_bytes(struct.pack('<I', 0xEAFFFFFE))
    state = {'timebase': {'profile': 'qemu-virtual-v1', 'icount_shift': 6}, 'format_version':1,'entry':0x1000,'cpsr':0xD3,'sctlr':0x90078,'registers':[0]*16,
        'instruction_limit':1000000000,'executable_ranges':[{'start':0x1000,'end':0x1004}],
        'regions':[{'name':'fixture.code','address':0x1000,'size':4096,'readonly':True,
                    'seeds':[{'address':0x1000,'file':str(code)}]}]}
    path = artifact_path(output, 'state.json', create=True);path.write_text(json.dumps(state)+'\n')
    return path, {'seconds': 0.03}


def test_wall_timeout_preserves_stopped_state_and_reaps_process(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'prepare', synthetic_preparation)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, trace=False)
    assert isinstance(failure, TimeoutError) and result['success'] is False
    assert result['snapshot']['observations'] > 0 and result['qemu_returncode'] is not None
    assert (tmp_path/'run/artifacts.sha256.json').exists()


def test_snapshot_chunks_release_restart_reset_and_running_guard(tmp_path,monkeypatch):
    monkeypatch.setattr(runner,'prepare',
        lambda root,spec,output,**options:synthetic_preparation(root,spec,output))
    checked=[]
    class CheckedQMP(QMP):
        def snapshot(self,deadline):
            value=super().snapshot(deadline)
            arguments={'path':'/machine','property':'snapshot-chunk'}
            with pytest.raises(RuntimeError,match='requires start'):
                self.execute('qom-get',deadline,arguments)
            if not checked:
                for command in ('release','start','start','release','start'):
                    self.execute('qom-set',deadline,{**arguments,'value':command})
                self.execute('system_reset',deadline)
                with pytest.raises(RuntimeError,match='requires start'):
                    self.execute('qom-get',deadline,arguments)
                with pytest.raises(RuntimeError,match='expects start or release'):
                    self.execute('qom-set',deadline,{**arguments,'value':'invalid'})
                checked.append('stopped')
            return value
        def observe(self,deadline,progress=None,display=None):
            if 'running' not in checked:
                with pytest.raises(RuntimeError,match='requires a stopped VM'):
                    self.execute('qom-set',deadline,
                        {'path':'/machine','property':'snapshot-chunk','value':'start'})
                checked.append('running')
            return super().observe(deadline,progress,display)
    monkeypatch.setattr(runner,'QMP',CheckedQMP)
    # A running observation is needed: a headless run no longer schedules
    # empty observations solely to wake the supervisor.
    from types import SimpleNamespace
    observer = SimpleNamespace(show_status=True, show_display=False,
        phase=lambda name: None, uart=lambda port,data: None, progress=lambda sample: None)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'capture', diagnostics=True, console=observer)
    assert isinstance(failure,TimeoutError)
    assert checked==['stopped','running'] and result['snapshot']['observations']>0
    assert result['qemu_returncode']==0


def test_engine_startup_rejection_preserves_diagnostics_and_finalized_trace(tmp_path, monkeypatch):
    def rejected(root, spec, output):
        path, spec = synthetic_preparation(root, spec, output)
        state = json.loads(path.read_text())
        state['memory_controller'] = {'profile':'word-latches-fixed-bank','bank_word':0}
        path.write_text(json.dumps(state)+'\n')
        return path, spec
    monkeypatch.setattr(runner, 'prepare', rejected)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, trace=True)
    assert failure is not None and result['success'] is False
    assert result['qemu_returncode'] == 1
    assert 'unsupported initial bank' in (artifact_path((tmp_path) / 'run', 'qemu.log')).read_text()
    parquet = pq.ParquetFile(artifact_path((tmp_path) / 'run', 'trace.parquet'))
    assert parquet.metadata.num_rows == 0


def test_process_crash_leaves_valid_trace_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'prepare', synthetic_preparation)
    processes = []
    original_popen = runner.subprocess.Popen
    original_wait = QMP.wait_stop
    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        if kwargs.get('start_new_session'):
            processes.append(process)
        return process
    def killed(qmp, deadline, **options):
        time.sleep(0.04)
        os.killpg(processes[0].pid, signal.SIGKILL)
        return original_wait(qmp, deadline, **options)
    monkeypatch.setattr(runner.subprocess, 'Popen', popen)
    monkeypatch.setattr(QMP, 'wait_stop', killed)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, trace=True)
    assert failure is not None and result['success'] is False
    assert result['qemu_returncode'] == -signal.SIGKILL and processes[0].poll() is not None
    parquet = pq.ParquetFile(artifact_path((tmp_path) / 'run', 'trace.parquet'))
    assert parquet.metadata.num_rows > 0
    assert pq.read_table(artifact_path((tmp_path) / 'run', 'trace.parquet')).column('index').to_pylist() == list(range(1, parquet.metadata.num_rows+1))


def test_json_duplicate_keys_fail_before_preparation(tmp_path):
    path = tmp_path/'bad.json';path.write_text('{"limit":1,"limit":2}')
    with pytest.raises(ValueError, match='duplicate JSON'):
        load_json(path)


def test_slow_consumer_is_lossless(tmp_path, monkeypatch):
    def prepared(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        state = json.loads(path.read_text());state['instruction_limit'] = 100000
        path.write_text(json.dumps(state)+'\n')
        return path, {'seconds': 5}
    original_commit = TraceCollector.commit
    def slow(collector, writer, records):
        time.sleep(0.01)
        return original_commit(collector, writer, records)
    monkeypatch.setattr(runner, 'prepare', prepared)
    monkeypatch.setattr(TraceCollector, 'commit', slow)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['success']
    assert result['collector']['observations'] == result['producer']['observations'] == 100001
    parquet = pq.ParquetFile(artifact_path((tmp_path) / 'run', 'trace.parquet'))
    assert parquet.metadata.num_rows == 100001 and parquet.metadata.num_row_groups == 13
    assert pq.read_table(artifact_path((tmp_path) / 'run', 'trace.parquet')).column('index').to_pylist() == list(range(1, 100002))


def test_storage_failure_stops_and_reaps_owned_engine(tmp_path, monkeypatch):
    if not Path('/dev/full').exists():
        pytest.skip('requires Linux ENOSPC device')
    def prepared(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        (artifact_path(output, 'trace.parquet', create=True)).symlink_to('/dev/full')
        return path, {'seconds': 1}
    monkeypatch.setattr(runner, 'prepare', prepared)
    # A deliberately failed destination is not a regular run artifact. Exclude
    # its device symlink from hashing while checking the actual capture failure.
    original_identity = runner.file_identity
    def identity(path):
        if path.is_symlink():
            return {'kind':'ENOSPC test device','target':str(path.resolve())}
        return original_identity(path)
    monkeypatch.setattr(runner, 'file_identity', identity)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, trace=True)
    assert failure is not None and result['success'] is False and result['qemu_returncode'] is not None
    assert 'capture' in result['error']['message'] or result.get('capture_errors')


def test_consumer_disconnect_finalizes_only_committed_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'prepare', synthetic_preparation)
    original_commit = TraceCollector.commit
    def disconnected(collector, writer, records):
        original_commit(collector, writer, records)
        raise ConnectionAbortedError('controlled trace consumer disconnect')
    monkeypatch.setattr(TraceCollector, 'commit', disconnected)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, trace=True)
    assert failure is not None and result['success'] is False and result['qemu_returncode'] is not None
    assert result['collector']['complete'] is False and result['collector']['parquet_finalized']
    assert pq.ParquetFile(artifact_path((tmp_path) / 'run', 'trace.parquet')).metadata.num_rows == BATCH
