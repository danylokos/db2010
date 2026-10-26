"""Native Demo Tour setup and its authenticated48-tap filter upload."""
from pathlib import Path
import struct
from support.mma_ring_fixtures import stages, POLL, CLEAR
from support.ringtone_fixtures import RECEIVE
from support.mma_playback_fixtures import make
from support.fa_wire10_fixtures import frame
from support.fa_dma_fixtures import arm
from support.dsp_peer_fixtures import packet
from support.mma_mode_fixtures import mode
from support.mma_playback_fixtures import encoded_packet

SETUP = [0xe00,3,1,1,0,3,1,3,0,0,1,0,0,32,0,0,0,0xffff,0xffff,1,0,1,0,0xcc01,18]
COEFFICIENTS = Path(__file__).resolve().parents[1]/'resources/audio/video-filter-3.le16'


def coefficient_packet():
    raw = bytearray([0xa5]*128)
    struct.pack_into('<4H', raw, 0, 0xe02, 0, 0, 48)
    raw[8:104] = COEFFICIENTS.read_bytes()
    struct.pack_into('<H', raw, 126, 18)
    return bytes(raw)


def configured():
    return stages()['registered'] + frame(SETUP) + RECEIVE


def upload():
    return arm(source=0x4c000000, control=0xa4492020) + packet(18, 64)


def offered():
    return configured() + upload() + POLL[:100] + CLEAR


def state(steps=None, raw=None):
    result = make(offered() if steps is None else steps,
                  coefficient_packet() if raw is None else raw)
    result['dsp_peer']['profile'] = 'wire20-modexp1024-mma-stream-peer'
    return result


def stream():
    import json
    source = COEFFICIENTS.with_name('video-first-16-aac.bin')
    data = source.read_bytes()
    lengths = json.loads(source.with_suffix('.json').read_text())['aac_unit_padded_bytes']
    raw = bytearray(coefficient_packet())
    parts = []
    at = 0
    for i, length in enumerate(lengths):
        body = encoded_packet(data[at:at+length], i*64000)
        at += length
        words = length//2+6
        if words < 28:
            part = frame(list(struct.unpack('<'+str(words+2)+'H',body[:4+words*2]))+[20])+POLL[:20]+CLEAR
        else:
            part = arm(source=0x4c000000+len(raw),control=0xa4492000|len(body)//4)+packet(20,len(body)//2)+POLL[:100]+CLEAR
        parts.append(part)
        raw.extend(body)
    delay = [('delay',600000,0,0)]
    steps = offered()+RECEIVE+parts[0]+mode(0)+RECEIVE+RECEIVE+delay+RECEIVE
    for part in parts[1:]:
        steps += part+mode(0)+RECEIVE+RECEIVE+delay+RECEIVE
    steps += frame([0x0804,6,2,1,1,0,0,0,20])+POLL[:20]+CLEAR+mode(0)+RECEIVE+RECEIVE+delay+RECEIVE
    result = state(steps,bytes(raw))
    result['record_diagnostics'] = False
    return result, steps, bytes(raw)
