/* Read-clear, repeated previews and direction/value fault priority against sealed C.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-dsp-peer.h"
const char *sealed_dbdsp_access(DB2010DspPeer *,uint32_t,unsigned,bool,uint32_t *,bool);
bool sealed_dbdsp_contains(uint32_t,unsigned);
int main(void)
{
    const uint32_t values[]={0,1,3,0x1b,0xdf,0x100,UINT32_MAX};
    for(unsigned off=0;off<16;off++) for(unsigned size=0;size<9;size++)
    for(unsigned w=0;w<2;w++) for(unsigned c=0;c<2;c++) for(unsigned v=0;v<G_N_ELEMENTS(values);v++) {
        DB2010DspPeer a={.state={.mask=3,.status=0x1b,.status_reads=7}},b=a;
        uint32_t addr=0xfa040008+off,x=values[v],y=x;
        const char *p=dbdsp_access(&a,addr,size,w,&x,c),*q=sealed_dbdsp_access(&b,addr,size,w,&y,c);
        assert((!p&&!q)||(p&&q&&!strcmp(p,q)));assert(x==y&&!memcmp(&a,&b,sizeof(a)));
        assert(dbdsp_contains(addr,size)==sealed_dbdsp_contains(addr,size));
    }
    return 0;
}
