"""Native QEMU DSP peer transport, read-clear status and IRQ9 guard fixtures."""
import argparse
import json
from pathlib import Path

from support.fa_dma_fixtures import state, arm, w
from support.fixtures import qemu_run, words

TIMER = {'profile': 'tick1-upcounter-irq-assumption', 'frequency_hz': 131072,
         'max_irqs': 100}


def packet(logical=2, length=112):
    return [w(0xfa040040, 0x1814, 2), w(0xfa040042, 2, 2),
            w(0xfa040044, logical, 2), w(0xfa040046, length, 2), w(0xfa040000, 1)]


def fixture(steps, *, latency=2, limit=None, source_bytes=224):
    s = state(steps, source_bytes)
    s['timer_irq'] = TIMER
    s['dsp_peer'] = {'profile': 'wire20-op2-logical2-sink', 'latency_ns': latency}
    s['device_registers'] = [{'address': 0xfa040014, 'width': 4, 'initial_value': 0,
                              'read_or_mask': 0, 'allowed_writes': [0, 0x10, 0x18], 'name': 'fixture.mode'}]
    s['device_registers'] += [
        {'address': a, 'width': 4, 'initial_value': 0xffffffff if a & 0xff == 4 else None,
         'read_or_mask': 0, 'name': 'fixture.intcon.' + hex(a),
         'allowed_writes': [0xfffffdff] if a & 0xff == 4 else [1]}
        for a in (0xfb020104, 0xfb020204, 0xfb020304, 0xfb020118, 0xfb020218, 0xfb020318)]
    s['instruction_limit'] = limit or len(steps) * 3 + 180
    return s


def run(output):
    checks = []
    prefix = arm() + [w(0xfa040014, 0x18), w(0xfa04000c, 0xdf)]

    def check(name, s, reason):
        result = qemu_run(s, output / name, trace=True)
        assert result['stop']['reason'] == reason, (name, result['stop'])
        checks.append({'name': name, 'passed': True, 'stop': result['stop']})
        return result

    for latency in (2, 16):
        r = check(f'complete-latency{latency}', fixture(prefix + packet(), latency=latency), 'instruction_limit')
        assert bytes.fromhex(r['dsp_peer']['payload_hex']) == bytes(range(224))
        assert r['dsp_peer']['event_status'] == 9 and r['dsp_peer']['phase'] == 4
        assert r['dsp_peer']['requests'] == r['dsp_peer']['mailbox_events'] == r['dsp_peer']['transfer_events'] == 1
        assert r['pl080']['raw_tc'] == 4 and r['pl080']['channels'][2]['source'] == 0x4c0000e0
        assert r['pl080']['channels'][2]['control'] == 0xa4492000
        assert not r['fa_mailbox_tx']['busy'] and not r['fa_dma']['armed']
    r = check('before-completion-no-TC', fixture(prefix + packet(), latency=16,
              limit=len(prefix + packet()) * 3 + 5), 'instruction_limit')
    # Native virtual time permits early DMA words before this observation.
    assert 0 <= r['dsp_peer']['bytes_received'] < 224
    assert r['pl080']['raw_tc'] == 0
    for times in (1, 2):
        polls = [('r', 0xfa040000, 0, 4)] * 50
        r = check('status-read-clear-' + str(times), fixture(prefix + packet() + polls +
                  [('r', 0xfa040010, 0, 4)] * times + [w(0xf2000008, 4)]), 'instruction_limit')
        assert r['registers'][2] == (9 if times == 1 else 0)
        assert r['dsp_peer']['event_status'] == 0 and r['pl080']['raw_tc'] == 0
    for name, steps, reason in [
        ('wrong-logical', prefix + packet(logical=3), 'dsp_peer_packet_contract_unmodeled'),
        ('wrong-length', prefix + packet(length=110), 'dsp_peer_packet_contract_unmodeled'),
        ('no-dma', [w(0xfa040014, 0x18)] + packet(), 'dsp_peer_pending_dma_unmodeled'),
        ('no-mode', arm() + packet(), 'dsp_peer_mode_unmodeled'),
        ('bad-mask', [w(0xfa04000c, 1)], 'dsp_peer_event_mask_unmodeled'),
        ('status-write', [w(0xfa040010, 1)], 'dsp_peer_status_write_unmodeled'),
        ('status-width', [w(0xfa040010, 0, 2)], 'dsp_peer_access_unmodeled'),
    ]:
        r = check(name, fixture(steps), reason)
        assert r['dsp_peer']['requests'] == r['dsp_peer']['bytes_received'] == 0
        assert r['pl080']['raw_tc'] == 0
    # An interrupt-masked fixture cannot run ISR code, even with a pending source.
    r = check('masked-primary9', fixture(prefix + packet()), 'instruction_limit')
    assert r['timer_irq']['dsp_irqs'] == 0 and not r['irq_events']
    # Let native ARM exception entry select the primary source and acknowledge it.
    for name, ack, reason in [('primary9-native-ack', 0xfb020118, 'instruction_limit'),
                              ('primary9-wrong-bank', 0xfb020218, 'irq_ack_wrong_bank')]:
        s = fixture([w(0xfb020104, 0xfffffdff)] + prefix + packet())
        s['cpsr'] = 0x53
        s['regions'] += [
            {'name': 'fixture.vector', 'address': 0, 'size': 4096, 'readonly': True,
             'seeds': [{'address': 0x18, 'data': words(0xea0007f8)}]},
            {'name': 'fixture.isr', 'address': 0x2000, 'size': 4096, 'readonly': True,
             'seeds': [{'address': 0x2000, 'data': words(
                 0xe59f4018, 0xe5943000, 0xe59f4014, 0xe5945000,
                 0xe59f4010, 0xe3a06001, 0xe5846000, 0xe25ef004,
                 0xfb020110, 0xfa040010, ack)}]}]
        s['executable_ranges'] += [{'start': 0x18, 'end': 0x1c}, {'start': 0x2000, 'end': 0x2020}]
        r = check(name, s, reason)
        assert r['timer_irq']['dsp_irqs'] >= 1 and r['registers'][3] == 9
    return {'count': len(checks), 'checks': checks, 'DSP_instruction_execution': False,
            'fixture_IRQ_handler': 'Controlled ARM fixture, not original firmware; boot probes use original handlers.'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    r = run(a.output)
    (a.output / 'verification.json').write_text(json.dumps(r, indent=2) + '\n')
    print(json.dumps({'passed': r['count']}))
