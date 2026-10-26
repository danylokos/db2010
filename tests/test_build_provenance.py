"""Traces must retain an authenticated producer built for the selected engine."""
from support.guarded import run as guarded_run
import json
from pathlib import Path
import shutil

import pytest

from db2010.paths import ROOT, emulator_build, qemu_source
from db2010.execution import runner
from db2010.diagnostics.artifacts import identity
from test_runner import synthetic_preparation


@pytest.mark.parametrize('changed', ['plugin', 'source', 'engine'])
def test_trace_rejects_mismatched_build_inputs(tmp_path, monkeypatch, changed):
    build = tmp_path/'build'
    (build/'plugins').mkdir(parents=True)
    shutil.copy2(emulator_build()/'plugins/trace.so', build/'plugins/trace.so')
    engine_hash = identity(qemu_source()/'build/qemu-system-arm')['sha256']
    for group, expected in (('qemu', engine_hash),
                            ('trace', identity(build/'plugins/trace.so')['sha256'])):
        records = [p for p in (emulator_build()/'provenance'/group).glob('*/build.json')
                   if json.loads(p.read_text())['binary_sha256'] == expected]
        record = sorted(records)[-1]
        shutil.copytree(record.parent, build/'provenance'/group/'record')
    trace = build/'provenance/trace/record'
    if changed == 'plugin':
        with (build/'plugins/trace.so').open('ab') as stream:
            stream.write(b'changed')
    elif changed == 'source':
        with (trace/'trace.c').open('a') as stream:
            stream.write('\n/* changed */\n')
    else:
        record = json.loads((trace/'build.json').read_text())
        record['engine_binary_sha256'] = '0'*64
        (trace/'build.json').write_text(json.dumps(record))
    monkeypatch.setattr(runner, 'emulator_build', lambda root: build)
    monkeypatch.setattr(runner, 'prepare', synthetic_preparation)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, trace=True)
    assert isinstance(failure, ValueError)
    assert 'trace producer' in str(failure)
    assert result.get('qemu_returncode') is None
