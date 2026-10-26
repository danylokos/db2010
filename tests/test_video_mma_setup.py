"""Video setup/filter acknowledgements, short prefill and checkpoint state."""
import copy
import struct
import pytest
from support.video_mma_fixtures import SETUP, configured, upload, offered, state, coefficient_packet, RECEIVE, POLL, CLEAR
from support.mma_ring_fixtures import stages
from support.fa_wire10_fixtures import frame
from support.fixtures import qemu_run
from test_native_checkpoint import engine, stopped, migration, comparable


def test_video_setup_filter_acknowledgements(tmp_path):
    r = qemu_run(state(), tmp_path/'filter', trace=False)
    assert r['stop']['reason'] == 'instruction_limit', r['stop']
    p = r['dsp_peer']
    assert p['audio_source_rate'] == 16000 and p['audio_source_channels'] == 2
    assert p['audio_coefficient_ready'] and p['mma_configured']
    assert p['audio_frames_decoded'] == 0 and not p['audio_active']
    assert struct.unpack('<32H', bytes.fromhex(p['rx_mailbox_hex']))[:3] == (0x100a, 0xe03, 18)


@pytest.mark.parametrize('index,value', [(1,5),(2,0),(3,2),(4,1),(7,2),(13,48),(23,0xcc02)])
def test_video_setup_rejects_unrecovered_fields(tmp_path,index,value):
    changed = SETUP.copy(); changed[index] = value
    r = qemu_run(state(stages()['registered']+frame(changed)), tmp_path/'setup', trace=False)
    assert r['stop']['reason'] == 'dsp_peer_mma_setup_unmodeled'
    assert r['dsp_peer']['mma_requests'] == 1


@pytest.mark.parametrize('offset,value', [(0,0xe04),(2,1),(4,1),(6,32),(8,0),(102,0),(126,20)])
def test_video_filter_rejects_unknown_coefficients(tmp_path,offset,value):
    raw = bytearray(coefficient_packet()); struct.pack_into('<H',raw,offset,value)
    r = qemu_run(state(raw=raw),tmp_path/'bad-filter',trace=False)
    assert r['stop']['reason'] == 'dsp_peer_mma_coefficients_unmodeled'
    assert not r['dsp_peer']['audio_coefficient_ready']


def test_native_short_aac_prefill_waits_for_mode0(tmp_path):
    # Actual first frame:6 ring metadata words plus8 source halfwords.
    words = [0x0804,14,1,1,1,0x0c41,0xa648,8,0xfff1,0x6080,0x007f,0xff00,0x2000,0,0,0x0e00,20]
    r = qemu_run(state(offered()+RECEIVE+frame(words)+POLL[:100]+CLEAR),tmp_path/'prefill',trace=False)
    assert r['stop']['reason'] == 'instruction_limit', r['stop']
    p = r['dsp_peer']
    assert p['ring4_buffered_words'] == 14 and p['mma_mode'] == 3
    assert not p['audio_active'] and p['audio_input_bytes'] == p['audio_frames_decoded'] == 0


@pytest.mark.parametrize('phase', ['dma','offered','acknowledged'])
def test_video_filter_native_checkpoint(tmp_path,phase):
    s = state(offered()+RECEIVE); s['record_diagnostics'] = False
    with engine(s,tmp_path/'whole') as vm: whole = stopped(vm)
    first = copy.deepcopy(s)
    first['instruction_limit'] = {'dma':len(configured()+upload())*4+32,
        'offered':len(offered())*4,'acknowledged':len(offered()+RECEIVE)*4}[phase]
    checkpoint = tmp_path/'filter.vmstate'
    with engine(first,tmp_path/'prefix') as vm:
        before = stopped(vm)
        assert before['dsp_peer']['audio_coefficient_ready'] == (phase != 'dma')
        migration(vm,'migrate',checkpoint)
    last = copy.deepcopy(s); last['instruction_limit'] -= first['instruction_limit']
    with engine(last,tmp_path/'restored',incoming=True) as vm:
        migration(vm,'migrate-incoming',checkpoint)
        final = stopped(vm)
    assert comparable(final) == comparable(whole)
