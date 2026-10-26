"""Execute the original program loader; storage captures do not run the sequencer."""
from support.guarded import run as guarded_run
from support.paths import RESOURCES
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
import struct

import pyarrow.parquet as pq
import pytest

from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation

ORIGINAL = json.loads((RESOURCES/'pdi-original.json').read_text())
POLICY = json.loads((RESOURCES/'pdi-reset-programs.json').read_text())


@pytest.mark.parametrize('known', [True, False])
def test_original_write_program_storage(tmp_path, monkeypatch, known):
    program = bytes.fromhex(POLICY['programs'][1]['bytes_hex'])
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        state = json.loads(path.read_text())
        loader = ORIGINAL['sources']['program_load']
        select = ORIGINAL['sources']['program_select']
        code = artifact_path(output, 'original.bin', create=True)
        code.write_bytes(bytes.fromhex(loader['hex']) + bytes.fromhex(select['hex']))
        obj = bytearray(0x1000)
        obj[0x40:0x54] = b'\xff' * 20
        struct.pack_into('<I', obj, 0xd0, 0x2400)
        struct.pack_into('<IIH', obj, 0x400, 0x2440, 0, len(program))
        obj[0x440:0x440+len(program)] = program
        ram = artifact_path(output, 'ram.bin', create=True)
        ram.write_bytes(obj)
        state['regions'] += [
            {'name': 'fixture.original', 'address': 0x447a0000, 'size': 4096, 'readonly': True,
             'seeds': [{'address': loader['start'], 'file': str(code)}]},
            {'name': 'fixture.ram', 'address': 0x2000, 'size': 4096, 'readonly': False,
             'seeds': [{'address': 0x2000, 'file': str(ram)}]},
        ]
        state['entry'] = loader['start']
        state['cpsr'] = 0xf3
        state['instruction_limit'] = 1500
        state['registers'][0:3] = [0x2000, 1, 0]
        state['registers'][13:15] = [0x2f00, 0x1000]
        state['executable_ranges'] += [{'start': loader['start'], 'end': 0x447a0e30},
                                       {'start': 0x447a0e38, 'end': 0x447a0e8c}]
        state['device_registers'] = [
            {'address': int(address, 16), 'width': 4, 'name': 'fixture.program_'+address,
             'initial_value': 0 if known else None, 'read_or_mask': 0, 'access': 'rw',
             'allowed_writes': values}
            for address, values in POLICY['word_writes'].items() if int(address, 16) < 0xf700011c
        ]
        path.write_text(json.dumps(state)+'\n')
        return path, {'seconds': 5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result, failure = guarded_run(ROOT, ROOT / 'unused', tmp_path / 'run', diagnostics=True, trace=True)
    assert failure is None
    snap = result['snapshot']
    assert snap['stop']['reason'] == ('instruction_limit' if known else 'device_register_initial_state_unknown')
    if known:
        assert snap['registers'][15] == 0x1000
        events = pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
        writes = [e for e in events if e['write'] and 0xf7000100 <= e['address'] < 0xf700011c]
        assert len(writes) == len(program)
        stored = b''.join(snap['device_registers'][str(0xf7000100+i)].to_bytes(4, 'little') for i in range(0, 28, 4))
        assert stored == program + b'\x00\x00'
        assert not any(0xf70001dc <= e['address'] <= 0xf70001fc for e in events)


@pytest.mark.parametrize('direct,data_hex,mask_hex,expected_data,expected_mask', [
    (1, '00000000002100000022', 'c000', [0x22000000, 0x21000000, 0], 0x30000000),
    (0, '00000000002100000022', 'c000', [0x22000000, 0x21000000, 0], 0xcfc00000),
    (1, '00070004001200000013101f', 'cccc', [0x1f101300, 0x1200, 0x4000700], 0xccc00000),
])
def test_original_data_mask_pack(tmp_path, monkeypatch, direct, data_hex, mask_hex, expected_data, expected_mask):
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        state = json.loads(path.read_text())
        source = ORIGINAL['sources']['transfer_setup']
        code = artifact_path(output, 'original.bin', create=True)
        code.write_bytes(bytes.fromhex(source['hex']))
        ram = bytearray(4096)
        ram[0x78] = direct
        payload = bytes.fromhex(data_hex)
        ram[0x440:0x440+len(payload)] = payload
        ram[0x460:0x462] = bytes.fromhex(mask_hex)
        data = artifact_path(output, 'ram.bin', create=True)
        data.write_bytes(ram)
        state['regions'] += [
            {'name': 'fixture.original', 'address': 0x447a0000, 'size': 4096, 'readonly': True,
             'seeds': [{'address': source['start'], 'file': str(code)}]},
            {'name': 'fixture.ram', 'address': 0x2000, 'size': 4096, 'readonly': False,
             'seeds': [{'address': 0x2000, 'file': str(data)}]},
        ]
        state['entry'] = source['start']
        state['cpsr'] = 0xf3
        state['instruction_limit'] = 1500
        state['registers'][0:4] = [0x2000, 0x2440, 0x2460, len(payload)]
        state['registers'][13:15] = [0x2f00, 0x1000]
        # The original Thumb veneer enters a Thumb-to-ARM signed divmod routine.
        state['executable_ranges'].append({'start': source['start'], 'end': 0x447a0aae})
        for name in ['div_veneer', 'signed_divmod']:
            original = ORIGINAL['sources'][name]
            helper = artifact_path(output, name+'.bin', create=True)
            helper.write_bytes(bytes.fromhex(original['hex']))
            state['regions'].append({'name': 'fixture.'+name, 'address': original['start'] & ~4095,
                                     'size': 4096, 'readonly': True,
                                     'seeds': [{'address': original['start'], 'file': str(helper)}]})
            state['executable_ranges'].append({'start': original['start'], 'end': original['end']})
        state['device_registers'] = [
            {'address': address, 'width': 4, 'initial_value': None,
             'read_or_mask': 0, 'access': 'write-only', 'name': 'fixture.packed_'+hex(address),
             'allowed_writes': [value]}
            for address, value in [(0xf700017c, expected_data[0]), (0xf7000178, expected_data[1]),
                                   (0xf7000174, expected_data[2]), (0xf700018c, expected_mask)]
        ]
        path.write_text(json.dumps(state)+'\n')
        return path, {'seconds': 5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result, failure = guarded_run(ROOT, ROOT / 'unused', tmp_path / 'run', diagnostics=True, trace=True)
    assert failure is None
    snap = result['snapshot']
    assert snap['stop']['reason'] == 'instruction_limit'
    assert snap['registers'][15] == 0x1000
    events = pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    writes = [(e['address'], e['value']) for e in events if e['write'] and 0xf7000100 <= e['address'] < 0xf7000200]
    assert writes == [(0xf700017c, expected_data[0]), (0xf7000178, expected_data[1]),
                      (0xf7000174, expected_data[2]), (0xf700018c, expected_mask)]
