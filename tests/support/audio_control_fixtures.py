"""Native FA audio control replies, query layouts, acknowledgement and guards."""


import argparse


import json


import struct


from pathlib import Path


from support.dsp_peer_fixtures import fixture, packet


from support.fa_dma_fixtures import arm, w


from support.fa_modexp_fixtures import ACK, POLL


from support.fa_wire10_fixtures import frame


from support.fixtures import qemu_run


from db2010.diagnostics.artifacts import identity


from db2010.paths import ROOT


PROFILE = 'wire20-modexp1024-peer'


CLEAR = [('r', 0xfa040010, 0, 4), w(0xf2000008, 4)]


def make(steps, raw=bytes(544), enabled=True):
    state = fixture(steps, source_bytes=224, limit=len(steps)*3+500)
    state['regions'][0]['size'] = 8192
    state['executable_ranges'][0]['end'] = 0x3000
    state['dsp_peer']['profile'] = PROFILE if enabled else 'wire10-message-wire20-variable-dma-sink'
    if enabled:
        state['device_registers'] = [r for r in state['device_registers'] if r['address'] != 0xfa040014]
    state['regions'][1]['size'] = 4096
    state['regions'][1]['seeds'] = [{'address': 0x4c000000, 'data': raw}]
    return state


def inline(opcode, size, logical=12, selector=0):
    raw = bytearray(size)
    struct.pack_into('<HH', raw, 0, opcode, selector)
    struct.pack_into('<H', raw, size-2, logical)
    return frame(struct.unpack('<'+'H'*(size//2), raw))


def iir(slot, enabled, route=3):
    raw = bytearray(58 if enabled else 12)
    struct.pack_into('<HHHH', raw, 0, 0x0910, slot, int(enabled), route)
    if enabled:
        raw[8:56] = bytes(range(48))
    struct.pack_into('<H', raw, len(raw)-2, 12)
    return frame(struct.unpack('<'+'H'*(len(raw)//2), raw))


def transport_prefix():
    # Complete an unrelated DMA request first, as the existing inline contract
    # requires. No firmware callback or OS signal is supplied by this fixture.
    return arm(control=0xa4492008)+[w(0xfa040014, 0x18), w(0xfa04000c, 0xdf)]+packet(7, 16)+POLL[:50]+CLEAR


def run(output):
    checks = []
    prefix = transport_prefix()

    def check(name, steps, raw=bytes(544), reason='instruction_limit', enabled=True):
        result = qemu_run(make(steps, raw, enabled), output/name, trace=True)
        assert result['stop']['reason'] == reason, (name, result['stop'])
        checks.append({'name': name, 'stop': result['stop'], 'passed': True})
        return result

    commands = [(0x0800, 46, 11), (0x0900, 6, 12), (0x0904, 8, 12),
                (0x0906, 12, 12), (0x0908, 10, 12), (0x090a, 8, 12),
                (0x0912, 36, 12), (0x0914, 46, 12)]
    for opcode, size, logical in commands:
        for selector in ([0, 6] if opcode == 0x0912 else [0, 4] if opcode == 0x0914 else [0]):
            steps = prefix+inline(opcode, size, logical, selector)+POLL[:20]
            r = check(f'offer-{opcode:04x}-{selector}', steps)
            peer = r['dsp_peer']
            assert peer['audio_requests'] == 1 and peer['audio_replies'] == 0
            assert peer['rx_mailbox_ready'] and peer['rx_inline'] and peer['phase'] == 6
            count = size//2 if opcode in (0x0912, 0x0914) else 2
            expected = [count << 11 | 10, opcode+1]+([selector]+[0]*(count-3) if count > 2 else [])+[logical]
            words = struct.unpack('<32H', bytes.fromhex(peer['rx_mailbox_hex']))
            assert list(words[:count+1]) == expected
            assert peer['event_status'] == 3 and peer['rx_bytes'] == 0
            assert not peer['audio_playback_modeled']
            r = check(f'ack-{opcode:04x}-{selector}', steps+ACK+CLEAR)
            peer = r['dsp_peer']
            assert not peer['rx_mailbox_ready'] and peer['phase'] == 4 and peer['audio_replies'] == 1
            assert peer['event_status'] == peer['rx_bytes'] == 0

    raw = bytearray((i*17)&255 for i in range(544))
    struct.pack_into('<HHH', raw, 0, 0x090c, 0, 1)
    struct.pack_into('<I', raw, 6, 256)
    struct.pack_into('<H', raw, 542, 12)
    upload = arm(control=0xa4492088)+packet(12, 272)+POLL
    r = check('coefficient-upload-DMA', prefix+upload, bytes(raw))
    peer = r['dsp_peer']
    assert peer['bytes_received'] == 544 and peer['audio_opcode'] == 0x090c
    assert bytes.fromhex(peer['payload_hex']) == raw and peer['rx_inline']
    assert bytes.fromhex(peer['rx_mailbox_hex'])[:6] == struct.pack('<3H', 0x100a, 0x090d, 12)
    r = check('DMA-ack-then-inline', prefix+upload+ACK+CLEAR+inline(0x0900, 6)+POLL[:20]+ACK, bytes(raw))
    assert r['dsp_peer']['audio_replies'] == 2
    small = bytearray(raw[:288])
    struct.pack_into('<I', small, 6, 128)
    struct.pack_into('<H', small, 286, 12)
    small_upload = arm(control=0xa4492048)+packet(12,144)+POLL
    r = check('128-tap-DMA-upload',prefix+small_upload+ACK,bytes(small))
    assert r['dsp_peer']['audio_replies'] == 1 and r['dsp_peer']['bytes_received'] == 288
    struct.pack_into('<I', small, 6, 64)
    check('unknown-tap-count',prefix+small_upload,bytes(small),reason='dsp_peer_audio_taps_unmodeled')
    r = check('inline-filter-disable', prefix+inline(0x090c, 10)+POLL[:20]+ACK)
    assert r['dsp_peer']['audio_replies'] == 1 and r['dsp_peer']['audio_opcode'] == 0x090c
    bad_filter = frame([0x090c, 0, 2, 0, 12])
    check('unknown-filter-enable', prefix+bad_filter+POLL[:20], reason='dsp_peer_audio_filter_unmodeled')
    programmed = prefix+iir(2,True)+POLL[:20]+ACK+CLEAR
    query = inline(0x0914,46,selector=2)+POLL[:10]
    r = check('programmed-slot-query', programmed+query)
    assert r['dsp_peer']['audio_iir_active'] == [0,0,1,0,0]
    words = struct.unpack('<32H', bytes.fromhex(r['dsp_peer']['rx_mailbox_hex']))
    assert words[22] == 1 and not any(words[3:22]) and words[23] == 12
    r = check('disabled-slot-query', programmed+query+ACK+CLEAR+iir(2,False)+POLL[:10]+ACK+CLEAR+query)
    assert r['dsp_peer']['audio_iir_active'] == [0]*5
    assert struct.unpack('<32H', bytes.fromhex(r['dsp_peer']['rx_mailbox_hex']))[22] == 0
    check('program-slot5',prefix+iir(5,True)+POLL[:20],reason='dsp_peer_audio_slot_unmodeled')
    check('program-route4',prefix+iir(0,True,4)+POLL[:20],reason='dsp_peer_audio_route_unmodeled')

    for name, packet_steps, reason in [
        ('unknown-opcode', inline(0x0902, 8), 'dsp_peer_audio_opcode_unmodeled'),
        ('wrong-channel-opcode', inline(0x0900, 6, 11), 'dsp_peer_audio_opcode_unmodeled'),
        ('short-query', inline(0x0912, 6), 'dsp_peer_audio_packet_unmodeled'),
        ('long-ack-request', inline(0x0900, 8), 'dsp_peer_audio_packet_unmodeled'),
        ('slot7', inline(0x0912, 36, selector=7), 'dsp_peer_audio_slot_unmodeled'),
        ('slot5', inline(0x0914, 46, selector=5), 'dsp_peer_audio_slot_unmodeled'),
    ]:
        r = check(name, prefix+packet_steps+POLL[:20], reason=reason)
        assert r['dsp_peer']['audio_requests'] == r['dsp_peer']['audio_replies'] == 0
    r = check('disabled-peer-still-sinks', prefix+inline(0x0900, 6)+POLL[:20], enabled=False)
    assert not r['dsp_peer']['application_reply_modeled'] and r['dsp_peer']['phase'] == 4
    return {'count': len(checks), 'checks': checks, 'firmware_injection': False,
            'engine': identity(ROOT/'qemu/build/qemu-system-arm')}
