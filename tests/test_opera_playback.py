"""Native Opera setup, guarded coefficient ownership and checkpoint continuation."""
import copy
import json
import struct
import time
import hashlib
import pytest
from support.opera_fixtures import state,configured,upload,coefficients_offered,coefficient_packet,RECEIVE,POLL,CLEAR
from support.fixtures import qemu_run
from test_native_checkpoint import engine,stopped,command,migration,comparable
from test_audio_stream import packets
from support.opera_fixtures import stream,SOURCE


def test_actual_mpeg2_samples_and_completion_match_independent_reference(tmp_path):
    initial,_,_=stream();path=tmp_path/'opera.audio'
    with engine(initial,tmp_path/'run',audio=f'file,id=audio,path={path}')as vm:result=stopped(vm)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    p=result['dsp_peer'];golden=json.loads(SOURCE.with_suffix('.json').read_text())
    raw=b''.join(packet.pcm for packet in packets(path))
    assert p['audio_coefficient_ready'] and p['audio_source_rate']==22050
    assert p['audio_frames_decoded']==16 and p['audio_source_frames']==16*576
    assert p['audio_completed'] and p['audio_completions_acked']==1
    assert p['audio_frames_played']==golden['output_frames']==len(raw)//4
    assert p['audio_nonzero_samples']==golden['nonzero_samples']>0
    assert p['audio_pcm_fnv1a64']==golden['output_pcm_fnv1a64']
    assert hashlib.sha256(raw).hexdigest()==golden['output_pcm_sha256']
    assert packets(path)[-1].header['eos']


@pytest.mark.parametrize('offset,value',[(1,0xfb),(2,0xf2),(2,0x86),(3,0xc8)])
def test_invalid_or_setup_mismatched_mpeg2_headers_are_guarded(tmp_path,offset,value):
    initial,_,raw=stream();changed=bytearray(raw);changed[16+(offset^1)]=value
    initial['regions'][1]['seeds'][0]['data']=bytes(changed)
    result=qemu_run(initial,tmp_path/'bad-format',trace=False)
    assert result['stop']['reason']=='dsp_peer_audio_source_format_unmodeled',result['stop']
    assert result['dsp_peer']['audio_frames_decoded']==0


def test_odd_final_mpeg2_source_byte_has_only_transport_alignment(tmp_path):
    source=tmp_path/'odd.mp3';source.write_bytes(SOURCE.read_bytes()[:-209])
    initial,_,_=stream(source)
    result=qemu_run(initial,tmp_path/'odd',trace=False)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    p=result['dsp_peer']
    assert p['audio_frames_decoded']==15 and p['audio_source_frames']==15*576
    assert p['audio_frames_played']==(15*576*320+146)//147
    assert p['audio_completed'] and not p['audio_input_bytes']


@pytest.mark.parametrize('offset',[5000,430000])
def test_mpeg2_converter_and_partial_packet_restore_with_coefficient_state(tmp_path,offset):
    initial,steps,raw=stream()
    paths=[tmp_path/name for name in ('whole.audio','first.audio','last.audio')]
    with engine(initial,tmp_path/'whole',audio=f'file,id=audio,path={paths[0]}')as vm:whole=stopped(vm)
    assert whole['stop']['reason']=='instruction_limit',whole['stop']
    first=copy.deepcopy(initial);cut=next(i for i,s in enumerate(steps)if s[0]=='delay')
    first['instruction_limit']=len(steps[:cut])*4+offset
    saved=tmp_path/'mpeg2.vmstate'
    with engine(first,tmp_path/'prefix',audio=f'file,id=audio,path={paths[1]}')as vm:
        before=stopped(vm);p=before['dsp_peer']
        assert p['audio_coefficient_ready'] and p['audio_resampler_phase']>0
        if offset==5000:assert p['audio_pending_pcm_frames']>1152
        else:assert p['audio_sample_partial_frames']>0
        layout=json.loads(command(vm,'qom-get',{'path':'/machine','property':'checkpoint-layout'}))
        migration(vm,'migrate',saved)
    last=copy.deepcopy(initial);last['instruction_limit']-=first['instruction_limit'];last['checkpoint_tcm']=layout
    with engine(last,tmp_path/'suffix',incoming=True,audio=f'file,id=audio,path={paths[2]}')as vm:
        migration(vm,'migrate-incoming',saved)
        assert vm.snapshot(time.monotonic()+5)['dsp_peer']==before['dsp_peer']
        final=stopped(vm)
    assert comparable(final)==comparable(whole)
    assert b''.join(p.pcm for p in packets(paths[0]))==b''.join(p.pcm for p in packets(paths[1])+packets(paths[2]))


