"""Regenerate compact synthetic MP3 vectors; libmp3lame is a generator input only.

Run from the project root after compiling audio_reference.c as documented in
the generic-audio investigation. Normal tests use retained bytes and hashes.
"""
import ctypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np


def main():
    reference = Path(sys.argv[1]).resolve()
    library = Path('/usr/lib/x86_64-linux-gnu/libmp3lame.so.0').resolve()
    lame = ctypes.CDLL(str(library))
    lame.lame_init.restype = ctypes.c_void_p
    lame.get_lame_version.restype = ctypes.c_char_p
    for name in ['lame_set_in_samplerate', 'lame_set_out_samplerate',
                 'lame_set_num_channels', 'lame_set_mode', 'lame_set_VBR',
                 'lame_set_VBR_q', 'lame_set_brate', 'lame_set_error_protection',
                 'lame_set_bWriteVbrTag']:
        getattr(lame, name).argtypes = [ctypes.c_void_p, ctypes.c_int]
    lame.lame_init_params.argtypes = [ctypes.c_void_p]
    lame.lame_encode_buffer_interleaved.argtypes = [ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_short), ctypes.c_int,
        ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
    lame.lame_encode_flush.argtypes = [ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
    lame.lame_close.argtypes = [ctypes.c_void_p]
    destination = Path(__file__).resolve().parents[1] / 'resources/audio/generic'
    destination.mkdir(exist_ok=True)
    cases = []
    for rate in [8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000]:
        for channels in [1, 2]:
            handle = lame.lame_init()
            settings = dict(in_samplerate=rate, out_samplerate=rate,
                num_channels=2, mode=3 if channels == 1 else 1,
                VBR=4 if channels == 2 else 0, VBR_q=4,
                brate=96 if rate >= 32000 else 48 if rate >= 16000 else 24,
                error_protection=channels == 1, bWriteVbrTag=0)
            for key, value in settings.items():
                assert getattr(lame, 'lame_set_' + key)(handle, int(value)) == 0
            assert lame.lame_init_params(handle) == 0
            t = np.arange(rate // 5) / rate
            # Chirped L/R signals with a silent interval exercise VBR choices.
            left = np.sin(2 * np.pi * (233 * t + 1300 * t*t)) * 15000
            right = np.sin(2 * np.pi * 701 * t) * 11000
            left[(t > .06) & (t < .10)] = 0
            right[(t > .06) & (t < .10)] = 0
            pcm = np.rint(np.stack([left, right], axis=1)).astype('<i2')
            output = (ctypes.c_ubyte * 65536)()
            n = lame.lame_encode_buffer_interleaved(handle,
                pcm.ctypes.data_as(ctypes.POINTER(ctypes.c_short)), len(pcm),
                output, len(output))
            assert n >= 0
            encoded = bytes(output[:n])
            n = lame.lame_encode_flush(handle, output, len(output))
            assert n >= 0
            encoded += bytes(output[:n])
            lame.lame_close(handle)
            path = destination / f'{rate}-{channels}ch.mp3'
            path.write_bytes(encoded)
            raw = subprocess.check_output([reference, path, str(rate), str(channels)])
            source = np.frombuffer(raw, dtype='<i2').reshape(-1, channels).astype(np.int64)
            if channels == 1:
                source = np.repeat(source, 2, axis=1)
            positions = np.arange((len(source)*48000+rate-1)//rate)*rate
            left = positions // 48000
            fraction = positions % 48000
            right = np.minimum(left+1, len(source)-1)
            values = source[left]*(48000-fraction[:, None])+source[right]*fraction[:, None]
            converted = (np.sign(values)*((np.abs(values)+24000)//48000)).astype('<i2')
            cases.append(dict(file=path.name, rate=rate, channels=channels,
                settings=settings, encoded_sha256=hashlib.sha256(encoded).hexdigest(),
                source_frames=len(source), output_frames=len(converted),
                output_sha256=hashlib.sha256(converted.tobytes()).hexdigest(),
                nonzero_samples=int(np.count_nonzero(converted))))
    manifest = dict(generator=Path(__file__).name,
        generator_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        encoder_version=lame.get_lame_version().decode(),
        encoder_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
        reference_source_sha256=hashlib.sha256(Path(__file__).with_name('audio_reference.c').read_bytes()).hexdigest(),
        minimp3_sha256=hashlib.sha256((Path(__file__).resolve().parents[2]/'qemu/hw/arm/db2010-minimp3/minimp3.h').read_bytes()).hexdigest(),
        numpy_version=np.__version__,
        policy='Whole-file scalar decode; global positions m*rate/48000; stereo duplication; integer linear interpolation, ties away from zero; final sample hold.',
        cases=cases)
    (destination/'vectors.json').write_text(json.dumps(manifest, indent=2)+'\n')


if __name__ == '__main__':
    main()
