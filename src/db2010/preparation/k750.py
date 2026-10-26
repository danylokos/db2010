"""Narrow, sparse original-byte preparation for the K750 CID49 prefix probe."""
import json
import struct

from ..diagnostics.artifacts import artifact, external_inputs
from ..paths import RESOURCES
from .cache import cached_input
from .inputs import MODELS, digest, load_json, reset_entry, resource


def container(root, entry):
    """Authenticate the entire cataloged file, including its opaque trailer."""
    data = resource(root, entry['input_path']).read_bytes()
    if (len(data) != entry['size'] or digest(data) != entry['sha256']
            or data[:4] != bytes.fromhex('babe0003')
            or struct.unpack_from('<I', data, 0x2e8)[0] != len(entry['segments'])):
        raise ValueError('K750 container identity differs')
    chunks, ranges = [], []
    offset = entry['segments'][0]['offset'] - 8
    for row in entry['segments']:
        address, size = row['address'], row['length']
        if (not 0 < size <= 65536 or row['offset'] != offset + 8
                or offset + 8 + size > len(data)
                or struct.unpack_from('<II', data, offset) != (address, size)
                or not entry['base'] <= address < address + size <= entry['end']
                or ranges and address < ranges[-1][1]):
            raise ValueError('K750 record mapping differs')
        raw = data[row['offset']:row['offset'] + size]
        if ranges and ranges[-1][1] == address:
            ranges[-1][1] += size
            chunks[-1] += raw
        else:
            ranges.append([address, address + size])
            chunks.append(raw)
        offset += 8 + size
    if (ranges != entry['ranges'] or offset != entry['record_stream_end']
            or digest(b''.join(chunks)) != entry['payload_sha256']):
        raise ValueError('K750 payload coverage differs')
    return list(zip(ranges, chunks))


def prepare_k750(root, spec_path, spec, output, *, images, recording, input_root=None):
    input_root = root if input_root is None else input_root
    if images or 'firmware_images' in spec or any(k in spec for k in (
            'nor_erased_tail', 'nor_gdfs_image', 'dtcm_reset_record',
            'f500_command_capture', 'synthetic_etx', 'synthetic_kgen', 'synthetic_sha')):
        raise ValueError('K750 prefix requires its cataloged sparse inputs without substitutions')
    config_path = RESOURCES / spec['baseline']
    manifest_path = RESOURCES / 'k750-r1db001-cid49-manifest.json'
    config, manifest = load_json(config_path), load_json(manifest_path)
    main, fs = manifest['entries']
    if main['id'] != 'main' or fs['id'] != 'fs':
        raise ValueError('K750 direct-MAIN baseline differs')
    seeds, main_chunks = [], []
    for entry in (main, fs):
        chunks = container(input_root, entry)
        if entry is main:
            main_chunks = chunks
            reset_pc = reset_entry(chunks[0][1], [bounds for bounds, _ in chunks])
        for (start, end), raw in chunks:
            seeds.append({'address': start, 'file': str(cached_input(root, raw))})
    regions = [{'name': 'db2010.nor-array', 'address': 0x44000000,
                'size': 0x2000000, 'readonly': True, 'seeds': seeds}]
    for row in config['memory']:
        region = {key: row[key] for key in ('name', 'address', 'size')}
        region['seeds'] = []
        if row['name'] == config['ram_initialization']['region']:
            region['assumed_fill'] = config['ram_initialization']['fill_byte']
        regions.append(region)
    proofs, proof_ranges = [], []
    if 'execution_contract' in config:
        contract_path = RESOURCES / 'k750-r1db001-cid49-execution-contract.json'
        if config['execution_contract'] != contract_path.name:
            raise ValueError('unsupported K750 execution contract')
        contract = load_json(contract_path)
        if contract['format_version'] != 1 or contract['payload_sha256'] != main['payload_sha256']:
            raise ValueError('K750 copy contract identity differs')
        for i, row in enumerate(contract['mappings']):
            address, source, size = (row[k] for k in ('address', 'source', 'size'))
            candidates = [raw[source-lo:source-lo+size] for (lo, hi), raw in main_chunks
                          if lo <= source < source+size <= hi]
            if (not 0x4C000000 <= address < address+size <= 0x4C800000
                    or len(candidates) != 1 or digest(candidates[0]) != row['sha256']):
                raise ValueError('K750 original-copy mapping differs')
            path = artifact(output, f'execution-proof-{i}.bin', create=True)
            path.write_bytes(candidates[0])
            proofs.append({'address': address, 'file': str(path)})
            proof_ranges.append([address, address+size])
        if recording:
            artifact(output, 'execution-provenance.json', create=True).write_text(
                json.dumps({'contract': contract, 'contract_sha256': digest(contract_path.read_bytes())},
                           indent=2) + '\n')
    if config['executable_ranges'] != main['ranges'] + proof_ranges:
        raise ValueError('K750 execution aperture differs from original-copy contract')
    state = {'format_version': 1, 'entry': reset_pc, 'cpsr': config['cpsr'],
             'sctlr': 0x90078, 'registers': [0] * 16,
             'instruction_limit': spec['instruction_limit'], 'regions': regions,
             'execution_proofs': proofs,
             'executable_ranges': [{'start': a, 'end': b} for a, b in config['executable_ranges']]}
    state.update({key: spec[key] for key in MODELS if key in spec})
    state_path = artifact(output, 'state.json', create=True)
    state_path.write_text(json.dumps(state, indent=2) + '\n')
    if recording:
        for name, path in [('experiment.json', spec_path), ('baseline.json', config_path),
                           ('manifest.json', manifest_path)]:
            artifact(output, name, create=True).write_bytes(path.read_bytes())
        artifact(output, 'inputs.json', create=True).write_text(json.dumps({
            'main': main, 'fs': fs, 'external_inputs': external_inputs(state, output),
            'manifest_sha256': digest(manifest_path.read_bytes()),
            'baseline_sha256': digest(config_path.read_bytes()),
            'experiment_sha256': digest(spec_path.read_bytes()),
            'assumptions': spec['assumptions'],
            'selected_engine_models': [key for key in MODELS if key in spec],
            'sparse_nor_policy': 'Seed authenticated records only; gaps and tails remain unknown.',
            'ram_execution_policy': 'Only approved original-copy ranges; proof bytes never seed RAM.'
        }, indent=2) + '\n')
    return state_path, spec
