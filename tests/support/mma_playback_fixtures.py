"""Real encoded fixture bytes through guest DMA, FA replies and virtual time."""
import struct
from pathlib import Path

from support.mma_buffer_fixtures import make as buffer_state, configured, POLL, CLEAR
from support.fa_dma_fixtures import arm
from support.dsp_peer_fixtures import packet
from support.mma_mode_fixtures import mode
from support.mma_ring_fixtures import ACK
from support.fa_wire10_fixtures import frame

PROFILE = 'wire20-modexp1024-mma-playback-peer'
SOURCE = Path(__file__).resolve().parents[1]/'resources/audio/startup-first-eight-frames.mp3'
DELAY = [('delay',190000,0,0)]  # 24.32ms at icount shift6.


def encoded_packet(data, timestamp=0, marker=1):
    assert len(data)%2 == 0
    count = len(data)//2+6
    raw = bytearray([0xa5]*((count*2+6+31)&~31))
    struct.pack_into('<8H',raw,0,0x0804,count,1,marker,1,timestamp>>16,timestamp&65535,len(data)//2)
    for i in range(0,len(data),2):raw[16+i:18+i]=data[i:i+2][::-1]
    struct.pack_into('<H',raw,len(raw)-2,20)
    return bytes(raw)


def make(steps,raw):
    state = buffer_state(steps,raw)
    state['dsp_peer']['profile'] = PROFILE
    state['regions'][1]['size'] = 16384
    size=max(0x10000,(len(state['regions'][0]['seeds'][0]['data'])+4095)&~4095)
    state['regions'][0]['size']=size
    state['executable_ranges'][0]['end']=0x1000+size
    return state


def source_packets(data):
    raw=bytearray()
    transfers=[]
    for offset in range(0,len(data),500):
        chunk=data[offset:offset+500]
        body=encoded_packet(chunk,(offset+len(chunk)-1)//960*24000,int(offset%960 in (0,500)))
        transfers.append(arm(source=0x4c000000+len(raw),control=0xa4492000|len(body)//4)+packet(20,len(body)//2)+POLL[:100]+CLEAR)
        raw.extend(body)
    return bytes(raw),transfers


def started():
    raw,parts=source_packets(SOURCE.read_bytes()[:1000])
    steps=configured()+parts[0]+parts[1]+mode(0)+POLL[:20]+ACK+CLEAR
    return raw,steps


def stream(frames=6):
    raw,parts=source_packets(SOURCE.read_bytes()[:frames*960])
    steps=configured()+parts[0]+parts[1]+mode(0)+POLL[:20]+ACK+CLEAR+POLL[:20]+ACK+CLEAR
    for i in range(2,len(parts),2):
        steps+=parts[i]
        if i+1<len(parts):steps+=parts[i+1]
        steps+=DELAY+POLL[:20]+ACK+CLEAR
    # Original counted ring EOF: six metadata words, no source halfwords.
    eof=frame([0x0804,6,2,1,1,0,0,0,20])
    steps+=eof+POLL[:20]+CLEAR+DELAY+POLL[:20]+ACK+CLEAR+POLL[:20]+ACK+CLEAR
    return raw,steps
