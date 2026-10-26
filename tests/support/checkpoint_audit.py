import hashlib

def execution_stream(path):
    # Native QEMU's timer v2 serializer includes two host elapsed-clock offsets.
    # Keep every CPU/RAM/model/virtual-clock byte, including timer/icount, intact.
    # See qemu/system/cpu-timers.c:vmstate_timers.
    data=path.read_bytes()
    marker=b'\x05timer'+bytes(4)+(2).to_bytes(4,'big')
    offset=data.rfind(marker)+len(marker)
    if offset<len(marker) or len(data)-offset>100000 or data[offset+24:offset+38]!=b'\x05\x0ctimer/icount':
        raise ValueError('unexpected native timer section; require explicit review')
    normalized=bytearray(data)
    normalized[offset:offset+8]=bytes(8)
    normalized[offset+16:offset+24]=bytes(8)
    return {'size_bytes':len(data),'sha256':hashlib.sha256(normalized).hexdigest(),
        'host_clock_offset_bytes_excluded':16,'timer_section_offset':offset}
