"""Actual AAC bytes, exact output, native EOF and pointer-free codec migration."""
import copy
import hashlib
import json
import time
import pytest
from support.video_mma_fixtures import stream, COEFFICIENTS
from support.fixtures import qemu_run
from test_native_checkpoint import engine, stopped, command, migration, comparable
from test_audio_stream import packets
from test_audio_codec import codec
from support.mma_mode_fixtures import mode
from support.video_mma_fixtures import RECEIVE


def test_native_actual_aac_output_matches_continuous_reference(tmp_path):
    s,_,_ = stream()
    path = tmp_path/'aac.audio'
    with engine(s,tmp_path/'run',audio=f'file,id=audio,path={path}') as vm:
        r = stopped(vm)
    assert r['stop']['reason'] == 'instruction_limit',r['stop']
    p = r['dsp_peer']
    golden = json.loads(COEFFICIENTS.with_name('video-first-16-aac.json').read_text())
    data = b''.join(item.pcm for item in packets(path))
    assert p['audio_source_codec'] == 'aac-lc' and p['audio_aac_primed']
    assert p['audio_underruns_offered'] == p['audio_underruns_acked'] == 16
    assert not p['audio_underrun']
    assert p['audio_frames_decoded'] == 16 and p['audio_source_frames'] == 16384
    assert p['audio_completed'] and p['audio_completions_acked'] == 1
    assert p['audio_frames_played'] == len(data)//4 == golden['output_frames']
    assert hashlib.sha256(data).hexdigest() == golden['output_pcm_sha256']
    assert p['audio_pcm_fnv1a64'] == golden['output_pcm_fnv1a64']
    assert p['audio_nonzero_samples'] == golden['nonzero_samples'] > 0
    assert packets(path)[-1].header['eos']


def test_encoded_evidence_is_observational_and_uses_actual_source_units(tmp_path):
    compact,_,_ = stream()
    recorded = copy.deepcopy(compact); recorded['record_diagnostics'] = True
    a = qemu_run(compact,tmp_path/'compact',trace=False)
    b = qemu_run(recorded,tmp_path/'recorded',trace=False)
    peer = b['dsp_peer']
    entries = peer.pop('audio_encoded_frames')
    assert peer.pop('audio_encoded_frames_total') == len(entries) == 16
    assert peer.pop('audio_encoded_frames_dropped') == 0
    raw = COEFFICIENTS.with_name('video-first-16-aac.bin').read_bytes()
    at = 0
    for i,row in enumerate(entries):
        frame = bytes.fromhex(row['bytes_hex'])
        assert row['decoded_index'] == i and raw[at:at+len(frame)] == frame
        at += (len(frame)+1)&~1
    assert at == len(raw)
    for value in (a,b):
        for key in ('elapsed_seconds','qmp_events','initial'):
            value.pop(key,None)
    assert comparable(a) == comparable(b)


@pytest.mark.parametrize('offset', [350000,1050000])
def test_native_aac_overlap_noise_history_and_partial_output_restore(tmp_path,offset):
    s,steps,_ = stream()
    audio = [tmp_path/name for name in ('whole.audio','first.audio','last.audio')]
    with engine(s,tmp_path/'whole',audio=f'file,id=audio,path={audio[0]}') as vm: whole = stopped(vm)
    cut = next(i for i,step in enumerate(steps) if step[0] == 'delay')
    first = copy.deepcopy(s); first['instruction_limit'] = len(steps[:cut])*4+offset
    checkpoint = tmp_path/'aac.vmstate'
    with engine(first,tmp_path/'prefix',audio=f'file,id=audio,path={audio[1]}') as vm:
        before = stopped(vm)
        assert before['dsp_peer']['audio_aac_primed']
        assert before['dsp_peer']['audio_frames_decoded'] == 1
        if offset == 1050000:
            assert before['dsp_peer']['audio_sample_partial_frames'] > 0
            assert before['dsp_peer']['audio_underrun']
            assert before['dsp_peer']['audio_underruns_offered'] == 1
            assert before['dsp_peer']['audio_underruns_acked'] == 0
            assert before['dsp_peer']['audio_rx_kind'] == 3
        else:
            assert not before['dsp_peer']['audio_underrun']
            assert before['dsp_peer']['audio_pending_pcm_frames'] > 0
            assert before['dsp_peer']['audio_underruns_offered'] == 0
        layout = json.loads(command(vm,'qom-get',{'path':'/machine','property':'checkpoint-layout'}))
        migration(vm,'migrate',checkpoint)
    last = copy.deepcopy(s); last['instruction_limit'] -= first['instruction_limit']; last['checkpoint_tcm'] = layout
    with engine(last,tmp_path/'suffix',incoming=True,audio=f'file,id=audio,path={audio[2]}') as vm:
        migration(vm,'migrate-incoming',checkpoint)
        assert vm.snapshot(time.monotonic()+5)['dsp_peer'] == before['dsp_peer']
        final = stopped(vm)
    assert comparable(final) == comparable(whole)
    assert b''.join(p.pcm for p in packets(audio[0])) == b''.join(p.pcm for p in packets(audio[1])+packets(audio[2]))


