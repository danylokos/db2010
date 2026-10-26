"""Transport completion/preview boundaries and sealed-C differential checks."""
import subprocess

from support.component_test import ROOT, component_inputs


def compile_check(tmp_path, code, *extra):
    source = tmp_path / 'check.c'
    source.write_text(code)
    exe = tmp_path / 'check'
    subprocess.run([
        'cc', '-Wall', '-Wextra', '-Werror', '-Wno-sign-compare',
        '-I' + str(ROOT / 'qemu/hw/arm'), str(source), *map(str, extra),
        '-o', str(exe), *component_inputs('i2c', 'fm-radio'),
    ], check=True)
    subprocess.run([str(exe)], check=True)


def test_i2c_transport_completion_and_private_peer_previews(tmp_path):
    compile_check(tmp_path, r'''
#include <assert.h>
#include <string.h>
#include "db2010-i2c.h"
static void access(DB2010I2c *t, unsigned off, uint32_t value, uint64_t now) {
    assert(!dbi_access(t, 0xf9040000 + off, 2, (now)*64, true, &value));
}
int main(void) {
    DB2010Pmic p = {0};
    DB2010FmRadio f = {0};
    DB2010I2c t = {.pmic = &p, .fm = &f};
    dbi_reset(&t);
    access(&t, 2, 4, 0); access(&t, 4, 0x60, 0);
    access(&t, 0, 0, 0); access(&t, 2, 0, 0);
    access(&t, 2, 0x24, 10);
    assert(t.state.phase == DBI_WRITE_ADDRESS && t.state.pending == DBI_START);
    assert(t.state.due == 12*64 && t.state.control == 0x24);
    dbi_finish(&t,(11)*64);
    assert(t.state.pending == DBI_START);
    DB2010I2cRuntime before = t.state;
    DB2010Access a = {.address = 0xf9040002, .size = 1, .now = 12*64};
    assert(!strcmp(dbi_bus_access(&t, &a), "i2c_register_access_unmodeled"));
    assert(!memcmp(&before, &t.state, sizeof(before))); /* Shape precedes finish. */
    a.size = 2;
    for (unsigned n = 0; n < 3; n++) {
        assert(!dbi_bus_access(&t, &a) && a.value == 4);
        assert(!memcmp(&before, &t.state, sizeof(before)));
    }
    a.commit = true;
    assert(!dbi_bus_access(&t, &a) && a.value == 4);
    assert(t.state.pending == DBI_NONE);
    access(&t, 0, 0x94, 12); /* Staging is separate from transmission. */
    assert(t.state.phase == DBI_WRITE_ADDRESS && t.state.pending == DBI_NONE);
    access(&t, 2, 0, 12);
    assert(t.state.phase == DBI_REGISTER && t.state.pending == DBI_TRANSMIT);
    dbi_finish(&t,(14)*64);
    assert(t.state.control == 8 && !t.state.irq_pending);

    /* Async completion and edge consumption are independent of SI status. */
    t.config.capture_async_init = t.config.complete_async_init = true;
    t.state.async_init_step = DBI_INIT_READY;
    t.state.async_phase = DBI_ASYNC_START_READY;
    access(&t, 2, 0xa4, 20);
    dbi_finish(&t,(21)*64); assert(!t.state.async_completions);
    dbi_finish(&t,(22)*64);
    assert(t.state.async_phase == DBI_ASYNC_BYTE_READY);
    assert(t.state.status == 8 && t.state.control == 0x8c && t.state.irq_pending);
    dbi_irq_consumed(&t);
    assert(!t.state.irq_pending && t.state.control == 0x8c);
    access(&t, 0, 1, 22);
    assert(t.state.staged == 1 && t.state.async_completions == 1);
    access(&t, 2, 0x80, 22); dbi_finish(&t,(24)*64);
    assert(t.state.status == 0x48 && t.state.async_completions == 2);
    access(&t, 2, 0x90, 24); dbi_finish(&t,(26)*64); dbi_finish(&t,(26)*64);
    assert(t.state.status == 0xf8 && t.state.async_completions == 3);
    assert(t.state.transactions == 1 && t.state.async_phase == DBI_ASYNC_PEER_READY);

    /* FM selection and PMIC staging in previews must use private child copies. */
    p.config.capture_async_power = true;
    t.state.peer_step = 4; t.state.staged = -1; f.config.enabled = true;
    DB2010PmicRuntime pb = p.state;
    DB2010FmRadioRuntime fb = f.state;
    before = t.state;
    a = (DB2010Access){.address=0xf9040000, .size=2, .now=30*64,
                      .write=true, .value=0x20};
    for (unsigned n = 0; n < 3; n++) {
        assert(!dbi_bus_access(&t, &a));
        assert(!memcmp(&before, &t.state, sizeof(before)));
        assert(!memcmp(&pb, &p.state, sizeof(pb)));
        assert(!memcmp(&fb, &f.state, sizeof(fb)));
    }
    a.commit = true; assert(!dbi_bus_access(&t, &a));
    assert(f.state.active && t.state.staged == 0x20);
    dbi_reset(&t);
    assert(t.config.complete_async_init && p.config.capture_async_power && f.config.enabled);
    assert(t.state.staged == -1 && !t.state.pending && !t.state.async_completions);
    assert(!f.state.active && !p.state.write_count);
    return 0;
}
''')


