"""Short native-QEMU guard/CPU fixtures; disposable supervisor, not the host runner."""
from db2010.diagnostics.artifacts import artifact as artifact_path
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import socket
import shutil
import struct
import subprocess
import tempfile
import time
import zipfile

from support.paths import ROOT
REGISTERS = [*(f'r{i}' for i in range(13)), 'sp', 'lr', 'pc', 'cpsr']


def words(*values):
    return struct.pack('<' + 'I' * len(values), *values)


def base_state(code, *, limit=8, entry=0x1000, cpsr=0xD3, regs=None):
    return {'timebase': {'profile': 'qemu-virtual-v1', 'icount_shift': 6}, 'format_version': 1, 'entry': entry, 'cpsr': cpsr, 'sctlr': 0x90078,
            'instruction_limit': limit, 'registers': regs or [0] * 16,
            'executable_ranges': [{'start': 0x1000, 'end': 0x2000}],
            'regions': [{'name': 'fixture.code', 'address': 0x1000, 'size': 0x1000,
                         'readonly': True, 'seeds': [{'address': 0x1000, 'data': code}]}]}


def freeze_state(state, output):
    import copy
    frozen = copy.deepcopy(state)
    for i, region in enumerate(frozen['regions']):
        for j, seed in enumerate(region['seeds']):
            path = artifact_path(output, f'region-{i}-seed-{j}.bin', create=True)
            path.write_bytes(seed.pop('data'))
            seed['file'] = str(path)
    path = artifact_path(output, 'state.json', create=True)
    path.write_text(json.dumps(frozen, indent=2) + '\n')
    return path


