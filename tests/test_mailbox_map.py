"""FA mailbox descriptor parity and owner-controlled peer preflight/commit."""
from support.paths import RESOURCES
import subprocess

import pytest

from support.component_test import ROOT, component_inputs


def compile_check(source, exe, *extra, components=('fa-mailbox',)):
    subprocess.run([
        'cc', '-Wall', '-Wextra', '-Werror', '-Wno-sign-compare',
        '-I' + str(ROOT / 'qemu/hw/arm'), str(source), *map(str, extra),
        '-o', str(exe), *component_inputs(*components),
    ], check=True)


@pytest.fixture(scope='module')
def mailbox_map(tmp_path_factory):
    exe = tmp_path_factory.mktemp('mailbox-map') / 'check'
    compile_check(RESOURCES/'checks/mailbox_map.c', exe,
                  components=('fa-mailbox', 'transfer-links', 'dsp-peer'))
    return exe


@pytest.mark.parametrize('case', ['layout', 'lifecycle', 'peer'])
def test_mailbox_map_owner_contract(mailbox_map, case):
    subprocess.run([str(mailbox_map), case], check=True)


def test_mailbox_map_matches_sealed_c(tmp_path):
    source = tmp_path / 'sealed.c'
    source.write_bytes(subprocess.check_output([
        'git', '-C', str(ROOT / 'qemu'), 'show',
        '6f9e19e74a5f09c651779bc0438198b14208ae04:hw/arm/db2010-fa-mailbox.c',
    ]))
    obj = tmp_path / 'sealed.o'
    symbols = ('registers', 'contains', 'access', 'snapshot', 'consumed', 'reset')
    subprocess.run([
        'cc', '-I' + str(ROOT / 'qemu/hw/arm'),
        *[f'-Ddbfm_{name}=sealed_dbfm_{name}' for name in symbols],
        '-c', str(source), '-o', str(obj),
        *[arg for arg in component_inputs('fa-mailbox') if not arg.endswith('.c')],
    ], check=True)
    exe = tmp_path / 'parity'
    compile_check(RESOURCES/'checks/mailbox_parity.c', exe, obj)
    subprocess.run([str(exe)], check=True)
