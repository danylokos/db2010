"""Original PDI busy-bit poll and strict read-only word-status guards."""
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


@pytest.mark.parametrize('value,target', [(0, 0x447a0468), (1, 0x447a0460),
                                        (0x100, 0x447a0468), (None, None)])
def test_original_pdi_bit0_poll(tmp_path, monkeypatch, value, target):
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        source = ORIGINAL['sources']['reset']
        code = artifact_path(output, 'original.bin', create=True)
        code.write_bytes(bytes.fromhex(source['hex']))
        state = json.loads(path.read_text())
        state['entry'] = 0x447a0460
        state['cpsr'] = 0xf3
        state['instruction_limit'] = 4
        state['regions'] = [{'name': 'fixture.original', 'address': 0x447a0000,
                            'size': 4096, 'readonly': True,
                            'seeds': [{'address': source['start'], 'file': str(code)}]}]
        state['executable_ranges'] = [{'start': 0x447a0434, 'end': 0x447a04e2}]
        state['device_registers'] = [{'address': 0xf70001f4, 'width': 4, 'initial_value': value,
                                     'read_or_mask': 0, 'access': 'read-only', 'name': 'fixture.pdi_status'}]
        path.write_text(json.dumps(state) + '\n')
        return path, {'seconds': 5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result, failure = guarded_run(ROOT, ROOT / 'unused', tmp_path / 'run', diagnostics=True, trace=True)
    assert failure is None
    snap = result['snapshot']
    assert snap['stop']['reason'] == ('device_register_initial_state_unknown' if target is None else 'instruction_limit')
    if target is not None:
        assert snap['registers'][15] == target
        events = [e for e in pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist() if e['address'] == 0xf70001f4]
        assert [(e['pc'], e['size'], e['value'], e['write']) for e in events] == [(0x447a0462, 4, value, False)]


@pytest.mark.parametrize('instruction,reason', [
    (0xe5810000, 'device_register_write_unmodeled'),  # STR word
    (0xe1d100b0, 'device_register_access_unmodeled'),  # LDRH
])
def test_pdi_status_guards(tmp_path, monkeypatch, instruction, reason):
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        code = artifact_path(output, 'code.bin')
        code.write_bytes(struct.pack('<I', instruction))
        state = json.loads(path.read_text())
        state['regions'][0]['seeds'] = [{'address': 0x1000, 'file': str(code)}]
        state['executable_ranges'] = [{'start': 0x1000, 'end': 0x1004}]
        state['registers'][0:2] = [0, 0xf70001f4]
        state['device_registers'] = [{'address': 0xf70001f4, 'width': 4, 'initial_value': 0,
                                     'read_or_mask': 0, 'access': 'read-only', 'name': 'fixture.pdi_status'}]
        path.write_text(json.dumps(state) + '\n')
        return path, {'seconds': 5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result, failure = guarded_run(ROOT, ROOT / 'unused', tmp_path / 'run', diagnostics=True, trace=True)
    assert failure is None
    assert result['snapshot']['stop']['reason'] == reason
