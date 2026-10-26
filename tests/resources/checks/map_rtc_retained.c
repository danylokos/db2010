/* Every retained start, boundary, width and raw-value guard against sealed C.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-rtc-retained.h"
const char *sealed_dbrtr_access(DB2010RtcRetained *,uint32_t,unsigned,bool,uint32_t *);
bool sealed_dbrtr_contains(uint32_t,unsigned);
int main(void)
{
    const uint32_t values[]={0,1,255,256,0xffff0000,UINT32_MAX};
    for(unsigned off=0;off<32;off++) for(unsigned size=0;size<9;size++)
    for(unsigned w=0;w<2;w++) for(unsigned v=0;v<G_N_ELEMENTS(values);v++) {
        DB2010RtcRetained a,b;for(unsigned k=0;k<25;k++)a.bytes[k]=k*7;b=a;
        uint32_t x=values[v],y=x,address=0xf9050028+off;
        const char *p=dbrtr_access(&a,address,size,w,&x);
        const char *q=sealed_dbrtr_access(&b,address,size,w,&y);
        assert((!p&&!q)||(p&&q&&!strcmp(p,q)));assert(x==y&&!memcmp(&a,&b,sizeof(a)));
        assert(dbrtr_contains(address,size)==sealed_dbrtr_contains(address,size));
    }
    const DB2010RegMap *map=dbrtr_map();
    assert(map->claim_count==1&&map->claims[0].span==26);
    return 0;
}
