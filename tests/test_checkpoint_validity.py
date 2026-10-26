"""Packed validity maps and unchanged memory guards through native migration."""
import copy
import struct

import pytest

from support.fixtures import base_state, words
from test_native_checkpoint import engine, stopped, migration, comparable


def region_map(raw, index=0):
    marker = struct.pack('>II', 0x44424350, 24)
    offset = raw.index(marker)+8+1+8+65+4
    count = struct.unpack_from('>I', raw, offset)[0]
    offset += 4
    assert index < count
    for i in range(count):
        _, size = struct.unpack_from('>II', raw, offset)
        offset += 11
        width = raw[offset]
        if i == index:
            return offset, width, raw[offset+1:offset+1+size*width//8]
        offset += 1+size*width//8


@pytest.mark.parametrize('assumed', [False, True])
def test_packed_validity_round_trip_and_width(tmp_path, assumed):
    state = base_state(words(0xeafffffe), limit=1)
    state['regions'][0]['readonly'] = False
    if assumed: state['regions'][0]['assumed_fill'] = 0
    checkpoint = tmp_path/'state.vmstate'
    with engine(state, tmp_path/'source') as vm:
        stopped(vm)
        migration(vm, 'migrate', checkpoint)
    _, width, packed = region_map(checkpoint.read_bytes())
    assert width == (2 if assumed else 1)
    assert packed == (b'\x55'+b'\xaa'*1023 if assumed else b'\x0f'+bytes(511))
    with engine(state, tmp_path/'restored', incoming=True) as vm:
        migration(vm, 'migrate-incoming', checkpoint)
        migration(vm, 'migrate', tmp_path/'again.vmstate')
    assert region_map((tmp_path/'again.vmstate').read_bytes())[1:] == (width, packed)


@pytest.mark.parametrize('corruption', ['zero-width', 'wide', 'reserved-value', 'nonminimal', 'legacy'])
def test_native_rejects_invalid_map_encodings_and_legacy_version(tmp_path, corruption):
    state = base_state(words(0xeafffffe), limit=1)
    state['regions'][0].update(readonly=False, assumed_fill=0)
    checkpoint = tmp_path/'state.vmstate'
    with engine(state, tmp_path/'source') as vm:
        stopped(vm)
        migration(vm, 'migrate', checkpoint)
    raw = bytearray(checkpoint.read_bytes())
    offset, _, _ = region_map(raw)
    if corruption == 'zero-width': raw[offset] = 0
    elif corruption == 'wide': raw[offset] = 3
    elif corruption == 'reserved-value': raw[offset+1] |= 3
    elif corruption == 'nonminimal': raw[offset+1:offset+1025] = bytes(1024)
    else:
        start = raw.index(struct.pack('>II', 0x44424350, 24))
        raw[start+4:start+8] = struct.pack('>I', 20)
    checkpoint.write_bytes(raw)
    with pytest.raises((AssertionError, RuntimeError, EOFError, ConnectionResetError)):
        with engine(state, tmp_path/'rejected', incoming=True) as vm:
            migration(vm, 'migrate-incoming', checkpoint)
    assert 'invalid DB2010 checkpoint state' in (tmp_path/'rejected/engine.log').read_text()


@pytest.mark.parametrize('kind', ['unknown-read', 'unknown-fetch', 'assumed-fetch', 'written-fetch'])
def test_memory_validity_guards_survive_restore(tmp_path, kind):
    op = 0xe5903000 if kind == 'unknown-read' else 0xe12fff10
    code = words(0xe5821000 if kind == 'written-fetch' else 0xe3a03001, op, 0xeafffffe)
    state = base_state(code, limit=6)
    state['registers'][0] = state['registers'][2] = 0x1100
    state['registers'][1] = 0xeafffffe
    state['regions'][0]['readonly'] = False
    if kind in ('assumed-fetch', 'written-fetch'): state['regions'][0]['assumed_fill'] = 0
    with engine(state, tmp_path/'whole') as vm: whole = stopped(vm)
    first = copy.deepcopy(state); first['instruction_limit'] = 1
    checkpoint = tmp_path/'state.vmstate'
    with engine(first, tmp_path/'source') as vm:
        stopped(vm)
        migration(vm, 'migrate', checkpoint)
    state['instruction_limit'] -= 1
    with engine(state, tmp_path/'resumed', incoming=True) as vm:
        migration(vm, 'migrate-incoming', checkpoint)
        final = stopped(vm)
    assert comparable(final) == comparable(whole)
    expected = {'unknown-read': 'read_unknown_ram', 'unknown-fetch': 'fetch_unknown_bytes',
                'assumed-fetch': 'fetch_assumed_ram_bytes', 'written-fetch': 'instruction_limit'}
    assert final['stop']['reason'] == expected[kind]


def test_flash_transfer_properties_are_retired(tmp_path):
    from test_native_checkpoint import command
    with engine(base_state(words(0xeafffffe)), tmp_path/'source') as vm:
        for name in ('flash-import', 'flash-export'):
            with pytest.raises(RuntimeError, match='not found'):
                command(vm, 'qom-set', {'path': '/machine', 'property': name, 'value': str(tmp_path)})


@pytest.mark.parametrize('kind', ['unknown', 'assumed', 'written'])
def test_exception_vector_validity_survives_restore(tmp_path, kind):
    state = base_state(words(0xe5801000 if kind == 'written' else 0xe3a03001,
                             0xef000000, 0xeafffffe), limit=5)
    state['registers'][:2] = [8, 0xeafffffe]
    vector = {'name': 'fixture.vectors', 'address': 0,
              'size': 4096, 'readonly': False, 'seeds': []}
    if kind != 'unknown': vector['assumed_fill'] = 0
    state['regions'].append(vector)
    state['executable_ranges'].append({'start': 0, 'end': 4096})
    with engine(state, tmp_path/'whole') as vm: whole = stopped(vm)
    first = copy.deepcopy(state);first['instruction_limit'] = 1
    checkpoint = tmp_path/'state.vmstate'
    with engine(first, tmp_path/'source') as vm:
        stopped(vm)
        migration(vm, 'migrate', checkpoint)
    state['instruction_limit'] -= 1
    with engine(state, tmp_path/'resumed', incoming=True) as vm:
        migration(vm, 'migrate-incoming', checkpoint)
        final = stopped(vm)
    assert comparable(final) == comparable(whole)
    assert final['stop']['reason'] == ('instruction_limit' if kind == 'written' else 'exception_vector_unknown')
