"""Guest DMA fills bounded persistent ring storage without consumption/credits."""
import copy
import json
import struct
import time

import pytest

from support.mma_buffer_fixtures import make, configured, payload, transfer, POLL, CLEAR
from support.mma_setup_fixtures import setup, stages
from support.mma_mode_fixtures import mode
from support.fixtures import qemu_run
from test_native_checkpoint import engine, stopped, command, migration, comparable


@pytest.mark.parametrize('count',[28,29,256,800])
def test_counted_dma_retains_metadata_and_source_excluding_padding(tmp_path,count):
    raw,words = payload(count)
    result = qemu_run(make(configured()+transfer(raw)+POLL[:100],raw),tmp_path/'buffer',trace=False)
    assert result['stop']['reason'] == 'instruction_limit'
    peer = result['dsp_peer']
    assert peer['ring4_packets'] == 1 and peer['ring4_buffered_words'] == count
    assert peer['ring4_free_words'] == 800-count and peer['mma_configured']
    assert bytes.fromhex(peer['ring4_buffer_hex']) == struct.pack('<'+'H'*count,*words)
    assert not peer['rx_mailbox_ready'] and peer['mma_requests'] == peer['mma_replies'] == 2
    assert not peer['audio_playback_modeled'] and peer['transfer_events'] == 2  # Includes transport handshake.
    assert result['pl080']['raw_tc'] == 4


def test_buffer_accumulates_without_credits_and_rejects_overflow(tmp_path):
    raw,words = payload(400)
    first = configured()+transfer(raw)+POLL[:100]+CLEAR
    second = first+transfer(raw)+POLL[:100]+CLEAR
    result = qemu_run(make(second,raw),tmp_path/'full',trace=False)
    assert result['stop']['reason'] == 'instruction_limit'
    peer = result['dsp_peer']
    assert peer['ring4_buffered_words'] == 800 and peer['ring4_packets'] == 2
    assert peer['ring4_free_words'] == 0 and not peer['rx_mailbox_ready']
    assert bytes.fromhex(peer['ring4_buffer_hex']) == struct.pack('<800H',*(words+words))
    result = qemu_run(make(second+transfer(raw)+POLL[:100],raw),tmp_path/'overflow',trace=False)
    assert result['stop']['reason'] == 'dsp_peer_ring_data_overflow'
    assert result['dsp_peer']['ring4_buffer_hex'] == peer['ring4_buffer_hex']
    assert result['dsp_peer']['ring4_packets'] == 2


@pytest.mark.parametrize('offset,value,reason',[
    (0,0x0805,'dsp_peer_ring_data_packet_unmodeled'),
    (2,5,'dsp_peer_ring_data_packet_unmodeled'),
    (2,27,'dsp_peer_ring_data_packet_unmodeled'),
    (2,801,'dsp_peer_ring_data_packet_unmodeled'),
    (2,240,'dsp_peer_ring_data_packet_unmodeled'),
    (542,18,'dsp_peer_ring_data_packet_unmodeled'),
    (4,3,'dsp_peer_ring_data_header_unmodeled'),
    (6,2,'dsp_peer_ring_data_header_unmodeled'),
    (8,0,'dsp_peer_ring_data_header_unmodeled'),
    (14,249,'dsp_peer_ring_data_header_unmodeled'),
])
def test_invalid_packets_do_not_mutate_buffer_or_supply_transport_completion(tmp_path,offset,value,reason):
    raw,_ = payload()
    changed = bytearray(raw)
    struct.pack_into('<H',changed,offset,value)
    result = qemu_run(make(configured()+transfer(changed)+POLL[:100],changed),tmp_path/'guard',trace=False)
    assert result['stop']['reason'] == reason
    peer = result['dsp_peer']
    assert peer['ring4_buffered_words'] == peer['ring4_packets'] == 0
    assert peer['transfer_events'] == 1  # Only the inherited transport handshake completed.
    assert not peer['rx_mailbox_ready'] and not peer['event_status']&8


def test_buffer_requires_native_setup_and_remains_opt_in(tmp_path):
    raw,_ = payload()
    result = qemu_run(make(stages()['registered']+transfer(raw),raw),tmp_path/'no-setup',trace=False)
    assert result['stop']['reason'] == 'dsp_peer_ring_data_lifecycle_unmodeled'
    assert result['dsp_peer']['dma_messages'] == 1  # Inherited transport handshake only.
    assert result['dsp_peer']['ring4_buffered_words'] == 0
    state = make(configured()+transfer(raw),raw)
    state['dsp_peer']['profile'] = 'wire20-modexp1024-mma-setup-peer'
    result = qemu_run(state,tmp_path/'old-profile',trace=False)
    assert result['stop']['reason'] == 'dsp_peer_ring_packet_unmodeled'


def test_inline_sized_ring_write_cannot_use_dma_rounding(tmp_path):
    raw,_ = payload(27)
    result = qemu_run(make(configured()+transfer(raw)+POLL[:100],raw),tmp_path/'inline-size',trace=False)
    assert result['stop']['reason'] == 'dsp_peer_ring_data_packet_unmodeled'
    assert result['dsp_peer']['ring4_packets'] == 0


@pytest.mark.parametrize('control',[mode(1),mode(4),setup()])
def test_pending_encoded_data_cannot_be_discarded_by_unmodeled_lifecycle(tmp_path,control):
    raw,_ = payload()
    result = qemu_run(make(configured()+transfer(raw)+POLL[:100]+CLEAR+control,raw),tmp_path/'lifecycle',trace=False)
    assert result['stop']['reason'] == 'dsp_peer_mma_buffer_lifecycle_unmodeled'
    assert result['dsp_peer']['ring4_buffered_words'] == 256


@pytest.mark.parametrize('phase',['pending','mid_dma','buffered'])
def test_pending_dma_and_encoded_queue_checkpoint_continuation(tmp_path,phase):
    raw,_ = payload()
    base = configured()+transfer(raw)
    portions = {'pending':base,'mid_dma':base+POLL[:10],'buffered':base+POLL[:100]}
    state = make(portions['buffered']+CLEAR+POLL[:20],raw)
    state['record_diagnostics'] = False
    with engine(state,tmp_path/'whole') as vm:
        whole = stopped(vm)
    first = copy.deepcopy(state)
    first['instruction_limit'] = len(portions[phase])*4
    saved = tmp_path/'buffer.vmstate'
    with engine(first,tmp_path/'prefix') as vm:
        start = stopped(vm)
        peer = start['dsp_peer']
        if phase == 'pending':assert peer['phase'] == 1 and peer['ring4_packets'] == 0
        elif phase == 'mid_dma':assert 0 < peer['bytes_received'] < len(raw) and peer['ring4_packets'] == 0
        else:assert peer['ring4_packets'] == 1 and peer['ring4_buffered_words'] == 256
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
    assert final['dsp_peer']['ring4_packets'] == 1
