"""Validate original firmware containers and prepare raw engine inputs on the host."""
from ..diagnostics.artifacts import artifact as artifact_path, external_inputs
import hashlib
import json
from pathlib import Path
import struct
from .provenance import authenticate
from .cache import cached_input

from ..paths import ROOT, RESOURCES
MODELS = ('timebase', 'keypad', 'simif', 'bluetooth_transport', 'serial_transport', 'memory_controller', 'region_registers', 'section_table', 'section_table_update_policy',
          'power_hold', 'startup_status', 'f500_control', 'f500_command_port', 'f500_aux_port', 'synthetic_etx', 'synthetic_kgen', 'synthetic_sha', 'fa_transfer', 'fa_mailbox_tx', 'dsp_peer', 'device_registers', 'memorystick_slot', 'nor_model', 'nor_byte_bridge',
          'nor_probe_placeholders', 'absent_bank', 'timer_irq', 'rtc_counter', 'rtc_retained_state', 'serial_lines', 'uart_transmit', 'pl080_dma', 'pdi', 'i2c_controller', 'fm_radio', 'nand', 'f500_command_capture')
SPEC_KEYS = {'format_version', 'experiment', 'baseline', 'machine', 'scope', 'instruction_limit',
             'seconds', 'assumptions', 'nor_erased_tail', 'nor_gdfs_image', 'dtcm_reset_record', 'firmware_images', *MODELS}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def reset_record_input(profile):
    """Declared cold input; leave the rest of DTCM unknown and guest-writable."""
    expected = {'profile': 'cold-zero-reset-record', 'address': 0x42000604,
                'size': 28, 'fill': 0}
    if profile != expected or any(type(profile[k]) is not int for k in ('address', 'size', 'fill')):
        raise ValueError('unsupported DTCM reset-record cold input')
    raw = bytes(28)
    return {'assumption': expected, 'size_bytes': len(raw), 'sha256': digest(raw),
            'physical_power_on_state_established': False,
            'warm_reset_retention_established': False}


def nand_spare_seeds(profile):
    """Explicit synthetic spare inputs; never fill the main-data area."""
    if profile not in ('first-erase-block-ff-valid-footer',
                       'all-spare-ff-first-valid-footer', 'all-spare-ff-valid-footers'):
        raise ValueError('unsupported NAND synthetic spare profile')
    pages = 32 if profile == 'first-erase-block-ff-valid-footer' else 65536
    seeds = bytearray()
    for page in range(pages):
        footer = page == 0 or (profile == 'all-spare-ff-valid-footers' and page % 32 == 0)
        for column in range(16):
            value = (0x54 if column == 14 else 0x12) if footer and column >= 14 else 255
            seeds.extend(struct.pack('<HBB', page, column, value))
    return seeds


def load_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'duplicate JSON key: {key}')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError(f'non-finite JSON number: {value}')
    return json.loads(Path(path).read_text(), object_pairs_hook=pairs, parse_constant=invalid)


def resource(root, name):
    path = (root/name).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f'resource escapes repository: {name}')
    return path


def nor_gdfs_input(root, entry):
    """Authenticate a full native-produced record bank; never reinterpret logical backups."""
    if (set(entry) != {'profile', 'address', 'end', 'input_path', 'size_bytes', 'sha256'}
            or entry['profile'] != 'native-generated-gdfs-bank'
            or type(entry['address']) is not int or entry['address'] != 0x45F00000
            or type(entry['end']) is not int or entry['end'] != 0x46000000
            or type(entry['size_bytes']) is not int or entry['size_bytes'] != 0x100000):
        raise ValueError('unsupported native GDFS bank input')
    raw = resource(root, entry['input_path']).read_bytes()
    if len(raw) != entry['size_bytes'] or digest(raw) != entry['sha256']:
        raise ValueError('native GDFS bank identity differs')
    return raw


def reset_entry(raw, ranges):
    """Read the observed ARM MAIN reset vector, within authenticated code ranges."""
    if len(raw) < 0x24 or struct.unpack_from('<I', raw)[0] != 0xE59FF018:
        raise ValueError('unsupported MAIN reset vector; expected ARM ldr pc, [pc, #0x18]')
    start = struct.unpack_from('<I', raw, 0x20)[0]
    if start % 4 or not any(lo <= start and start + 4 <= hi for lo, hi in ranges):
        raise ValueError('MAIN reset entry is not an aligned instruction in supplied MAIN code')
    return start


