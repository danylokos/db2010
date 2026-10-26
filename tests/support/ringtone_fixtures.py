"""Recovered44.1kHz setup and actual first eight guest-authenticated MP3 frames."""
from pathlib import Path
import struct
from support.mma_setup_fixtures import SETUP
from support.mma_ring_fixtures import stages, POLL, ACK, CLEAR
from support.mma_playback_fixtures import encoded_packet, make as playback_state
from support.mma_mode_fixtures import mode
from support.fa_wire10_fixtures import frame
from support.fa_dma_fixtures import arm
from support.dsp_peer_fixtures import packet

SOURCE=Path(__file__).resolve().parents[1]/'resources/audio/ringtone-first-eight-frames.mp3'
SETUP44=list(SETUP);SETUP44[1]=7;SETUP44[5]=2
MONO_SOURCE=SOURCE.with_name('bulls-eye-first-eight-frames.mp3')
COO_SOURCE=SOURCE.with_name('coo-coo-first-four-frames.mp3')
SETUP_MONO=list(SETUP44);SETUP_MONO[2]=0;SETUP_MONO[21]=0xffff
DELAY=[('delay',220000,0,0)] #28.16ms, longer than a converted1152-frame block.
RECEIVE=POLL[:20]+ACK+CLEAR


def stream(count=8,source=SOURCE,tail=b'',setup=SETUP44):
    # A418-byte transfer contains two64kbit/s mono frames. Allow both output
    # deadlines to elapse before the fixture reads the next consumed-word credit.
    delay=[('delay',600000,0,0)] if setup[2]==0 or setup[1]==4 else DELAY
    data=source.read_bytes()[:count*418]+tail
    raw=bytearray();parts=[]
    for offset in range(0,len(data),418):
        chunk=data[offset:offset+418]
        if len(chunk)%2:chunk+=b'\0'
        body=encoded_packet(chunk,offset//418*26000)
        words=len(chunk)//2+6
        if words<28:
            parts.append(frame(list(struct.unpack('<'+str(words+2)+'H',body[:4+words*2]))+[20])+POLL[:20]+CLEAR)
        else:
            parts.append(arm(source=0x4c000000+len(raw),control=0xa4492000|len(body)//4)+packet(20,len(body)//2)+POLL[:100]+CLEAR)
        raw.extend(body)
    steps=stages()['registered']+frame(setup)+RECEIVE+parts[0]+mode(0)+RECEIVE+RECEIVE+delay
    for part in parts[1:]:steps+=part+RECEIVE+delay
    steps+=frame([0x0804,6,2,1,1,0,0,0,20])+POLL[:20]+CLEAR+RECEIVE+delay+RECEIVE
    state=playback_state(steps,bytes(raw));state['dsp_peer']['profile']='wire20-modexp1024-mma-stream-peer'
    return state,steps,bytes(raw)
