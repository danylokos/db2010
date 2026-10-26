"""Native synthetic MMA mode acceptance, no-reply cleanup and unknown guards."""


import argparse


import json


import struct


from pathlib import Path


from support.audio_control_fixtures import make as audio_state, transport_prefix, inline, ACK, POLL, CLEAR


from support.fa_wire10_fixtures import frame


from support.dsp_peer_fixtures import packet


from support.fa_dma_fixtures import arm


from support.fixtures import qemu_run


from db2010.diagnostics.artifacts import identity


from db2010.paths import ROOT


PROFILE = 'wire20-modexp1024-mma-mode-peer'


def make(steps, enabled=True):
    state = audio_state(steps)
    if enabled:
        state['dsp_peer']['profile'] = PROFILE
    return state


def mode(value):
    return frame([0x0e04,value,18])


def run(output):
    checks = []
    prefix = transport_prefix()

    def check(name,steps,reason='instruction_limit',enabled=True):
        result = qemu_run(make(steps,enabled),output/name,trace=True)
        assert result['stop']['reason'] == reason,(name,result['stop'])
        checks.append(dict(name=name,passed=True,stop=result['stop']))
        return result

    for value in (3,4):
        offered = prefix+mode(value)+POLL[:20]
        result = check(f'offer-{value}',offered)
        peer = result['dsp_peer']
        assert peer['mma_mode']==value and peer['mma_requests']==1 and peer['mma_replies']==0
        assert peer['rx_mma'] and peer['rx_inline'] and peer['rx_mailbox_ready']
        assert struct.unpack('<32H',bytes.fromhex(peer['rx_mailbox_hex']))[:4] == (0x180a,0x0e05,1,18)
        assert peer['rx_bytes']==peer['audio_requests']==peer['audio_replies']==0
        result = check(f'ack-{value}',offered+ACK+CLEAR)
        peer = result['dsp_peer']
        assert peer['mma_replies']==1 and not peer['rx_mailbox_ready'] and peer['phase']==4
        assert peer['event_status']==0
    result = check('mode1-no-response',prefix+mode(1)+POLL[:20])
    peer = result['dsp_peer']
    assert peer['mma_mode']==1 and peer['mma_requests']==1 and peer['mma_replies']==0
    assert not peer['rx_mailbox_ready'] and peer['phase']==4
    result = check('native-lifecycle-and-audio-interleave',prefix+mode(3)+POLL[:20]+ACK+CLEAR+
                   inline(0x0900,6)+POLL[:20]+ACK+CLEAR+mode(1)+POLL[:20]+CLEAR+
                   mode(4)+POLL[:20]+ACK+CLEAR)
    peer = result['dsp_peer']
    assert peer['mma_requests']==3 and peer['mma_replies']==2 and peer['mma_mode']==4
    assert peer['audio_requests']==peer['audio_replies']==1
    for value in (0,2,5,0xffff):
        result = check(f'unmodeled-mode-{value}',prefix+mode(value)+POLL[:20],
                       'dsp_peer_mma_mode_unmodeled')
        assert result['dsp_peer']['mma_requests']==0
    for name,payload in (('unknown-opcode',[0x0e06,0,18]),
                         ('short',[0x0e04,18]),('long',[0x0e04,3,0,18])):
        result = check(name,prefix+frame(payload)+POLL[:20], 'dsp_peer_mma_packet_unmodeled')
        assert result['dsp_peer']['mma_requests']==0
    result = check('occupied-reply-guard',prefix+mode(3)+POLL[:20]+mode(4),
                   'dsp_peer_packet_contract_unmodeled')
    assert result['dsp_peer']['mma_requests']==1 and result['dsp_peer']['mma_replies']==0
    result = check('unmodeled-mode-DMA',prefix+arm(control=0xa4492008)+packet(18,16),
                   'dsp_peer_mma_packet_unmodeled')
    assert result['dsp_peer']['mma_requests']==0
    result = check('disabled-peer-retains-silent-transport',prefix+mode(3)+POLL[:20],enabled=False)
    assert not result['dsp_peer']['rx_mailbox_ready'] and 'mma_requests' not in result['dsp_peer']
    return dict(count=len(checks),checks=checks,synthetic_mode_acceptance=True,
                playback_modeled=False,engine=identity(ROOT/'qemu/build/qemu-system-arm'))
