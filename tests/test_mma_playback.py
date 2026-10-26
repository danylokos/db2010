"""Actual source decoding, bounded credits, PCM clock and native migration."""
import copy
import json
import struct
import time

import pytest

from support.mma_playback_fixtures import make, started, stream, DELAY, POLL, ACK, CLEAR, SOURCE
from support.mma_mode_fixtures import mode
from support.mma_buffer_fixtures import configured
from support.fixtures import qemu_run
from test_native_checkpoint import engine, stopped, command, migration, comparable


def test_mode0_decodes_guest_stream_and_returns_actual_ring_credit(tmp_path):
    raw,steps=started()
    result=qemu_run(make(steps,raw),tmp_path/'started',trace=False)
    assert result['stop']['reason']=='instruction_limit'
    p=result['dsp_peer']
    assert p['mma_mode']==0 and p['mma_requests']==p['mma_replies']==3
    assert p['audio_frames_decoded']==1 and p['audio_pending_pcm_frames']==1152
    assert p['audio_frames_played']==0 and p['audio_input_bytes']==40
    assert p['ring4_buffered_words']==0 and p['audio_words_credited']==512
    assert p['audio_rx_kind']==1 and p['audio_credits_offered']==1
    reply=struct.unpack('<32H',bytes.fromhex(p['rx_mailbox_hex']))
    assert reply[:4]==(0x180a,0x0504,512,20)
    assert not p['audio_completed']


def test_elapsed_virtual_pcm_and_native_eof_completion(tmp_path):
    raw,steps=stream()
    result=qemu_run(make(steps,raw),tmp_path/'stream',trace=False)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    p=result['dsp_peer']
    oracle=json.loads(SOURCE.with_suffix('.json').read_text())['scalar_minimp3_oracle']['6']
    assert p['audio_frames_decoded']==6
    assert p['audio_frames_played']==oracle['pcm_frames']
    assert p['audio_pcm_fnv1a64']==oracle['fnv1a64']
    assert p['audio_nonzero_samples']==oracle['nonzero_samples']>0
    assert p['audio_completed'] and p['audio_eof'] and not p['audio_active']
    assert p['audio_input_bytes']==p['ring4_buffered_words']==0
    assert p['audio_completions_acked']==1 and p['audio_credits_offered']==p['audio_credits_acked']
    assert not p['rx_mailbox_ready']
    assert result['virtual_ns']-p['audio_start_ns']>=6*24000000


def test_start_requires_prefill_and_native_configuration(tmp_path):
    result=qemu_run(make(configured()+mode(0),bytes(544)),tmp_path/'empty',trace=False)
    assert result['stop']['reason']=='dsp_peer_audio_start_lifecycle_unmodeled'


@pytest.mark.parametrize('offset,value',[(16,0),(18,0xe3)])
def test_unknown_encoded_format_stops_before_pcm_or_credit_reply(tmp_path,offset,value):
    raw,steps=started()
    changed=bytearray(raw)
    changed[offset]=value
    result=qemu_run(make(steps,changed),tmp_path/'format',trace=False)
    assert result['stop']['reason']=='dsp_peer_audio_source_format_unmodeled'
    p=result['dsp_peer']
    assert p['audio_frames_decoded']==p['audio_frames_played']==p['audio_credits_offered']==0
    assert not p['audio_completed']


@pytest.mark.parametrize('phase',['start_reply','pending_pcm','credit_acknowledged','codec_history'])
def test_codec_reservoir_pending_pcm_credit_and_clock_migrate(tmp_path,phase):
    if phase=='codec_history':
        raw,steps=stream(8)
        cut=[i for i,s in enumerate(steps)if s[0]=='delay'][3]+1
        prefix=steps[:cut]
        expected_frames=8*1152
    else:
        raw,base=started()
        portions={'start_reply':base[:-len(ACK+CLEAR)],'pending_pcm':base,
                  'credit_acknowledged':base+POLL[:20]+ACK+CLEAR}
        prefix=portions[phase]
        steps=portions['credit_acknowledged']+DELAY
        expected_frames=1152
    state=make(steps,raw)
    state['record_diagnostics']=False
    with engine(state,tmp_path/'whole')as vm:whole=stopped(vm)
    first=copy.deepcopy(state)
    first['instruction_limit']=sum(s[1]*2+2 if s[0]=='delay'else 4 for s in prefix)+100
    saved=tmp_path/'audio.vmstate'
    with engine(first,tmp_path/'prefix')as vm:
        start=stopped(vm)
        assert start['dsp_peer']['audio_pending_pcm_frames']==1152
        layout=json.loads(command(vm,'qom-get',{'path':'/machine','property':'checkpoint-layout'}))
        migration(vm,'migrate',saved)
    continuation=copy.deepcopy(state)
    continuation['instruction_limit']-=first['instruction_limit']
    continuation['checkpoint_tcm']=layout
    with engine(continuation,tmp_path/'resumed',incoming=True)as vm:
        migration(vm,'migrate-incoming',saved)
        restored=vm.snapshot(time.monotonic()+5)
        assert restored['dsp_peer']==start['dsp_peer']
        final=stopped(vm)
    assert comparable(final)==comparable(whole)
    assert final['dsp_peer']['audio_frames_played']==expected_frames
    if phase=='codec_history':
        oracle=json.loads(SOURCE.with_suffix('.json').read_text())['scalar_minimp3_oracle']['8']
        assert final['dsp_peer']['audio_pcm_fnv1a64']==oracle['fnv1a64']
        assert final['dsp_peer']['audio_nonzero_samples']==oracle['nonzero_samples']>0
