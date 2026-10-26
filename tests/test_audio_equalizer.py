"""Recovered equalizer control envelopes; no DSP coefficient arithmetic claim."""
import struct

import pytest

from support.audio_control_fixtures import make, transport_prefix, ACK, POLL, CLEAR
from support.fa_wire10_fixtures import frame
from support.fixtures import qemu_run


def equalizer(*, enabled=1, sections=9, section=0, coefficients=(16384,-15741,0,-15741,0), selector=0, gain=0):
    if not enabled:
        # Native disable initializes only opcode/selector/enable. Remaining
        # allocation bytes have no semantic value and need not be zero.
        return [0x090e,selector,0,0xffff,0xeeee,0xdddd,0xcccc,12]
    return [0x090e,selector,enabled,sections,section,gain,*[x&65535 for x in coefficients],12]


@pytest.mark.parametrize('selector,sections,section,coefficients,gain',[
    (0,9,0,(16384,-15741,0,-15741,0),0),
    (0,9,8,(-32768,32767,-1,0,1),65535),
    (1,1,0,(1,2,3,4,5),27),
])
def test_equalizer_acknowledges_five_word_sections_without_echoing_coefficients(tmp_path,selector,sections,section,coefficients,gain):
    request=equalizer(selector=selector,sections=sections,section=section,coefficients=coefficients,gain=gain)
    steps=transport_prefix()+frame(request)+POLL[:20]
    offered=qemu_run(make(steps),tmp_path/'offered',trace=False)
    assert offered['stop']['reason']=='instruction_limit',offered['stop']
    peer=offered['dsp_peer']
    assert peer['audio_requests']==1 and peer['audio_replies']==0
    assert peer['rx_mailbox_ready'] and peer['rx_inline'] and not peer['audio_playback_modeled']
    assert struct.unpack('<32H',bytes.fromhex(peer['rx_mailbox_hex']))[:3]==(0x100a,0x090f,12)
    final=qemu_run(make(steps+ACK+CLEAR),tmp_path/'acked',trace=False)
    assert final['stop']['reason']=='instruction_limit',final['stop']
    assert final['dsp_peer']['audio_replies']==1 and not final['dsp_peer']['rx_mailbox_ready']


def test_equalizer_sections_then_disable_use_separate_native_replies(tmp_path):
    steps=transport_prefix()
    for section in (0,4,8):steps+=frame(equalizer(section=section))+POLL[:2]+ACK+CLEAR
    steps+=frame(equalizer(enabled=0))+POLL[:2]+ACK+CLEAR
    result=qemu_run(make(steps),tmp_path/'bank',trace=False)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    assert result['dsp_peer']['audio_requests']==result['dsp_peer']['audio_replies']==4
    assert not result['dsp_peer']['rx_mailbox_ready']


@pytest.mark.parametrize('payload,reason',[
    (equalizer(enabled=2),'dsp_peer_audio_filter_unmodeled'),
    (equalizer(sections=0),'dsp_peer_audio_equalizer_section_unmodeled'),
    (equalizer(sections=10),'dsp_peer_audio_equalizer_section_unmodeled'),
    (equalizer(sections=3,section=3),'dsp_peer_audio_equalizer_section_unmodeled'),
    (equalizer()[:-2]+[12],'dsp_peer_audio_packet_unmodeled'),
    (equalizer()[:-1]+[0,12],'dsp_peer_audio_packet_unmodeled'),
    (equalizer(enabled=0)[:-2]+[12],'dsp_peer_audio_packet_unmodeled'),
    ([0x090e,12],'dsp_peer_audio_filter_unmodeled'),
    ([0x0902,0,0,12],'dsp_peer_audio_opcode_unmodeled'),
])
def test_unsupported_equalizer_contracts_still_stop_before_acceptance(tmp_path,payload,reason):
    result=qemu_run(make(transport_prefix()+frame(payload)+POLL[:20]),tmp_path/'guard',trace=False)
    assert result['stop']['reason']==reason,result['stop']
    assert result['dsp_peer']['audio_requests']==result['dsp_peer']['audio_replies']==0
