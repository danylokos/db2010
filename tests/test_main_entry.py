"""Derive direct-MAIN entries while retaining vector and original-input guards."""
import json
import struct

import pytest

from db2010.paths import ROOT, RESOURCES
from db2010.preparation.inputs import digest, payload, reset_entry


def vector(start, instruction=0xE59FF018):
    raw = bytearray(0x24)
    struct.pack_into('<I', raw, 0, instruction)
    struct.pack_into('<I', raw, 0x20, start)
    return raw


@pytest.mark.parametrize('base,start', [(0x44020000, 0x44020114),
    (0x44020000, 0x44020FB4), (0x10000000, 0x10001FFC)])
def test_entry_comes_from_vector_literal(base, start):
    assert reset_entry(vector(start), [(base, base+0x2000)]) == start


def test_sparse_entry_can_be_in_a_later_supplied_range():
    ranges = [(0x44020000, 0x44020EDC), (0x44020EE0, 0x440221C6)]
    assert reset_entry(vector(0x44020FB4), ranges) == 0x44020FB4
    with pytest.raises(ValueError, match='supplied MAIN code'):
        reset_entry(vector(0x44020EDC), ranges)


@pytest.mark.parametrize('start', [0x4401FFFC, 0x44020001, 0x44022000,
                                  0x45400000, 0xFFFFFFFF])
def test_entry_rejects_unaligned_or_unsupplied_code(start):
    with pytest.raises(ValueError, match='aligned instruction in supplied MAIN code'):
        reset_entry(vector(start), [(0x44020000, 0x44022000)])


def test_entry_requires_a_complete_instruction():
    with pytest.raises(ValueError, match='supplied MAIN code'):
        reset_entry(vector(0x44020100), [(0x44020000, 0x44020102)])


@pytest.mark.parametrize('length', [0, 4, 0x20, 0x23])
def test_truncated_vector_is_guarded(length):
    with pytest.raises(ValueError, match='unsupported MAIN reset vector'):
        reset_entry(vector(0x44020114)[:length], [(0x44020000, 0x44022000)])


def test_unsupported_vector_instruction_is_guarded():
    with pytest.raises(ValueError, match='unsupported MAIN reset vector'):
        reset_entry(vector(0x44020114, instruction=0xEA000000),
                    [(0x44020000, 0x44022000)])


@pytest.mark.parametrize('group', ['provisioned', 'non-provisioned'])
def test_raw_main_keeps_authentication_and_optional_legacy_assertion(tmp_path, group):
    manifest = json.loads((RESOURCES/'firmware-manifest.json').read_text())
    main = next(entry for entry in manifest['entries'] if entry['id'] == 'main')
    path = ROOT/'fw'/group/'w800i-r1l002-cid49-main.bin'
    raw = payload(ROOT, main, 'main', input_path=path)
    assert payload(ROOT, main, 'main', 0x44020114, path) == raw
    with pytest.raises(ValueError, match='entry differs from reset vector'):
        payload(ROOT, main, 'main', 0x44020FB4, path)
    # Changing only the reset literal to another valid in-range handler must
    # still fail original-payload authentication.
    changed = bytearray(raw)
    struct.pack_into('<I', changed, 0x20, 0x44020FB4)
    damaged = tmp_path/'main.bin'
    damaged.write_bytes(changed)
    with pytest.raises(ValueError, match='input identity or coverage differs'):
        payload(ROOT, main, 'main', input_path=damaged)


def test_sparse_preparation_uses_reset_literal_after_loading_fs(tmp_path, monkeypatch):
    from db2010.preparation import k750

    # An authenticated container fixture puts the handler in MAIN's second
    # range. Subsequent MAIN/FS record addresses must not replace the reset PC.
    def image(identity, chunks):
        data = bytearray(0x2EC)
        data[:4] = bytes.fromhex('babe0003')
        struct.pack_into('<I', data, 0x2E8, len(chunks))
        segments, ranges = [], []
        for address, raw in chunks:
            segments.append({'offset': len(data)+8, 'address': address, 'length': len(raw)})
            ranges.append([address, address+len(raw)])
            data.extend(struct.pack('<II', address, len(raw)))
            data.extend(raw)
        path = tmp_path/(identity+'.bin')
        path.write_bytes(data)
        return {'id': identity, 'input_path': path.name, 'size': len(data),
                'sha256': digest(data), 'base': ranges[0][0], 'end': ranges[-1][1],
                'segments': segments, 'ranges': ranges, 'record_stream_end': len(data),
                'payload_sha256': digest(b''.join(raw for _, raw in chunks))}

    target = 0x44020FB4
    main = image('main', [(0x44020000, vector(target)),
                          (target, bytes.fromhex('1eff2fe1'))])
    fs = image('fs', [(0x45400000, b'FS00')])
    (tmp_path/'k750-r1db001-cid49-manifest.json').write_text(
        json.dumps({'entries': [main, fs]}))
    baseline = tmp_path/'baseline.json'
    baseline.write_text(json.dumps({'cpsr': 0xD3, 'memory': [],
                                   'executable_ranges': main['ranges']}))
    monkeypatch.setattr(k750, 'RESOURCES', tmp_path)
    spec = {'baseline': baseline.name, 'instruction_limit': 100}
    state, _ = k750.prepare_k750(tmp_path, tmp_path/'experiment.json', spec,
                                tmp_path/'prepared', images={}, recording=False)
    assert json.loads(state.read_text())['entry'] == target
