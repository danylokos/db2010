"""Bounded successful guest bus events and host-side display visibility audit."""
from .artifacts import artifact as artifact_path
import json
import struct
import time

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .trace import BATCH

HEADER = b'DBMEM001'+struct.pack('<II', 1, 52)
DTYPE = np.dtype([(name, kind) for name, kind in [
    ('event', '<u8'), ('index', '<u8'), ('address', '<u8'), ('physical', '<u8'),
    ('value', '<u8'), ('pc', '<u4'), ('size', '<u4'), ('flags', '<u4')]])
SCHEMA = pa.schema([
    *(pa.field(name, pa.uint64(), nullable=False) for name in
      ('event', 'index', 'address', 'physical', 'value')),
    pa.field('pc', pa.uint32(), nullable=False), pa.field('size', pa.uint32(), nullable=False),
    *(pa.field(name, pa.bool_(), nullable=False) for name in ('write', 'io', 'big_endian')),
], metadata={b'db2010.memory_schema': b'1',
             b'db2010.memory_semantics': b'post-successful guest data access; not rejected attempts, fetches, page walks or debugger access'})


class MemoryCollector:
    def __init__(self, output):
        self.output = output
        self.rows = 0
        self.last_index = 0
        self.metrics = {'events': 0, 'batch_rows': BATCH, 'wire_record_bytes': 52,
                        'complete': False, 'parquet_finalized': False}

    def collect(self, listener, sockets, socket_lock, timeout):
        writer = connection = None
        error = None
        started = time.perf_counter()
        try:
            writer = pq.ParquetWriter(artifact_path(self.output, 'memory.parquet', create=True), SCHEMA, compression='zstd',
                                     compression_level=3, use_dictionary=False)
            connection, _ = listener.accept()
            connection.settimeout(timeout)
            with socket_lock:
                sockets.append(connection)
            with connection.makefile('rb', buffering=0) as stream:
                def read(size):
                    data = bytearray()
                    while len(data) < size:
                        chunk = stream.read(size-len(data))
                        if not chunk:
                            break
                        data.extend(chunk)
                    return data
                if read(16) != HEADER:
                    raise ValueError('unsupported/truncated memory header')
                while data := read(BATCH*52):
                    length = len(data)//52
                    records = np.frombuffer(data, dtype=DTYPE, count=length)
                    indices = records['index']
                    good = ((records['event'] == np.arange(self.rows+1, self.rows+length+1, dtype=np.uint64))
                            & (indices > 0) & (indices >= np.r_[self.last_index, indices[:-1]])
                            & np.isin(records['size'], [1, 2, 4, 8]) & (records['flags'] < 8)
                            & (records['address'] <= 0xFFFFFFFF) & (records['physical'] <= 0xFFFFFFFF))
                    # Values are zero extended from the transaction width.
                    for size in (1, 2, 4):
                        good &= (records['size'] != size) | (records['value'] < (1 << (8*size)))
                    invalid = np.flatnonzero(~good)
                    accepted = int(invalid[0]) if len(invalid) else length
                    if accepted:
                        kept = records[:accepted]
                        table = pa.Table.from_arrays([
                            *(pa.array(kept[name]) for name in ('event', 'index', 'address', 'physical', 'value', 'pc', 'size')),
                            *(pa.array((kept['flags'] & bit) != 0) for bit in (1, 2, 4))], schema=SCHEMA)
                        writer.write_table(table, row_group_size=BATCH)
                        self.rows += accepted
                        self.last_index = int(kept['index'][-1])
                    if len(invalid) or len(data)%52:
                        (artifact_path(self.output, 'memory-error-tail.bin', create=True)).write_bytes(data[accepted*52:])
                        raise ValueError('invalid/unordered/truncated memory record')
        except BaseException as exc:
            error = exc
        finally:
            try:
                if writer:
                    writer.close()
                    self.metrics['parquet_finalized'] = True
            except BaseException as exc:
                error = error or exc
            if connection:
                connection.close()
            self.metrics.update(events=self.rows, complete=error is None,
                                elapsed_seconds=time.perf_counter()-started)
            if error:
                self.metrics['error'] = {'type': type(error).__name__, 'message': str(error)}
            (artifact_path(self.output, 'memory-collector.json', create=True)).write_text(json.dumps(self.metrics, indent=2)+'\n')
        if error:
            raise error
        return self.metrics