def test_active_aac_mode0_cannot_reset_a_running_decoder(tmp_path):
    _,steps,raw = stream()
    cut = next(i for i,step in enumerate(steps) if step[0] == 'delay')
    from support.video_mma_fixtures import state
    r = qemu_run(state(steps[:cut]+mode(0),raw),tmp_path/'duplicate',trace=False)
    assert r['stop']['reason'] == 'dsp_peer_audio_start_lifecycle_unmodeled'
    assert r['dsp_peer']['audio_frames_decoded'] == 1
    assert not r['dsp_peer']['audio_underrun']


def test_stopping_underrun_aac_acknowledges_and_ends_only_elapsed_pcm(tmp_path):
    _,steps,raw = stream()
    cut = next(i for i,step in enumerate(steps) if step[0] == 'delay')
    from support.video_mma_fixtures import state
    r = qemu_run(state(steps[:cut+1]+RECEIVE+mode(1)+RECEIVE,raw),tmp_path/'stop',trace=False)
    assert r['stop']['reason'] == 'instruction_limit',r['stop']
    p = r['dsp_peer']
    assert p['audio_interrupted'] and not p['audio_underrun'] and not p['audio_active']
    assert p['audio_frames_played'] == 3070 and not p['audio_completed']
    assert p['audio_completions_acked'] == 0 and p['audio_sample_ended']


@pytest.mark.parametrize('chunk', [1,2,7,64,500,1588])
def test_aac_framing_is_independent_of_source_fragmentation(codec,chunk):
    source = COEFFICIENTS.with_name('video-first-16-aac.bin')
    r = codec(source.read_bytes(),16000,2,chunk=chunk,source_codec=1)
    assert r.returncode == 0,r.stderr
    golden = json.loads(source.with_suffix('.json').read_text())
    assert hashlib.sha256(r.stdout).hexdigest() == golden['output_pcm_sha256']


@pytest.mark.parametrize('offset,value', [(0,0),(3,0),(5,0),(7,1),(15,1),(8,0)])
def test_aac_unknown_header_alignment_and_syntax_are_guarded(codec,offset,value):
    raw = bytearray(COEFFICIENTS.with_name('video-first-16-aac.bin').read_bytes())
    raw[offset] = value
    r = codec(raw,16000,2,chunk=7,source_codec=1)
    assert r.returncode == 2
    assert r.stderr.startswith(b'dsp_peer_aac_') and not r.stdout


def test_native_generated_counter_is_opaque_to_delivered_aac_decode(codec):
    source = COEFFICIENTS.with_name('video-first-16-aac.bin')
    golden = json.loads(source.with_suffix('.json').read_text())
    raw = bytearray(source.read_bytes()); at = 0
    for i, count in enumerate(golden['aac_unit_padded_bytes']):
        raw[at+6] = (255-i*3) & 255
        at += count
    r = codec(raw,16000,2,source_codec=1)
    assert r.returncode == 0,r.stderr
    assert hashlib.sha256(r.stdout).hexdigest() == golden['output_pcm_sha256']


@pytest.mark.parametrize('rate,channels', [(48000,2),(16000,1)])
def test_aac_setup_mismatch_is_guarded(codec,rate,channels):
    raw = COEFFICIENTS.with_name('video-first-16-aac.bin').read_bytes()
    r = codec(raw,rate,channels,source_codec=1)
    assert r.returncode == 2 and r.stderr.startswith(b'dsp_peer_audio_source_format_unmodeled')
