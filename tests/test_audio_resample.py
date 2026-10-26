"""Deterministic sample rate conversion, stereo endpoints and fragmented input."""
from pathlib import Path
import math
import subprocess

import numpy as np
import pytest

from db2010.paths import ROOT


@pytest.fixture(scope='module')
def converter(tmp_path_factory):
    output=tmp_path_factory.mktemp('resample')/'convert'
    subprocess.run(['cc','-std=c11','-I'+str(ROOT/'qemu/hw/arm'),
        str(Path(__file__).parent/'support/audio_resample.c'),
        str(ROOT/'qemu/build/libsystem_arm.a.p/hw_arm_db2010-audio-resample.c.o'),
        '-o',str(output)],check=True,capture_output=True)
    def convert(pcm,chunk,rate=44100):
        return np.frombuffer(subprocess.run([output,str(chunk),str(rate)],input=pcm.astype('<i2').tobytes(),capture_output=True,check=True).stdout,dtype='<i2').reshape(-1,2)
    return convert


@pytest.mark.parametrize('count',[0,1,2,147,1152,44100])
@pytest.mark.parametrize('rate,scale',[(48000,1),(44100,160),(32000,3),(24000,2),(22050,320),(16000,3),(12000,4),(11025,640),(8000,6)])
def test_exact_ratio_duration_and_stereo_extremes(converter,count,rate,scale):
    pcm=np.tile(np.array([[-32768,32767]],dtype=np.int16),(count,1))
    output=converter(pcm,1152,rate)
    assert len(output)==(count*48000+rate-1)//rate
    assert np.array_equal(output,np.tile([[-32768,32767]],(len(output),1)))


@pytest.mark.parametrize('chunk',[1,3,146,147,500,1152])
@pytest.mark.parametrize('rate,scale',[(48000,1),(44100,160),(32000,3),(24000,2),(22050,320),(16000,3),(12000,4),(11025,640),(8000,6)])
def test_continuous_linear_ramp_is_independent_of_chunking(converter,chunk,rate,scale):
    x=np.arange(40,dtype=np.int16)*scale
    output=converter(np.stack([x,-x],axis=1),chunk,rate)
    step=rate//math.gcd(rate,48000)
    expected=np.minimum(np.arange(len(output))*step,39*scale)
    assert np.array_equal(output[:,0],expected)
    assert np.array_equal(output[:,1],-expected)


@pytest.mark.parametrize('rate',[48000,44100,32000,24000,22050,16000,12000,11025,8000])
def test_pitch_and_gain_at_one_khz(converter,rate):
    x=np.rint(np.sin(np.arange(rate)*2*np.pi*1000/rate)*16000).astype(np.int16)
    output=converter(np.stack([x,-x],axis=1),500,rate)
    assert len(output)==48000
    peak=np.argmax(np.abs(np.fft.rfft(output[:,0])))
    assert peak==1000
    assert .94<float(np.sqrt(np.mean(output[:,0].astype(float)**2))/np.sqrt(np.mean(x.astype(float)**2)))<1.01
    assert np.array_equal(output[:,0],-output[:,1])