def audit(output):
    """SQL views preserve all writes, including NOR command stores and MMIO."""
    # Definitions follow the retained 0060 audit. Visibility is not a display model.
    queries = {
        'trace': 'CREATE VIEW trace AS SELECT * FROM read_parquet(?)',
        'memory': 'CREATE VIEW memory AS SELECT * FROM read_parquet(?)',
        'writes': 'CREATE VIEW writes AS SELECT * FROM memory WHERE write',
        'device_events': 'CREATE VIEW device_events AS SELECT * FROM memory WHERE io OR '
                         '(physical >= 1140850688 AND physical < 1174413312) OR physical >= 4026531840',
        'integrity': 'SELECT count(*) FROM memory m LEFT JOIN trace t USING ("index") WHERE t.pc IS NULL OR m.pc != t.pc',
        'counts': 'SELECT count(*) AS events, count(*) FILTER (WHERE write) AS writes, '
                  'count(*) FILTER (WHERE io) AS io FROM memory',
        'device_count': 'SELECT count(*) FROM device_events',
        'pdi_candidate': 'SELECT * FROM memory WHERE physical >= 4143972352 AND physical < 4144037888 ORDER BY event',
        'dma': 'SELECT * FROM memory WHERE physical >= 4060086272 AND physical < 4060090368 ORDER BY event',
        'gpio': 'SELECT * FROM memory WHERE physical >= 4177526784 AND physical < 4177530880 ORDER BY event',
    }
    report = {'scope': 'Successful guest data accesses and recovered code ranges; no rendered display or handset response is inferred.',
              'rejected_access': 'See stopped snapshot; failed attempts have no successful memory event.',
              'code_ranges_source': 'tools/unicorn/analysis/boot/audit_display_0060.py (0060 investigation)'}
    from .integrity import memory_identity
    memory_identity(artifact_path(output, 'trace.parquet'), artifact_path(output, 'memory.parquet'))
    report['integrity_method'] = 'bounded chronological merge of instruction and bus batches'
    with duckdb.connect() as db:
        # DuckDB does not allow prepared parameters in CREATE VIEW statements.
        for name in ('trace', 'memory'):
            path = str(artifact_path(output, f'{name}.parquet')).replace("'", "''")
            db.execute(queries[name].replace('?', f"'{path}'"))
        for name in ('writes', 'device_events'):
            db.execute(queries[name])
        row = db.execute(queries['counts']).fetchone()
        report.update(zip(('events', 'writes', 'qemu_io_events'), row, strict=True))
        report['device_events'] = db.execute(queries['device_count']).fetchone()[0]
        for name in ('pdi_candidate', 'dma', 'gpio'):
            result = db.execute(queries[name])
            columns = [field[0] for field in result.description]
            # GPIO/PDI/DMA traffic is small for this guarded prefix; retain full
            # events in Parquet, bounded summaries as models advance.
            report[name] = {'sample': [dict(zip(columns, row, strict=True)) for row in result.fetchmany(64)],
                            'count': db.execute('SELECT count(*) FROM ('+queries[name]+')').fetchone()[0]}
        report['code_ranges'] = []
        for name, lo, hi in [
            ('mc_pdi_factory', 0x447A0118, 0x447A01E8), ('mc_pdi_driver', 0x447A01E8, 0x447A0F00),
            ('i2c_display_factory', 0x4479FE50, 0x4479FEB0),
            ('display_parser_and_i2c_helpers', 0x4479F2EC, 0x447A0118),
            ('dispif_source_reference_region', 0x44EB8CE0, 0x44EB9BCC)]:
            sql = f'SELECT count(*) FROM trace WHERE pc >= {lo} AND pc < {hi}'
            queries[name] = sql
            report['code_ranges'].append({'name': name, 'start': lo, 'end': hi, 'snapshots': db.execute(sql).fetchone()[0]})
    (artifact_path(output, 'memory-audit-queries.json', create=True)).write_text(json.dumps(queries, indent=2)+'\n')
    (artifact_path(output, 'display-audit.json', create=True)).write_text(json.dumps(report, indent=2)+'\n')
    return report
