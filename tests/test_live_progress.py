"""Live QMP framing and CLI stream separation, including STOP races."""
from concurrent.futures import ThreadPoolExecutor
import base64
import json
import socket
import time

import pytest

from db2010 import cli
from db2010.execution.qmp import QMP


@pytest.mark.parametrize('bad', [None, 'offset', 'total', 'empty', 'oversize', 'base64'])
def test_snapshot_chunk_integrity_and_utf8_reassembly(bad):
    payload=json.dumps({'text':'a'*300000+'🙂'},ensure_ascii=False).encode()
    # Deliberately split the UTF-8 sequence as well as spanning multiple pages.
    cuts=[0,256*1024,len(payload)-4,len(payload)]
    chunks=[{'offset':start,'total':len(payload),
             'data_base64':base64.b64encode(payload[start:end]).decode()}
            for start,end in zip(cuts,cuts[1:])]
    if bad=='offset': chunks[1]['offset']+=1
    if bad=='total': chunks[1]['total']+=1
    if bad=='empty': chunks[1]['data_base64']=''
    if bad=='oversize': chunks[1]['data_base64']=base64.b64encode(b'x'*(256*1024+1)).decode()
    if bad=='base64': chunks[1]['data_base64']='!invalid!'
    client=QMP.__new__(QMP)
    requests=[]
    def execute(name,deadline,arguments):
        requests.append((name,arguments))
        return {} if name=='qom-set' else json.dumps(chunks.pop(0))
    client.execute=execute
    if bad:
        with pytest.raises(ValueError): client.snapshot(time.monotonic()+5)
    else:
        assert client.snapshot(time.monotonic()+5)==json.loads(payload)
        assert len(requests)==4
    assert requests[0]==('qom-set',{'path':'/machine','property':'snapshot-chunk','value':'start'})


@pytest.mark.parametrize('reject', [False, True])
def test_progress_poll_preserves_fragmented_stop_event_and_errors(tmp_path, reject):
    path = tmp_path / 'qmp'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    requests = []

    def server():
        connection, _ = listener.accept()
        with connection, connection.makefile('rb') as stream:
            connection.sendall(b'{"QMP":{}}\n')
            cap = json.loads(stream.readline())
            connection.sendall(json.dumps({'id': cap['id'], 'return': {}}).encode() + b'\n')
            for index in range(2):
                request = json.loads(stream.readline())
                requests.append(request)
                assert request['execute'] == 'qom-get'
                assert request['arguments'] == {'path': '/machine', 'property': 'progress'}
                if reject:
                    connection.sendall(json.dumps({'id': request['id'], 'error': {'class': 'CommandNotFound'}}).encode() + b'\n')
                    break
                if index:
                    connection.sendall(b'"STOP"}\n')
                sample = json.dumps({'observations': index+1, 'pc': 0x1000})
                connection.sendall(json.dumps({'id': request['id'], 'return': sample}).encode() + b'\n')
                if not index:
                    connection.sendall(b'{"event":')
                    time.sleep(0.04)  # Partial event outlives the refresh deadline.

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(server)
            client = QMP(path, time.monotonic()+5, lambda: None)
            samples = []
            try:
                if reject:
                    with pytest.raises(RuntimeError, match='QMP error'):
                        client.wait_stop(time.monotonic()+5, samples.append, interval=0.01)
                else:
                    client.wait_stop(time.monotonic()+5, samples.append, interval=0.01)
                    assert [s['observations'] for s in samples] == [1, 2]
                    assert client.events == [{'event': 'STOP'}]
            finally:
                client.close()
            future.result(timeout=5)
    finally:
        listener.close()


