"""Native odd-address halfword accesses and bounded RTC latch guards."""
from support.guarded import run as guarded_run
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
from pathlib import Path
import struct

import pytest

from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation


@pytest.mark.parametrize('address', [0xF905002A, 0xF905002B, 0xF9050042])
def test_native_retained_halfword_latches(tmp_path, monkeypatch, address):
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<3I', 0xE1C100B0, 0xE1D120B0, 0xEAFFFFFE))
        state = json.loads(path.read_text())
        state['instruction_limit'] = 3
        state['executable_ranges'][0]['end'] = 0x100C
        state['registers'][:3] = [0xA5, address, 77]
        state['rtc_retained_state'] = {'profile': 'cold-zero-lowbyte-bank'}
        path.write_text(json.dumps(state) + '\n')
        return path, {'seconds': 5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path / 'run', diagnostics=True)
    assert failure is None
    if address & 1:
        # MMU-off ARM fixtures treat the aperture as device memory; the CPU
        # rejects this odd store before bus dispatch. Native firmware's MMU-on
        # backup loop supplies the original-code odd-access proof instead.
        assert result['snapshot']['stop']['reason'] == 'exception_vector_unmodeled'
        assert result['snapshot']['registers'][2] == 77
    else:
        assert result['snapshot']['stop']['reason'] == 'instruction_limit'
        assert result['snapshot']['registers'][2] == 0xA5


@pytest.mark.parametrize('instruction,value,address,reason', [
    (0xE1C100B0, 0x1A5, 0xF905002B, 'rtc_retained_upper_byte_unmodeled'),
    (0xE5912000, 0, 0xF905002A, 'rtc_retained_access_unmodeled'),
    (0xE5D12000, 0, 0xF905002B, 'rtc_retained_access_unmodeled'),
    (0xE1D120B0, 0, 0xF9050043, 'rtc_retained_access_unmodeled'),
])
def test_retained_guards(tmp_path, monkeypatch, instruction, value, address, reason):
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<I', instruction))
        state = json.loads(path.read_text())
        state['registers'][:3] = [value, address, 77]
        state['rtc_retained_state'] = {'profile': 'cold-zero-lowbyte-bank'}
        path.write_text(json.dumps(state) + '\n')
        return path, {'seconds': 5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path / 'run', diagnostics=True)
    assert failure is None
    assert result['snapshot']['stop']['reason'] == reason
    assert result['snapshot']['registers'][2] == 77
