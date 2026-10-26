"""Independent F500 variants, side-effect-free preview, delayed finish and reset."""
import subprocess
from support.component_test import ROOT, component_inputs


def test_f500_descriptor_engine(tmp_path):
    source = tmp_path/'ports.c'
    source.write_text(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include "db2010-f500-port.h"
#include "db2010-f500-control.h"
static void port_write(DB2010F500Port *p, unsigned reg, uint32_t value, uint64_t now) {
 const DB2010RegisterSpec *r=&p->config.variant->registers[reg];
 assert(!dbfp_access(p,r->address,r->span,now,true,&value,true));
}
int main(void) {
 DB2010F500Port p={.config={&dbfp_primary,8}}, a={.config={&dbfp_auxiliary,3}};
 port_write(&p,0,0x19,1);port_write(&a,0,0xf,1);
 port_write(&p,1,0x25a,2);port_write(&a,1,0x689,2);
 DB2010F500Port before=p;uint32_t value=1;
 assert(!strcmp(dbfp_access(&p,0xf50005e2,1,3,true,&value,false),"f500_port_write_while_busy_unmodeled"));
 assert(!memcmp(&before,&p,sizeof(p)));
 dbfp_finish(&a,5);dbfp_finish(&p,5);
 assert(a.state.completions==1&&a.state.tag_known[9]&&a.state.last_by_tag[9]==0x689);
 assert(p.state.pending&&p.state.completions==0&&!p.state.tag_known[2]);
 before=p;value=0;
 assert(!dbfp_access(&p,0xf50005e6,1,11,false,&value,false)&&value==1);
 assert(!memcmp(&before,&p,sizeof(p))); /* Preview completion is private. */
 assert(!dbfp_access(&p,0xf50005e6,1,11,false,&value,true)&&value==1);
 assert(p.state.reads==1&&p.state.completions==1);
 port_write(&p,2,1,12);port_write(&p,2,0,13);assert(p.state.pulses==1);
 before=p;value=0;
 assert(!strcmp(dbfp_access(&p,0xf50005d9,1,14,false,&value,false),"f500_port_access_unmodeled"));
 assert(!memcmp(&before,&p,sizeof(p)));
 assert(dbfp_contains(0xf50005d7,2,&dbfp_primary)); /* crossing touches owner */
 assert(!dbfp_contains(0xf50005e7,1,&dbfp_primary)); /* neighbor unowned */
 dbfp_reset(&p);dbfp_reset(&a);
 assert(p.config.variant==&dbfp_primary&&p.config.latency==8&&!p.state.setup_known&&!p.state.commands);
 assert(a.config.variant==&dbfp_auxiliary&&a.config.latency==3&&!a.state.tag_known[9]);
 DB2010F500Control c={.config={8}};uint32_t latch=0, command=0x400;
 assert(!dbfc_access(&c,&latch,true,1,2,true,&command));
 dbfc_advance(&c,&latch,9);assert(latch==0&&c.state.completions==1);
 dbfc_reset(&c);assert(c.config.latency==8&&!c.state.commands&&!c.state.pending);
 return 0;
}
''')
    exe = tmp_path/'ports'
    subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'qemu/hw/arm'),
                    str(source), '-o', str(exe),
                    *component_inputs('f500-port', 'f500-control', 'contract')], check=True)
    subprocess.run([str(exe)], check=True)
