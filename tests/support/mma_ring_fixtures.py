"""Native ring4 announcement/registration controls; data and codec remain guarded."""


import argparse


import json


import struct


from pathlib import Path


from support.mma_mode_fixtures import make as mode_state, mode, transport_prefix, POLL, ACK, CLEAR


from support.fa_wire10_fixtures import frame


from support.fa_dma_fixtures import arm


from support.dsp_peer_fixtures import packet


from support.fixtures import qemu_run


from db2010.diagnostics.artifacts import identity


from db2010.paths import ROOT


PROFILE = 'wire20-modexp1024-mma-ring-peer'


def make(steps, latency=2):
    state = mode_state(steps)
    state['dsp_peer'].update(profile=PROFILE, latency_ns=latency)
    return state


def stages():
    offered = transport_prefix()+mode(3)+POLL[:20]
    return dict(announcement=offered, mode_offer=offered+ACK,
                mode_response=offered+ACK+CLEAR+POLL[:20],
                registered=offered+ACK+CLEAR+POLL[:20]+ACK+CLEAR+frame([0x0404,240,20])+POLL[:20])


def run(output):
    checks = []
    def check(name, steps, reason='instruction_limit', latency=2):
        result = qemu_run(make(steps,latency),output/name,trace=True)
        assert result['stop']['reason']==reason,(name,result['stop'])
        checks.append(dict(name=name,passed=True,stop=result['stop']))
        return result['dsp_peer']
    parts = stages()
    peer = check('capacity-before-mode-acceptance',parts['announcement'])
    assert peer['rx_ring4'] and not peer['rx_mma'] and peer['rx_mailbox_ready']
    assert struct.unpack('<32H',bytes.fromhex(peer['rx_mailbox_hex']))[:4]==(0x180a,0x0304,801,20)
    assert not peer['ring4_announced'] and peer['ring4_announcements']==0
    assert peer['mma_requests']==1 and peer['mma_replies']==peer['audio_replies']==0
    peer = check('mode-after-capacity-ack',parts['mode_response'])
    assert not peer['rx_ring4'] and peer['rx_mma'] and peer['ring4_announced']
    assert struct.unpack('<32H',bytes.fromhex(peer['rx_mailbox_hex']))[:4]==(0x180a,0x0e05,1,18)
    assert peer['ring4_announcements']==1 and peer['mma_replies']==peer['audio_replies']==0
    peer = check('native-registration',parts['registered'])
    assert peer['ring4_registered'] and peer['ring4_threshold']==240 and peer['ring4_registrations']==1
    assert peer['mma_requests']==peer['mma_replies']==1 and peer['audio_replies']==0
    assert not peer['rx_mailbox_ready'] and not peer['audio_playback_modeled']
    bootstrap = frame([0x0300,0x65,20])+POLL[:20]+CLEAR
    peer = check('inherited-ring0-bootstrap',transport_prefix()+bootstrap)
    assert peer['ring4_announcements']==peer['ring4_registrations']==0
    for value in (1,4):
        steps=transport_prefix()+mode(value)+POLL[:20]
        if value==4:steps+=ACK+CLEAR
        peer=check(f'mode{value}-no-announcement',steps)
        assert peer['ring4_announcements']==0 and not peer['rx_ring4']
    check('registration-before-announcement',transport_prefix()+frame([0x0404,240,20]),
          'dsp_peer_ring_registration_unmodeled')
    for threshold in (0,239,241,800):
        check(f'wrong-threshold-{threshold}',parts['mode_response']+ACK+CLEAR+frame([0x0404,threshold,20]),
              'dsp_peer_ring_registration_unmodeled')
    check('duplicate-registration',parts['registered']+frame([0x0404,240,20]),
          'dsp_peer_ring_registration_unmodeled')
    check('repeated-mode3',parts['registered']+mode(3),'dsp_peer_ring_lifecycle_unmodeled')
    for name,payload in (('wrong-subchannel',[0x0405,240,20]),('unknown-operation',[0x0504,1,20]),
                         ('data-not-supported',[0x0804,1,0,20]),('short',[0x0404,20]),
                         ('long',[0x0404,240,0,20]),('wrong-ring0-capacity',[0x0300,0x66,20])):
        check(name,parts['mode_response']+ACK+CLEAR+frame(payload),'dsp_peer_ring_packet_unmodeled')
    check('DMA-not-supported',parts['registered']+arm(control=0xa4492008)+packet(20,16),
          'dsp_peer_ring_packet_unmodeled')
    check('decoder-configuration-not-supported',parts['registered']+frame([0x0e00]+[0]*23+[18]),
          'dsp_peer_mma_packet_unmodeled')
    peer=check('separated-announcement-and-mode-latency512',parts['registered'],latency=512)
    assert peer['ring4_announcements']==peer['ring4_registrations']==peer['mma_replies']==1
    return dict(count=len(checks),checks=checks,engine=identity(ROOT/'qemu/build/qemu-system-arm'),
                capacity_policy_halfwords=801,usable_halfwords=800,playback_modeled=False)
