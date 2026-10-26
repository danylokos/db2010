"""Differential checks against the authenticated task 074 resume engine's C."""
from support.paths import RESOURCES
import re
import subprocess

import pytest

from support.component_test import ROOT, component_inputs

BASELINE = '954c913347cd6b2246046f2dc859c27e482bc89f'


@pytest.mark.parametrize('model', ['f500-port', 'f500-control', 'fa-mailbox', 'fa-transfer', 'dsp-peer', 'dma', 'timer', 'pdi', 'panel'])
def test_typed_handlers_match_sealed_c(tmp_path, model):
    old = subprocess.check_output([
        'git', '-C', str(ROOT / 'qemu'), 'show',
        f'{BASELINE}:hw/arm/db2010-{model}.c',
    ]).decode()
    source = tmp_path / 'sealed.c'
    source.write_text(old.replace('(*s->time.count) + 1;', '(*s->time.count) + 64;') if model=='dsp-peer' else old)
    symbols = re.findall(r'^(?:const\s+)?(?:char\s*\*|bool\s*\*?|void|QDict\s*\*|gint|DB2010\w+\s*\*?)\s*(\w+)\s*[\[(=]', old, re.M)
    flags = component_inputs(model, *(['dma', 'fa-mailbox'] if model == 'dsp-peer' else []))
    obj = tmp_path / 'sealed.o'
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
        str(RESOURCES / f'checks/handlers_{model.replace("-", "_")}.c'),
        str(obj), '-o', str(exe), *flags,
    ], check=True)
    subprocess.run([str(exe)], check=True)
