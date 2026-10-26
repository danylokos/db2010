"""Compile the out-of-tree producer and retain useful failure diagnostics."""
import hashlib
import json
import subprocess

import pytest

from db2010.development import build
from db2010.paths import qemu_source


def test_plugin_build_and_load(tmp_path):
    source = qemu_source()
    build.build_plugin(source, tmp_path)
    plugin = tmp_path/'build/plugins/trace.so'
    record = next((tmp_path/'build/provenance/trace').glob('*/build.json'))
    manifest = json.loads(record.read_text())
    assert manifest['binary_sha256'] == hashlib.sha256(plugin.read_bytes()).hexdigest()
    assert manifest['source_sha256'] == hashlib.sha256((record.parent/'trace.c').read_bytes()).hexdigest()
    assert manifest['engine_binary_sha256'] == hashlib.sha256((source/'build/qemu-system-arm').read_bytes()).hexdigest()
    # Reaching our install callback proves that QEMU could resolve its API imports.
    result = subprocess.run([str(source/'build/qemu-system-arm'), '-machine', 'none',
                             '-nodefaults', '-display', 'none', '-plugin', str(plugin)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'db2010 trace requires a bounded socket= path' in result.stderr


def test_plugin_build_failure_reports_log(tmp_path, monkeypatch):
    broken = tmp_path/'broken.c'
    broken.write_text('#error deliberate_plugin_build_failure\n')
    monkeypatch.setattr(build, 'TRACE_SOURCE', broken)
    with pytest.raises(SystemExit, match='Trace plugin build failed') as failure:
        build.build_plugin(qemu_source(), tmp_path)
    log = next((tmp_path/'build/provenance/trace').glob('*/build.log'))
    assert str(log) in str(failure.value)
    assert 'deliberate_plugin_build_failure' in log.read_text()
    assert (log.parent/'trace.c').read_bytes() == broken.read_bytes()
    assert not (log.parent/'build.json').exists()
