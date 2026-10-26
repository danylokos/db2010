"""FM prefix/STOP lifecycle and parity with the sealed peer implementation."""
import subprocess

from test_i2c_handlers import compile_check
from support.component_test import ROOT, component_inputs


def test_fm_frame_staging_stop_and_private_completion(tmp_path):
    compile_check(tmp_path, r'''
#include <assert.h>
#include <string.h>
#include "db2010-i2c.h"
static const char *write(DB2010I2c *t, unsigned off, unsigned value, uint64_t now) {
    uint32_t v = value;
    return dbi_access(t, 0xf9040000 + off, 2, (now)*64, true, &v);
}
int main(void) {
    DB2010Pmic p = {.config={.capture_async_power=true}};
    DB2010FmRadio f = {.config={.enabled=true, .count=2, .size={3,3},
        .allowed={{1,2,3},{1,4,5}}}};
    DB2010I2c t = {.config={true,true}, .pmic=&p, .fm=&f};
    dbi_reset(&t);
    t.state.async_init_step=DBI_INIT_READY;
    t.state.async_phase=DBI_ASYNC_PEER_READY;
    t.state.peer_step=DBI_PEER_ADDRESS_READY;
    assert(!strcmp(write(&t,0,0x21,0),"fm_i2c_read_unmodeled"));
    assert(!write(&t,0,0x20,0) && f.state.active==DBFR_SELECTED);
    assert(t.state.staged==0x20 && !t.state.pending && !f.state.used);
    assert(!write(&t,2,0x80,0)); dbi_finish(&t,(2)*64);
    assert(t.state.peer_step==DBI_PEER_FIRST_BYTE_READY);
    assert(!strcmp(write(&t,2,0x90,2),"fm_i2c_short_frame_unmodeled"));
    assert(!write(&t,0,1,2));
    assert(!f.state.used && t.state.staged==1); /* No payload effect until TX. */
    assert(!write(&t,2,0x80,2)); dbi_finish(&t,(4)*64);
    assert(f.state.used==1 && f.state.pending[0]==1);
    assert(!write(&t,0,4,4)); assert(!write(&t,2,0x80,4)); dbi_finish(&t,(6)*64);
    assert(!strcmp(write(&t,0,3,6),"fm_i2c_payload_unmodeled"));
    assert(t.state.staged==-1 && f.state.used==2); /* Cannot mix frame prefixes. */
    assert(!write(&t,0,5,6)); assert(!write(&t,2,0x80,6)); dbi_finish(&t,(8)*64);
    assert(!strcmp(write(&t,0,0,8),"fm_i2c_payload_unmodeled"));
    assert(!write(&t,2,0x90,8)); dbi_finish(&t,(9)*64);
    assert(f.state.active==DBFR_SELECTED && !f.state.frames);
    DB2010I2cRuntime before=t.state;
    DB2010FmRadioRuntime fb=f.state;
    DB2010PmicRuntime pb=p.state;
    DB2010Access a={.address=0xf9040002,.size=2,.now=10*64};
    for (unsigned n=0;n<3;n++) {
        assert(!dbi_bus_access(&t,&a));
        assert(!memcmp(&before,&t.state,sizeof(before)));
        assert(!memcmp(&fb,&f.state,sizeof(fb)));
        assert(!memcmp(&pb,&p.state,sizeof(pb)));
    }
    a.commit=true; assert(!dbi_bus_access(&t,&a)); dbi_finish(&t,(10)*64);
    assert(f.state.active==DBFR_IDLE && f.state.frames==1 && f.state.bytes==3);
    assert(f.state.latest_size==3 && !memcmp(f.state.latest,"\1\4\5",3));
    assert(!f.state.used && t.state.transactions==1 && !p.state.power_writes);
    assert(!p.state.write_count && !p.state.followup_cursor);
    dbi_reset(&t);
    assert(f.config.count==2 && !f.state.frames && !f.state.active);
    return 0;
}
''')


def test_fm_events_match_sealed_c_peer(tmp_path):
    source = tmp_path / 'sealed-fm.c'
    source.write_bytes(subprocess.check_output([
        'git', '-C', str(ROOT / 'qemu'), 'show',
        'e0bd3a1e801eebe9f1de53819c6ff9eb7e523daf:hw/arm/db2010-fm-radio.c',
    ]))
    obj = tmp_path / 'sealed-fm.o'
    subprocess.run([
        'cc', '-I' + str(ROOT / 'qemu/hw/arm'),
        *[f'-Ddbfr_{name}=sealed_fm_{name}' for name in
          ('byte_known', 'frame_known', 'complete', 'reset', 'transport')],
        '-c', str(source), '-o', str(obj),
        *[arg for arg in component_inputs('i2c', 'fm-radio') if not arg.endswith('.c')],
    ], check=True)
    compile_check(tmp_path, r'''
#include <assert.h>
#include <string.h>
#include "db2010-i2c.h"
const char *sealed_fm_transport(DB2010I2c *,unsigned,uint64_t,uint32_t *,bool *);
void sealed_fm_complete(DB2010FmRadio *);
int main(void) {
    static const uint32_t values[]={0,1,2,3,4,5,0x20,0x21,0x80,0x90,256};
    static const int staged[]={-1,0,1,0x20,0x21};
    for(unsigned phase=0;phase<20;phase++) for(unsigned active=0;active<2;active++)
    for(unsigned enabled=0;enabled<2;enabled++) for(unsigned used=0;used<4;used++)
    for(unsigned si=0;si<5;si++) for(unsigned off=0;off<8;off+=2)
    for(unsigned vi=0;vi<sizeof(values)/sizeof(values[0]);vi++) {
        DB2010Pmic p={0},q={0};
        DB2010FmRadio f={.config={.count=2,.size={3,3},.allowed={{1,2,3},{1,4,5}}}},g;
        f.config.enabled=enabled; f.state.active=active; f.state.used=used;
        memcpy(f.state.pending,"\1\4\5",3);
        DB2010I2c t={.pmic=&p,.fm=&f},u;
        t.state.peer_step=phase;t.state.staged=staged[si];
        t.state.control=0x88;t.state.irq_pending=true;
        g=f;u=t;u.pmic=&q;u.fm=&g;
        uint32_t x=values[vi],y=x;bool hx=false,hy=false;
        const char *a=dbfr_transport(&t,off,10,&x,&hx);
        const char *b=sealed_fm_transport(&u,off,10,&y,&hy);
        assert((!a&&!b)||(a&&b&&!strcmp(a,b)));
        assert(x==y&&hx==hy&&!memcmp(&t.state,&u.state,sizeof(t.state)));
        assert(!memcmp(&f,&g,sizeof(f))&&!memcmp(&p,&q,sizeof(p)));
        dbfr_complete(&f);sealed_fm_complete(&g);
        assert(!memcmp(&f,&g,sizeof(f)));
    }
    return 0;
}
''', obj)
