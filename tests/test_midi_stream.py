"""Timed guest event ingress, real PCM sink, lifecycle and migration."""
import copy
import json
import struct
import time

from support.mma_playback_fixtures import make as timed_state
from support.fa_wire10_fixtures import frame
from test_midi_setup import PROFILE, prepared_prefix, POLL, ACK, CLEAR
from test_native_checkpoint import engine, stopped, command, migration, comparable
from test_audio_stream import packets


def state(steps):
    result = timed_state(steps, bytes(544))
    result['dsp_peer']['profile'] = PROFILE
    result['record_diagnostics'] = False
    return result


def delay(count):
    return [('delay', count, 0, 0)]


def started(end=960, markers=1):
    data = [0x90, 69, 127, 0, 0, 0x80, 69, 0, end, 0] + [0] * (markers * 5)
    return (prepared_prefix() + ACK + CLEAR + POLL[:20] + ACK + CLEAR +
            frame([0x401, 750, 20]) + POLL[:20] + CLEAR +
            frame([0x801, len(data), *data, 20]) + POLL[:20] + CLEAR +
            frame([0x608, 0, 9]) + POLL[:20] + ACK + CLEAR +
            POLL[:20] + ACK + CLEAR + frame([0x60a, 0, 0, 9]) + POLL[:20] + CLEAR)


def finished(markers=1):
    return (started(markers=markers) + delay(300000) + POLL[:20] + ACK + CLEAR +
            delay(800000) + POLL[:20] + ACK + CLEAR)


def test_timed_pcm_sink_and_absent_sink_are_identical(tmp_path):
    prepared = state(finished())
    with engine(prepared, tmp_path/'absent') as vm:
        absent = stopped(vm)
    path = tmp_path/'midi.audio'
    with engine(prepared, tmp_path/'sink', audio=f'file,id=audio,path={path}') as vm:
        captured = stopped(vm)
    assert comparable(absent) == comparable(captured)
    assert captured['stop']['reason'] == 'instruction_limit'
    peer = captured['dsp_peer']
    assert peer['midi_frames'] == 960 + 3072
    assert peer['midi_events'] == 2 and peer['midi_completed']
    assert peer['midi_completion_sent'] and peer['midi_nonzero_samples'] > 0
    assert peer['midi_words_received'] == peer['midi_words_credited'] == 15
    chunks = packets(path)
    assert sum(p.header['frame_count'] for p in chunks) == 4032
    assert chunks[-1].header['eos']
    assert all(p.header['frame_count'] <= 960 for p in chunks)


def test_partial_triangle_packet_and_voice_phase_survive_checkpoint(tmp_path):
    prefix_steps = started() + delay(70000)
    prepared = state(finished())
    first = copy.deepcopy(prepared)
    first['instruction_limit'] = state(prefix_steps)['instruction_limit'] - 500
    paths = [tmp_path/n for n in ('whole.audio', 'first.audio', 'last.audio')]
    with engine(prepared, tmp_path/'whole', audio=f'file,id=audio,path={paths[0]}') as vm:
        whole = stopped(vm)
    saved = tmp_path/'midi.vmstate'
    with engine(first, tmp_path/'first', audio=f'file,id=audio,path={paths[1]}') as vm:
        before = stopped(vm)
        assert 0 < before['dsp_peer']['midi_frames'] < 960
        layout = json.loads(command(vm, 'qom-get', {'path':'/machine', 'property':'checkpoint-layout'}))
        migration(vm, 'migrate', saved)
    suffix = copy.deepcopy(prepared)
    suffix['instruction_limit'] -= first['instruction_limit']
    suffix['checkpoint_tcm'] = layout
    with engine(suffix, tmp_path/'last', incoming=True, audio=f'file,id=audio,path={paths[2]}') as vm:
        migration(vm, 'migrate-incoming', saved)
        assert vm.snapshot(time.monotonic()+5)['dsp_peer'] == before['dsp_peer']
        final = stopped(vm)
    assert comparable(final) == comparable(whole)
    original = packets(paths[0]); resumed = packets(paths[1]) + packets(paths[2])
    assert b''.join(p.pcm for p in original) == b''.join(p.pcm for p in resumed)
    assert [(p.header['first_frame'], p.header['frame_count']) for p in original] == [
        (p.header['first_frame'], p.header['frame_count']) for p in resumed]


