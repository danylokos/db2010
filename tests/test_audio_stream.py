"""Actual native PCM stream, bounded framing, sample ownership and migration."""
from support.guarded import run as guarded_run
import copy
import json
import socket
import struct
import time
import wave

import pytest

from db2010.execution.audio import AudioCapture, AudioFramer, AudioPacket
from support.mma_playback_fixtures import make as playback_state, stream, SOURCE, POLL, ACK, CLEAR, DELAY, source_packets
from support.mma_buffer_fixtures import configured
from support.mma_mode_fixtures import mode
from support.fa_wire10_fixtures import frame
from test_native_checkpoint import engine, stopped, command, migration, comparable

PROFILE = 'wire20-modexp1024-mma-stream-peer'


def state(steps, raw):
    result=playback_state(steps,raw)
    result['dsp_peer']['profile']=PROFILE
    result['regions'][1]['size']=max(16384,(len(raw)+4095)&~4095)
    result['record_diagnostics']=False
    return result


def packets(path):
    parser=AudioFramer();result=[]
    with path.open('rb') as source:
        while data:=source.read(8192):result.extend(parser.feed(data))
    assert not parser.buffer
    return result


def test_native_pcm_recording_matches_oracle_and_absent_sink(tmp_path):
    raw,steps=stream(8);prepared=state(steps,raw)
    with engine(prepared,tmp_path/'absent')as vm:absent=stopped(vm)
    path=tmp_path/'native.audio'
    with engine(prepared,tmp_path/'file',audio=f'file,id=audio,path={path}')as vm:captured=stopped(vm)
    assert comparable(captured)==comparable(absent)
    chunks=packets(path)
    assert chunks and chunks[-1].header['eos']
    assert all(0<=p.header['frame_count']<=960 for p in chunks)
    assert sum(p.header['frame_count']for p in chunks)==8*1152
    capture=AudioCapture(tmp_path/'recorded')
    for p in chunks:capture.accept(p)
    metrics=capture.close()
    oracle=json.loads(SOURCE.with_suffix('.json').read_text())['scalar_minimp3_oracle']['8']
    assert metrics['fnv1a64']==oracle['fnv1a64']
    assert metrics['frames']==oracle['pcm_frames'] and metrics['gaps']==0 and metrics['eos']==1
    assert captured['dsp_peer']['audio_sample_ended']
    assert captured['dsp_peer']['audio_sample_partial_frames']==0
    with wave.open(str(tmp_path/'recorded/audio.wav'))as wav:
        assert wav.getparams()[:4]==(2,2,48000,8*1152)
        assert wav.readframes(wav.getnframes())==(tmp_path/'recorded/audio.pcm').read_bytes()


def test_aligned_host_pacing_is_independent_of_sink_presence(tmp_path):
    raw,steps=stream(8);prepared=state(steps,raw)
    peers=[]
    for name,audio in [('absent',None),('file',f'file,id=audio,path={tmp_path}/paced.audio')]:
        with engine(prepared,tmp_path/name,audio=audio,icount='shift=6,sleep=on,align=on')as vm:
            result=stopped(vm)
            output=json.loads(command(vm,'qom-get',{'path':'/machine/fa-mailbox/dsp','property':'audio-output'}))
        peer=result['dsp_peer'];peers.append(peer)
        assert result['stop']['reason']=='instruction_limit'
        assert output['paced_packets']==peer['audio_sample_packets']>0
        assert peer['audio_completed'] and peer['audio_completions_acked']==1
        assert peer['audio_frames_played']==8*1152
    for key in ['audio_frames_decoded','audio_frames_played','audio_pcm_fnv1a64',
                'audio_words_credited','audio_credits_acked']:
        assert peers[0][key]==peers[1][key]


@pytest.mark.parametrize('cut_delay',[0,3])
def test_partial_native_packet_survives_restore_without_repeating_samples(tmp_path,cut_delay):
    raw,steps=stream(8);prepared=state(steps,raw)
    # Stop after a decode block's end with part of the next packet assembled.
    cut=[i for i,s in enumerate(steps)if s[0]=='delay'][cut_delay]+1
    prefix=copy.deepcopy(prepared)
    prefix['instruction_limit']=sum(s[1]*2+2 if s[0]=='delay'else 4 for s in steps[:cut])+100
    full_path,first_path,last_path=[tmp_path/x for x in ('full.audio','first.audio','last.audio')]
    with engine(prepared,tmp_path/'whole',audio=f'file,id=audio,path={full_path}')as vm:whole=stopped(vm)
    saved=tmp_path/'sample.vmstate'
    with engine(prefix,tmp_path/'prefix',audio=f'file,id=audio,path={first_path}')as vm:
        before=stopped(vm)
        assert before['dsp_peer']['audio_sample_partial_frames']>0
        layout=json.loads(command(vm,'qom-get',{'path':'/machine','property':'checkpoint-layout'}))
        migration(vm,'migrate',saved)
    suffix=copy.deepcopy(prepared)
    suffix['instruction_limit']-=prefix['instruction_limit'];suffix['checkpoint_tcm']=layout
    with engine(suffix,tmp_path/'restored',incoming=True,audio=f'file,id=audio,path={last_path}')as vm:
        migration(vm,'migrate-incoming',saved)
        assert vm.snapshot(time.monotonic()+5)['dsp_peer']==before['dsp_peer']
        final=stopped(vm)
    assert comparable(whole)==comparable(final)
    full=packets(full_path);joined=packets(first_path)+packets(last_path)
    assert b''.join(p.pcm for p in full)==b''.join(p.pcm for p in joined)
    assert [(p.header['first_frame'],p.header['frame_count'])for p in full]==[(p.header['first_frame'],p.header['frame_count'])for p in joined]
    assert joined[-1].header['eos']


