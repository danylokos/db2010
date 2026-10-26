"""Bounded native encoded-word ingress; no decoder or returned credits."""
import struct

from support.mma_setup_fixtures import make as setup_state, setup, stages
from support.mma_ring_fixtures import POLL, ACK, CLEAR
from support.fa_dma_fixtures import arm
from support.dsp_peer_fixtures import packet
from support.fixtures import words as arm_words

PROFILE = 'wire20-modexp1024-mma-buffer-peer'


def configured():
    return stages()['registered']+setup()+POLL[:20]+ACK+CLEAR


def payload(count=256, *, flags=1, marker=1, version=1, timestamp=123456):
    words = (flags,marker,version,timestamp>>16,timestamp&65535,count-6)
    words += tuple((i*257+0x80ff)&65535 for i in range(count-6))
    size = (count*2+6+31)&~31
    raw = bytearray([0xa5]*size)
    struct.pack_into('<HH',raw,0,0x0804,count)
    struct.pack_into('<'+'H'*count,raw,4,*words)
    struct.pack_into('<H',raw,size-2,20)
    return bytes(raw), words


def transfer(raw):
    return arm(control=0xa4492000|len(raw)//4)+packet(20,len(raw)//2)


def make(steps, raw, latency=512):
    state = setup_state([],latency)
    state['dsp_peer']['profile'] = PROFILE
    # Inline literals avoid the older short fixture's 4KiB LDR pool limit.
    code = []
    cost = 0
    for op,address,value,width in steps:
        if op == 'delay':
            code.extend((0xe59f7008,0xe2577001,0x1afffffd,0xea000000,address))
            cost += address*2+2
            continue
        instruction = {('w',4):0xe5810000, ('w',2):0xe1c100b0,
                       ('r',4):0xe5912000, ('r',2):0xe1d120b0}[(op,width)]
        code.extend((0xe59f0008,0xe59f1008,instruction,0xea000001,value,address))
        cost += 4
    code.append(0xeafffffe)
    state['regions'][0]['size'] = 0x10000
    state['regions'][0]['seeds'][0]['data'] = arm_words(*code)
    state['executable_ranges'][0]['end'] = 0x11000
    state['instruction_limit'] = cost+500
    state['regions'][1]['seeds'] = [{'address':0x4c000000,'data':raw}]
    return state
