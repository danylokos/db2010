"""Native QEMU checks for FA DMA arming, pending mailbox and pre-effect guards."""
import argparse
import json
from pathlib import Path

from support.fixtures import base_state, qemu_run, words


def state(steps, source_bytes=224):
    code = []
    literals = []
    for i, (op, address, value, width) in enumerate(steps):
        offset = (len(steps) * 3 + 1) * 4 + i * 8 - (i * 12 + 8)
        code.extend([0xe59f0000 | offset, 0xe59f1000 | offset,
                     {('w', 4): 0xe5810000, ('w', 2): 0xe1c100b0,
                      ('w', 1): 0xe5c10000, ('r', 4): 0xe5912000,
                      ('r', 2): 0xe1d120b0}[(op, width)]])
        literals.extend([value, address])
    code.append(0xeafffffe)
    s = base_state(words(*code, *literals), limit=len(code) + 8)
    s['pl080_dma'] = {'profile': 'cold-disabled-display-stream-fa-tx',
                      'address': 0xf2000000, 'channels': 8}
    s['pdi'] = {'profile': 'r61500-write-packets'}
    s['fa_mailbox_tx'] = {'profile': 'outbound-single-slot'}
    s['regions'].append({'name': 'fixture.source', 'address': 0x4c000000,
                         'size': 4096, 'seeds': [{'address': 0x4c000000,
                                                'data': bytes(range(source_bytes))}]})
    return s


def w(address, value, width=4):
    return 'w', address, value, width


def arm(**changes):
    values = {'global': 1, 'source': 0x4c000000, 'destination': 0xfa040020,
              'lli': 0, 'control': 0xa4492038, 'configuration': 0xc8c6}
    values.update(changes)
    return [w(0xf2000030, values['global']),
            *[w(0xf2000140 + 4 * i, values[k]) for i, k in enumerate(
                ['source', 'destination', 'lli', 'control', 'configuration'])],
            w(0xf2000150, 0xc8c7)]


def packet(count=3):
    return [w(0xfa040040, count << 11 | 20, 2),
            *[w(0xfa040042 + 2 * i, i + 1, 2) for i in range(count)],
            w(0xfa040000, 1)]


def run(output):
    checks = []

    def check(name, steps, reason, source_bytes=224):
        result = qemu_run(state(steps, source_bytes), output / name, trace=True)
        assert result['stop']['reason'] == reason, (name, result['stop'])
        assert result['fa_dma']['transferred_bytes'] == 0
        assert not result['fa_dma']['completion_supplied']
        assert result['pl080']['raw_tc'] == result['pl080']['raw_error'] == 0
        checks.append({'name': name, 'passed': True, 'stop': result['stop']})
        return result

    result = check('arm-waits-no-data-or-irq', arm(), 'instruction_limit')
    assert result['fa_dma']['armed'] and result['pl080']['channels'][2]['source'] == 0x4c000000
    for field, value in [('global', 0), ('destination', 0xfa040024),
                         ('source', 0x4c000001), ('lli', 0x1000), ('control', 0xa4492037)]:
        reason = 'fa_dma_source_unmodeled' if field == 'source' else 'fa_dma_configuration_unmodeled'
        result = check('bad-' + field, arm(**{field: value}), reason)
        assert not result['fa_dma']['armed']
    result = check('unknown-last-source-byte', arm(), 'fa_dma_source_unknown', 223)
    assert not result['fa_dma']['armed']
    check('other-channel', [w(0xf2000130, 1)], 'dma_channel_execution_unmodeled')
    check('wrong-enable', arm()[:-1] + [w(0xf2000150, 1)], 'dma_channel_execution_unmodeled')
    result = check('mailbox-pending-busy-read', packet() + [('r', 0xfa040000, 0, 4)], 'instruction_limit')
    assert result['registers'][2] == 1
    assert result['fa_mailbox_tx']['submissions'] == 1 and result['fa_mailbox_tx']['busy']
    assert result['fa_mailbox_tx']['submitted_packet_hex'] == '1418010002000300'
    result = check('maximum-mailbox', packet(31), 'instruction_limit')
    assert result['fa_mailbox_tx']['known_halfword_mask'] == 0xffffffff
    assert result['fa_mailbox_tx']['payload_halfwords'] == 31
    for name, steps, reason in [
        ('header-unknown', [w(0xfa040000, 1)], 'fa_mailbox_header_unknown'),
        ('payload-unknown', packet()[:2] + [w(0xfa040000, 1)], 'fa_mailbox_payload_unknown'),
        ('header-flags', [w(0xfa040040, 0x1834, 2), w(0xfa040000, 1)], 'fa_mailbox_header_unmodeled'),
        ('wrong-command', [w(0xfa040000, 2)], 'fa_mailbox_command_unmodeled'),
        ('wrong-command-width', [w(0xfa040000, 1, 2)], 'fa_mailbox_access_unmodeled'),
        ('wrong-buffer-width', [w(0xfa040040, 0x1814)], 'fa_mailbox_access_unmodeled'),
        ('buffer-read', [('r', 0xfa040040, 0, 2)], 'fa_mailbox_tx_buffer_read_unmodeled'),
        ('duplicate-commit', packet() + [w(0xfa040000, 1)], 'fa_mailbox_peer_consumption_unmodeled'),
        ('pending-overwrite', packet() + [w(0xfa040040, 0, 2)], 'fa_mailbox_peer_consumption_unmodeled'),
        ('neighbor', [w(0xfa040038, 1)], 'device_register_access_unmodeled'),
    ]:
        check(name, steps, reason)
    return {'checks': checks, 'count': len(checks), 'DMA_bytes_transferred': 0,
            'unsolicited_completions_or_DSP_data': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    result = run(args.output)
    (args.output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'passed': result['count']}))
