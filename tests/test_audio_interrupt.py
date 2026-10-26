"""Active mode1 acknowledgement, exact elapsed PCM and occupied-ring ownership."""
import copy
import json
import struct
import time

import pytest

from support.mma_playback_fixtures import started,source_packets,SOURCE,stream
from support.mma_mode_fixtures import mode,transport_prefix
from support.fixtures import qemu_run
from test_mma_drain import make as drain_state,RECEIVE,FLUSH,UNREGISTER,POLL,CLEAR
from test_native_checkpoint import engine,stopped,command,migration,comparable
from test_audio_stream import packets


def make(steps,raw):
    state=drain_state(steps,raw)
    state['dsp_peer']['profile']='wire20-modexp1024-mma-stream-peer'
    return state


def stopping(delay=40000):
    raw,parts=source_packets(SOURCE.read_bytes()[:2000])
    _,base=started()
    steps=base+RECEIVE+parts[2]+parts[3]+[('delay',delay,0,0)]+(RECEIVE if delay>=187500 else [])+mode(1)+POLL[:20]+CLEAR
    return raw,steps


def cleanup():
    return RECEIVE+FLUSH+RECEIVE+UNREGISTER+POLL[:20]+CLEAR+mode(4)+RECEIVE


@pytest.mark.parametrize('delay',[1,40000,130000,250000])
def test_stop_retires_only_elapsed_native_samples_and_waits_for_flush(tmp_path,delay):
    raw,steps=stopping(delay);path=tmp_path/'stopped.audio'
    with engine(make(steps,raw),tmp_path/'stop',audio=f'file,id=audio,path={path}')as vm:result=stopped(vm)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    peer=result['dsp_peer'];output=packets(path)
    assert peer['audio_interrupted'] and not peer['audio_active'] and not peer['audio_completed']
    assert peer['audio_completions_acked']==0 and peer['audio_input_bytes']==0
    assert peer['audio_pending_pcm_frames']==0 and peer['mma_mode']==1
    assert struct.unpack('<32H',bytes.fromhex(peer['rx_mailbox_hex']))[:4]==(0x180a,0xe05,1,18)
    assert peer['rx_mma'] and output[-1].header['eos']
    pcm=b''.join(p.pcm for p in output)
    assert len(pcm)//4==peer['audio_frames_played']
    assert 0<=len(pcm)//4<peer['audio_frames_decoded']*1152
    full_raw,full_steps=stream(8);full_path=tmp_path/'full.audio'
    with engine(make(full_steps,full_raw),tmp_path/'full',audio=f'file,id=audio,path={full_path}')as vm:whole=stopped(vm)
    assert whole['stop']['reason']=='instruction_limit'
    assert pcm==b''.join(p.pcm for p in packets(full_path))[:len(pcm)]
    fnv=14695981039346656037
    for byte in pcm:fnv=((fnv^byte)*1099511628211)&((1<<64)-1)
    assert peer['audio_pcm_fnv1a64']==f'{fnv:016x}'
    assert sum(p.header['frame_count'] for p in output)==peer['audio_frames_played']


def test_stop_flush_returns_occupied_words_without_refill_or_natural_completion(tmp_path):
    raw,base=stopping()
    before=qemu_run(make(base,raw),tmp_path/'before',trace=False)['dsp_peer']
    expected=before['ring4_buffered_words']+before['audio_pending_credit']
    assert expected>0
    offered=qemu_run(make(base+RECEIVE+FLUSH+POLL[:20]+CLEAR,raw),tmp_path/'offered',trace=False)
    assert offered['stop']['reason']=='instruction_limit',offered['stop']
    peer=offered['dsp_peer']
    assert struct.unpack('<32H',bytes.fromhex(peer['rx_mailbox_hex']))[:4]==(0x180a,0x0204,expected,20)
    assert peer['ring4_buffered_words']==peer['audio_pending_credit']==0
    result=qemu_run(make(base+cleanup(),raw),tmp_path/'cleanup',trace=False)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    peer=result['dsp_peer']
    assert peer['audio_interrupted'] and not peer['audio_completed'] and not peer['ring4_registered']
    assert peer['mma_mode']==4 and peer['audio_completions_acked']==0
    assert peer['audio_words_credited']==1024
    assert peer['audio_credits_offered']==peer['audio_credits_acked']


def test_interrupt_then_reopen_starts_fresh_decoder_stream(tmp_path):
    raw,base=stopping();full_raw,full=stream(6)
    state=make(base+cleanup()+full[len(transport_prefix()):],full_raw)
    result=qemu_run(state,tmp_path/'reopen',trace=False)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    peer=result['dsp_peer']
    assert not peer['audio_interrupted'] and peer['audio_completed']
    assert peer['audio_completions_acked']==1 and peer['audio_frames_played']==6*1152


@pytest.mark.parametrize('stage',['stop_reply','flush_reply'])
def test_interrupted_reply_and_occupied_flush_survive_checkpoint(tmp_path,stage):
    raw,base=stopping()
    prefix=base if stage=='stop_reply' else base+RECEIVE+FLUSH+POLL[:20]+CLEAR
    steps=base+cleanup();state=make(steps,raw)
    with engine(state,tmp_path/'whole')as vm:whole=stopped(vm)
    first=copy.deepcopy(state)
    first['instruction_limit']=sum(s[1]*2+2 if s[0]=='delay' else 4 for s in prefix)
    saved=tmp_path/'interrupted.vmstate'
    with engine(first,tmp_path/'first')as vm:
        before=stopped(vm);assert before['dsp_peer']['audio_interrupted']
        layout=json.loads(command(vm,'qom-get',{'path':'/machine','property':'checkpoint-layout'}))
        migration(vm,'migrate',saved)
    last=copy.deepcopy(state);last['instruction_limit']-=first['instruction_limit'];last['checkpoint_tcm']=layout
    with engine(last,tmp_path/'last',incoming=True)as vm:
        migration(vm,'migrate-incoming',saved)
        assert vm.snapshot(time.monotonic()+5)['dsp_peer']==before['dsp_peer']
        final=stopped(vm)
    assert comparable(final)==comparable(whole)
