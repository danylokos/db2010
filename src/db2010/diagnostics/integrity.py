"""Bounded streaming checks for chronological instruction and bus ledgers."""
import numpy as np
import pyarrow.parquet as pq

BATCH = 262144


def ordinals(parquet, field, message):
    next_index = 1
    for batch in parquet.iter_batches(batch_size=BATCH, columns=[field]):
        values = batch.column(0).to_numpy()
        if not np.array_equal(values, np.arange(next_index, next_index+len(values), dtype=np.uint64)):
            raise ValueError(message)
        next_index += len(values)


def memory_identity(trace_path, memory_path):
    """Merge ordered ledgers, retaining at most one batch from each stream."""
    trace, memory = pq.ParquetFile(trace_path), pq.ParquetFile(memory_path)
    ordinals(trace, 'index', 'trace ordinal integrity differs')
    batches = (b for b in memory.iter_batches(batch_size=BATCH, columns=['index','pc']) if b.num_rows)
    current, offset, previous, validated = next(batches, None), 0, 0, False
    for batch in trace.iter_batches(batch_size=BATCH, columns=['index','pc']):
        indices, pcs = (batch.column(i).to_numpy() for i in (0,1))
        if not len(indices):
            continue
        while current is not None:
            mi, mp = (current.column(i).to_numpy() for i in (0,1))
            if not validated:
                if mi[0] < previous or np.any(mi[1:] < mi[:-1]):
                    raise ValueError('memory instruction order differs')
                previous = mi[-1]
                validated = True
            end = int(np.searchsorted(mi, indices[-1], side='right'))
            if end == offset:
                break
            selected = mi[offset:end]
            if selected[0] < indices[0] or not np.array_equal(pcs[(selected-indices[0]).astype(np.intp)], mp[offset:end]):
                raise ValueError('memory event/instruction identity differs')
            offset = end
            if offset < len(mi):
                break
            current, offset, validated = next(batches, None), 0, False
    if current is not None:
        raise ValueError('memory event/instruction identity differs')


def reference_instructions(actual, expected):
    fields = ['index','pc','size','thumb','bytes','registers']
    left, right = (iter(p.iter_batches(batch_size=65536, columns=fields)) for p in (actual,expected))
    a, b, ai, bi = next(left,None), next(right,None), 0, 0
    while a is not None and b is not None:
        count = min(a.num_rows-ai, b.num_rows-bi)
        if not a.slice(ai,count).equals(b.slice(bi,count)):
            raise ValueError('reference instruction/register fields differ')
        ai += count
        bi += count
        if ai == a.num_rows:
            a, ai = next(left,None), 0
        if bi == b.num_rows:
            b, bi = next(right,None), 0
    if a is not None or b is not None:
        raise ValueError('reference observation count differs')
