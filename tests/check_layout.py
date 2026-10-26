"""Verify checkout and wheel boot/trace/restore with no parent research inputs.

Run from the emulator root: .venv/bin/python tests/check_layout.py
Requires the already built QEMU/plugin and the environment’s build tools.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from zipfile import ZipFile

from db2010.paths import ROOT, PROJECT, emulator_build, qemu_source, DEFAULT_EXPERIMENT
from db2010.diagnostics.artifacts import identity
from db2010.diagnostics.verify import verify


def main():
    with tempfile.TemporaryDirectory(prefix='db2010-isolation-') as temporary:
        root = Path(temporary)
        project = root/'emulator'
        ignored = shutil.ignore_patterns('build', 'dist', '__pycache__', '.pytest_cache', '*.egg-info', '.venv', '.git')
        def ignore(directory, names):
            return set(ignored(directory, names)) | ({'qemu'} if Path(directory) == PROJECT else set())
        shutil.copytree(PROJECT, project, ignore=ignore)
        configured = json.loads(DEFAULT_EXPERIMENT.read_text())['firmware_images']
        for image in configured.values():
            target = project/image['input_path']
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(ROOT/image['input_path'], target)
        engine = project/'qemu/build/qemu-system-arm'
        engine.parent.mkdir(parents=True)
        shutil.copy2(qemu_source()/'build/qemu-system-arm', engine)
        plugin = project/'build/plugins/trace.so'
        plugin.parent.mkdir(parents=True)
        shutil.copy2(emulator_build()/'plugins/trace.so', plugin)
        for group, target in (('qemu', engine), ('trace', plugin)):
            records = [p for p in (emulator_build()/'provenance'/group).glob('*/build.json')
                       if json.loads(p.read_text())['binary_sha256'] == identity(target)['sha256']]
            record = sorted(records)[-1]
            shutil.copytree(record.parent, project/'build/provenance'/group/record.parent.name)

        def command(arguments, *, env=None):
            result = subprocess.run([sys.executable, *arguments], cwd=root, env=env,
                                    text=True, capture_output=True, timeout=90)
            if result.returncode:
                raise AssertionError(f'{arguments}:\n{result.stdout}\n{result.stderr}')
            return result

        command(['-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--no-index',
                 '--wheel-dir', str(root/'wheels'), str(project)])
        wheel = next((root/'wheels').glob('*.whl'))
        with ZipFile(wheel) as archive:
            for name in ('db2010/resources/boot.json',
                         'db2010/resources/non-provisioned-boot.json',
                         'db2010/_build_resources/qemu/source-lock.json',
                         'db2010/_build_resources/trace/trace.c',
                         'db2010/_build_resources/project/pyproject.toml',
                         'db2010/ui/assets/model/w800i.glb'):
                assert archive.read(name)
            assert not any(n.startswith(('tools/', 'tests/')) for n in archive.namelist())
        command(['-m', 'pip', 'install', '--no-deps', '--target', str(root/'installed'), str(wheel)])
        outcomes = []
        for mode, package in (('checkout', project/'src'), ('wheel', root/'installed')):
            env = {**os.environ, 'PYTHONPATH': str(package)}
            base = ['-m', 'db2010', '--research-guards', '--repo', str(project), '--seconds', '20',
                    '--json-summary', '--no-progress', '--diagnostics', '--snapshot', '--save-flash']
            for start, extra in (('fresh', ['--trace', '--limit', '200000']),
                                 ('restore', ['--restore', mode+'-fresh', '--limit', '200010']),
                                 ('flash', ['--load-flash', mode+'-fresh', '--limit', '200000']),
                                 ('non-provisioned', ['--non-provisioned', '--limit', '200000'])):
                label = mode+'-'+start
                result = command([*base, '--label', label, *extra], env=env)
                summary = json.loads(result.stderr)
                assert summary['success'] and summary['stop']['reason'] == 'instruction_limit'
                run = project/'artifacts'/label
                assert verify(run)['integrity'] == 'passed'
                assert {p.name for p in (run/'snapshots/final').iterdir()} == {'snapshot.json','state.vmstate'}
                assert not (run/'inputs/start').exists()
                assert {p.name for p in (run/'flash').iterdir()} == {
                    'flash.json', 'main.bin', 'fs.bin', 'gdfs.bin', 'nand-main.bin', 'nand-spare.bin'}
                manifest = json.loads((run/'run.json').read_text())
                assert manifest['external_inputs']
                assert all(Path(path).is_relative_to(root) for path in manifest['external_inputs'])
                with ZipFile(run/'provenance/runner-sources.zip') as archive:
                    for name in ('pyproject.toml', 'native/trace/trace.c',
                                 'src/db2010/resources/execution-contract.json'):
                        assert archive.read(name)
                outcomes.append({'mode': mode, 'start': start, 'stop': summary['stop'],
                                 'trace_rows': summary['trace_rows']})
        assert not any((root/name).exists() for name in ('tools', 'derived', 'references', 'AGENTS.md'))
        print(json.dumps({'isolated_layout': 'passed', 'runs': outcomes}, indent=2))


if __name__ == '__main__':
    main()
