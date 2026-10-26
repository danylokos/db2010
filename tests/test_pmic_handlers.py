"""Independent PMIC clients, burst previews and sealed-C event parity."""
import subprocess

from test_i2c_handlers import compile_check
from support.component_test import ROOT, component_inputs


def test_pmic_events_match_sealed_c_peer(tmp_path):
    source = tmp_path / 'sealed-pmic.c'
    source.write_bytes(subprocess.check_output([
        'git', '-C', str(ROOT / 'qemu'), 'show',
        'e0bd3a1e801eebe9f1de53819c6ff9eb7e523daf:hw/arm/db2010-pmic.c',
    ]))
    obj = tmp_path / 'sealed-pmic.o'
    symbols = [f'dbi_{name}' for name in ('write_slot', 'retained_writes',
        'write_index', 'capture_write', 'extra_write_known', 'extra_read_index',
        'burst_has_next', 'followup_index')]
    symbols += ['dbpmic_async_access', 'dbpmic_async_finish', 'dbpmic_reset']
    subprocess.run([
        'cc', '-I' + str(ROOT / 'qemu/hw/arm'),
        *[f'-D{name}=sealed_{name}' for name in symbols],
        '-c', str(source), '-o', str(obj),
        *[arg for arg in component_inputs('i2c', 'fm-radio') if not arg.endswith('.c')],
    ], check=True)
    compile_check(tmp_path, r'''
#include <assert.h>
#include <string.h>
#include "db2010-i2c.h"
const char *sealed_dbpmic_async_access(DB2010I2c *,unsigned,uint64_t,uint32_t *);
void sealed_dbpmic_async_finish(DB2010I2c *);
static uint32_t seed=0x740003;
static unsigned next(void) { seed^=seed<<13;seed^=seed>>17;seed^=seed<<5;return seed; }
int main(void) {
    static const uint32_t values[]={0,1,4,0x60,0x70,0x71,0x80,0x84,
        0x90,0x92,0x93,0x94,0x95,0xa4,0xa8,0xab,0xff,0x100};
    static const int bytes[]={-1,0,0x94,0x95,0x70,0xa8,0xab};
    for(unsigned i=0;i<100000;i++) {
        DB2010Pmic p={0},q;
        DB2010FmRadio f={0},g={0};
        p.config.complete_power_init=next()%2;
        p.config.read_power_status=next()%2;
        p.config.startup_register_reads=next()%2;
        p.config.power_known=next()%2;p.config.power_value=0x80;
        p.config.status_input_known[0]=next()%2;p.config.status_input_known[1]=next()%2;
        p.config.register_read_count=1;p.config.read_reg[0]=0x20;
        p.config.read_kind[0]=next()%3;p.config.read_value[0]=0xe3;
        p.config.followup_count=next()%2?0:2;
        p.config.followup_reg[0]=0x70;p.config.followup_value[0]=0xa8;
        p.config.followup_read[0]=next()%2;p.config.followup_known[0]=next()%2;
        p.config.followup_reg[1]=0x71;p.config.followup_value[1]=64;
        p.config.followup_read[1]=true;p.config.followup_known[1]=true;
        p.config.repeat_followups=p.config.followup_count && next()%2;
        p.config.followup_repeat_from=0;
        p.config.extra_write_count=1;p.config.extra_write_reg[0]=0x70;
        p.config.extra_write_value[0]=0xab;
        p.config.extra_read_count=2;p.config.extra_read_reg[0]=0x71;
        p.config.extra_read_reg[1]=0x92;
        p.config.extra_read_kind[0]=next()%3;p.config.extra_read_kind[1]=next()%3;
        p.config.extra_read_value[0]=64;p.config.extra_read_value[1]=0x12;
        p.config.bounded_history=next()%2;
        p.state.power_writes=next()%2?0:27;
        p.state.register_reads=next()%2;p.state.status_reads=next()%4;
        p.state.followup_cursor=next()%4;
        p.state.extra_active=next()%2;p.state.extra_read=next()%2;
        p.state.extra_receive_more=next()%2;
        p.state.extra_reg=next()%2?0x70:0x92;
        p.state.last_known[0x70]=next()%2;p.state.last_value[0x70]=0xab;
        p.state.last_known[0x92]=next()%2;p.state.last_value[0x92]=0x12;
        p.state.write_count=next()%2?0:64;
        DB2010I2c t={.pmic=&p,.fm=&f},u;
        t.state.peer_step=next()%20;t.state.staged=bytes[next()%7];
        t.state.pending=DBI_TRANSMIT;t.state.mask=0x20;
        t.state.control=0xa4;t.state.irq_pending=true;
        q=p;u=t;u.pmic=&q;u.fm=&g;
        unsigned off=2*(next()%4);uint32_t x=values[next()%18],y=x;
        const char *a=dbpmic_async_access(&t,off,10,&x);
        const char *b=sealed_dbpmic_async_access(&u,off,10,&y);
        assert((!a&&!b)||(a&&b&&!strcmp(a,b)));
        assert(x==y&&!memcmp(&t.state,&u.state,sizeof(t.state)));
        assert(!memcmp(&p,&q,sizeof(p))&&!memcmp(&f,&g,sizeof(f)));
        dbpmic_async_finish(&t);sealed_dbpmic_async_finish(&u);
        assert(!memcmp(&t.state,&u.state,sizeof(t.state)));
        assert(!memcmp(&p,&q,sizeof(p))&&!memcmp(&f,&g,sizeof(f)));
    }
    return 0;
}
''', obj)


