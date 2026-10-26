"""Enforce maintained original-MAIN copy contracts without supplying RAM state."""
from pathlib import Path
import json
from ..paths import RESOURCES
from ..diagnostics.artifacts import artifact as artifact_path


def authenticate(root, config, main, payload, output, *, recording=True):
    from .inputs import digest, load_json
    contract = load_json(RESOURCES/'execution-contract.json')
    if (contract['format_version'] != 1
            or digest(payload) != contract['payload_sha256']
            or main['payload_sha256'] != contract['payload_sha256']
            or config['memory'][0] != dict(name='external_ram', address=0x4C000000,
                                          size=0x800000, permissions='rwx')):
        raise ValueError('RAM execution contract input or declared region differs')
    result = []
    for i, mapping in enumerate(contract['mappings']):
        address, source, size = (mapping[k] for k in ('address', 'source', 'size'))
        if (any(type(v) is not int for v in (address, source, size)) or size <= 0
                or not main['base'] <= source < source+size <= main['end']
                or not 0x4C000000 <= address < address+size <= 0x4C800000):
            raise ValueError('RAM execution contract mapping differs')
        raw = payload[source-main['base']:source-main['base']+size]
        if digest(raw) != mapping['sha256']:
            raise ValueError('RAM execution contract bytes differ from original MAIN')
        path = artifact_path(output, f'execution-proof-{i}.bin', create=True)
        path.write_bytes(raw)
        result.append({**mapping, 'file': str(path)})
    ranges = [[main['base'], main['end']],
              *[[row['address'], row['address']+row['size']] for row in result]]
    if config['executable_ranges'] != ranges:
        raise ValueError('execution aperture differs from authenticated copy contract')
    if recording:
        artifact_path(output, 'execution-provenance.json', create=True).write_text(json.dumps({
            'semantics': contract['semantics'],
            'mappings': [{**row, 'file': Path(row['file']).relative_to(Path(output).resolve()).as_posix()}
                         for row in result],
            'contract_sha256': digest((RESOURCES/'execution-contract.json').read_bytes()),
            'provenance': contract['provenance'], 'promotion': contract['promotion'],
        }, indent=2)+'\n')
    # The engine requires known, guest-written bytes matching these expectations.
    # No proof is a RAM seed; changed/unknown instruction bytes remain guarded.
    return [{'address': row['address'], 'file': row['file']} for row in result]
