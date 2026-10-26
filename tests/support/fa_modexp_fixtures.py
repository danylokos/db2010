"""QEMU-only controls for calculated FA RSA replies, native DMA and guarded RX."""


import argparse


import json


import re


import struct


from pathlib import Path


from support.dsp_peer_fixtures import fixture, packet


from support.fa_dma_fixtures import arm, w


from support.fixtures import qemu_run


from support.paths import ROOT


PROFILE = 'wire20-modexp1024-peer'


def make(steps, request, enabled=True):
    s = fixture(steps, limit=len(steps)*3+200, source_bytes=224)
    s['dsp_peer']['profile'] = PROFILE if enabled else 'wire10-message-wire20-variable-dma-sink'
    if enabled:
        s['device_registers'] = [r for r in s['device_registers'] if r['address'] != 0xfa040014]
    s['regions'][0]['size'] = 8192
    s['executable_ranges'][0]['end'] = 0x3000
    s['regions'][1]['seeds'] = [{'address':0x4c000000, 'data':request + bytes(1088-len(request))}]
    s['regions'].append({'name':'fixture.destination', 'address':0x4c001000, 'size':4096, 'seeds':[]})
    return s


def transmit():
    return arm(control=0xa4492110) + [w(0xfa040014,0x18),w(0xfa04000c,0xdf)] + packet(15,544)


POLL = [('r',0xfa040000,0,4)]*120


ACK = [('r',0xfa040080+2*i,0,2) for i in range(32)] + [w(0xfa040004,0xfffffffe)]


def receive(destination=0x4c001000, control=0xa8492022):
    return [w(0xf2000008,4),
            *[w(0xf2000140+4*i,value) for i,value in enumerate(
                [0xfa040020,destination,0,control,0xf0c6])],
            w(0xf2000150,0xf0c7), w(0xfa040018,0x1000),
            w(0xfa04001c,34),w(0xfa040014,0x19)]


def expected(request):
    def field(offset):
        count = struct.unpack_from('<H',request,offset)[0]
        return int.from_bytes(request[offset+6:offset+2*count],'little')
    radix,montgomery,exponent,n = [field(o) for o in (2,268,534,800)]
    assert radix == pow(2,1024,n)
    base = montgomery * pow(1<<1024,-1,n) % n
    value = pow(base,exponent,n)
    used = max(1,(value.bit_length()+31)//32)
    return struct.pack('<HHHH',0xb01,used*2+3,0,max(0,(value>>(32*(used-1))).bit_length()-1)) + value.to_bytes(128,'little')
