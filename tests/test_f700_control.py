"""Original F7000300 RMW sequence and exact word/command guards."""
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

ORIGINAL = json.loads((RESOURCES/'f700-control-original.json').read_text())


@pytest.mark.parametrize('initial', [0, 1, 0xffffffff, None])
def test_original_control_rmw(tmp_path, monkeypatch, initial):
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        original = ORIGINAL['initializer_with_literals']
        code = artifact_path(output, 'original.bin', create=True)
        code.write_bytes(bytes.fromhex(original['hex']))
        state = json.loads(path.read_text())
        state['entry'] = ORIGINAL['control_slice']['start']
        state['cpsr'] = 0xf3
        state['instruction_limit'] = ORIGINAL['control_slice']['instruction_count']
        state['regions'] = [{'name': 'fixture.original', 'address': 0x44ad8000,
                            'size': 4096, 'readonly': True,
                            'seeds': [{'address': original['start'], 'file': str(code)}]}]
        state['executable_ranges'] = [{'start': original['start'], 'end': ORIGINAL['initializer']['end']}]
        # Each variant's whitelist comes from independent native RMW expectations.
        value = initial or 0
        expected = []
        for mask, set_bits in [(0xfffffffd, 0), (0xfffffdff, 0), (0xffffffff, 0x200),
                               (0xffffffbf, 0), (0xffffffff, 0x40), (0xffffff7f, 0),
                               (0xffffffff, 0x80), (0xffffffdf, 0), (0xffffffff, 0x20),
                               (0xffffffe7, 0)]:
            value = value & mask | set_bits
            expected.append(value)
        state['device_registers'] = [{'address': 0xf7000300, 'width': 4,
                                     'initial_value': initial, 'read_or_mask': 0,
                                     'access': 'rw', 'allowed_writes': sorted(set(expected)),
                                     'name': 'fixture.control'}]
        path.write_text(json.dumps(state) + '\n')
        return path, {'seconds': 5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result, failure = guarded_run(ROOT, ROOT / 'unused', tmp_path / 'run', diagnostics=True, trace=True)
    assert failure is None
    snap = result['snapshot']
    assert snap['stop']['reason'] == ('device_register_initial_state_unknown' if initial is None else 'instruction_limit')
    if initial is not None:
        assert snap['registers'][15] == ORIGINAL['control_slice']['end']
        assert snap['device_registers'][str(0xf7000300)] == (initial & 0xfffffd05) | 0x2e0
        events = [e for e in pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
                  if e['address'] == 0xf7000300]
        assert len(events) == 20
        assert all(e['address'] == 0xf7000300 and e['size'] == 4 for e in events)
        for read, write in zip(events[::2], events[1::2]):
            assert not read['write'] and write['write']


@pytest.mark.parametrize('instruction,reason', [
    (0xe1d100b0, 'device_register_access_unmodeled'),  # LDRH
    (0xe5810000, 'device_register_command_unmodeled'),  # unsupported word store
])
def test_control_guards(tmp_path, monkeypatch, instruction, reason):
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        code = artifact_path(output, 'code.bin')
        code.write_bytes(struct.pack('<I', instruction))
        state = json.loads(path.read_text())
        state['regions'][0]['seeds'] = [{'address': 0x1000, 'file': str(code)}]
        state['executable_ranges'] = [{'start': 0x1000, 'end': 0x1004}]
        state['registers'][0:2] = [1, 0xf7000300]
        state['device_registers'] = [{'address': 0xf7000300, 'width': 4, 'initial_value': 0,
                                     'read_or_mask': 0, 'access': 'rw', 'name': 'fixture.control',
                                     'allowed_writes': [0, 0x200, 0x240, 0x2c0, 0x2e0]}]
        path.write_text(json.dumps(state) + '\n')
        return path, {'seconds': 5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result, failure = guarded_run(ROOT, ROOT / 'unused', tmp_path / 'run', diagnostics=True, trace=True)
    assert failure is None
    assert result['snapshot']['stop']['reason'] == reason
    assert result['snapshot']['device_registers'][str(0xf7000300)] == 0