def qemu_run(state, output, *, trace=True, inspect=None, timeout=15, binary=None, bus=False,
             reset=False, qom_inspect=None, qom_set=None, research_guards=True,
             icount='shift=6,sleep=off,align=off', after_cont=None):
    output.mkdir(parents=True, exist_ok=False)
    prepared = freeze_state(state, output)
    binary = binary or ROOT / 'qemu/build/qemu-system-arm'
    plugin = ROOT / 'build/plugins/trace.so'
    events = []
    with tempfile.TemporaryDirectory(prefix='db2010-') as temporary, ThreadPoolExecutor(max_workers=2) as executor:
        qmp_path = str(Path(temporary) / 'qmp')
        trace_path = str(Path(temporary) / 'trace')
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.settimeout(timeout)
        listener.bind(trace_path)
        listener.listen(1)
        memory_listener = None
        if bus:
            if not trace:
                raise ValueError('bus capture requires instruction trace')
            import threading
            from db2010.diagnostics.memory import MemoryCollector
            memory_path = str(Path(temporary) / 'memory')
            memory_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            memory_listener.settimeout(timeout)
            memory_listener.bind(memory_path)
            memory_listener.listen(1)
            memory_collector = MemoryCollector(output)
        command = [str(binary), '-machine', f'db2010,research-guards={"on" if research_guards else "off"},state={prepared}', '-accel', 'tcg,thread=single',
                   '-icount', icount,
                   '-nodefaults', '-display', 'none', '-monitor', 'none', '-serial', 'none',
                   '-S', '-qmp', f'unix:{qmp_path},server=on,wait=off']
        if 'uart_transmit' in state:
            for port in range(4):
                uart = artifact_path(output, f'uart{port}.bin', create=True)
                command.extend(['-chardev', f'file,id=uart{port},path={uart}'])
        if trace:
            synthetic = ',synthetic-kgen='+state['synthetic_kgen']['profile'] if 'synthetic_kgen' in state else ''
            if 'synthetic_sha' in state:
                synthetic += ',synthetic-sha='+state['synthetic_sha']['profile']
            if 'synthetic_etx' in state:
                synthetic += ',synthetic-etx='+state['synthetic_etx']['profile']
            memory_options = f',memsocket={memory_path},memmetrics={artifact_path(output, "memory-producer.json", create=True)}' if bus else ''
            command.extend(['-plugin', f'file={plugin},socket={trace_path},metrics={artifact_path(output, "producer.json", create=True)}'+synthetic+memory_options])
        (artifact_path(output, 'command.json', create=True)).write_text(json.dumps(command, indent=2) + '\n')

        def collect():
            connection, _ = listener.accept()
            connection.settimeout(timeout)
            with connection, (artifact_path(output, 'trace.bin', create=True)).open('wb') as file:
                while chunk := connection.recv(65536):
                    file.write(chunk)

        started = time.perf_counter()
        with (artifact_path(output, 'qemu.log', create=True)).open('w') as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            future = executor.submit(collect) if trace else None
            memory_future = executor.submit(memory_collector.collect, memory_listener, [], threading.Lock(), timeout) if bus else None
            qmp = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            qmp.settimeout(timeout)
            try:
                deadline = time.monotonic() + timeout
                while not Path(qmp_path).exists():
                    if process.poll() is not None:
                        raise RuntimeError(f'QEMU exited before QMP: {output}/qemu.log')
                    if time.monotonic() >= deadline:
                        raise TimeoutError('QMP startup')
                    time.sleep(0.01)
                # bind() publishes the path before QEMU calls listen(). Under
                # parallel fixture load that interval is observable.
                while True:
                    try:
                        qmp.connect(qmp_path)
                        break
                    except (FileNotFoundError, ConnectionRefusedError):
                        if process.poll() is not None:
                            raise RuntimeError(f'QEMU exited before QMP connect: {output}/qemu.log')
                        if time.monotonic() >= deadline:
                            raise TimeoutError('QMP connect deadline')
                        time.sleep(0.005)
                stream = qmp.makefile('rwb', buffering=65536)
                greeting = json.loads(stream.readline())
                assert 'QMP' in greeting, greeting

                def execute(name, arguments=None):
                    request = {'execute': name}
                    if arguments is not None:
                        request['arguments'] = arguments
                    stream.write(json.dumps(request).encode() + b'\n')
                    stream.flush()
                    while True:
                        response = json.loads(stream.readline())
                        if 'event' in response:
                            events.append(response)
                            continue
                        if 'error' in response:
                            raise RuntimeError(response)
                        return response.get('return')

                execute('qmp_capabilities')
                initial = json.loads(execute('qom-get', {'path': '/machine', 'property': 'snapshot'}))
                assert initial['research_guards'] is research_guards
                for path, properties in (qom_set or {}).items():
                    for prop, value in properties.items():
                        execute('qom-set', {'path': path, 'property': prop, 'value': value})
                execute('cont')
                if after_cont:
                    after_cont(process)
                while True:
                    event = json.loads(stream.readline())
                    events.append(event)
                    if event.get('event') == 'STOP':
                        break
                result = json.loads(execute('qom-get', {'path': '/machine', 'property': 'snapshot'}))
                status = execute('query-status')
                def inspect_properties():
                    return {path: {prop: execute('qom-get', {'path': path, 'property': prop})
                                   for prop in properties}
                            for path, properties in qom_inspect.items()}
                if qom_inspect:
                    result['qom_inspection'] = inspect_properties()
                if inspect:
                    result['memory_inspection'] = execute('human-monitor-command', {'command-line': inspect})
                if reset:
                    execute('system_reset')
                    while not any(event.get('event') == 'RESET' for event in events):
                        events.append(json.loads(stream.readline()))
                    result['after_reset'] = json.loads(execute('qom-get', {'path': '/machine', 'property': 'snapshot'}))
                    if qom_inspect:
                        result['qom_after_reset'] = inspect_properties()
                execute('quit')
                process.wait(timeout=timeout)
                if process.returncode:
                    raise RuntimeError(f'QEMU exit {process.returncode}')
                if future:
                    future.result(timeout=timeout)
                if memory_future:
                    memory_future.result(timeout=timeout)
            except BaseException as error:
                failure = {'error_type': type(error).__name__, 'error': str(error),
                           'qemu_returncode': process.poll(), 'qmp_events': events}
                (artifact_path(output, 'failure.json', create=True)).write_text(json.dumps(failure, indent=2) + '\n')
                raise
            finally:
                qmp.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
                listener.close()
                if memory_listener:
                    memory_listener.close()
        result.update(initial=initial, qmp_status=status, qmp_events=events,
                      elapsed_seconds=time.perf_counter() - started)
        (artifact_path(output, 'result.json', create=True)).write_text(json.dumps(result, indent=2) + '\n')
    if trace:
        records = read_trace(artifact_path(output, 'trace.bin', create=True))
        metrics = json.loads((artifact_path(output, 'producer.json')).read_text())
        assert metrics['observations'] == len(records)
        import capstone
        from db2010.diagnostics.trace import SCHEMA
        import pyarrow as pa
        import pyarrow.parquet as pq
        disassemblers = {thumb: capstone.Cs(capstone.CS_ARCH_ARM,
                          capstone.CS_MODE_THUMB if thumb else capstone.CS_MODE_ARM)
                        for thumb in (False, True)}
        decoded_records = []
        for record in records:
            decoded = next(disassemblers[record['thumb']].disasm(
                           bytes.fromhex(record['bytes']), record['pc'], count=1), None)
            decoded_records.append({**record, 'mnemonic': decoded.mnemonic if decoded else None,
                                    'operands': decoded.op_str if decoded else None})
        pq.write_table(pa.Table.from_pylist(decoded_records, schema=SCHEMA),
                       artifact_path(output, 'trace.parquet', create=True))
        result['trace_records'] = records
    return result


def read_trace(path):
    data = path.read_bytes()
    assert len(data) >= 16 and data[:8] == b'DBTRC001'
    version, size = struct.unpack_from('<II', data, 8)
    assert version == 1 and size == 92 and (len(data) - 16) % size == 0
    records = []
    for fields in struct.iter_unpack('<Q21I', data[16:]):
        index, pc, length, thumb, raw, *regs = fields
        assert index == len(records) + 1 and length in (2, 4) and thumb in (0, 1)
        records.append({'index': index, 'pc': pc, 'size': length, 'thumb': bool(thumb),
                        'bytes': raw.to_bytes(4, 'little')[:length].hex(),
                        'registers': dict(zip(REGISTERS, regs, strict=True))})
    return records
