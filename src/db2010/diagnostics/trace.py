"""Bounded binary snapshot ingestion; validation precedes Parquet commit."""
from .artifacts import artifact as artifact_path
from collections import OrderedDict
import json
from pathlib import Path
import struct
import time

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

REGISTERS = [*(f'r{i}' for i in range(13)), 'sp', 'lr', 'pc', 'cpsr']
SCHEMA = pa.schema([
    pa.field('index', pa.uint64(), nullable=False), pa.field('pc', pa.uint32(), nullable=False),
    pa.field('size', pa.uint32(), nullable=False), pa.field('thumb', pa.bool_(), nullable=False),
    pa.field('registers', pa.struct([pa.field(name, pa.uint32(), nullable=False) for name in REGISTERS]), nullable=False),
    pa.field('bytes', pa.string(), nullable=False), pa.field('mnemonic', pa.string()), pa.field('operands', pa.string()),
], metadata={b'db2010.trace_schema': b'1', b'db2010.snapshot': b'pre-instruction; hook does not prove retirement'})
DTYPE = np.dtype([('index', '<u8'), ('words', '<u4', 21)])
HEADER = b'DBTRC001'+struct.pack('<II', 1, 92)
BATCH = 8192


class TraceCollector:
    def __init__(self, output, *, disassemble=False):
        self.output = output
        self.rows = self.batches = 0
        self.disassemble = disassemble
        self.cache = OrderedDict()
        self.decode = None
        self.metrics = {'batch_rows': BATCH, 'wire_record_bytes': 92, 'disassembly': disassemble,
                        'read_seconds': 0.0, 'convert_seconds': 0.0, 'write_seconds': 0.0,
                        'observations': 0, 'complete': False, 'parquet_finalized': False}

    def annotations(self, fields, hex_bytes):
        if not self.disassemble:
            return [None]*len(fields), [None]*len(fields)
        if self.decode is None:
            import capstone
            self.decode = {thumb: capstone.Cs(capstone.CS_ARCH_ARM,
                capstone.CS_MODE_THUMB if thumb else capstone.CS_MODE_ARM) for thumb in (False, True)}
        mnemonics, operands = [], []
        for row, raw in zip(fields, hex_bytes, strict=True):
            # Branch/literal operands depend on PC. Bound the cache explicitly.
            key = (int(row[0]), bool(row[2]), raw)
            if key not in self.cache:
                instruction = next(self.decode[key[1]].disasm(bytes.fromhex(raw), key[0], count=1), None)
                self.cache[key] = (instruction.mnemonic, instruction.op_str) if instruction else (None, None)
                if len(self.cache) > 4096:
                    self.cache.popitem(last=False)
            else:
                self.cache.move_to_end(key)
            mnemonic, operand = self.cache[key]
            mnemonics.append(mnemonic);operands.append(operand)
        return mnemonics, operands

    def commit(self, writer, records):
        if not len(records):
            return
        phase = time.perf_counter()
        fields = records['words']
        registers = pa.StructArray.from_arrays([pa.array(fields[:, i+4], type=pa.uint32())
                                               for i in range(17)], fields=SCHEMA.field('registers').type)
        raw = [int(value).to_bytes(4, 'little')[:int(size)].hex()
               for value, size in zip(fields[:, 3], fields[:, 1], strict=True)]
        mnemonic, operands = self.annotations(fields, raw)
        table = pa.Table.from_arrays([
            pa.array(records['index']), pa.array(fields[:, 0]), pa.array(fields[:, 1]),
            pa.array(fields[:, 2] != 0), registers, pa.array(raw, type=pa.string()),
            pa.array(mnemonic, type=pa.string()), pa.array(operands, type=pa.string())], schema=SCHEMA)
        self.metrics['convert_seconds'] += time.perf_counter()-phase
        phase = time.perf_counter()
        writer.write_table(table, row_group_size=BATCH)
        self.metrics['write_seconds'] += time.perf_counter()-phase
        self.rows += len(records)
        self.batches += 1

    def collect(self, listener, sockets, socket_lock, timeout):
        started = time.perf_counter()
        # Even a malformed/disconnected stream leaves a closed Parquet prefix.
        writer = None
        error = None
        connection = None
        try:
            writer = pq.ParquetWriter(artifact_path(self.output, 'trace.parquet', create=True), SCHEMA, compression='zstd', compression_level=3,
                                     use_dictionary=False, write_statistics=True, data_page_version='1.0')
            connection, _ = listener.accept()
            connection.settimeout(timeout)
            with socket_lock:
                sockets.append(connection)
            stream = connection.makefile('rb', buffering=0)
            def read_up_to(size):
                data = bytearray()
                while len(data) < size:
                    chunk = stream.read(size-len(data))
                    if not chunk:
                        break
                    data.extend(chunk)
                return data
            header = read_up_to(16)
            if header != HEADER:
                raise ValueError('unsupported/truncated trace header')
            while True:
                phase = time.perf_counter()
                data = read_up_to(BATCH*92)
                self.metrics['read_seconds'] += time.perf_counter()-phase
                if not data:
                    break
                length = len(data)//92
                records = np.frombuffer(data, dtype=DTYPE, count=length)
                fields = records['words']
                good = ((records['index'] == np.arange(self.rows+1, self.rows+length+1, dtype=np.uint64))
                        & ((fields[:, 1] == 2) | (fields[:, 1] == 4)) & (fields[:, 2] <= 1)
                        & ((fields[:, 2] != 0) | (fields[:, 1] == 4))
                        & ((fields[:, 1] != 2) | ((fields[:, 3] >> 16) == 0))
                        & (fields[:, 0] == fields[:, 19]) & (fields[:, 2] == ((fields[:, 20] >> 5) & 1)))
                invalid = np.flatnonzero(~good)
                accepted = int(invalid[0]) if len(invalid) else length
                self.commit(writer, records[:accepted])
                if len(invalid) or len(data)%92:
                    (artifact_path(self.output, 'stream-error-tail.bin', create=True)).write_bytes(data[accepted*92:])
                    raise ValueError('invalid/unordered/truncated trace record')
            stream.close()
        except BaseException as exc:
            error = exc
            self.metrics['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        finally:
            phase = time.perf_counter()
            try:
                if writer:
                    writer.close()
                    self.metrics['parquet_finalized'] = True
            except BaseException as exc:
                error = error or exc
                self.metrics['parquet_finalized'] = False
                self.metrics['error'] = {'type': type(exc).__name__, 'message': str(exc)}
            finally:
                self.metrics.update(observations=self.rows, batches=self.batches,
                    finalize_seconds=time.perf_counter()-phase, elapsed_seconds=time.perf_counter()-started,
                    complete=error is None)
                (artifact_path(self.output, 'collector.json', create=True)).write_text(json.dumps(self.metrics, indent=2)+'\n')
                if connection:
                    connection.close()
        if error:
            raise error
        return self.metrics