def test_pause_keeps_virtual_musical_position(tmp_path):
    steps = started(9600) + delay(100000) + frame([0x608, 1, 9]) + POLL[:20] + ACK + CLEAR
    with engine(state(steps), tmp_path/'pause') as vm:
        paused = stopped(vm)['dsp_peer']
    with engine(state(steps + delay(1000000)), tmp_path/'wait') as vm:
        waiting = stopped(vm)['dsp_peer']
    assert paused['midi_paused'] and not paused['midi_active']
    assert paused['midi_frames'] == waiting['midi_frames']
    assert paused['midi_pcm_fnv1a64'] == waiting['midi_pcm_fnv1a64']
    resume = steps + delay(1000000) + frame([0x608, 0, 9]) + POLL[:20] + ACK + CLEAR + delay(200000)
    with engine(state(resume), tmp_path/'resume') as vm:
        resumed = stopped(vm)
    assert resumed['stop']['reason'] == 'instruction_limit'
    assert resumed['dsp_peer']['midi_active']
    assert resumed['dsp_peer']['midi_frames'] > waiting['midi_frames']


def test_stop_flush_close_and_fresh_prepare(tmp_path):
    steps = (started(9600) + delay(100000) + frame([0x608, 2, 9]) + POLL[:20] + ACK + CLEAR +
             frame([0x701, 20]) + POLL[:20] + ACK + CLEAR +
             frame([0x101, 20]) + POLL[:20] + CLEAR +
             frame([0x606, 9]) + POLL[:20] + ACK + CLEAR)
    path = tmp_path/'stop.audio'
    with engine(state(steps), tmp_path/'stop', audio=f'file,id=audio,path={path}') as vm:
        stopped_peer = stopped(vm)
    assert stopped_peer['stop']['reason'] == 'instruction_limit'
    peer = stopped_peer['dsp_peer']
    assert not peer['midi_prepared'] and not peer['midi_active']
    assert not peer['midi_buffered_words'] and not peer['midi_credit']
    assert peer['midi_words_credited'] == peer['midi_words_received'] == 15
    assert not peer['midi_completion_sent']
    chunks = packets(path)
    assert chunks[-1].header['eos']
    assert sum(p.header['frame_count'] for p in chunks) == peer['midi_frames']
    with engine(state(steps + frame([0x604, 9]) + POLL[:20] + ACK + CLEAR + POLL[:20] + ACK + CLEAR),
                tmp_path/'fresh') as vm:
        fresh = stopped(vm)
    assert fresh['stop']['reason'] == 'instruction_limit'
    assert fresh['dsp_peer']['midi_prepared'] and not fresh['dsp_peer']['midi_frames']


def test_repeated_native_end_markers_do_not_extend_tail_or_duplicate_events(tmp_path):
    with engine(state(finished(markers=2)), tmp_path/'repeated') as vm:
        result = stopped(vm)
    assert result['stop']['reason'] == 'instruction_limit'
    peer = result['dsp_peer']
    assert peer['midi_frames'] == 4032 and peer['midi_events'] == 2
    assert peer['midi_words_credited'] == peer['midi_words_received'] == 20
    assert peer['midi_completion_sent']
    steps = (started() + delay(300000) + POLL[:20] + ACK + CLEAR +
             frame([0x801, 5, 0, 0, 0, 0, 0, 20]) + POLL[:20] + ACK + CLEAR +
             delay(800000) + POLL[:20] + ACK + CLEAR)
    with engine(state(steps), tmp_path/'late') as vm:
        late = stopped(vm)
    assert late['stop']['reason'] == 'instruction_limit'
    assert late['dsp_peer']['midi_frames'] == 4032
    assert late['dsp_peer']['midi_words_credited'] == 20
    steps = finished() + frame([0x801, 5, 0, 0, 0, 0, 0, 20]) + POLL[:20] + ACK + CLEAR
    with engine(state(steps), tmp_path/'completed') as vm:
        completed = stopped(vm)
    assert completed['stop']['reason'] == 'instruction_limit'
    assert completed['dsp_peer']['midi_frames'] == 4032
    assert not completed['dsp_peer']['midi_buffered_words']
    assert completed['dsp_peer']['midi_words_credited'] == 20
    bad = finished() + frame([0x801, 5, 0x90, 69, 127, 0, 0, 20])
    with engine(state(bad), tmp_path/'data-after-end') as vm:
        guarded = stopped(vm)
    assert guarded['stop']['reason'] == 'dsp_peer_midi_data_after_end_unmodeled'
    assert guarded['dsp_peer']['midi_words_received'] == 15
    assert guarded['dsp_peer']['midi_frames'] == 4032


def test_position_query_ignores_uninitialized_request_scratch_words(tmp_path):
    steps = (started(9600) + delay(100000) + frame([0x608, 1, 9]) + POLL[:20] + ACK + CLEAR +
             frame([0x61a, 0xa55a, 0xbeef, 9]) + POLL[:20])
    with engine(state(steps), tmp_path/'position') as vm:
        result = stopped(vm)
    assert result['stop']['reason'] == 'instruction_limit'
    peer = result['dsp_peer']
    words = struct.unpack('<32H', bytes.fromhex(peer['rx_mailbox_hex']))
    assert words[:5] == (0x200a, 0x61b, peer['midi_frames'], 0, 9)
    assert peer['midi_paused']
