/* Trigger/status fault ordering, packet staging and busy/completion boundaries.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-pdi.h"
const char *sealed_dbpdi_access(DB2010Pdi *,uint32_t,unsigned,uint64_t,bool,uint32_t *);
static uint32_t seed=0x740011;
static unsigned next(void) {seed^=seed<<13;seed^=seed>>17;seed^=seed<<5;return seed;}
int main(void)
{
    const uint32_t offsets[]={0,4,0x64,0x68,0x7c,0x80,0x8c,0x90,0xdc,0xe0,0xf4,0xf8,0xfc};
    const uint32_t values[]={0,1,5,0x20,0x120,0x200,0x208,0x180200,0x180208,0x1c1c1c,UINT32_MAX};
    for(unsigned i=0;i<100000;i++) {
        DB2010Pdi a={.config={.repeat_frames=next()%2,.retained_latches=next()%2}},b;
        a.state.dma_armed=next()%2;a.state.dma_complete=next()%2;a.state.busy_until=10;
        a.state.data_known=next()%64;a.state.data_dirty=next()%64;a.state.mask_known=next()%2;a.state.mask_dirty=next()%2;
        a.state.config=values[next()%G_N_ELEMENTS(values)];a.state.stop_commands=next()%3;b=a;
        uint32_t addr=0xf7000100+offsets[next()%G_N_ELEMENTS(offsets)],x=values[next()%G_N_ELEMENTS(values)],y=x;
        unsigned size=next()%6;bool w=next()%2;uint64_t now=9+next()%3;
        const char *p=dbpdi_access(&a,addr,size,now,w,&x),*q=sealed_dbpdi_access(&b,addr,size,now,w,&y);
        assert((!p&&!q)||(p&&q&&!strcmp(p,q)));assert(x==y&&!memcmp(&a,&b,sizeof(a)));
    }
    return 0;
}
