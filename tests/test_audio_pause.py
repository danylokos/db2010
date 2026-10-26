"""Native mode2 suspension preserves samples, source state and ownership."""
import copy
import json
import time

import pytest

from support.mma_playback_fixtures import started, stream, source_packets, SOURCE, POLL, CLEAR
from support.mma_mode_fixtures import mode
from support.fixtures import qemu_run
from test_audio_interrupt import make, cleanup
from test_mma_drain import RECEIVE
from test_audio_stream import packets
from test_native_checkpoint import engine, stopped, command, migration, comparable


def paused_stream(delay=40000,refill=False):
    raw,whole=stream(6)
    split=len(started()[1])+len(RECEIVE)
    prefix=whole[:split]+[('delay',delay,0,0)]+mode(2)+POLL[:20]+CLEAR
    transfer=source_packets(SOURCE.read_bytes()[:6*960])[1][2] if refill else []
    suffix=RECEIVE+[('delay',500000,0,0)]+transfer+mode(0)+RECEIVE+whole[split+len(transfer):]
    return raw,prefix,suffix


@pytest.mark.parametrize('delay,refill',[(1,False),(40000,False),(130000,False),(40000,True)])
def test_pause_resume_preserves_exact_continuous_waveform(tmp_path,delay,refill):
    raw,prefix,suffix=paused_stream(delay,refill)
    paused=qemu_run(make(prefix+RECEIVE+[('delay',500000,0,0)],raw),tmp_path/'paused',trace=False)
    assert paused['stop']['reason']=='instruction_limit',paused['stop']
    p=paused['dsp_peer']
    assert p['audio_paused'] and not p['audio_active'] and not p['audio_interrupted']
    assert not p['audio_completed'] and not p['audio_sample_ended'] and p['mma_mode']==2
    assert p['audio_pending_pcm_frames']>0 and p['audio_completions_acked']==0
    assert p['ring4_registered']
    output=tmp_path/'resumed.audio';baseline=tmp_path/'whole.audio'
    with engine(make(prefix+suffix,raw),tmp_path/'resumed',audio=f'file,id=audio,path={output}')as vm:result=stopped(vm)
    reference_raw,reference_steps=stream(6)
    with engine(make(reference_steps,reference_raw),tmp_path/'whole',audio=f'file,id=audio,path={baseline}')as vm:reference=stopped(vm)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    actual_packets=packets(output)
    assert b''.join(p.pcm for p in actual_packets)==b''.join(p.pcm for p in packets(baseline))
    for key in ('audio_pcm_fnv1a64','audio_frames_played','audio_frames_decoded','audio_nonzero_samples','audio_words_credited'):
        assert result['dsp_peer'][key]==reference['dsp_peer'][key],key
    assert result['dsp_peer']['audio_completed'] and not result['dsp_peer']['audio_paused']
    assert sum(p.header['eos'] for p in actual_packets)==1
    assert result['dsp_peer']['audio_completions_acked']==1


def test_pause_then_stop_returns_ownership_without_resuming_or_completing(tmp_path):
    raw,prefix,_=paused_stream()
    result=qemu_run(make(prefix+RECEIVE+mode(1)+POLL[:20]+CLEAR+cleanup(),raw),tmp_path/'stop',trace=False)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    p=result['dsp_peer']
    assert p['audio_interrupted'] and not p['audio_paused'] and not p['audio_completed']
    assert p['audio_sample_ended'] and p['mma_mode']==4 and not p['ring4_registered']
    assert p['audio_completions_acked']==0


@pytest.mark.parametrize('pending_reply',[False,True])
def test_paused_decoder_and_partial_pcm_survive_native_checkpoint(tmp_path,pending_reply):
    raw,prefix,suffix=paused_stream()
    if not pending_reply:prefix+=RECEIVE;suffix=suffix[len(RECEIVE):]
    state=make(prefix+suffix,raw)
    with engine(state,tmp_path/'whole')as vm:whole=stopped(vm)
    first=copy.deepcopy(state)
    first['instruction_limit']=make(prefix,raw)['instruction_limit']-500
    saved=tmp_path/'paused.vmstate'
    with engine(first,tmp_path/'first')as vm:
        start=stopped(vm)
        assert start['dsp_peer']['audio_paused'] and start['dsp_peer']['audio_pending_pcm_frames']>0
        assert start['dsp_peer']['rx_mailbox_ready']==pending_reply
        layout=json.loads(command(vm,'qom-get',{'path':'/machine','property':'checkpoint-layout'}))
        migration(vm,'migrate',saved)
    state['instruction_limit']-=first['instruction_limit'];state['checkpoint_tcm']=layout
    with engine(state,tmp_path/'restored',incoming=True)as vm:
        migration(vm,'migrate-incoming',saved)
        assert vm.snapshot(time.monotonic()+5)['dsp_peer']==start['dsp_peer']
        final=stopped(vm)
    assert comparable(final)==comparable(whole)
