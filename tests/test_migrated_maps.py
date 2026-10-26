"""Compiled differential checks against the frozen, pre-map C implementation."""
from support.paths import RESOURCES
import re
import subprocess

import pytest

from support.component_test import ROOT, component_inputs

BASELINE = '6f9e19e74a5f09c651779bc0438198b14208ae04'


@pytest.mark.parametrize('model', ['simple', 'rtc-retained', 'dma', 'dsp-peer', 'memory-contracts'])
def test_migrated_map_matches_sealed_c(tmp_path, model):
    source = tmp_path / 'sealed.c'
    old = subprocess.check_output([
        'git', '-C', str(ROOT / 'qemu'), 'show',
        f'{BASELINE}:hw/arm/db2010-{model}.c',
    ]).decode()
    source.write_text(old)
    # Rename exported definitions only; calls to other components remain real.
    symbols = re.findall(r'^(?:const\s+)?(?:char\s*\*|bool|void|QDict\s*\*|DB2010RegisterSpec)\s*(\w+)\s*[\[(=]', old, re.M)
    obj = tmp_path / 'sealed.o'
    flags = component_inputs(model, *(['nor'] if model == 'memory-contracts' else []))
    subprocess.run([
        'cc', '-I' + str(ROOT / 'qemu/hw/arm'),
        *[f'-D{name}=sealed_{name}' for name in symbols],
        '-c', str(source), '-o', str(obj),
        *[arg for arg in flags if not arg.endswith('.c')],
    ], check=True)
    exe = tmp_path / 'parity'
    subprocess.run([
        'cc', '-Wall', '-Wextra', '-Werror', '-Wno-sign-compare',
        '-I' + str(ROOT / 'qemu/hw/arm'),
        str(RESOURCES / f'checks/map_{model.replace("-", "_")}.c'),
        str(obj), '-o', str(exe), *flags,
    ], check=True)
    subprocess.run([str(exe)], check=True)


def test_configured_register_views(tmp_path):
    exe = tmp_path / 'bank'
    subprocess.run([
        'cc', '-Wall', '-Wextra', '-Werror', '-Wno-sign-compare',
        '-I' + str(ROOT / 'qemu/hw/arm'),
        str(RESOURCES/'checks/map_bank.c'), '-o', str(exe),
        *component_inputs('register-bank', 'timer'),
    ], check=True)
    subprocess.run([str(exe)], check=True)
