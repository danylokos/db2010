"""Framed mailbox-only sends after the original bounded DMA peer contract."""


import argparse


import json


from pathlib import Path


from support.dsp_peer_fixtures import fixture,packet


from support.fa_dma_fixtures import arm,w


from support.fixtures import qemu_run


PROFILE='wire10-message-wire20-dma-sink'


def frame(payload,wire=10):
    return [w(0xfa040040,(len(payload)<<11)|wire,2)]+[
        w(0xfa040042+i*2,value,2)for i,value in enumerate(payload)]+[w(0xfa040000,1)]


def run(output):
    checks=[]
    prefix=arm()+[w(0xfa040014,0x18),w(0xfa04000c,0xdf)]+packet()+[
        ('r',0xfa040000,0,4)]*50+[('r',0xfa040010,0,4),w(0xf2000008,4)]
    def check(name,steps,reason,latency=2,extra=180):
        s=fixture(steps,latency=latency,limit=len(steps)*3+extra)
        s['dsp_peer']['profile']=PROFILE
        r=qemu_run(s,output/name,trace=True);assert r['stop']['reason']==reason,(name,r['stop'])
        checks.append({'name':name,'passed':True,'stop':r['stop']});return r
    for latency in [2,16]:
        for name,payload in [('observed-ring',[0x300,0x65,20]),
                             ('observed-next',[0x1101,0x65,20,0,0,0,22]),
                             ('full-capacity',list(range(31)))]:
            r=check(f'{name}-{latency}',prefix+frame(payload)+[('r',0xfa040000,0,4)]*20+
                    [('r',0xfa040010,0,4)],'instruction_limit',latency)
            p=r['dsp_peer'];assert p['requests']==p['mailbox_events']==2
            assert p['mailbox_only_messages']==p['transfer_events']==1
            assert p['event_status']==0 and r['registers'][2]==1
            assert bytes.fromhex(p['payload_hex'])==bytes(range(224))
            assert r['pl080']['channels'][2]['source']==0x4c0000e0
            assert r['pl080']['raw_tc']==0 and not r['fa_mailbox_tx']['busy']
    for name,steps,reason in [
        ('wire11',prefix+frame([0x300,0x65,20],11),'dsp_peer_packet_contract_unmodeled'),
        ('before-DMA',frame([0x300,0x65,20]),'dsp_peer_packet_contract_unmodeled'),
        ('zero-count',prefix+[w(0xfa040040,10,2),w(0xfa040000,1)],'fa_mailbox_header_unmodeled'),
        ('unknown-word',prefix+[w(0xfa040040,0x380a,2),w(0xfa040000,1)],'fa_mailbox_payload_unknown'),
        ('unmodeled-RX',prefix+frame([0x300,0x65,20])+[('r',0xfa040080,0,2)],'device_register_access_unmodeled'),
    ]:
        check(name,steps,reason)
    return {'count':len(checks),'checks':checks,'DSP_application_reply_supplied':False}
