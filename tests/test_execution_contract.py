"""Maintained contracts authenticate original bytes without archived run inputs."""
import copy
import json
from pathlib import Path

import pytest

from db2010.paths import RESOURCES, ROOT
from db2010.preparation import provenance
from db2010.preparation.inputs import digest, load_json
from db2010.diagnostics.artifacts import artifact


def inputs():
    config = load_json(RESOURCES/'baseline.json')
    manifest = load_json(RESOURCES/'firmware-manifest.json')
    main = next(e for e in manifest['entries'] if e['id'] == 'main')
    raw = (ROOT/'fw/non-provisioned/w800i-r1l002-cid49-main.bin').read_bytes()
    return config, main, raw


def test_contract_preserves_approved_bytes_without_seeding_ram(tmp_path):
    config, main, raw = inputs()
    proofs = provenance.authenticate(ROOT, config, main, raw, tmp_path)
    assert len(proofs) == 2
    expected = [(0x4C000000, raw[0x1400:0xA0A8]),
                (0x4C008CB0, raw[0xA0B0:0xAB1C])]
    for proof, (address, data) in zip(proofs, expected, strict=True):
        assert proof['address'] == address
        assert Path(proof['file']).read_bytes() == data
    assert all(not region.get('seeds') for region in config['memory'])
    recorded = json.loads(artifact(tmp_path, 'execution-provenance.json').read_text())
    assert recorded['contract_sha256'] == digest((RESOURCES/'execution-contract.json').read_bytes())


@pytest.mark.parametrize('change', ['payload', 'range', 'region', 'mapping_hash', 'mapping_source'])
def test_contract_rejects_changed_input_or_aperture(tmp_path, monkeypatch, change):
    config, main, raw = inputs()
    if change == 'payload':
        raw = raw[:100]+bytes([raw[100]^1])+raw[101:]
    elif change == 'range':
        config['executable_ranges'][1][1] += 4
    elif change == 'region':
        config['memory'][0]['size'] += 4096
    else:
        contract = copy.deepcopy(load_json(RESOURCES/'execution-contract.json'))
        if change == 'mapping_hash':
            contract['mappings'][0]['sha256'] = '0'*64
        else:
            contract['mappings'][0]['source'] = main['end']
        original = load_json
        monkeypatch.setattr('db2010.preparation.inputs.load_json',
            lambda path: contract if Path(path).name == 'execution-contract.json' else original(path))
    with pytest.raises(ValueError, match='contract|aperture'):
        provenance.authenticate(ROOT, config, main, raw, tmp_path)