def payload(root, entry, identity, start=None, input_path=None):
    """Authenticate payload bytes; start is an optional legacy entry assertion."""
    base, end = (0x44020000, 0x451C4BB8) if identity == 'main' else (0x45400000, 0x45E23CC0)
    data = (Path(input_path) if input_path is not None else resource(root, entry['input_path'])).read_bytes()
    if identity == 'main' and len(data) in (end-base, 0x45400000-base) and digest(data[:end-base]) == entry['payload_sha256']:
        # Authenticate the original payload; an expanded storage tail is data. Existing
        # instruction/relocation proofs still authenticate its executable bytes.
        if entry['base'] != base or entry['end'] != end:
            raise ValueError('MAIN raw input mapping differs')
        parsed = reset_entry(data, [(base, end)])
        if start is not None and start != parsed:
            raise ValueError('MAIN entry differs from reset vector')
        return data
    if (entry['id'] != identity or len(data) != entry['size'] or digest(data) != entry['sha256']
            or data[:4] != bytes.fromhex('babe0003') or entry['base'] != base or entry['end'] != end
            or len(entry['segments']) != 344):
        raise ValueError(f'{identity} input identity or coverage differs')
    offset, address = 0x4D8, base
    chunks = []
    for segment in entry['segments']:
        length = segment['length']
        if (type(length) is not int or length <= 0 or offset+8+length > len(data)
                or segment != {'offset': offset+8, 'address': address, 'length': length}
                or struct.unpack_from('<II', data, offset) != (address, length)):
            raise ValueError(f'{identity} invalid record bounds/order')
        chunks.append(data[offset+8:offset+8+length])
        offset += 8+length
        address += length
    raw = b''.join(chunks)
    if offset != len(data) or address != end or digest(raw) != entry['payload_sha256']:
        raise ValueError(f'{identity} complete payload identity differs')
    if identity == 'main':
        parsed = reset_entry(raw, [(base, end)])
        if start is not None and start != parsed:
            raise ValueError('MAIN entry differs from reset vector')
    if identity == 'fs' and start is not None:
        raise ValueError('FS is data, not an entry point')
    if 'archive_sha256' in entry and digest(resource(root, entry['source']).read_bytes()) != entry['archive_sha256']:
        raise ValueError(f'{identity} original archive hash differs')
    return raw


