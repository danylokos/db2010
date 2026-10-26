"""Ring0 close is transport-only; unrelated packets and pending replies stay guarded."""
import json

import pytest

from support.mma_ring_fixtures import make, stages, transport_prefix, POLL, ACK, CLEAR
from support.fa_wire10_fixtures import frame
from support.fixtures import qemu_run


@pytest.mark.parametrize('latency', [2, 512])
@pytest.mark.parametrize('registered', [False, True])
def test_ring0_close_has_no_application_reply_or_ring4_effect(tmp_path, latency, registered):
    prefix = stages()['registered'] if registered else transport_prefix()
    before = qemu_run(make(prefix, latency), tmp_path/'before', trace=False)
    after = qemu_run(make(prefix+frame([0x0600,20])+POLL[:20], latency),
                     tmp_path/'after', trace=False)
    assert after['stop']['reason'] == 'instruction_limit'
    first, last = before['dsp_peer'], after['dsp_peer']
    assert last['requests'] == first['requests']+1
    assert last['mailbox_events'] == first['mailbox_events']+1
    assert not last['rx_mailbox_ready']
    for key in ('ring4_announced', 'ring4_registered', 'ring4_threshold',
                'ring4_announcements', 'ring4_registrations', 'mma_requests',
                'mma_replies', 'audio_requests', 'audio_replies', 'rx_mailbox_hex'):
        assert last[key] == first[key], key


@pytest.mark.parametrize('payload', [
    [0x0601,20], [0x0604,20], [0x0605,20],
    [0x0600,0,20], [0x0600,0xffff,20], [0x0700,20], [0x0800,0,20],
])
def test_other_ring_close_shapes_remain_guarded(tmp_path, payload):
    result = qemu_run(make(transport_prefix()+frame(payload)), tmp_path/'guard', trace=False)
    assert result['stop']['reason'] == 'dsp_peer_ring_packet_unmodeled'
    peer = result['dsp_peer']
    assert peer['ring4_announcements'] == peer['ring4_registrations'] == 0
    assert peer['mma_requests'] == peer['audio_requests'] == 0


def test_ring0_close_cannot_overwrite_pending_ring4_reply(tmp_path):
    result = qemu_run(make(stages()['announcement']+frame([0x0600,20])),
                      tmp_path/'pending', trace=False)
    assert result['stop']['reason'] == 'dsp_peer_packet_contract_unmodeled'
    assert result['dsp_peer']['rx_ring4'] and result['dsp_peer']['rx_mailbox_ready']


@pytest.mark.parametrize('registered', [False, True])
def test_pending_ring0_close_checkpoint_preserves_native_continuation(tmp_path, registered):
    import copy
    from test_native_checkpoint import engine, stopped, command, migration, comparable
    prefix = (stages()['registered'] if registered else transport_prefix())+frame([0x0600,20])
    state = make(prefix+POLL[:20], latency=512)
    state['record_diagnostics'] = False
    with engine(state, tmp_path/'whole') as vm:
        whole = stopped(vm)
    first = copy.deepcopy(state)
    first['instruction_limit'] = len(prefix)*3
    checkpoint = tmp_path/'close.vmstate'
    with engine(first, tmp_path/'prefix') as vm:
        start = stopped(vm)
        assert start['dsp_peer']['phase'] == 1
        assert not start['dsp_peer']['rx_mailbox_ready']
        layout = json.loads(command(vm, 'qom-get',
            {'path': '/machine', 'property': 'checkpoint-layout'}))
        migration(vm, 'migrate', checkpoint)
    continuation = copy.deepcopy(state)
    continuation['instruction_limit'] -= first['instruction_limit']
    continuation['checkpoint_tcm'] = layout
    with engine(continuation, tmp_path/'resumed', incoming=True) as vm:
        migration(vm, 'migrate-incoming', checkpoint)
        final = stopped(vm)
    assert comparable(final) == comparable(whole)
    assert not final['dsp_peer']['rx_mailbox_ready']
