"""Independent pitch, envelope, voice ownership and packet-boundary checks."""
from array import array
from pathlib import Path
import subprocess

import pytest
from db2010.paths import ROOT


@pytest.fixture(scope='module')
def synth(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('midi-synth')
    # Only the module's QEMU standard-header umbrella is replaced for this
    # host-only harness; compile the unchanged production source.
    (tmp/'qemu').mkdir()
    (tmp/'qemu/osdep.h').write_text('#include <stdint.h>\n#include <stdbool.h>\n#include <stddef.h>\n')
    binary = tmp/'synth'
    subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
        '-I'+str(tmp), '-I'+str(ROOT/'qemu/hw/arm'),
        str(ROOT/'qemu/hw/arm/db2010-midi-synth.c'),
        str(Path(__file__).parent/'support/midi_synth.c'), '-o', str(binary)],
        check=True, capture_output=True)
    def run(script, ok=True):
        result = subprocess.run([binary], input=script.encode(), capture_output=True)
        if ok:
            assert result.returncode == 0, result.stderr
        pcm = array('h', result.stdout)
        assert pcm[::2] == pcm[1::2]
        return pcm[::2], result
    return run


def test_a4_pitch_velocity_and_fixed_headroom(synth):
    loud, _ = synth('e 144 69 127\nr 48000\n')
    soft, _ = synth('e 144 69 63\nr 48000\n')
    crossings = sum(a <= 0 < b for a, b in zip(loud, loud[1:]))
    assert crossings == 440
    assert 810 <= max(loud) <= 820
    assert 0.49 < max(soft)/max(loud) < 0.51


def test_release_ends_at_64ms_and_no_noteoff_cross_channel(synth):
    pcm, _ = synth('e 144 60 127\nr 100\ne 145 60 0\nr 100\ne 128 60 0\nr 3072\nr 100\n')
    assert any(pcm[100:200])
    assert any(pcm[200:2200])
    assert not any(pcm[-100:])


def test_same_key_releases_oldest_held_voice(synth):
    _, result = synth('e 144 60 100\ne 144 60 120\ne 144 60 0\ns\n')
    assert result.stderr == b'2 0 0:0:60:0 1:0:60:1\n'


def test_steal_releasing_before_oldest_held(synth):
    script = ''.join(f'e 144 {i} 127\n' for i in range(40))
    _, result = synth(script+'e 128 35 0\ne 145 99 100\ns\ne 145 98 100\ns\n')
    lines = result.stderr.decode().splitlines()
    assert lines[0].startswith('41 1 0:0:0:1 ')
    assert '35:1:99:1' in lines[0]
    assert lines[1].startswith('42 2 0:1:98:1 ')


def test_packet_boundaries_and_programs_do_not_change_triangle(synth):
    whole, _ = synth('e 153 69 127\nr 5000\n')
    split, _ = synth('e 201 81 0\ne 153 69 127\nr 17\nr 1000\nr 3983\n')
    assert whole == split


def test_channel_volume_and_two_semitone_bend(synth):
    silent, _ = synth('e 176 7 0\ne 144 69 127\nr 48000\n')
    assert not any(silent)
    bent, _ = synth('e 224 127 127\ne 144 69 127\nr 48000\n')
    crossings = sum(a <= 0 < b for a, b in zip(bent, bent[1:]))
    assert 493 <= crossings <= 494


@pytest.mark.parametrize('event', ['176 64 100', '208 64 0', '240 0 0', '144 128 1'])
def test_unimplemented_controls_are_guarded(synth, event):
    _, result = synth(f'e {event}\n', ok=False)
    assert result.returncode == 2
    assert result.stderr == b'dsp_peer_midi_event_unmodeled\n'
