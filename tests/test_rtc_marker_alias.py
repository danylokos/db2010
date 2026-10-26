"""Native bus checks for the narrowly scoped RTC/SEFLAFLA width alias."""
from support.guarded import run as guarded_run
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
from pathlib import Path
import struct

import pytest

from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation


@pytest.mark.parametrize('value', [0xA5, 0x5A])
def test_marker_byte_and_halfword_share_native_state(tmp_path, monkeypatch, value):
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        words = [0xE5C10000, 0xE1D120B0, 0xE1C100B0, 0xE5D13000, 0xEAFFFFFE]
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<5I', *words))
        state = json.loads(path.read_text())
        state['instruction_limit'] = 5
        state['executable_ranges'][0]['end'] = 0x1014
        state['registers'][:2] = [value, 0xF9050054]
        state['device_registers'] = [{'address': 0xF9050054, 'width': 1,
            'name': 'RTC marker alias fixture', 'initial_value': None,
            'read_or_mask': 0, 'allowed_writes': [0xA5, 0x5A],
            'rtc_halfword_alias': True}]
        path.write_text(json.dumps(state) + '\n')
        return path, {'seconds': 5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path / 'run', diagnostics=True)
    assert failure is None
    assert result['snapshot']['registers'][2:4] == [value, value]


@pytest.mark.parametrize('alias,initial,instruction,value,address,reason', [
    (True, None, 0xE1D120B0, 0, 0xF9050054, 'device_register_initial_state_unknown'),
    (True, 0xA5, 0xE1C100B0, 0x1A5, 0xF9050054, 'rtc_marker_upper_byte_unmodeled'),
    (False, 0xA5, 0xE1D120B0, 0, 0xF9050054, 'device_register_access_unmodeled'),
    (True, 0xA5, 0xE5912000, 0, 0xF9050054, 'device_register_access_unmodeled'),
    (True, 0xA5, 0xE5D12000, 0, 0xF9050055, 'device_register_access_unmodeled'),
])
def test_alias_guards_before_access(tmp_path, monkeypatch, alias, initial,
                                   instruction, value, address, reason):
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<I', instruction))
        state = json.loads(path.read_text())
        state['registers'][:3] = [value, address, 77]
        state['device_registers'] = [{'address': 0xF9050054, 'width': 1,
            'name': 'RTC marker alias fixture', 'initial_value': initial,
            'read_or_mask': 0, 'allowed_writes': [0xA5, 0x5A],
            'rtc_halfword_alias': alias}]
        path.write_text(json.dumps(state) + '\n')
        return path, {'seconds': 5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path / 'run', diagnostics=True)
    assert failure is None
    assert result['snapshot']['stop']['reason'] == reason
    assert result['snapshot']['registers'][2] == 77
