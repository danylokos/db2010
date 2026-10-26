"""Native QEMU fixtures for the bounded F905005C power-startup status latch."""
import argparse
import json
from pathlib import Path

from support.fixtures import base_state, qemu_run, words


def fixture(steps, initial):
    code, literals = [], []
    for i, (op, address, value, width) in enumerate(steps):
        offset = (len(steps) * 3 + 1) * 4 + i * 8 - (i * 12 + 8)
        code += [0xe59f0000 | offset, 0xe59f1000 | offset,
                 {('w', 1): 0xe5c10000, ('w', 2): 0xe1c100b0,
                  ('w', 4): 0xe5810000, ('r', 1): 0xe5d12000,
                  ('r', 2): 0xe1d120b0, ('r', 4): 0xe5912000}[(op, width)]]
        literals += [value, address]
    code += [0xeafffffe]
    s = base_state(words(*code, *literals), limit=len(code) + 8)
    s['startup_status'] = {'profile': 'mask05-lowbyte-clear', 'initial_value': initial}
    return s


def run(output):
    checks = []

    def check(name, steps, initial, reason):
        r = qemu_run(fixture(steps, initial), output / name, trace=True)
        assert r['stop']['reason'] == reason, (name, r['stop'])
        checks.append({'name': name, 'passed': True, 'stop': r['stop']})
        return r

    for initial in (0, 1, 4, 5):
        r = check('read-' + str(initial), [('r', 0xf905005c, 0, 2)], initial, 'instruction_limit')
        assert r['registers'][2] == initial and r['startup_status']['value'] == initial
        r = check('clear-' + str(initial), [('r', 0xf905005c, 0, 2),
                  ('w', 0xf905005c, 0, 1), ('r', 0xf905005c, 0, 2)], initial, 'instruction_limit')
        assert r['registers'][2] == r['startup_status']['value'] == 0
        assert r['startup_status']['reads'] == 2 and r['startup_status']['clears'] == 1
    for name, step, reason in [
        ('byte-read', ('r', 0xf905005c, 0, 1), 'startup_status_access_unmodeled'),
        ('word-read', ('r', 0xf905005c, 0, 4), 'startup_status_access_unmodeled'),
        ('halfword-write', ('w', 0xf905005c, 0, 2), 'startup_status_access_unmodeled'),
        ('upper-byte', ('w', 0xf905005d, 0, 1), 'startup_status_access_unmodeled'),
        ('set-flag', ('w', 0xf905005c, 1, 1), 'startup_status_write_unmodeled'),
        ('other-bit', ('w', 0xf905005c, 2, 1), 'startup_status_write_unmodeled'),
        ('neighbor', ('w', 0xf905005e, 0, 2), 'device_register_access_unmodeled'),
    ]:
        r = check(name, [step], 5, reason)
        assert r['startup_status']['value'] == 5 and r['startup_status']['clears'] == 0
    return {'count': len(checks), 'checks': checks}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    r = run(args.output)
    (args.output / 'verification.json').write_text(json.dumps(r, indent=2) + '\n')
    print(json.dumps({'passed': r['count']}))
