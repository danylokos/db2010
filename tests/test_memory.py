from support.guarded import run as guarded_run
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import struct
import threading

import pyarrow.parquet as pq
import pytest

from db2010.execution import runner
from db2010.paths import ROOT
from db2010.diagnostics.memory import HEADER, SCHEMA, MemoryCollector
from test_runner import synthetic_preparation


@pytest.mark.parametrize('tail', [b'partial', struct.pack('<5Q3I', 4, 1, 0x2000, 0x2000, 1, 0x1000, 4, 1)])
def test_memory_bad_tail_preserves_valid_events(tmp_path, tail):
    path = tmp_path/'socket'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path));listener.listen(1);listener.settimeout(2)
    collector = MemoryCollector(tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(collector.collect, listener, [], threading.Lock(), 2)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(path))
                frame = struct.pack('<5Q3I', 1, 1, 0x2000, 0x2000, 255, 0x1000, 1, 1)
                for chunk in (HEADER[:3], HEADER[3:]+frame+tail):
                    client.sendall(chunk)
            with pytest.raises(ValueError, match='memory record'):
                future.result(timeout=3)
        table = pq.read_table(artifact_path(tmp_path, 'memory.parquet'))
        assert table.schema.equals(SCHEMA, check_metadata=True)
        assert table.to_pylist()[0]['value'] == 255
        assert collector.rows == 1 and not collector.metrics['complete']
        assert (artifact_path(tmp_path, 'memory-error-tail.bin')).read_bytes() == tail
    finally:
        listener.close()


def test_native_memory_values_and_failed_read(tmp_path, monkeypatch):
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        # mov r0,#0x2000; mov r1,#0x7f; strb r1,[r0]; ldrb r2,[r0];
        # ldr r3,[r0,#4] (unknown, rejected before successful callback)
        code = struct.pack('<5I', 0xE3A00A02, 0xE3A0107F, 0xE5C01000, 0xE5D02000, 0xE5903004)
        artifact_path(output, 'code.bin', create=True).write_bytes(code)
        state = json.loads(path.read_text())
        state['executable_ranges'][0]['end'] = 0x1000+len(code)
        state['regions'].append({'name':'fixture.ram', 'address':0x2000, 'size':4096, 'readonly':False, 'seeds':[]})
        path.write_text(json.dumps(state)+'\n')
        return path, {'seconds': 5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['success']
    rows = pq.read_table(artifact_path(tmp_path/'run', 'memory.parquet')).to_pylist()
    assert [(r['index'], r['address'], r['size'], r['value'], r['write']) for r in rows] == [
        (3, 0x2000, 1, 0x7F, True), (4, 0x2000, 1, 0x7F, False)]
    assert result['snapshot']['stop']['address'] == 0x2004
    assert result['memory_producer']['events'] == 2
    assert result['display_audit']['writes'] == 1
