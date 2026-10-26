"""Initialize and check the commit-pinned QEMU submodule using ordinary Git."""
import argparse
import json
from pathlib import Path
import subprocess

from ..paths import ROOT, DEPENDENCIES, qemu_source
LOCK = DEPENDENCIES / 'source-lock.json'


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def tag_commit(source, tag):
    try:
        return git(source, 'rev-parse', '--verify', '--quiet', f'refs/tags/{tag}^{{commit}}')
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
        return None


def init(root, lock):
    subprocess.run(['git', '-C', str(root), 'submodule', 'update', '--init', 'qemu'], check=True)
    source = qemu_source(root)
    if tag_commit(source, lock['tag']) is None:
        subprocess.run(['git', '-C', str(source), 'fetch', '--no-tags', lock['upstream'],
                        'tag', lock['tag']], check=True)
    return check(root, lock)


def check(root, lock):
    source = qemu_source(root)
    if not (source/'.git').exists():
        raise RuntimeError('QEMU submodule is not initialized; run python -m db2010.development.source init')
    if git(source, 'status', '--porcelain'):
        raise RuntimeError('QEMU has uncommitted changes; commit and pin them before building')
    if git(source, 'rev-parse', 'HEAD') != lock['fork_commit']:
        raise RuntimeError('QEMU revision differs from source-lock.json; update the submodule pin and lock together')
    base = tag_commit(source, lock['tag'])
    if base is None:
        raise RuntimeError(f"QEMU release tag {lock['tag']} is missing; "
                           'run python -m db2010.development.source init to fetch it')
    if base != lock['base_commit']:
        raise RuntimeError('QEMU release tag differs from the pinned base commit')
    git(source, 'merge-base', '--is-ancestor', lock['base_commit'], 'HEAD')
    pin = git(root, 'ls-files', '--stage', 'qemu').split()
    if len(pin) < 2 or pin[:2] != ['160000', lock['fork_commit']]:
        raise RuntimeError('QEMU submodule index pin differs from source-lock.json')
    return lock['fork_commit']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'check'])
    parser.add_argument('--repo', type=Path, default=ROOT, help='emulator checkout')
    args = parser.parse_args()
    root = args.repo.resolve()
    lock = json.loads(LOCK.read_text())
    if lock['format_version'] != 1:
        raise ValueError('Unsupported source lock')
    verify = init if args.action == 'init' else check
    print(f'QEMU pin verified: {verify(root, lock)}')


if __name__ == '__main__':
    main()