def test_i2c_events_match_sealed_c_transport(tmp_path):
    # The pinned history is retained in the QEMU submodule. Compile the original
    # transport with renamed entrypoints; no archived CPU runner is invoked.
    sealed = subprocess.check_output([
        'git', '-C', str(ROOT / 'qemu'), 'show',
        'e0bd3a1e801eebe9f1de53819c6ff9eb7e523daf:hw/arm/db2010-i2c.c',
    ])
    source = tmp_path / 'sealed.c'
    source.write_bytes(sealed.replace(b'now + 2', b'now + 128'))
    obj = tmp_path / 'sealed.o'
    symbols = ('transport_reset', 'reset', 'finish', 'begin', 'access',
               'registers', 'bus_access', 'export', 'irq_consumed')
    inputs = component_inputs('i2c', 'fm-radio')
    subprocess.run([
        'cc', '-I' + str(ROOT / 'qemu/hw/arm'),
        *[f'-Ddbi_{name}=sealed_{name}' for name in symbols],
        '-c', str(source), '-o', str(obj),
        *[arg for arg in inputs if not arg.endswith('.c')],
    ], check=True)
    compile_check(tmp_path, r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "db2010-i2c.h"
const char *sealed_bus_access(void *, DB2010Access *);
void sealed_finish(DB2010I2c *, uint64_t);
static uint32_t seed = 0x2010074;
static unsigned next(void) { seed ^= seed << 13; seed ^= seed >> 17; seed ^= seed << 5; return seed; }
int main(void) {
    static const uint32_t values[] = {0,1,4,8,0x10,0x20,0x24,0x60,0x80,
        0x84,0x90,0x94,0x95,0xa4,0xaa,0x100,0x10004,0xffff};
    static const int bytes[] = {-1,0,1,0x94,0x95,0xaa};
    for (unsigned n = 0; n < 100000; n++) {
        DB2010Pmic p = {0}, q = {0};
        DB2010FmRadio f = {0}, g = {0};
        DB2010I2c t = {.pmic=&p, .fm=&f}, u;
        dbi_reset(&t);
        t.config.capture_async_init = next() % 2;
        t.config.complete_async_init = next() % 2;
        p.config.power_known = next() % 2; p.config.power_value = 0x80;
        p.config.capture_writes = next() % 2;
        t.state.phase = next() % 11;
        t.state.async_init_step = next() % 4;
        t.state.async_phase = next() % 7;
        t.state.pending = next() % 5;
        t.state.control_known = next() % 2; t.state.clock_known = next() % 2;
        t.state.control = next() & 0xff; t.state.clock = 0x60;
        t.state.mask = next() & 0x30; t.state.due = 10;
        t.state.staged = bytes[next() % 6];
        t.state.received = next() % 2 ? -1 : 0x80;
        t.state.slave = 0x4a; t.state.reg = next() % 2 ? 0xb0 : 0x23;
        t.state.wire_size = next() % 2 ? 0 : 16;
        t.state.irq_pending = next() % 2;
        t.state.status_known = next() % 2; t.state.status = 8;
        p.state.write_count = next() % 2 ? 0 : 16;
        u = t; q = p; g = f; u.pmic = &q; u.fm = &g;
        DB2010Access a = {.address=0xf9040000 + 2*(next()%5),
            .size=1u << (next()%3), .now=9+next()%3,
            .write=next()%2, .commit=next()%2,
            .value=values[next() % (sizeof(values)/sizeof(values[0]))]}, b = a;
        const char *x = dbi_bus_access(&t, &a), *y = sealed_bus_access(&u, &b);
        assert((!x && !y) || (x && y && !strcmp(x,y)));
        assert(!memcmp(&a,&b,sizeof(a)));
        assert(!memcmp(&t.state,&u.state,sizeof(t.state)));
        assert(!memcmp(&p,&q,sizeof(p)) && !memcmp(&f,&g,sizeof(f)));
        dbi_finish(&t,(12)*64); sealed_finish(&u,12*64);
        assert(!memcmp(&t.state,&u.state,sizeof(t.state)));
    }
    puts("100000 sealed-C transport event/state comparisons match");
    return 0;
}
''', obj)


def test_i2c_map_runtime_profile_guard_is_not_cached(tmp_path):
    compile_check(tmp_path, r'''
#include <assert.h>
#include <string.h>
#include "db2010-i2c.h"
int main(void) {
    DB2010Pmic p = {0}; DB2010FmRadio f = {0};
    DB2010I2c t = {.pmic = &p, .fm = &f};
    dbi_reset(&t);
    t.config.capture_async_init = true;
    t.state.async_init_step = DBI_INIT_READY;
    t.state.status_known = true; t.state.status = 0x48;
    uint32_t value = 0;
    assert(!strcmp(dbi_access(&t, 0xf9040006, 2, (0)*64, false, &value),
                   "i2c_register_access_unmodeled"));
    t.config.complete_async_init = true;
    assert(!dbi_access(&t, 0xf9040006, 2, (0)*64, false, &value) && value == 0x48);
    t.config.complete_async_init = false;
    assert(!strcmp(dbi_access(&t, 0xf9040006, 2, (0)*64, false, &value),
                   "i2c_register_access_unmodeled"));
    assert(dbi_map()->claim_count == 1 && dbi_map()->claims[0].span == 4096);
    return 0;
}
''')