def prepare(root, spec_path, output, *, images=None, recording=True, input_root=None):
    root = Path(root).resolve()
    input_root = root if input_root is None else Path(input_root).resolve()
    images = {name: Path(path).resolve() for name, path in (images or {}).items()}
    if images.keys() - {'main', 'fs', 'gdfs', 'nand-main', 'nand-spare'}:
        raise ValueError('unsupported image override')
    spec_path = spec_path.resolve()
    spec = load_json(spec_path)
    cli_images = set(images)
    configured_images = spec.get('firmware_images', {})
    if not isinstance(configured_images, dict) or configured_images.keys()-{'main', 'fs', 'gdfs', 'nand-main', 'nand-spare'}:
        raise ValueError('unsupported configured firmware images')
    for name, entry in configured_images.items():
        if set(entry) != {'input_path', 'size_bytes', 'sha256'}:
            raise ValueError('unsupported configured firmware image identity')
        if name not in images:
            path = resource(input_root, entry['input_path'])
            data = path.read_bytes()
            if len(data) != entry['size_bytes'] or digest(data) != entry['sha256']:
                raise ValueError('configured firmware image identity differs: '+name)
            images[name] = path
    if (set(spec)-SPEC_KEYS or spec['format_version'] != 1 or spec['machine'] != 'db2010'
            or type(spec['instruction_limit']) is not int or spec['instruction_limit'] <= 0
            or type(spec['seconds']) not in (int, float) or spec['seconds'] <= 0):
        raise ValueError('unsupported prepared-experiment fields or limits')
    if (spec.get('timebase') != {'profile': 'qemu-virtual-v1', 'icount_shift': 6}
            or type(spec['timebase']['icount_shift']) is not int):
        raise ValueError('archived instruction-clock experiment: reproduce with its frozen runner and QEMU source bundle; native execution requires qemu-virtual-v1 with icount_shift=6')
    if spec['baseline'] in ('k750-r1db001-cid49-baseline.json',
                            'k750-r1db001-cid49-ram-baseline.json'):
        from .k750 import prepare_k750
        return prepare_k750(root, spec_path, spec, output, images=images, recording=recording, input_root=input_root)
    if 'nor_erased_tail' in spec and 'nor_gdfs_image' in spec:
        raise ValueError('erased and native GDFS bank inputs are mutually exclusive')
    # Accept the historical baseline identifier without reading its research file.
    if spec['baseline'] not in ('baseline.json', 'tools/unicorn/resources/experiments/0073.json'):
        raise ValueError('unsupported firmware baseline')
    config_path = RESOURCES/'baseline.json'
    config = load_json(config_path)
    if config['format_version'] != 1 or config['cpu'] != 'ARM926' or config['manifest_id'] != 'main':
        raise ValueError('requires the authenticated ARM926 direct-MAIN baseline')
    if (config['registers'] != {**{f'r{i}': 0 for i in range(13)}, 'lr': 0}
            or config['banked_sp'] != dict.fromkeys(('usr', 'fiq', 'irq', 'svc', 'abt', 'und'), 0)
            or config['cp15_control_clear_mask'] != 0x3085):
        raise ValueError('unsupported incoming register/banked/CP15 baseline')
    manifest_path = RESOURCES/config['manifest']
    manifest = load_json(manifest_path)
    def select(identity):
        entries = [entry for entry in manifest['entries'] if entry['id'] == identity]
        if len(entries) != 1:
            raise ValueError(f'ambiguous manifest identity: {identity}')
        return entries[0]
    main, fs = select('main'), select('fs')
    if config['fs_image'] != {key: fs['id' if key == 'manifest_id' else key]
                             for key in ('manifest_id', 'sha256', 'payload_sha256')}:
        raise ValueError('FS configured identity differs')
    main_raw = payload(input_root, main, 'main', input_path=images.get('main'))
    reset_pc = reset_entry(main_raw, [(main['base'], main['end'])])
    raw_fs = images.get('fs') is not None and images['fs'].stat().st_size == 11*1024*1024
    fs_raw = images['fs'].read_bytes() if raw_fs else payload(input_root, fs, 'fs', None, images.get('fs'))
    overrides = {}
    for name, entry in [('main', main), ('fs', fs)]:
        if name in cli_images:
            data = images[name].read_bytes()
            overrides[name] = {'input_path': str(images[name]), 'size_bytes': len(data),
                               'sha256': digest(data), 'payload_sha256': digest(main_raw[:main['end']-main['base']] if name == 'main' else fs_raw),
                               'validation': 'raw 11 MiB FS; contents supplied by caller' if name == 'fs' and raw_fs
                                    else 'authenticated original MAIN payload; storage tail supplied by caller'
                                    if name == 'main' and len(data) == 0x45400000-main['base']
                                    else 'selected firmware manifest identity and container mapping'}
    gdfs_raw = None
    if 'gdfs' in images:
        gdfs_raw = images['gdfs'].read_bytes()
        if len(gdfs_raw) != 0x100000:
            raise ValueError('GDFS image must be a raw 1 MiB NOR bank, not a logical backup')
        gdfs_identity = {'input_path': str(images['gdfs']), 'size_bytes': len(gdfs_raw),
                             'sha256': digest(gdfs_raw), 'address': 0x45F00000, 'end': 0x46000000,
                             'replaces': next((key for key in ('nor_gdfs_image', 'nor_erased_tail')
                                               if key in spec), 'unseeded bank'),
                             'validation': 'complete bank size; logical contents supplied by caller'}
        if 'gdfs' in cli_images:
            overrides['gdfs'] = gdfs_identity
    proofs = authenticate(root, config, main, main_raw[:main['end']-main['base']], output, recording=recording)
    main_padding = 0x45400000-main['base']-len(main_raw)
    main_raw += b'\xff'*main_padding
    tail_spec = {'profile': 'synthetic-erased-fs-remainder-valid-footers', 'address': 0x45E23CC0,
                 'end': 0x45F00000, 'fill': 255, 'footer_value': 0x1254,
                 'footer_addresses': [0x45E3FFFE, 0x45E7FFFE, 0x45EBFFFE, 0x45EFFFFE]}
    if config['fs_nor_tail'] != tail_spec:
        raise ValueError('unsupported synthetic FS tail')
    if any(lo < tail_spec['end'] and fs['base'] < hi for lo, hi in config['executable_ranges']):
        raise ValueError('FS or synthetic tail overlaps execution')
    tail = bytearray()
    if not raw_fs:
        # Historical container configurations retain their frozen preparation.
        # Ready raw FS inputs take the direct path and are never filled/edited.
        tail = bytearray([255])*(tail_spec['end']-tail_spec['address'])
        for address in tail_spec['footer_addresses']:
            offset = address-tail_spec['address']
            tail[offset:offset+2] = struct.pack('<H', 0x1254)
    fs_image = fs_raw if raw_fs else fs_raw+bytes(tail)
    seeds = []
    for name, address, data in [('main', main['base'], main_raw), ('fs', fs['base'], fs_image)]:
        supplied = images.get(name, resource(input_root, (main if name == 'main' else fs)['input_path']))
        path = supplied if supplied.stat().st_size == len(data) and supplied.read_bytes() == data else cached_input(root, data)
        seeds.append({'address': address, 'file': str(path)})
    regions = [{'name': 'db2010.nor-array', 'address': 0x44000000, 'size': 0x2000000,
                'readonly': True, 'seeds': seeds}]
    erased_tail = None
    if gdfs_raw is not None:
        path = images['gdfs']
        seeds.append({'address': 0x45F00000, 'file': str(path)})
    elif 'nor_erased_tail' in spec:
        expected = {'profile': 'erased-remaining-record-bank', 'address': 0x45f00000,
                    'end': 0x46000000, 'fill': 255}
        if spec['nor_erased_tail'] != expected:
            raise ValueError('unsupported erased NOR record-bank input')
        raw = bytes([255]) * (expected['end'] - expected['address'])
        path = cached_input(root, raw)
        seeds.append({'address': expected['address'], 'file': str(path)})
        erased_tail = {'assumption': expected, 'size_bytes': len(raw), 'sha256': digest(raw)}
    elif 'nor_gdfs_image' in spec:
        raw = nor_gdfs_input(input_root, spec['nor_gdfs_image'])
        path = resource(input_root, spec['nor_gdfs_image']['input_path'])
        seeds.append({'address': 0x45F00000, 'file': str(path)})
    for region in config['memory']:
        prepared = {key: region[key] for key in ('name', 'address', 'size')}
        prepared['seeds'] = []
        if region['name'] == config['ram_initialization']['region']:
            prepared['assumed_fill'] = config['ram_initialization']['fill_byte']
        regions.append(prepared)
    reset_record = reset_record_input(spec['dtcm_reset_record']) if 'dtcm_reset_record' in spec else None
    state = {'format_version': 1, 'entry': reset_pc, 'cpsr': config['cpsr'], 'sctlr': 0x90078,
             'registers': [0]*16, 'instruction_limit': spec['instruction_limit'], 'regions': regions,
             'execution_proofs': proofs,
             'executable_ranges': [{'start': lo, 'end': hi} for lo, hi in config['executable_ranges']]}
    for key in MODELS:
        if key in spec:
            state[key] = spec[key]
    if 'f500_control' in spec:
        fc = spec['f500_control']
        if (not isinstance(fc, dict) or set(fc) != {'profile', 'latency_ns'}
                or fc['profile'] != 'bit10-self-clear-latency'
                or type(fc['latency_ns']) is not int
                or not 1 <= fc['latency_ns'] <= 65536):
            raise ValueError('unsupported F500 control profile')
    if 'f500_command_port' in spec:
        port = spec['f500_command_port']
        if (not isinstance(port, dict) or set(port) != {'profile', 'latency_ns'}
                or port['profile'] != 'word-ready-strobe-latency'
                or type(port['latency_ns']) is not int
                or not 1 <= port['latency_ns'] <= 65536):
            raise ValueError('unsupported F500 command-port profile')
    if 'f500_aux_port' in spec:
        port = spec['f500_aux_port']
        if (not isinstance(port, dict) or set(port) != {'profile', 'latency_ns'}
                or port['profile'] != 'lowtag-ready-strobe-latency'
                or type(port['latency_ns']) is not int
                or not 1 <= port['latency_ns'] <= 65536):
            raise ValueError('unsupported F500 command-port profile')
    if 'synthetic_etx' in spec:
        etx = spec['synthetic_etx']
        if (not isinstance(etx, dict) or set(etx) != {'profile', 'expected_request_hex'}
                or etx['profile'] != 'gdfs-static-8040-status-success'
                or not isinstance(etx['expected_request_hex'], str)
                or len(etx['expected_request_hex']) != 164
                or any(c not in '0123456789abcdef' for c in etx['expected_request_hex'])
                or bytes.fromhex(etx['expected_request_hex'])[20:22] != b'\x3c\x00'):
            raise ValueError('unsupported synthetic ETX profile')
    if 'synthetic_sha' in spec and spec['synthetic_sha'] != {'profile':'sha1-le92-init-update-final-8040'}:
        raise ValueError('unsupported synthetic SHA profile')
    if 'synthetic_kgen' in spec:
        if spec['synthetic_kgen'] not in ({'profile': 'sha1-le92-init-update-8040'},
                                         {'profile': 'sha1-le92-init-update-final-8040'}):
            raise ValueError('unsupported synthetic KGEN profile')
    if reset_record is not None:
        state['dtcm_reset_record'] = spec['dtcm_reset_record']
    if any(name in images for name in ('nand-main', 'nand-spare')) and 'nand' not in spec:
        raise ValueError('NAND images require a supported NAND storage model')
    if 'nand' in spec:
        spare_profile = spec['nand'].get('synthetic_spare', {}).get('profile')
        expected = {'profile':'samsung-256m-x8-reset-id-status','address':0x50000000,
                    'manufacturer_id':0xEC,'device_id':0x35,'completion':'immediate-ready','write_protected':False,
                    'spare_read':{'profile':'samsung-x8-read2-unknown-contents'},
                    'spare_program':{'profile':'samsung-x8-known-spare-byte-program'},
                    'synthetic_spare':{'profile':spare_profile}}
        if 'block_erase' in spec['nand']:
            expected['block_erase'] = {'profile':'samsung-x8-block-erase-immediate'}
        if spec['nand'].get('spare_program') == {'profile':'samsung-x8-known-spare-four-pulse-startup'}:
            expected['spare_program'] = {'profile':'samsung-x8-known-spare-four-pulse-startup'}
        if spec['nand'].get('spare_program') == {'profile':'samsung-x8-known-spare-lifecycle-no-wear'}:
            expected['spare_program'] = {'profile':'samsung-x8-known-spare-lifecycle-no-wear'}
        if 'main_array' in spec['nand']:
            if 'block_erase' not in spec['nand']:
                raise ValueError('NAND known main-array profile requires block erase')
            expected['main_array'] = {'profile':'samsung-x8-known-main-read-program'}
        if 'copy_back' in spec['nand']:
            profile = spec['nand']['copy_back'].get('profile')
            if 'main_array' not in spec['nand'] or profile not in ('samsung-x8-known-page-copyback', 'samsung-x8-known-page-copyback-no-wear'):
                raise ValueError('unsupported NAND copy-back preparation profile')
            expected['copy_back'] = {'profile': profile}
        if spec['nand'] != expected:
            raise ValueError('unsupported NAND baseline/input preparation profile')
        seeds = nand_spare_seeds(spare_profile)
        path = cached_input(root, seeds)
        state['nand'] = {**spec['nand'], 'prepared_spare': str(path)}
        for name, size in (('nand-main', 65536*512), ('nand-spare', 65536*16)):
            if name not in images:
                continue
            if name == 'nand-main' and 'main_array' not in spec['nand']:
                raise ValueError('NAND main image requires the known main-array storage model')
            if images[name].stat().st_size != size:
                raise ValueError(f'{name} image must be exactly {size} bytes')
            state['nand'][name.replace('-', '_')+'_image'] = str(images[name])
            if name in cli_images:
                overrides[name] = {'input_path': str(images[name]), 'size_bytes': size,
                    'sha256': digest(images[name].read_bytes()),
                    'validation': 'complete raw storage geometry; all bytes supplied by caller'}
    if 'f500_command_capture' in spec:
        helper = bytes.fromhex('00200149088070471c0800f5')
        offset = 0x451B3E64-main['base']
        if spec['f500_command_capture'] != {'profile':'081c-halfword-zero-command-capture'} or main_raw[offset:offset+len(helper)] != helper:
            raise ValueError('F500 zero-command helper/profile differs from original MAIN')
        if recording:
            artifact_path(output, 'f500-helper.bin', create=True).write_bytes(helper)
    state_path = artifact_path(output, 'state.json', create=True)
    state_path.write_text(json.dumps(state, indent=2)+'\n')
    if not recording:
        return state_path, spec
    (artifact_path(output, 'experiment.json', create=True)).write_text(json.dumps(spec, indent=2)+'\n')
    (artifact_path(output, 'baseline.json', create=True)).write_bytes(config_path.read_bytes())
    (artifact_path(output, 'manifest.json', create=True)).write_bytes(manifest_path.read_bytes())
    identities = {'main_storage': {'size_bytes': len(main_raw), 'sha256': digest(main_raw),
                  'erased_padding_bytes': main_padding, 'fill': 255, 'physical_contents_established': False},
                  'main': main, 'fs': fs, 'manifest_sha256': digest(manifest_path.read_bytes()),
                  'baseline_sha256': digest(config_path.read_bytes()), 'experiment_sha256': digest(spec_path.read_bytes()),
                  'external_inputs': external_inputs(state, output),
                  'prepared_fs': {'input_path': state['regions'][0]['seeds'][1]['file'], 'address': fs['base'], 'end': tail_spec['end'],
                                  'size_bytes': len(fs_image), 'sha256': digest(fs_image)},
                  'synthetic_fs_extension': {'assumption': tail_spec, 'size_bytes': len(tail),
                                             'sha256': digest(tail), 'physical_contents_established': False},
                  'assumptions': spec['assumptions'], 'selected_engine_models': [k for k in MODELS if k in spec],
                  'unported_baseline_models': [k for k in ('timer_irq', 'rtc_counter', 'serial_lines', 'uart_transmit',
                      'i2c_controller', 'nand', 'pl080_dma', 'f500_command_capture') if k not in spec]}
    if erased_tail is not None:
        identities['synthetic_erased_nor_record_bank'] = erased_tail
    if overrides:
        identities['cli_image_overrides'] = overrides
    if configured_images:
        identities['configured_firmware_images'] = configured_images
    if raw_fs:
        identities.pop('synthetic_fs_extension')
        identities['raw_fs_image'] = {'input_path': str(images['fs']), 'size_bytes': len(fs_raw),
            'sha256': digest(fs_raw), 'preprocessing': 'none',
            'stock_fs_manifest_is_reference_only': True, 'physical_contents_established': False}
    if gdfs_raw is not None:
        if 'gdfs' in cli_images:
            identities['assumptions'] = [*spec['assumptions'],
                'CLI --gdfs replaces the configured NOR record-bank input with a caller-selected raw1MiB image. '
                'Its exact bytes/hash are frozen in this run; no logical conversion, code patch or hardware '
                'validity claim is implied. MAIN/FS, peripheral and original-code guards remain unchanged.']
        identities['nor_record_bank_image'] = gdfs_identity
    elif 'nor_gdfs_image' in spec:
        identities['native_generated_nor_record_bank'] = spec['nor_gdfs_image']
    if reset_record is not None:
        identities['synthetic_dtcm_reset_record'] = reset_record
    (artifact_path(output, 'inputs.json', create=True)).write_text(json.dumps(identities, indent=2)+'\n')
    return state_path, spec
