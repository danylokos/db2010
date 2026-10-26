"""Native44.1kHz decode,48kHz delivery, strict format guards and checkpointing."""
import copy
import hashlib
import time
import json
import pytest

from support.ringtone_fixtures import stream, SOURCE, MONO_SOURCE, COO_SOURCE, SETUP_MONO, SETUP44
from support.fixtures import qemu_run
from test_native_checkpoint import engine,stopped,command,migration,comparable
from test_audio_stream import packets


def test_ringtone_decodes_and_completes_at_output_rate(tmp_path):
    state,steps,raw=stream()
    path=tmp_path/'ringtone.audio'
    with engine(state,tmp_path/'run',audio=f'file,id=audio,path={path}')as vm:result=stopped(vm)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    p=result['dsp_peer'];output=packets(path)
    assert p['audio_source_rate']==44100 and p['audio_source_frames']==8*1152
    assert p['audio_frames_played']==p['audio_resampled_frames']==(8*1152*160+146)//147
    assert p['audio_frames_decoded']==8 and p['audio_nonzero_samples']>0
    assert p['audio_completed'] and p['audio_completions_acked']==1
    assert p['audio_words_credited']==8*(418//2+6)+6
    assert sum(x.header['frame_count']for x in output)==p['audio_frames_played']
    assert output[-1].header['eos']
    oracle=json.loads(SOURCE.with_suffix('.json').read_text())['scalar_minimp3_integer_linear_oracle']
    assert hashlib.sha256(b''.join(p.pcm for p in output)).hexdigest()==oracle['output_pcm_sha256']
    assert p['audio_pcm_fnv1a64']==oracle['output_pcm_fnv1a64']


@pytest.mark.parametrize('offset,value',[(2,0xe4),(1,0xf9)])
def test_stereo_format_mismatch_stays_guarded(tmp_path,offset,value):
    state,steps,raw=stream(1)
    changed=bytearray(raw);changed[16+(offset^1)]=value
    state['regions'][1]['seeds'][0]['data']=bytes(changed)
    result=qemu_run(state,tmp_path/'mismatch',trace=False)
    assert result['stop']['reason']=='dsp_peer_audio_source_format_unmodeled',result['stop']
    assert result['dsp_peer']['audio_frames_decoded']==0


@pytest.mark.parametrize('offset',[5000,430000])
@pytest.mark.parametrize('source,setup',[(SOURCE,SETUP44),(MONO_SOURCE,SETUP_MONO),(COO_SOURCE,SETUP_MONO)])
def test_interpolator_phase_and_partial_output_survive_checkpoint(tmp_path,offset,source,setup):
    state,steps,raw=stream(source=source,setup=setup)
    full_path,first_path,last_path=[tmp_path/n for n in ['full.audio','first.audio','last.audio']]
    with engine(state,tmp_path/'full',audio=f'file,id=audio,path={full_path}')as vm:whole=stopped(vm)
    assert whole['stop']['reason']=='instruction_limit',whole['stop']
    prefix=copy.deepcopy(state)
    cut=next(i for i,s in enumerate(steps)if s[0]=='delay')
    prefix['instruction_limit']=sum(s[1]*2+2 if s[0]=='delay'else 4 for s in steps[:cut])+offset
    saved=tmp_path/'rate.vmstate'
    with engine(prefix,tmp_path/'prefix',audio=f'file,id=audio,path={first_path}')as vm:
        before=stopped(vm)
        assert before['dsp_peer']['audio_resampler_phase']>0
        if offset==5000:assert before['dsp_peer']['audio_pending_pcm_frames']>1152
        else:assert before['dsp_peer']['audio_sample_partial_frames']>0
        layout=json.loads(command(vm,'qom-get',{'path':'/machine','property':'checkpoint-layout'}))
        migration(vm,'migrate',saved)
    suffix=copy.deepcopy(state);suffix['instruction_limit']-=prefix['instruction_limit'];suffix['checkpoint_tcm']=layout
    with engine(suffix,tmp_path/'restored',incoming=True,audio=f'file,id=audio,path={last_path}')as vm:
        migration(vm,'migrate-incoming',saved)
        assert vm.snapshot(time.monotonic()+5)['dsp_peer']==before['dsp_peer']
        final=stopped(vm)
    assert comparable(final)==comparable(whole)
    assert b''.join(p.pcm for p in packets(full_path))==b''.join(p.pcm for p in packets(first_path)+packets(last_path))


@pytest.mark.parametrize('source,frames',[(MONO_SOURCE,8),(COO_SOURCE,4)])
def test_native_mono_decode_duplicates_channels_and_matches_independent_oracle(tmp_path,source,frames):
    state,_,_=stream(source=source,setup=SETUP_MONO)
    path=tmp_path/'mono.audio'
    with engine(state,tmp_path/'run',audio=f'file,id=audio,path={path}')as vm:result=stopped(vm)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    peer=result['dsp_peer'];output=packets(path)
    raw=b''.join(p.pcm for p in output)
    oracle=json.loads(source.with_suffix('.json').read_text())
    assert peer['audio_source_channels']==1 and peer['audio_source_rate']==44100
    assert peer['audio_frames_decoded']==frames and peer['audio_source_frames']==frames*1152
    assert peer['audio_frames_played']==oracle['output_frames']==len(raw)//4
    assert peer['audio_completed'] and peer['audio_completions_acked']==1
    assert peer['audio_nonzero_samples']==oracle['nonzero_samples']>0
    assert peer['audio_pcm_fnv1a64']==oracle['output_pcm_fnv1a64']
    assert hashlib.sha256(raw).hexdigest()==oracle['output_pcm_sha256']
    assert raw[0::4]==raw[2::4] and raw[1::4]==raw[3::4]
    assert output[-1].header['eos']


@pytest.mark.parametrize('offset,value',[(1,0xf3),(2,0x5c),(3,0x44)])
def test_mono_format_mismatch_is_guarded_before_decode(tmp_path,offset,value):
    state,_,raw=stream(source=MONO_SOURCE,setup=SETUP_MONO)
    changed=bytearray(raw);changed[16+(offset^1)]=value
    state['regions'][1]['seeds'][0]['data']=bytes(changed)
    result=qemu_run(state,tmp_path/'mismatch',trace=False)
    assert result['stop']['reason']=='dsp_peer_audio_source_format_unmodeled',result['stop']
    assert result['dsp_peer']['audio_frames_decoded']==0


@pytest.mark.parametrize('index,value',[(1,8),(2,2),(21,1)])
def test_mono_setup_keeps_unrecovered_combinations_guarded(tmp_path,index,value):
    from support.mma_setup_fixtures import make, setup, stages
    changed=list(SETUP_MONO);changed[index]=value
    result=qemu_run(make(stages()['registered']+setup(changed)),tmp_path/'setup',trace=False)
    assert result['stop']['reason']=='dsp_peer_mma_setup_unmodeled',result['stop']
    assert result['dsp_peer']['mma_requests']==result['dsp_peer']['mma_replies']==1


def test_odd_source_byte_alignment_completes_without_extra_mp3_samples(tmp_path):
    source=SOURCE.with_name('ringtone-first-24-frames.mp3')
    state,steps,raw=stream(24,source=source)
    result=qemu_run(state,tmp_path/'odd',trace=False)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    p=result['dsp_peer']
    assert p['audio_frames_decoded']==24 and p['audio_source_frames']==24*1152
    assert p['audio_frames_played']==(24*1152*160+146)//147
    assert p['audio_completed'] and p['audio_completions_acked']==1
    assert p['audio_input_bytes']==0


@pytest.mark.parametrize('count,source,tail',[
    (8,SOURCE,b'\0\0'),
    (24,SOURCE.with_name('ringtone-first-24-frames.mp3'),b'\1'),
])
def test_alignment_does_not_hide_other_trailing_bytes(tmp_path,count,source,tail):
    state,steps,raw=stream(count,source=source,tail=tail)
    result=qemu_run(state,tmp_path/'bad-tail',trace=False)
    assert result['stop']['reason']=='dsp_peer_audio_truncated_source',result['stop']
    assert not result['dsp_peer']['audio_completed']


@pytest.mark.parametrize('source,setup',[(SOURCE,SETUP44),(MONO_SOURCE,SETUP_MONO),(COO_SOURCE,SETUP_MONO)])
def test_repeated_converted_stream_resets_filter_phase_and_epoch(tmp_path,source,setup):
    from support.mma_playback_fixtures import make
    from support.mma_mode_fixtures import transport_prefix
    from test_mma_reopen import cleanup
    _,steps,raw=stream(source=source,setup=setup)
    state=make(steps+cleanup()+steps[len(transport_prefix()):],raw)
    state['dsp_peer']['profile']='wire20-modexp1024-mma-stream-peer'
    output=tmp_path/'replay.audio'
    with engine(state,tmp_path/'engine',audio=f'file,id=audio,path={output}')as vm:result=stopped(vm)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    captured=packets(output);epochs=list(dict.fromkeys(p.header['epoch']for p in captured))
    assert len(epochs)==2
    oracle=(json.loads(source.with_suffix('.json').read_text()) if source!=SOURCE else
        json.loads(SOURCE.with_suffix('.json').read_text())['scalar_minimp3_integer_linear_oracle'])
    for epoch in epochs:
        stream_packets=[p for p in captured if p.header['epoch']==epoch]
        assert stream_packets[0].header['sequence']=='0' and stream_packets[-1].header['eos']
        assert hashlib.sha256(b''.join(p.pcm for p in stream_packets)).hexdigest()==oracle['output_pcm_sha256']


@pytest.mark.parametrize('words',[7,20,27])
def test_inline_counted_data_owns_exact_ring_words(tmp_path,words):
    from support.mma_playback_fixtures import make
    from support.fa_wire10_fixtures import frame
    from support.ringtone_fixtures import POLL,CLEAR
    _,steps,raw=stream(1)
    cut=next(i for i,s in enumerate(steps)if s[0]=='delay')
    body=[0x0804,words,1,0,1,0,0,words-6]+[0x1234]*(words-6)+[20]
    state=make(steps[:cut]+frame(body)+POLL[:20]+CLEAR,raw)
    state['dsp_peer']['profile']='wire20-modexp1024-mma-stream-peer'
    result=qemu_run(state,tmp_path/'inline',trace=False)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    peer=result['dsp_peer']
    assert peer['ring4_buffered_words']==words and peer['audio_frames_decoded']==1


@pytest.mark.parametrize('index,value',[(1,21),(2,3),(3,2),(4,2),(7,13)])
def test_inline_malformed_header_is_rejected_before_ownership(tmp_path,index,value):
    from support.mma_playback_fixtures import make
    from support.fa_wire10_fixtures import frame
    _,steps,raw=stream(1)
    cut=next(i for i,s in enumerate(steps)if s[0]=='delay')
    body=[0x0804,20,1,0,1,0,0,14]+[0x1234]*14+[20];body[index]=value
    state=make(steps[:cut]+frame(body),raw)
    state['dsp_peer']['profile']='wire20-modexp1024-mma-stream-peer'
    result=qemu_run(state,tmp_path/'guard',trace=False)
    assert result['stop']['reason']=='dsp_peer_ring_data_header_unmodeled',result['stop']
    assert result['dsp_peer']['ring4_buffered_words']==0
