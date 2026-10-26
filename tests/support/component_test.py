"""Compile focused C checks against the independently compiled QEMU components."""
from support.paths import RESOURCES
from pathlib import Path
import shlex
import subprocess

from support.paths import ROOT


def component_inputs(*names):
    names = tuple(dict.fromkeys((*names, *(['pmic'] if 'i2c' in names else []), *(['dma', 'fa-mailbox', 'audio-samples', 'audio-output', 'audio-codec', 'audio-resample', 'midi-stream', 'midi-synth'] if 'dsp-peer' in names else []), 'contract')))
    source = ROOT/'qemu'
    build = ROOT/'qemu/build'
    target_flags = []
    if 'memory-contracts' in names or 'nor' in names:
        target_flags = ['-I'+str(source), '-I'+str(source/'target/arm'),
                        '-DCPU_DEFS_H', '-DCOMPILING_SYSTEM_VS_USER', '-DCONFIG_SOFTMMU']
    return [*target_flags, '-ffunction-sections', '-fdata-sections', '-Wl,--gc-sections', '-std=gnu11', '-D_GNU_SOURCE', '-Wno-unused-parameter', '-Wno-missing-field-initializers', '-I'+str(source/'include'), '-I'+str(build), '-I'+str(build/'include'),
            *(str(source/'hw/arm'/f'db2010-{name}.c') for name in names),
            *([str(RESOURCES/'checks/absent_chardev.c')] if 'dsp-peer' in names else []),
            *([str(RESOURCES/'checks/virtual_clock_stub.c'),str(source/'hw/arm/db2010-clock.c')] if set(names) & {'timer','rtc','dsp-peer'} else []),
            *([str(build/'hw/arm/db2010-faad2/libdb2010-faad-lc.a'), str(build/'libqemuutil.a'), '-lm'] if 'dsp-peer' in names else []),
            *shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'glib-2.0'], text=True))]
