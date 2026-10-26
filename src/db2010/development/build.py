"""Build the pinned ARM engine out of tree and retain build provenance."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import resource
import shlex
import subprocess
import time
import sys
import shutil
from ..paths import ROOT, DEPENDENCIES, TRACE_SOURCE, qemu_source, emulator_build


def output(argv, cwd=ROOT):
    return subprocess.check_output(argv, cwd=cwd, text=True).strip()


def build_plugin(source, root=ROOT):
    """Compile our producer separately; freeze the exact source beside its record."""
    plugin = emulator_build(root)/'plugins/trace.so'
    plugin.parent.mkdir(parents=True, exist_ok=True)
    evidence = emulator_build(root)/'provenance/trace'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    evidence.mkdir(parents=True)
    frozen = evidence/'trace.c'
    shutil.copyfile(TRACE_SOURCE, frozen)
    flags = shlex.split(output(['pkg-config', '--cflags', '--libs', 'glib-2.0']))
    # Match QEMU's Meson shared_module: macOS resolves the QEMU API at load time.
    link_flags = ['-bundle', '-Wl,-undefined,dynamic_lookup'] if sys.platform == 'darwin' else ['-shared']
    command = ['cc', '-std=c11', '-D_POSIX_C_SOURCE=200809L', '-O2', '-Wall', '-Wextra',
               '-Wno-unused-parameter', *link_flags, '-fPIC', '-I'+str(source/'include/plugins'),
               str(frozen), '-o', str(plugin), *flags]
    with (evidence/'build.log').open('w') as log:
        result = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise SystemExit(f'Trace plugin build failed ({result.returncode}); see {evidence}/build.log')
    manifest = {'format_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
                'source_file': 'trace.c', 'source_sha256': hashlib.sha256(frozen.read_bytes()).hexdigest(),
                'binary_sha256': hashlib.sha256(plugin.read_bytes()).hexdigest(),
                'engine_binary_sha256': hashlib.sha256((source/'build/qemu-system-arm').read_bytes()).hexdigest(),
                'qemu_plugin_header_sha256': hashlib.sha256((source/'include/plugins/qemu-plugin.h').read_bytes()).hexdigest(),
                'command': command, 'compiler': output(['cc', '--version'])}
    (evidence/'build.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(f'Built {plugin}; provenance: {evidence}/build.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=ROOT, help='emulator checkout')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--configure', action='store_true')
    args = parser.parse_args()
    root = args.repo.resolve()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    lock = json.loads((DEPENDENCIES / 'source-lock.json').read_text())
    source = qemu_source(root)
    from .source import check
    check(root, lock)
    build = source / 'build'
    evidence = emulator_build(root) / 'provenance/qemu' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    build.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True)
    commands = []
    if args.configure or not (build / 'build.ninja').exists():
        commands.append([str(source / 'configure'), '--python=' + sys.executable,
                         *lock['configure']])
    commands.append(['ninja', '-j', str(args.jobs), 'qemu-system-arm'])
    started = time.perf_counter()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    with (evidence / 'build.log').open('w') as log:
        for command in commands:
            result = subprocess.run(command, cwd=build, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                raise SystemExit(f'Build failed ({result.returncode}); see {evidence}/build.log')
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    binary = build / 'qemu-system-arm'
    subprojects = {}
    for path in sorted((source / 'subprojects').iterdir()):
        if (path / '.git').exists():
            subprojects[path.name] = output(['git', 'rev-parse', 'HEAD'], path)
    manifest = {
        'format_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
        'source_lock': lock, 'source_commit': output(['git', 'rev-parse', 'HEAD'], source),
        'source_status': output(['git', 'status', '--porcelain'], source),
        'source_diff_sha256': hashlib.sha256(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=source)).hexdigest(),
        'subprojects': subprojects, 'commands': commands,
        'compiler': output(['cc', '--version']), 'ninja': output(['ninja', '--version']),
        'python': output([sys.executable, '--version']),
        'build_python_packages': output([str(build / 'pyvenv/bin/python'), '-m', 'pip',
                                         '--disable-pip-version-check', '--no-cache-dir', 'freeze']),
        'pkg_config': output(['pkg-config', '--modversion', 'glib-2.0', 'gmodule-2.0', 'pixman-1']),
        'elapsed_seconds': time.perf_counter() - started,
        'user_seconds': after.ru_utime - before.ru_utime,
        'system_seconds': after.ru_stime - before.ru_stime,
        'children_peak_rss_kib': after.ru_maxrss,
        'binary': str(binary.relative_to(root)), 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'version': output([str(binary), '--version']),
    }
    (evidence/'build.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(f'Built {binary}; provenance: {evidence}/build.json')
    build_plugin(source, root)


if __name__ == '__main__':
    main()
