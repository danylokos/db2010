"""Optional source/dependency capture and evidence sealing."""
import json
import platform
import shutil
import subprocess
import time
import sys
from ..paths import PACKAGE, PROJECT, DEPENDENCIES, TRACE_SOURCE
import zipfile
from .artifacts import artifact as artifact_path

def save_provenance(root, output):
    try:
        revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL).strip()
        status = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True)
    except subprocess.CalledProcessError:
        revision, status = None, None
    artifact_path(output, 'emulator-source.json', create=True).write_text(json.dumps({
        'repository': 'git@github.com:danylokos/db2010.git', 'commit': revision,
        'status': status}, indent=2)+'\n')
    # Every persistent family needs reproducible engine and host identities.
    for source in (DEPENDENCIES/'source-lock.json',):
        shutil.copyfile(source, artifact_path(output, source.name, create=True))
    artifact_path(output, 'dependencies.json', create=True).write_text(json.dumps({
        'python': platform.python_version(), 'packages': subprocess.check_output([
            sys.executable, '-m', 'pip', '--disable-pip-version-check',
            '--no-cache-dir', 'freeze'], text=True).splitlines()}, indent=2)+'\n')
    with zipfile.ZipFile(artifact_path(output, 'runner-sources.zip', create=True), 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PACKAGE.rglob('*')):
            if (path.is_file() and not {'__pycache__', '_build_resources'} & set(path.parts)
                    and path.suffix != '.pyc'):
                archive.write(path, 'src/db2010/'+path.relative_to(PACKAGE).as_posix())
        for source in (DEPENDENCIES/'source-lock.json',):
            archive.write(source, 'dependencies/qemu/'+source.name)
        archive.write(TRACE_SOURCE, 'native/trace/trace.c')
        packaging = PROJECT if (PROJECT/'pyproject.toml').is_file() else PACKAGE/'_build_resources/project'
        for name in ('pyproject.toml', 'setup.py', 'MANIFEST.in', 'README.md', '.gitmodules'):
            archive.write(packaging/name, name)


def save_diagnostics(root, output, result, started, finalizing):
    result['phases']['finalization_seconds'] = time.perf_counter()-finalizing
    result['phases']['total_seconds'] = time.perf_counter()-started
    artifact_path(output, 'result.json', create=True).write_text(json.dumps(result, indent=2)+'\n')
