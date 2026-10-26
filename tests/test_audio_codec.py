"""Format coverage uses synthetic streams, independent of ringtone identities."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from db2010.paths import ROOT

RESOURCES = Path(__file__).parent / 'resources/audio'
VECTORS = json.loads((RESOURCES/'generic/vectors.json').read_text())['cases']
RATES = [8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000]


@pytest.fixture(scope='module')
def codec(tmp_path_factory):
    binary = tmp_path_factory.mktemp('codec')/'decode'
    subprocess.run(['cc', '-std=c11', '-O2', '-I'+str(ROOT/'qemu/hw/arm'),
        str(Path(__file__).parent/'support/audio_codec.c'),
        str(ROOT/'qemu/build/libsystem_arm.a.p/hw_arm_db2010-audio-codec.c.o'),
        str(ROOT/'qemu/build/libsystem_arm.a.p/hw_arm_db2010-audio-resample.c.o'),
        str(ROOT/'qemu/build/hw/arm/db2010-faad2/libdb2010-faad-lc.a'),
        '-lm', '-o', binary], check=True, capture_output=True)
    def decode(raw, rate, channels, chunk=500, source_codec=0):
        return subprocess.run([binary, str(rate), str(channels), str(chunk), str(source_codec)],
                              input=raw, capture_output=True)
    return decode


def silence_frame(rate, bitrate_index, mode, crc, padding):
    version = 3 if rate >= 32000 else 2 if rate >= 16000 else 0
    rates = [44100, 48000, 32000] if version == 3 else [22050, 24000, 16000] if version == 2 else [11025, 12000, 8000]
    bitrates = ([0,32,40,48,56,64,80,96,112,128,160,192,224,256,320] if version == 3 else
                [0,8,16,24,32,40,48,56,64,80,96,112,128,144,160])
    size = (144000 if version == 3 else 72000)*bitrates[bitrate_index]//rate+padding
    header = bytes([255, 0xe0 | version << 3 | 2 | (not crc),
                    bitrate_index << 4 | rates.index(rate) << 2 | padding << 1,
                    mode << 6])
    return header + bytes(size-4)


@pytest.mark.parametrize('rate', RATES)
@pytest.mark.parametrize('mode', [0, 1, 2, 3])
@pytest.mark.parametrize('crc', [False, True])
def test_all_indexed_bitrates_modes_crc_and_padding_in_one_vbr_stream(codec, rate, mode, crc):
    # Zero side information needs no reservoir or audio file. Protection-bit
    # coverage here does not claim CRC validation; generated vectors use LAME CRC.
    raw = b''.join(silence_frame(rate, index, mode, crc, index % 2) for index in range(1,15))
    result = codec(raw, rate, 1 if mode == 3 else 2, chunk=1588)
    assert result.returncode == 0, result.stderr
    samples = 1152 if rate >= 32000 else 576
    assert result.stderr == b'frames=14\n'
    assert len(result.stdout) == ((14*samples*48000+rate-1)//rate)*4
    assert not any(result.stdout)


@pytest.mark.parametrize('case', VECTORS, ids=lambda c:c['file'])
@pytest.mark.parametrize('chunk', [1, 7, 500, 1588])
def test_generated_nonzero_cbr_crc_and_vbr_waveforms(codec, case, chunk):
    raw = (RESOURCES/'generic'/case['file']).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == case['encoded_sha256']
    result = codec(raw, case['rate'], case['channels'], chunk)
    assert result.returncode == 0, result.stderr
    assert len(result.stdout) == case['output_frames']*4
    assert hashlib.sha256(result.stdout).hexdigest() == case['output_sha256']
    assert case['nonzero_samples'] > 0


@pytest.mark.parametrize('header', [
    '00fb9000', # sync
    'fff99000', # reserved version
    'fffd9000', # Layer II
    'fffb0000', # free format needs a different framing service
    'fffbf000', # reserved bitrate
    'fffb9c00', # reserved sample rate
    'fffb90c0', # mono contradicts declared stereo
    'fffb9400', #48kHz contradicts declared44.1kHz
])
def test_invalid_headers_and_setup_mismatches_stop_without_resync(codec, header):
    result = codec(bytes.fromhex(header)+bytes(1500), 44100, 2)
    assert result.returncode == 2
    assert result.stderr.strip() == b'dsp_peer_audio_source_format_unmodeled'
    assert not result.stdout


@pytest.mark.parametrize('tail', [b'\0\0', b'\1', b'\xff\xfb\x90', b'\xff\xfb\x90\0'])
def test_invalid_tails_are_not_hidden(codec, tail):
    result = codec(silence_frame(44100,9,0,False,0)+tail,44100,2)
    assert result.returncode == 2


def test_one_zero_transport_byte_is_valid_for_identity_rate_too(codec):
    raw = silence_frame(48000,14,0,False,0)
    first = codec(raw,48000,2)
    aligned = codec(raw+b'\0',48000,2)
    assert first.returncode == aligned.returncode == 0
    assert first.stdout == aligned.stdout


@pytest.mark.parametrize('rate', RATES)
def test_bitrate_padding_protection_and_stereo_mode_can_change_per_frame(codec,rate):
    raw=b''.join(silence_frame(rate,index,index%3,index%2==0,index%2) for index in range(1,15))
    result=codec(raw,rate,2,chunk=7)
    assert result.returncode==0,result.stderr
    assert result.stderr==b'frames=14\n'
    assert not any(result.stdout)
