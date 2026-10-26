/* Both immutable variants, completion edges, preview and competing errors.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-f500-port.h"
const char *sealed_dbfp_access(DB2010F500Port *,uint32_t,unsigned,uint64_t,bool,uint32_t *,bool);
void sealed_dbfp_finish(DB2010F500Port *,uint64_t);
static uint32_t seed=0x740004;
static unsigned next(void) {seed^=seed<<13;seed^=seed>>17;seed^=seed<<5;return seed;}
int main(void)
{
    const DB2010F500Variant *variants[]={&dbfp_primary,&dbfp_auxiliary};
    const uint32_t values[]={0,1,0x19,0xf,0x100,0x401,0x200,0x300,0x400,UINT32_MAX};
    for(unsigned i=0;i<100000;i++) {
        DB2010F500Port a={.config={.variant=variants[next()%2],.latency=2}},b;
        a.state.pending=next()%2;a.state.status_known=next()%2;a.state.setup_known=next()%2;
        a.state.strobe_high=next()%2;a.state.due=10;
        a.state.word=a.config.variant==&dbfp_primary?0x200:0x401;b=a;
        unsigned reg=next()%4,size=next()%4;
        uint32_t addr=a.config.variant->registers[reg].address+(next()%4==0),x=values[next()%G_N_ELEMENTS(values)],y=x;
        uint64_t now=9+next()%3;bool w=next()%2,c=next()%2;
        const char *p=dbfp_access(&a,addr,size,now,w,&x,c),*q=sealed_dbfp_access(&b,addr,size,now,w,&y,c);
        assert((!p&&!q)||(p&&q&&!strcmp(p,q)));assert(x==y&&!memcmp(&a,&b,sizeof(a)));
        dbfp_finish(&a,now);sealed_dbfp_finish(&b,now);assert(!memcmp(&a,&b,sizeof(a)));
    }
    return 0;
}
