"""Atomic raw flash images for a new cold boot, separate from execution restore."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from ..diagnostics.artifacts import identity, load_run, relative_artifact, write_json
from .checkpoint import verify_required

NOR = {'main': (0x44020000, 0x45400000), 'fs': (0x45400000, 0x45F00000),
       'gdfs': (0x45F00000, 0x46000000)}
SIZES = {**{name: end-start for name, (start, end) in NOR.items()},
         'nand-main': 65536*512, 'nand-spare': 65536*16}
GEOMETRY = {'nor': {name: {'start': start, 'end': end} for name, (start, end) in NOR.items()},
            'nand': {'pages': 65536, 'pages_per_block': 32, 'page_data_bytes': 512,
                     'page_spare_bytes': 16}}


def validate_storage(state):
    """Reject unsupported exports before launching the engine."""
    if ('nor_model' not in state or state.get('nand', {}).get('main_array') !=
            {'profile': 'samsung-x8-known-main-read-program'}):
        raise ValueError('flash saving requires supported NOR and NAND main storage models')


def load_flash_images(source):
    """Authenticate only this family; missing/pruned diagnostics do not prevent loading."""
    source = Path(source).resolve()
    load_run(source, family='flash')
    seals = json.loads((source/'artifacts.sha256.json').read_text())
    metadata = relative_artifact(source, 'flash/flash.json')
    verify_required(source, {'run.json': identity(source/'run.json'),
                            'flash/flash.json': identity(metadata)}, seals)
    spec = json.loads(metadata.read_text())
    if (set(spec) != {'schema_version', 'geometry', 'images'} or
            type(spec['schema_version']) is not int or spec['schema_version'] != 1 or
            spec['geometry'] != GEOMETRY or set(spec['images']) != set(SIZES)):
        raise ValueError('incompatible flash geometry or image inventory')
    expected_names = {'flash.json', *(name+'.bin' for name in SIZES)}
    if {p.name for p in metadata.parent.iterdir()} != expected_names:
        raise ValueError('flash file inventory differs')
    inventory = {}
    for name, size in SIZES.items():
        row = spec['images'][name]
        if (set(row) != {'file', 'size_bytes', 'sha256'} or row['file'] != name+'.bin' or
                type(row['size_bytes']) is not int or row['size_bytes'] != size):
            raise ValueError(f'incompatible flash image: {name}')
        inventory['flash/'+row['file']] = {key: row[key] for key in ('size_bytes', 'sha256')}
    verify_required(source, inventory, seals)
    return {name: relative_artifact(source, 'flash/'+name+'.bin') for name in SIZES}


def save_flash_images(qmp, artifacts, result, state):
    validate_storage(state)
    started = time.perf_counter()
    final = artifacts.require('flash')
    if final.exists():
        raise FileExistsError('flash images already exist')
    pending = Path(tempfile.mkdtemp(prefix='.flash-', dir=artifacts.path))
    try:
        qmp.execute('qom-set', time.monotonic()+30,
                    {'path': '/machine', 'property': 'flash-byte-export', 'value': str(pending)})
        if {p.name for p in pending.iterdir()} != {name+'.bin' for name in SIZES}:
            raise ValueError('native flash export inventory differs')
        images = {}
        for name, size in SIZES.items():
            row = identity(pending/(name+'.bin'))
            if row['size_bytes'] != size:
                raise ValueError(f'native flash export size differs: {name}')
            images[name] = {'file': name+'.bin', **row}
        write_json(pending/'flash.json', {'schema_version': 1, 'geometry': GEOMETRY, 'images': images})
        for path in pending.iterdir():
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
        descriptor = os.open(pending, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
        os.rename(pending, final)
        descriptor = os.open(artifacts.path, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
        result['flash'] = {'manifest': 'flash/flash.json', 'unknown_byte_fill': 255,
                           'assumption': 'Unknown storage bytes exported as erased FF; model state unchanged.'}
        result['phases']['flash_save_seconds'] = time.perf_counter()-started
    finally:
        if pending.exists(): shutil.rmtree(pending)
