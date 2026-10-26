"""Exact startup setup acceptance and virtual-clock reply through native FA."""
import copy
import json
import struct
import time

import pytest

from support.mma_setup_fixtures import make, setup, SETUP, stages
from support.mma_ring_fixtures import make as ring_state, transport_prefix, POLL, ACK, CLEAR
from support.mma_mode_fixtures import mode
from support.fa_wire10_fixtures import frame
from support.fixtures import qemu_run, words
from test_native_checkpoint import engine, stopped, command, migration, comparable


def offered():
    return stages()['registered']+setup()+POLL[:20]


@pytest.mark.parametrize('latency', [2,512])
def test_native_setup_reply_status_clock_and_ack(tmp_path, latency):
    state = make(offered(), latency)
    result = qemu_run(state, tmp_path/'offer', trace=False)
    assert result['stop']['reason'] == 'instruction_limit'
    peer = result['dsp_peer']
    packet = struct.unpack('<32H', bytes.fromhex(peer['rx_mailbox_hex']))
    assert packet[:3] == (0x280a,0x0e01,1) and packet[5] == 18
    clock_us = packet[3]*65536+packet[4]
    assert 0 <= result['virtual_ns']//1000-clock_us < 100
    assert peer['rx_mma'] and peer['rx_mailbox_ready'] and peer['mma_mode'] == 3
    assert peer['mma_requests'] == 2 and peer['mma_replies'] == 1
    assert not peer['audio_playback_modeled']
    result = qemu_run(make(offered()+ACK+CLEAR, latency), tmp_path/'ack', trace=False)
    peer = result['dsp_peer']
    assert not peer['rx_mailbox_ready'] and peer['mma_replies'] == 2
    assert peer['ring4_registrations'] == 1 and peer['ring4_threshold'] == 240


def test_setup_clock_high_and_low_words_use_virtual_microseconds(tmp_path):
    state = make(offered())
    iterations = 0x90000
    code = state['regions'][0]['seeds'][0]
    code['data'] = words(0xe2577001,0x1afffffd)+code['data']
    state['registers'][7] = iterations
    state['instruction_limit'] += iterations*2
    result = qemu_run(state, tmp_path/'clock', trace=False)
    assert result['stop']['reason'] == 'instruction_limit'
    packet = struct.unpack('<32H', bytes.fromhex(result['dsp_peer']['rx_mailbox_hex']))
    assert packet[3] > 0
    clock_us = packet[3]*65536+packet[4]
    assert 0 <= result['virtual_ns']//1000-clock_us < 100


@pytest.mark.parametrize('index,value', [
    (1,7), (2,0), (3,1), (4,0), (5,0), (16,0), (19,1), (21,0), (23,0xcc01),
])
@pytest.mark.parametrize('research_guards', [False, True])
def test_unsupported_setup_parameters_rejected_before_acceptance(tmp_path,index,value,research_guards):
    changed = list(SETUP)
    changed[index] = value
    state = make(stages()['registered']+setup(changed))
    if not research_guards:
        state.pop('instruction_limit')
    result = qemu_run(state, tmp_path/'guard', trace=False, research_guards=research_guards)
    assert result['stop']['reason'] == 'dsp_peer_mma_setup_unmodeled'
    assert result['dsp_peer']['mma_requests'] == result['dsp_peer']['mma_replies'] == 1


@pytest.mark.parametrize('changed', [SETUP[:-2]+(18,), SETUP[:-1]+(0,18)])
def test_setup_lengths_remain_guarded(tmp_path, changed):
    result = qemu_run(make(stages()['registered']+setup(changed)), tmp_path/'length', trace=False)
    assert result['stop']['reason'] == 'dsp_peer_mma_setup_unmodeled'


@pytest.mark.parametrize('prefix', [transport_prefix(), transport_prefix()+mode(1)+POLL[:20],
                                   transport_prefix()+mode(3)+POLL[:20]+ACK+CLEAR+POLL[:20]+ACK+CLEAR])
def test_setup_requires_mode3_and_native_ring_registration(tmp_path,prefix):
    result = qemu_run(make(prefix+setup()), tmp_path/'lifecycle', trace=False)
    assert result['stop']['reason'] == 'dsp_peer_mma_setup_lifecycle_unmodeled'


def test_setup_does_not_replace_pending_reply(tmp_path):
    result = qemu_run(make(stages()['announcement']+setup()),tmp_path/'pending',trace=False)
    assert result['stop']['reason'] == 'dsp_peer_packet_contract_unmodeled'
    assert result['dsp_peer']['rx_ring4']


def test_setup_is_opt_in(tmp_path):
    result = qemu_run(ring_state(offered()),tmp_path/'old-profile',trace=False)
    assert result['stop']['reason'] == 'dsp_peer_mma_packet_unmodeled'


@pytest.mark.parametrize('packet,reason', [([0x0804,1,0,20],'dsp_peer_ring_packet_unmodeled'),
                                         ([0x0e02,0,18],'dsp_peer_mma_packet_unmodeled'),
                                         ([0x0e06,18],'dsp_peer_mma_packet_unmodeled')])
def test_setup_does_not_accept_unrecovered_data_or_completion(tmp_path, packet, reason):
    result = qemu_run(make(offered()+ACK+CLEAR+frame(packet)),tmp_path/'data',trace=False)
    assert result['stop']['reason'] == reason
    assert not result['dsp_peer']['audio_playback_modeled']


@pytest.mark.parametrize('phase', ['submitted','offered','acknowledged'])
def test_setup_pending_reply_clock_and_checkpoint_continuation(tmp_path, phase):
    base = stages()['registered']+setup()
    portions = {'submitted':base, 'offered':base+POLL[:20],
                'acknowledged':base+POLL[:20]+ACK+CLEAR}
    state = make(portions['acknowledged']+POLL[:20])
    state['record_diagnostics'] = False
    with engine(state,tmp_path/'whole') as vm:
        whole = stopped(vm)
    first = copy.deepcopy(state)
    first['instruction_limit'] = len(portions[phase])*3
    saved = tmp_path/'setup.vmstate'
    with engine(first,tmp_path/'prefix') as vm:
        start = stopped(vm)
        assert start['dsp_peer']['phase'] == {'submitted':1,'offered':6,'acknowledged':4}[phase]
        layout = json.loads(command(vm,'qom-get',{'path':'/machine','property':'checkpoint-layout'}))
        migration(vm,'migrate',saved)
    continuation = copy.deepcopy(state)
    continuation['instruction_limit'] -= first['instruction_limit']
    continuation['checkpoint_tcm'] = layout
    with engine(continuation,tmp_path/'resumed',incoming=True) as vm:
        migration(vm,'migrate-incoming',saved)
        restored = vm.snapshot(time.monotonic()+5)
        assert restored['dsp_peer'] == start['dsp_peer']
        final = stopped(vm)
    assert comparable(final) == comparable(whole)
    assert final['dsp_peer']['mma_requests'] == final['dsp_peer']['mma_replies'] == 2
