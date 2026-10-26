"""Compile the real shared access layer and exercise its independent contracts."""
from support.paths import RESOURCES
import subprocess

import pytest

from support.component_test import ROOT, component_inputs


@pytest.fixture(scope='module')
def access_maps(tmp_path_factory):
    exe = tmp_path_factory.mktemp('access-maps') / 'check'
    subprocess.run([
        'cc', '-Wall', '-Wextra', '-Werror',
        '-I' + str(ROOT / 'qemu/hw/arm'),
        str(RESOURCES/'checks/access_maps.c'),
        '-o', str(exe), *component_inputs('bus'),
    ], check=True)
    return exe


@pytest.mark.parametrize('case', ['layouts', 'actions', 'ownership', 'invalid'])
def test_access_map_contract(access_maps, case):
    subprocess.run([str(access_maps), case], check=True)