def test_pmic_burst_rejection_and_completion_previews(tmp_path):
    compile_check(tmp_path, r'''
#include <assert.h>
#include <string.h>
#include "db2010-i2c.h"
int main(void) {
    DB2010Pmic p={.config={.capture_async_power=true,.complete_power_init=true,
        .read_power_status=true,.startup_register_reads=true,.register_read_count=1,
        .followup_count=2,.followup_reg={0x70,0x71},.followup_value={0xa8,64},
        .followup_known={true,true},.extra_read_count=2,
        .extra_read_reg={0x92,0x93},.extra_read_value={0x12,0x34},
        .extra_read_kind={1,0}}};
    DB2010FmRadio f={0};
    DB2010I2c t={.config={true,true},.pmic=&p,.fm=&f};
    dbi_reset(&t);
    p.state.power_writes=27;p.state.register_reads=1;p.state.status_reads=3;
    p.state.extra_active=DBPMIC_INDEPENDENT;p.state.extra_read=DBPMIC_READ;
    p.state.extra_receive_more=DBPMIC_MORE_ACK;p.state.extra_reg=0x92;
    t.state.async_init_step=DBI_INIT_READY;t.state.async_phase=DBI_ASYNC_PEER_READY;
    t.state.peer_step=DBI_PEER_READ_COMPLETE;t.state.staged=-1;
    DB2010I2cRuntime tb=t.state;DB2010PmicRuntime pb=p.state;
    DB2010Access a={.address=0xf9040002,.size=2,.now=10*64,.write=true,.value=0x80};
    for(unsigned n=0;n<3;n++) {
        assert(!strcmp(dbi_bus_access(&t,&a),"i2c_extra_read_input_unknown"));
        assert(!memcmp(&tb,&t.state,sizeof(tb))&&!memcmp(&pb,&p.state,sizeof(pb)));
    }
    /* Known next input lets the same request proceed exactly once on commit. */
    p.config.extra_read_kind[1]=1;
    for(unsigned n=0;n<3;n++) {
        assert(!dbi_bus_access(&t,&a));
        assert(!memcmp(&tb,&t.state,sizeof(tb))&&!memcmp(&pb,&p.state,sizeof(pb)));
    }
    a.commit=true;assert(!dbi_bus_access(&t,&a));
    assert(p.state.extra_reg==0x93&&t.state.received==0x34);
    assert(t.state.pending==DBI_RECEIVE&&t.state.peer_step==DBI_PEER_RECEIVE_PENDING);
    assert(!p.state.extra_receive_more);
    dbi_finish(&t,(12)*64);
    assert(t.state.status==0x58&&t.state.peer_step==DBI_PEER_RECEIVED);
    a=(DB2010Access){.address=0xf9040000,.size=2,.now=12*64,.commit=true};
    assert(!dbi_bus_access(&t,&a)&&a.value==0x34);
    a=(DB2010Access){.address=0xf9040002,.size=2,.now=12*64,.write=true,.value=0x90,.commit=true};
    assert(!dbi_bus_access(&t,&a));dbi_finish(&t,(14)*64);dbi_finish(&t,(14)*64);
    assert(p.state.extra_reads==1&&!p.state.extra_active&&!p.state.followup_cursor);
    assert(t.state.transactions==1&&p.state.power_writes==27);
    return 0;
}
''')
