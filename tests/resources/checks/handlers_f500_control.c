/* Knowledge-before-time, pending commands, completion edges and latch parity.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-f500-control.h"
const char *sealed_dbfc_access(DB2010F500Control *,uint32_t *,bool,uint64_t,unsigned,bool,uint32_t *);
static uint32_t seed=0x740005;
static unsigned next(void) {seed^=seed<<13;seed^=seed>>17;seed^=seed<<5;return seed;}
int main(void)
{
    const uint32_t values[]={0,0x4000,0x400,0x43ff,0x4bff,1,UINT32_MAX};
    for(unsigned i=0;i<100000;i++) {
        DB2010F500Control a={.config={.latency=2}},b;
        a.state.pending=next()%2;a.state.commands=next()%2;a.state.completions=next()%2;a.state.due=10;b=a;
        uint32_t l=0x4400,m=l,x=values[next()%G_N_ELEMENTS(values)],y=x;
        unsigned size=next()%5;bool w=next()%2,k=next()%2;uint64_t now=9+next()%3;
        const char *p=dbfc_access(&a,&l,k,now,size,w,&x),*q=sealed_dbfc_access(&b,&m,k,now,size,w,&y);
        assert((!p&&!q)||(p&&q&&!strcmp(p,q)));assert(x==y&&l==m&&!memcmp(&a,&b,sizeof(a)));
    }
    return 0;
}
