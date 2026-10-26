"""Opt-in original MIDI setup reply over native FA mailbox/IRQ, no OS injection."""
import copy
import json
import struct
import time

import pytest

from support.audio_control_fixtures import make, transport_prefix, POLL, ACK, CLEAR
from support.fa_wire10_fixtures import frame
from support.fixtures import qemu_run
from support.fa_dma_fixtures import arm
from support.dsp_peer_fixtures import packet
from test_native_checkpoint import engine, stopped, command, migration, comparable

PROFILE = 'wire20-modexp1024-mma-midi-peer'
SETUP = [0x600, 2, 0, 40, 16, 0x7fff, 0, 1, 9]


def state(steps, enabled=True):
    result = make(steps)
    result['dsp_peer']['profile'] = PROFILE if enabled else 'wire20-modexp1024-mma-stream-peer'
    return result


def test_setup_preserves_original_body_without_mma_status(tmp_path):
    result = qemu_run(state(transport_prefix() + frame(SETUP) + POLL[:20]), tmp_path / 'run')
    assert result['stop']['reason'] == 'instruction_limit'
    peer = result['dsp_peer']
    words = struct.unpack('<32H', bytes.fromhex(peer['rx_mailbox_hex']))
    assert words[:10] == tuple([0x480a, 0x601, *SETUP[1:]])
    assert peer['rx_mailbox_ready'] and peer['phase'] == 6
    assert peer['mma_requests'] == peer['audio_requests'] == 0


def test_reply_ack_and_next_unmodeled_command(tmp_path):
    steps = transport_prefix() + frame(SETUP) + POLL[:20] + ACK + CLEAR
    acknowledged = qemu_run(state(steps), tmp_path / 'ack')
    assert acknowledged['stop']['reason'] == 'instruction_limit'
    assert acknowledged['dsp_peer']['phase'] == 4
    assert not acknowledged['dsp_peer']['rx_mailbox_ready']
    assert acknowledged['dsp_peer']['audio_replies'] == 0
    guarded = qemu_run(state(steps + frame([0x61e, 9])), tmp_path / 'guard')
    assert guarded['stop']['reason'] == 'dsp_peer_midi_command_unmodeled'


@pytest.mark.parametrize('word,value', [(0,0x604), (1,1), (2,1), (3,16),
                                      (4,40), (5,0), (6,1), (7,0)])
def test_unqualified_setup_fields_are_guarded(tmp_path, word, value):
    request = SETUP.copy()
    request[word] = value
    result = qemu_run(state(transport_prefix() + frame(request)), tmp_path / 'run')
    assert result['stop']['reason'] == 'dsp_peer_midi_command_unmodeled'
    assert not result['dsp_peer']['rx_mailbox_ready']


def test_disabled_profile_retains_transport_only_behavior(tmp_path):
    result = qemu_run(state(transport_prefix() + frame(SETUP) + POLL[:20], False), tmp_path / 'run')
    assert result['stop']['reason'] == 'instruction_limit'
    assert not result['dsp_peer']['rx_mailbox_ready']


def test_pending_setup_reply_survives_checkpoint(tmp_path):
    prefix = transport_prefix() + frame(SETUP) + POLL[:20]
    prepared = state(prefix + ACK + CLEAR)
    with engine(prepared, tmp_path / 'whole') as vm:
        whole = stopped(vm)
    first = copy.deepcopy(prepared)
    first['instruction_limit'] = state(prefix)['instruction_limit'] - 500
    saved = tmp_path / 'midi.vmstate'
    with engine(first, tmp_path / 'first') as vm:
        before = stopped(vm)
        assert before['dsp_peer']['rx_mailbox_ready']
        layout = command(vm, 'qom-get', {'path':'/machine', 'property':'checkpoint-layout'})
        migration(vm, 'migrate', saved)
    prepared['instruction_limit'] -= first['instruction_limit']
    prepared['checkpoint_tcm'] = json.loads(layout)
    with engine(prepared, tmp_path / 'restored', incoming=True) as vm:
        migration(vm, 'migrate-incoming', saved)
        assert vm.snapshot(time.monotonic()+5)['dsp_peer'] == before['dsp_peer']
        final = stopped(vm)
    assert comparable(final) == comparable(whole)


def prepared_prefix():
    return (transport_prefix() + frame(SETUP) + POLL[:20] + ACK + CLEAR +
            frame([0x604, 9]) + POLL[:20])


def test_ring_announcement_precedes_prepare_reply_then_native_registration(tmp_path):
    prefix = prepared_prefix()
    offered = qemu_run(state(prefix), tmp_path/'offer')
    words = struct.unpack('<32H', bytes.fromhex(offered['dsp_peer']['rx_mailbox_hex']))
    assert words[:4] == (0x180a, 0x301, 1001, 20)
    prefix += ACK + CLEAR + POLL[:20]
    reply = qemu_run(state(prefix), tmp_path/'prepare')
    peer = reply['dsp_peer']
    assert peer['midi_ring_announced'] and peer['midi_prepared']
    assert struct.unpack('<32H', bytes.fromhex(peer['rx_mailbox_hex']))[:3] == (0x100a, 0x605, 9)
    registered = qemu_run(state(prefix + ACK + CLEAR + frame([0x401, 750, 20]) + POLL[:20]),
                          tmp_path/'register')
    peer = registered['dsp_peer']
    assert registered['stop']['reason'] == 'instruction_limit'
    assert peer['midi_ring_registered'] and peer['midi_ring_threshold'] == 750
    assert peer['midi_requests'] == peer['midi_replies'] == 2
    assert not peer['ring4_registered']


def test_prepare_without_setup_and_registration_without_announcement_are_guarded(tmp_path):
    for i, (steps, reason) in enumerate([
        (frame([0x604, 9]), 'dsp_peer_midi_prepare_lifecycle_unmodeled'),
        (frame([0x401, 750, 20]), 'dsp_peer_midi_ring_packet_unmodeled'),
    ]):
        result = qemu_run(state(transport_prefix() + steps), tmp_path/str(i))
        assert result['stop']['reason'] == reason


def test_pending_announcement_survives_checkpoint(tmp_path):
    prefix = prepared_prefix()
    prepared = state(prefix + ACK + CLEAR + POLL[:20] + ACK + CLEAR)
    with engine(prepared, tmp_path/'whole') as vm:
        whole = stopped(vm)
    first = copy.deepcopy(prepared)
    first['instruction_limit'] = state(prefix)['instruction_limit'] - 500
    saved = tmp_path/'ring.vmstate'
    with engine(first, tmp_path/'first') as vm:
        before = stopped(vm)
        assert before['dsp_peer']['midi_prepared']
        assert not before['dsp_peer']['midi_ring_announced']
        layout = command(vm, 'qom-get', {'path':'/machine', 'property':'checkpoint-layout'})
        migration(vm, 'migrate', saved)
    prepared['instruction_limit'] -= first['instruction_limit']
    prepared['checkpoint_tcm'] = json.loads(layout)
    with engine(prepared, tmp_path/'restored', incoming=True) as vm:
        migration(vm, 'migrate-incoming', saved)
        assert vm.snapshot(time.monotonic()+5)['dsp_peer'] == before['dsp_peer']
        final = stopped(vm)
    assert comparable(final) == comparable(whole)


def test_unrecovered_bank_dma_is_guarded_before_data_movement(tmp_path):
    result = qemu_run(state(transport_prefix() + arm(control=0xa4492008) + packet(9, 16)),
                      tmp_path/'bank')
    assert result['stop']['reason'] == 'dsp_peer_midi_bank_unmodeled'
    assert not result['dsp_peer']['midi_events']
