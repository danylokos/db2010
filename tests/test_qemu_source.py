"""Source checks stay offline; initialization obtains missing upstream tags."""
import subprocess

import pytest

from db2010.development.source import check, init


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


@pytest.fixture
def pinned_source(tmp_path):
    root = tmp_path/'checkout'; root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.name', 'Fixture')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    qemu = root/'qemu'; qemu.mkdir()
    git(qemu, 'init', '-q', '-b', 'db2010')
    git(qemu, 'config', 'user.name', 'Fixture')
    git(qemu, 'config', 'user.email', 'fixture@example.invalid')
    version = qemu/'VERSION'; version.write_text('base\n')
    git(qemu, 'add', 'VERSION'); git(qemu, 'commit', '-qm', 'base')
    git(qemu, 'tag', 'v-test')
    base = git(qemu, 'rev-parse', 'HEAD')
    version.write_text('fork\n'); git(qemu, 'commit', '-qam', 'fork')
    head = git(qemu, 'rev-parse', 'HEAD')
    lock = {'tag': 'v-test', 'base_commit': base, 'fork_commit': head}
    git(root, 'update-index', '--add', '--cacheinfo', f'160000,{head},qemu')
    return root, lock


def test_submodule_pin_and_dirty_revision_guards(pinned_source):
    root, lock = pinned_source
    qemu = root/'qemu'; version = qemu/'VERSION'
    assert check(root, lock) == lock['fork_commit']
    version.write_text('dirty\n')
    with pytest.raises(RuntimeError, match='uncommitted changes'):
        check(root, lock)
    version.write_text('fork\n')
    with pytest.raises(RuntimeError, match='revision differs'):
        check(root, {**lock, 'fork_commit': lock['base_commit']})
    with pytest.raises(RuntimeError, match='release tag'):
        check(root, {**lock, 'base_commit': '0'*40})
    git(qemu, 'tag', '-d', lock['tag'])
    with pytest.raises(RuntimeError, match='release tag .* missing; run python -m db2010.development.source init'):
        check(root, lock)
    git(qemu, 'tag', lock['tag'], lock['base_commit'])
    git(root, 'update-index', '--cacheinfo', f"160000,{lock['base_commit']},qemu")
    with pytest.raises(RuntimeError, match='index pin'):
        check(root, lock)
    with pytest.raises(RuntimeError, match='not initialized'):
        check(root/'missing', lock)


def test_init_fetches_missing_tag_from_upstream(pinned_source, tmp_path, monkeypatch):
    root, lock = pinned_source
    qemu = root/'qemu'
    upstream = tmp_path/'upstream.git'
    # Only the upstream has the annotated release tag; the fork has the commits.
    git(qemu, 'tag', '-f', '-a', '-m', 'release', lock['tag'], lock['base_commit'])
    git(tmp_path, 'clone', '--bare', str(qemu), str(upstream))
    git(qemu, 'tag', '-d', lock['tag'])
    git(root, 'config', '-f', '.gitmodules', 'submodule.qemu.path', 'qemu')
    git(root, 'config', '-f', '.gitmodules', 'submodule.qemu.url', str(qemu))
    git(root, 'add', '.gitmodules')
    git(root, 'commit', '-qm', 'pin fork')
    fresh = tmp_path/'fresh'
    git(tmp_path, 'clone', str(root), str(fresh))
    monkeypatch.setenv('GIT_ALLOW_PROTOCOL', 'file')
    lock = {**lock, 'upstream': str(upstream)}

    assert init(fresh, lock) == lock['fork_commit']
    assert check(fresh, lock) == lock['fork_commit']
    assert git(fresh/'qemu', 'cat-file', '-t', f"refs/tags/{lock['tag']}") == 'tag'
    assert git(fresh/'qemu', 'rev-parse', 'HEAD') == lock['fork_commit']

    # Existing tags need no upstream connection, and conflicts are not replaced.
    offline_lock = {**lock, 'upstream': str(tmp_path/'unavailable.git')}
    assert init(fresh, offline_lock) == lock['fork_commit']
    git(fresh/'qemu', 'tag', '-f', lock['tag'], lock['fork_commit'])
    with pytest.raises(RuntimeError, match='release tag differs'):
        init(fresh, offline_lock)
    assert git(fresh/'qemu', 'rev-parse', f"refs/tags/{lock['tag']}") == lock['fork_commit']
