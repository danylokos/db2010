/* Independent source selection, cascade acknowledgements, masks and fault priority.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-timer.h"
const char *sealed_dbtimer_access(DB2010Timer *,uint32_t,unsigned,bool,uint32_t *);
const char *sealed_dbtimer_observe(DB2010Timer *,uint32_t,uint32_t);
const char *sealed_dbtimer_select(DB2010Timer *);
void sealed_dbtimer_set_line(DB2010Timer *,DB2010IrqLine,bool);
static uint32_t seed=0x740010;
static unsigned next(void) {seed^=seed<<13;seed^=seed>>17;seed^=seed<<5;return seed;}
int main(void)
{
    const uint32_t addresses[]={0xf9010000,0xf9010004,0xf901001c,0xfb020100,0xfb020200,0xfb020300,
        0xfb020110,0xfb020210,0xfb020310,0xfb020104,0xfb020204,0xfb020304,0xfb020118,0xfb020218,0xfb020318};
    for(unsigned i=0;i<100000;i++) {
        DB2010Timer a={.config={.i2c_route=next()%2,.display_route=next()%2,.dsp_route=next()%2,.max_irqs=2}},b;
        a.state.selected=next()%2;a.state.secondary_acked=next()%2;
        a.state.pending=next()%2;a.state.i2c_pending=next()%2;a.state.pdi_pending=next()%2;a.state.dma_pending=next()%2;a.state.dsp_pending=next()%2;
        a.state.i2c_selected=next()%2;a.state.pdi_selected=next()%2;a.state.dma_selected=next()%2;a.state.dsp_selected=next()%2;
        a.state.load_known=next()%2;a.state.irqs=next()%3;
        for(unsigned bank=0;bank<3;bank++) {a.state.masks_known[bank]=next()%2;a.state.masks[bank]=next()%2?0:next();}
        b=a;uint32_t addr=addresses[next()%G_N_ELEMENTS(addresses)],x=next()%3,y=x;
        unsigned size=next()%6;bool w=next()%2;const char *p,*q;
        switch(next()%4) {
        case 0:p=dbtimer_access(&a,addr,size,w,&x);q=sealed_dbtimer_access(&b,addr,size,w,&y);break;
        case 1:p=dbtimer_observe(&a,addr,x);q=sealed_dbtimer_observe(&b,addr,y);break;
        case 2:p=dbtimer_select(&a);q=sealed_dbtimer_select(&b);break;
        default:dbtimer_set_line(&a,x,w);sealed_dbtimer_set_line(&b,y,w);p=q=NULL;
        }
        assert((!p&&!q)||(p&&q&&!strcmp(p,q)));assert(x==y&&!memcmp(&a,&b,sizeof(a)));
    }
    return 0;
}
