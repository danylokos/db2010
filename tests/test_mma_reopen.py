"""Only a fully completed/unregistered stream may reopen through native mode3."""
import copy
import json
import struct
import time

import pytest

from support.fa_wire10_fixtures import frame
from support.mma_mode_fixtures import mode, transport_prefix
from support.mma_playback_fixtures import stream, started, POLL, ACK, CLEAR
from support.fixtures import qemu_run
from test_mma_drain import make, FLUSH, UNREGISTER, RECEIVE
from test_native_checkpoint import engine, stopped, command, migration, comparable


def cleanup():
    return FLUSH+RECEIVE+mode(1)+POLL[:20]+CLEAR+FLUSH+RECEIVE+UNREGISTER+POLL[:20]+CLEAR+mode(4)+RECEIVE


def test_drained_reopen_reannounces_and_resets_only_stream_state(tmp_path):
    raw,base=stream()
    result=qemu_run(make(base+cleanup()+mode(3)+POLL[:20],raw),tmp_path/'reopen',trace=False)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    p=result['dsp_peer']
    assert p['rx_ring4'] and not p['rx_mma'] and p['rx_mailbox_ready']
    assert struct.unpack('<32H',bytes.fromhex(p['rx_mailbox_hex']))[:4]==(0x180a,0x0304,801,20)
    assert not p['ring4_registered'] and not p['mma_configured']
    assert not p['audio_completed'] and not p['audio_eof'] and not p['audio_active']
    assert p['audio_frames_decoded']==p['audio_frames_played']==p['audio_completions_acked']==0
    assert p['mma_mode']==3 and p['mma_requests']==6


def test_second_stream_decodes_from_fresh_history_and_completes_once(tmp_path):
    raw,base=stream()
    baseline=qemu_run(make(base,raw),tmp_path/'single',trace=False)['dsp_peer']
    again=base[len(transport_prefix()):]
    result=qemu_run(make(base+cleanup()+again,raw),tmp_path/'twice',trace=False)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    p=result['dsp_peer']
    for key in ('audio_frames_decoded','audio_frames_played','audio_nonzero_samples','audio_pcm_fnv1a64','audio_words_credited'):
        assert p[key]==baseline[key]
    assert p['ring4_announcements']==p['ring4_registrations']==2
    assert p['audio_completed'] and p['audio_completions_acked']==1


@pytest.mark.parametrize('stage',['active','completed_registered','unregistered_mode1'])
def test_reopen_with_retained_ownership_stays_guarded(tmp_path,stage):
    if stage=='active':
        raw,base=started();steps=base+RECEIVE+mode(3)
        reason='dsp_peer_audio_active_control_unmodeled'
    else:
        raw,base=stream()
        suffix=[] if stage=='completed_registered' else cleanup()[:-len(mode(4)+RECEIVE)]
        steps=base+suffix+mode(3);reason='dsp_peer_ring_lifecycle_unmodeled'
    result=qemu_run(make(steps,raw),tmp_path/stage,trace=False)
    assert result['stop']['reason']==reason,result['stop']
    assert result['dsp_peer']['ring4_announcements']==1


def test_reopen_announcement_survives_native_checkpoint(tmp_path):
    raw,base=stream()
    prefix=base+cleanup()+mode(3)+POLL[:20]
    suffix=ACK+CLEAR+POLL[:20]+ACK+CLEAR+frame([0x0404,240,20])+POLL[:20]
    prepared=make(prefix+suffix,raw)
    with engine(prepared,tmp_path/'whole')as vm:whole=stopped(vm)
    first=copy.deepcopy(prepared)
    first['instruction_limit']=make(prefix,raw)['instruction_limit']-500
    saved=tmp_path/'reopen.vmstate'
    with engine(first,tmp_path/'first')as vm:
        before=stopped(vm)
        assert before['dsp_peer']['rx_ring4']
        layout=json.loads(command(vm,'qom-get',{'path':'/machine','property':'checkpoint-layout'}))
        migration(vm,'migrate',saved)
    prepared['instruction_limit']-=first['instruction_limit'];prepared['checkpoint_tcm']=layout
    with engine(prepared,tmp_path/'restored',incoming=True)as vm:
        migration(vm,'migrate-incoming',saved)
        assert vm.snapshot(time.monotonic()+5)['dsp_peer']==before['dsp_peer']
        final=stopped(vm)
    assert comparable(final)==comparable(whole)
    assert final['dsp_peer']['ring4_registrations']==2


def test_reopened_pcm_uses_new_epoch_with_fresh_sequence_and_eos(tmp_path):
    from test_audio_stream import packets
    raw,base=stream()
    state=make(base+cleanup()+base[len(transport_prefix()):],raw)
    state['dsp_peer']['profile']='wire20-modexp1024-mma-stream-peer'
    path=tmp_path/'two.audio'
    with engine(state,tmp_path/'engine',audio=f'file,id=audio,path={path}')as vm:
        final=stopped(vm)
    assert final['stop']['reason']=='instruction_limit',final['stop']
    captured=packets(path)
    epochs=list(dict.fromkeys(p.header['epoch']for p in captured))
    assert len(epochs)==2
    streams=[[p for p in captured if p.header['epoch']==epoch]for epoch in epochs]
    for output in streams:
        assert output[0].header['sequence']=='0' and output[0].header['first_frame']=='0'
        assert output[0].header['discontinuity'] and output[-1].header['eos']
        assert sum(p.header['frame_count']for p in output)==6*1152
    assert b''.join(p.pcm for p in streams[0])==b''.join(p.pcm for p in streams[1])
