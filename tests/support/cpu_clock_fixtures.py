"""Controlled native ARM MMIO fixtures for firmware-derived SYSCON clocks."""
import argparse
import json
from pathlib import Path

from support.fixtures import qemu_run
from support.startup_status_fixtures import fixture

SETUP, MUX, PLL = 0xf909000c, 0xf909001c, 0xf909f800


def configured(steps=(), *, initial_mux=0, ready_mask=1):
    state = fixture(steps, 0)
    state.pop('startup_status')
    state['device_registers'] = [
        {'address': address, 'width': 2, 'initial_value': value,
         'read_or_mask': mask, 'name': f'fixture.clock.{address:x}'}
        for address, value, mask in ((SETUP, 0, 0), (MUX, initial_mux, 0), (PLL, 0, ready_mask))]
    return state


def write(address, value, width=2):
    return ('w', address, value, width)


def select(mhz):
    if mhz == 13:
        return [write(MUX, 0x400), write(MUX, 0), write(MUX, 0x800)]
    return [write(MUX, 0), write(SETUP, 0xf800), write(PLL, {52:0x410, 104:0x810}[mhz]),
            write(MUX, {52:0x100, 104:0x200}[mhz]), write(MUX, {52:0x500, 104:0x600}[mhz])]


def run(output):
    checks = []

    def check(name, steps, expected, *, reason='instruction_limit', **options):
        state = configured(steps, **options)
        r = qemu_run(state, output/name, trace=False, reset=True,
            inspect='info mtree', qom_inspect={'/machine/syscon': ('contract', 'active', 'nominal-cpu-hz')})
        assert r['stop']['reason'] == reason, (name, r['stop'])
        assert r['cpu_hz'] == (expected*1000000 if expected else None), (name, r['syscon'])
        assert r['syscon']['bus_hz'] == (expected*1000000//(1 if expected == 13 else 2) if expected else None)
        assert r['qom_inspection']['/machine/syscon']['nominal-cpu-hz'] == (r['cpu_hz'] or 0)
        assert 'db2010-syscon.f909001c' in r['memory_inspection']
        assert {k:v for k,v in r['after_reset'].items() if k != 'virtual_ns'} == {
            k:v for k,v in r['initial'].items() if k != 'virtual_ns'}
        checks.append({'name':name, 'passed':True, 'cpu_hz':r['cpu_hz'], 'syscon':r['syscon'],
                       'stop':r['stop'], 'virtual_ns':r['virtual_ns'], 'reset':'exact'})
        return r

    check('unknown-startup', [], None)
    check('unknown-initial-latch', [], None, initial_mux=None)
    check('guard-unknown-read', [('r', MUX, 0, 2)], None, initial_mux=None,
          reason='device_register_initial_state_unknown')
    check('bypass', [], 13, initial_mux=0x800)
    for mhz in (52, 104):
        steps = select(mhz)
        for count in range(1, len(steps)+1):
            r = check(f'select-{mhz}-stage-{count}', steps[:count], mhz if count == 5 else None)
            if count == 3:
                assert r['syscon']['pll_hz'] == mhz*1000000 and r['syscon']['pll_ready']
    check('104-to-52', select(104)+select(52), 52)
    r = check('104-to-13-retains-pll', select(104)+select(13), 13)
    assert r['syscon']['pll_hz'] == 104000000 and r['syscon']['pll_ready']
    check('13-to-104', select(104), 104, initial_mux=0x800)
    check('programmed-pll-still-bypassed', [write(SETUP, 0xf800), write(PLL, 0x810)], 13,
          initial_mux=0x800)
    check('mux-pll-mismatch', select(104)+[write(MUX, 0x500)], None)
    check('unknown-pll-command', select(104)+[write(PLL, 0x910)], None)
    check('unknown-setup', select(104)+[write(SETUP, 0x7800)], None)
    check('conflicting-bypass', select(104)+[write(MUX, 0xe00)], None)
    check('not-ready', select(104), None, ready_mask=0)
    for width in (1, 4):
        r = check(f'reject-width-{width}', select(104)+[write(MUX, 0x800, width)], 104,
                  reason='device_register_access_unmodeled')
        assert r['device_registers'][str(MUX)] == 0x600
    r = check('ready-readback', select(104)+[('r', PLL, 0, 2)], 104)
    assert r['registers'][2] == 0x811
    assert r['device_registers'][str(PLL)] == 0x810
    return {'passed':len(checks), 'checks':checks,
            'scope':'Native ARM MMIO selection, unknown states, exact guards and cold resets; no CPI model.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = run(args.output)
    (args.output/'verification.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'passed':report['passed']}))


if __name__ == '__main__':
    main()