def test_known_coefficient_payload_offers_minimal_native_ack(tmp_path):
    result=qemu_run(state(coefficients_offered()),tmp_path/'offered',trace=False)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    p=result['dsp_peer']
    assert p['audio_source_rate']==22050 and p['audio_source_channels']==2
    assert p['audio_coefficient_ready'] and p['mma_configured']
    assert p['audio_frames_decoded']==0 and not p['audio_active']
    assert struct.unpack('<32H',bytes.fromhex(p['rx_mailbox_hex']))[:3]==(0x100a,0xe03,18)
    assert p['rx_mma'] and p['rx_mailbox_ready']
    assert p['mma_requests']==3 and p['mma_replies']==2
    result=qemu_run(state(coefficients_offered()+RECEIVE),tmp_path/'acked',trace=False)
    assert result['stop']['reason']=='instruction_limit',result['stop']
    assert result['dsp_peer']['mma_replies']==3 and not result['dsp_peer']['rx_mailbox_ready']


@pytest.mark.parametrize('offset,value',[(0,0xe04),(2,1),(4,1),(6,31),(8,0),(70,0),(94,20)])
def test_unknown_coefficients_are_guarded_before_processing_acceptance(tmp_path,offset,value):
    raw=bytearray(coefficient_packet());struct.pack_into('<H',raw,offset,value)
    result=qemu_run(state(coefficients_offered(),bytes(raw)),tmp_path/'bad',trace=False)
    assert result['stop']['reason']=='dsp_peer_mma_coefficients_unmodeled',result['stop']
    assert not result['dsp_peer']['audio_coefficient_ready']
    assert result['dsp_peer']['mma_requests']==result['dsp_peer']['mma_replies']==2


def test_duplicate_coefficient_upload_remains_guarded(tmp_path):
    result=qemu_run(state(coefficients_offered()+RECEIVE+upload()),tmp_path/'again',trace=False)
    assert result['stop']['reason']=='dsp_peer_mma_coefficients_lifecycle_unmodeled',result['stop']
    assert result['dsp_peer']['audio_coefficient_ready']


@pytest.mark.parametrize('phase',['dma','offered','acknowledged'])
def test_coefficient_dma_reply_and_ready_state_survive_checkpoint(tmp_path,phase):
    steps=coefficients_offered()+RECEIVE
    whole_state=state(steps);whole_state['record_diagnostics']=False
    with engine(whole_state,tmp_path/'whole')as vm:whole=stopped(vm)
    assert whole['stop']['reason']=='instruction_limit',whole['stop']
    first=copy.deepcopy(whole_state)
    first['instruction_limit']={'dma':len(configured()+upload())*4+32,
        'offered':len(coefficients_offered())*4,'acknowledged':len(steps)*4}[phase]
    checkpoint=tmp_path/'coefficient.vmstate'
    with engine(first,tmp_path/'prefix')as vm:
        before=stopped(vm);p=before['dsp_peer']
        assert p['audio_coefficient_ready']==(phase!='dma')
        if phase=='dma':assert p['logical_channel']==18 and 0<p['bytes_received']<96
        if phase=='offered':assert p['rx_mailbox_ready']
        layout=json.loads(command(vm,'qom-get',{'path':'/machine','property':'checkpoint-layout'}))
        migration(vm,'migrate',checkpoint)
    last=copy.deepcopy(whole_state);last['instruction_limit']-=first['instruction_limit'];last['checkpoint_tcm']=layout
    with engine(last,tmp_path/'restored',incoming=True)as vm:
        migration(vm,'migrate-incoming',checkpoint)
        assert vm.snapshot(time.monotonic()+5)['dsp_peer']==before['dsp_peer']
        final=stopped(vm)
    assert comparable(final)==comparable(whole)