def sample_packet(**changes):
    header=dict(type='audio',version=1,stream_id='test',epoch='1',sequence='0',
        first_frame='0',virtual_start_ns='0',sample_rate=48000,channels=2,format='s16le',
        frame_count=2,discontinuity=True,eos=False)
    header.update(changes)
    return AudioPacket(header,struct.pack('<4h',-32768,32767,-1,0))


def test_fragmented_envelopes_and_signed_stereo_endpoints(tmp_path):
    packet=sample_packet();wire=packet.wire();reader=AudioFramer();decoded=[]
    for byte in wire:decoded.extend(reader.feed(bytes([byte])))
    assert decoded==[packet] and not reader.buffer
    capture=AudioCapture(tmp_path/'recording');capture.accept(decoded[0]);capture.close()
    assert struct.unpack('<4h',(tmp_path/'recording/audio.pcm').read_bytes())==(-32768,32767,-1,0)


@pytest.mark.parametrize('change',[{'sample_rate':44100},{'frame_count':961},{'sequence':'9007199254740993x'},
    {'first_frame':'01'},{'epoch':1},{'discontinuity':1},{'frame_count':0}])
def test_malformed_audio_is_rejected_before_delivery(change):
    reader=AudioFramer()
    with pytest.raises(ValueError):list(reader.feed(sample_packet(**change).wire()))


def test_stale_and_unannounced_gaps_are_rejected():
    capture=AudioCapture();capture.accept(sample_packet())
    with pytest.raises(ValueError):capture.accept(sample_packet())
    with pytest.raises(ValueError):capture.accept(sample_packet(sequence='1',first_frame='4',discontinuity=False))
    capture.accept(sample_packet(sequence='2',first_frame='4',discontinuity=True))
    assert capture.close()['gaps']==1


def test_new_logical_stream_is_not_a_missing_sample_gap():
    capture=AudioCapture()
    capture.accept(sample_packet(eos=True))
    capture.accept(sample_packet(epoch='2',discontinuity=True,eos=True))
    assert capture.close()['epochs']==2 and capture.metrics['gaps']==0


def test_compiled_sampler_exact_virtual_second_and_channel_order(tmp_path):
    from pathlib import Path
    import subprocess
    from db2010.paths import ROOT
    output=tmp_path/'sampler'
    subprocess.run(['cc','-std=c11','-I'+str(ROOT/'qemu/hw/arm'),
        str(Path(__file__).parent/'support/audio_sampler.c'),
        str(ROOT/'qemu/build/libsystem_arm.a.p/hw_arm_db2010-audio-samples.c.o'),
        '-o',str(output)],check=True,capture_output=True)
    result=subprocess.run([output],check=True,capture_output=True,text=True)
    assert '48000 stereo frames' in result.stdout


def test_stalled_socket_drops_bounded_presentation_without_changing_native_state(tmp_path):
    raw,steps=stream(8);prepared=state(steps,raw)
    with engine(prepared,tmp_path/'absent')as vm:absent=stopped(vm)
    from contextlib import ExitStack
    with ExitStack() as resources:
        writer,reader=socket.socketpair()
        resources.enter_context(writer);resources.enter_context(reader)
        writer.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,1024)
        reader.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024)
        with engine(prepared,tmp_path/'stalled',audio=f'socket,id=audio,fd={writer.fileno()}',pass_fds=(writer.fileno(),))as vm:
            stalled=stopped(vm)
            transport=json.loads(command(vm,'qom-get',{'path':'/machine/fa-mailbox/dsp','property':'audio-output'}))
    assert comparable(stalled)==comparable(absent)
    assert stalled['stop']['reason']=='instruction_limit',stalled['stop']
    assert stalled['dsp_peer']['audio_completed'] and stalled['dsp_peer']['audio_completions_acked']==1
    assert transport['queued']<=10 and transport['dropped_frames']>0


def test_production_runner_records_audio_without_diagnostics(tmp_path,monkeypatch):
    from db2010.execution import runner
    from db2010.diagnostics.artifacts import verify_seal
    from db2010.paths import ROOT
    from support.fixtures import freeze_state
    raw,steps=stream(8)
    def prepare(root,experiment,output,**kwargs):
        return freeze_state(state(steps,raw),output),{'seconds':10}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,None,tmp_path/'run',record_audio=True)
    assert failure is None,result.get('error')
    assert result['success'] and result['audio']['frames']==8*1152
    assert result['audio']['fnv1a64']==json.loads(SOURCE.with_suffix('.json').read_text())['scalar_minimp3_oracle']['8']['fnv1a64']
    manifest=json.loads((tmp_path/'run/run.json').read_text())
    assert manifest['families']==['audio'] and manifest['audio']==result['audio']
    assert not (tmp_path/'run/diagnostics/state.json').exists()
    assert verify_seal(tmp_path/'run')
    from db2010.diagnostics.retention import regeneration_command
    assert '--record-audio' in regeneration_command(tmp_path/'run')
