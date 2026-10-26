"""Recovered Opera setup and exact native filter2 coefficient DMA."""
from pathlib import Path
import struct
from support.ringtone_fixtures import RECEIVE,DELAY
from support.mma_ring_fixtures import stages,POLL,CLEAR
from support.mma_playback_fixtures import make
from support.fa_wire10_fixtures import frame
from support.fa_dma_fixtures import arm
from support.dsp_peer_fixtures import packet

RESOURCES=Path(__file__).resolve().parents[1]/'resources/audio'
SETUP=[0xe00,4,1,2,1,2,1,2,0,0,1,0,0,32,0,0,0,0xffff,0xffff,1,0,1,0,0xcc02,18]
COEFFICIENTS=RESOURCES/'opera-filter-2.le16'


def coefficient_packet():
    data=bytearray([0xa5]*96)
    struct.pack_into('<4H',data,0,0xe02,0,0,32)
    data[8:72]=COEFFICIENTS.read_bytes()
    struct.pack_into('<H',data,94,18)
    return bytes(data)


def configured():return stages()['registered']+frame(SETUP)+RECEIVE

def upload(source=0x4c000000):return arm(source=source,control=0xa4492018)+packet(18,48)

def coefficients_offered():return configured()+upload()+POLL[:100]+CLEAR

def state(steps,raw=None):
    result=make(steps,coefficient_packet()if raw is None else raw)
    result['dsp_peer']['profile']='wire20-modexp1024-mma-stream-peer'
    return result


SOURCE=RESOURCES/'opera-first-16-frames.mp3'


def stream(source=SOURCE):
    from support.ringtone_fixtures import stream as ringtone
    _,steps,raw=ringtone(source=source,setup=SETUP)
    prefix=configured()
    assert steps[:len(prefix)]==prefix
    steps=prefix+upload(0x4c000000+len(raw))+POLL[:100]+CLEAR+RECEIVE+steps[len(prefix):]
    raw+=coefficient_packet()
    return state(steps,raw),steps,raw