@pytest.mark.parametrize('summary', [False, True])
def test_cli_streams_before_return_without_json_or_duplicate_uart(tmp_path, monkeypatch, capsys, summary):
    streamed = []

    def run(root, experiment, output, **options):
        options['console'].uart(0, b'early\npartial')
        streamed.append(capsys.readouterr())
        options['console'].uart(0, b' tail\n')
        return {'success': True, 'snapshot': {'cpu_hz':104000000, 'stop': {'reason': 'instruction_limit', 'pc': 0x1000, 'observations': 12}},
                'phases': {'total_seconds': 1}, 'uart': {}}, None

    monkeypatch.setattr(cli, 'run', run)
    args = ['db2010', '--research-guards', '--uart', '--diagnostics', '--artifacts-dir', str(tmp_path), '--label', 'run']
    if summary:
        args.append('--json-summary')
    monkeypatch.setattr('sys.argv', args)
    cli.main()
    captured = capsys.readouterr()
    assert streamed[0].out == 'early\npartial'
    assert captured.out == ' tail\n'
    if summary:
        assert json.loads(captured.err)['stop']['reason'] == 'instruction_limit'
        assert 'cpu_hz' not in json.loads(captured.err)
    else:
        assert 'Stopped: instruction_limit' in captured.err and '"success"' not in captured.err
        assert 'CPU' not in captured.err


@pytest.mark.parametrize('uart', [False, True])
@pytest.mark.parametrize('no_progress', [False, True])
@pytest.mark.parametrize('output', [False, True])
@pytest.mark.parametrize('debug', [False, True])
def test_cli_independent_console_controls(tmp_path, monkeypatch, uart, no_progress, output, debug):
    from io import StringIO
    from test_console import Terminal
    monkeypatch.setenv('TERM', 'xterm')
    stdout, stderr = StringIO(), Terminal()
    monkeypatch.setattr('sys.stdout', stdout)
    monkeypatch.setattr('sys.stderr', stderr)
    arguments = ['db2010', '--research-guards']
    if uart:
        arguments.append('--uart')
    if no_progress:
        arguments.append('--no-progress')
    if output:
        arguments += ['--diagnostics', '--artifacts-dir', str(tmp_path), '--label', 'capture']
    if debug:
        arguments += ['--gdb-command', 'info registers']
    monkeypatch.setattr('sys.argv', arguments)
    def run(root, experiment, destination, **options):
        console = options['console']
        assert console.uart_enabled == uart
        assert console.show_status == (not (no_progress or debug))
        assert options['diagnostics'] == output
        assert options['trace'] is False
        console.phase('run')
        console.uart(0, b'early\n')
        return {'success': True, 'phases': {}, 'uart': {}}, None
    monkeypatch.setattr(cli, 'run', run)
    cli.main()
    assert stdout.getvalue() == ('early\n' if uart else '')
    assert 'Stopped: complete' in stderr.getvalue()


def test_display_read_preserves_stop_between_progress_and_display(tmp_path):
    path = tmp_path/'qmp'
    listener = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    listener.bind(str(path));listener.listen(1)
    requests=[]
    def server():
        connection,_=listener.accept()
        with connection,connection.makefile('rb') as stream:
            connection.sendall(b'{"QMP":{}}\n')
            capability=json.loads(stream.readline())
            connection.sendall(json.dumps({'id':capability['id'],'return':{}}).encode()+b'\n')
            for property in ('progress','display'):
                request=json.loads(stream.readline());requests.append(request)
                assert request['arguments']['property'] == property
                if property == 'display':
                    connection.sendall(b'{"event":')
                    time.sleep(.03)
                    connection.sendall(b'"STOP"}\n')
                response=json.dumps({'id':request['id'],'return':json.dumps({'property':property})}).encode()+b'\n'
                for offset in range(0,len(response),7):
                    connection.sendall(response[offset:offset+7])
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(server)
        client=QMP(path,time.monotonic()+5,lambda:None)
        progress,display=[],[]
        try:
            client.wait_stop(time.monotonic()+5,progress.append,display=display.append)
            assert progress == [{'property':'progress'}] and display == [{'property':'display'}]
            assert client.events == [{'event':'STOP'}]
        finally:
            client.close();listener.close()
        future.result(5)
