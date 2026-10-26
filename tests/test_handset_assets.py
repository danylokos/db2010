"""Source identity, preserved topology, LCD fit and offline asset preparation."""
import hashlib
import json
from pathlib import Path
import struct
from zipfile import ZipFile

import numpy as np
import pytest

from db2010.ui.keypad import BUTTONS

from db2010.paths import ROOT, PACKAGE
ASSETS = PACKAGE/"ui/assets"


def glb_header(path):
    raw = path.read_bytes()
    assert struct.unpack_from('<III', raw) == (0x46546c67, 2, len(raw))
    length, kind = struct.unpack_from('<II', raw, 12)
    assert kind == 0x4e4f534a
    return json.loads(raw[20:20+length])


def test_packaged_handset_preserves_faces_contacts_and_lcd_fit():
    manifest = json.loads((ASSETS/'model/manifest.json').read_text())
    raw = (ASSETS/'model/w800i.glb').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == manifest['glb']['sha256']
    assert sum(manifest['original_triangles'].values()) == 172110
    assert sum(m['triangles'] for m in manifest['mappings'].values()) == 172110
    contacts = {m['contact'] for m in manifest['mappings'].values()}-{None, 'joystick'}
    assert contacts | {'up', 'right', 'down', 'left', 'select', 'camera-focus'} == BUTTONS
    header = glb_header(ASSETS/'model/w800i.glb')
    meshes = {m['name']: m for m in header['meshes']}
    assert 'lcd' in meshes and 'joystick' in meshes
    position = meshes['lcd']['primitives'][0]['attributes']['POSITION']
    bounds = header['accessors'][position]
    size = np.array(bounds['max'])-bounds['min']
    assert size[0]/size[1] == pytest.approx(176/220)
    assert size[2] == 0
    assert manifest['screen']['flip_y'] is False
    assert all('bufferView' in image and 'uri' not in image for image in header['images'])
    vendor = json.loads((ASSETS/'vendor/manifest.json').read_text())
    for name, item in vendor['files'].items():
        assert hashlib.sha256((ASSETS/'vendor'/name).read_bytes()).hexdigest() == item['sha256']
    assert len(raw)+sum(item['bytes'] for item in vendor['files'].values()) < 15*1024**2





def test_frozen_runner_sources_include_binary_assets_licenses_and_manifests(tmp_path):
    from db2010.diagnostics.recording import save_provenance
    from db2010.diagnostics.artifacts import artifact
    save_provenance(ROOT, tmp_path)
    with ZipFile(artifact(tmp_path, 'runner-sources.zip')) as archive:
        for path in ASSETS.rglob('*'):
            if path.is_file():
                assert archive.read('src/db2010/'+path.relative_to(PACKAGE).as_posix()) == path.read_bytes()
