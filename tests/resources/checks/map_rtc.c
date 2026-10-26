/* Observation overflow versus unknown fields, control bypass and calendar writes.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-rtc.h"
const char *sealed_dbrtc_access(DB2010Rtc *,uint64_t,uint64_t,uint32_t,unsigned,bool,uint32_t *);
bool sealed_dbrtc_contains(uint32_t,unsigned);
static uint32_t seed=0x750015;
static unsigned next(void) {seed^=seed<<13;seed^=seed>>17;seed^=seed<<5;return seed;}
int main(void)
{
    const uint64_t clocks[]={0,1,13000000,UINT64_MAX};
    const uint32_t values[]={0,1,23,31,59,99,100,UINT32_MAX};
    const int64_t adjust[]={-32768,-1,0,32768};
    for(unsigned i=0;i<50000;i++) {
        DB2010Rtc a={0},b;
        a.config.calendar_writable=next()%2;a.config.initial_ticks=clocks[next()%4];a.config.status=0x8421;
        a.state.control=0x20;a.state.counter_adjust_ticks=adjust[next()%4];a.state.calendar_adjust_seconds=(int)next()%86400;b=a;
        uint32_t addr=0xf904fffeu + next()%38,x=values[next()%G_N_ELEMENTS(values)],y=x;
        uint64_t count=clocks[next()%4],idle=clocks[next()%4];unsigned size=next()%6;bool w=next()%2;
        const char *p=dbrtc_access(&a,count,idle,addr,size,w,&x),*q=sealed_dbrtc_access(&b,count,idle,addr,size,w,&y);
        assert((!p&&!q)||(p&&q&&!strcmp(p,q)));assert(x==y&&!memcmp(&a,&b,sizeof(a)));
        assert(dbrtc_contains(addr,size)==sealed_dbrtc_contains(addr,size));
    }
    return 0;
}
