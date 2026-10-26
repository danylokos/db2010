/* Read-clear, repeated previews and direction/value fault priority against sealed C.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-components.h"
const char *sealed_dbdsp_access(DB2010DspPeer *,uint32_t,unsigned,bool,uint32_t *,bool);
bool sealed_dbdsp_contains(uint32_t,unsigned);
void sealed_dbdsp_request(DB2010DspPeer *,bool,unsigned,unsigned,uint64_t);
void sealed_dsp_peer_advance(DB2010FaLink *);
extern uint64_t db2010_test_virtual_ns;
static uint8_t data[224],known[224];
static bool memory_view(void *opaque,uint32_t address,unsigned size,DB2010MemoryView *view)
{
    assert(address>=0x1000 && address+size<=0x1000+224);
    *view=(DB2010MemoryView){0x1000,data,known};return true;
}
static void stop_fault(void *opaque,const char *reason,uint32_t address,unsigned size) {assert(false);}
static void check_lifecycle(void)
{
    memset(known,1,sizeof(known));for(unsigned i=0;i<sizeof(data);i++)data[i]=i;
    for(unsigned only=0;only<2;only++) for(unsigned variable=0;variable<2;variable++) {
        DB2010DspPeer a={.config={.latency=2,.variable_dma=variable}},b=a;
        DB2010Dma da={0},db={0};uint64_t now=0;
        uint32_t *ch=da.state.channel[2];ch[0]=0x1000;ch[1]=0xfa040020;ch[3]=0xa4492038;ch[4]=0xc8c7;db=da;
        DB2010FaLink la={.dsp_peer=&a,.dma=&da,.has_dsp_peer=true,.time={.count=&now},
            .memory={.view=memory_view},.faults={.stop=stop_fault}},lb=la;
        la.fa_mailbox.busy=DBFM_OCCUPIED;lb.fa_mailbox=la.fa_mailbox;lb.dsp_peer=&b;lb.dma=&db;
        dbdsp_request(&a,only,224,2,0);sealed_dbdsp_request(&b,only,224,2,0);
        for(now=0;now<70*64;now+=64) {
            db2010_test_virtual_ns=now;
            dsp_peer_advance(&la);sealed_dsp_peer_advance(&lb);
            assert(!memcmp(&a,&b,sizeof(a))&&!memcmp(&da,&db,sizeof(da)));
            assert(!memcmp(&la.fa_mailbox,&lb.fa_mailbox,sizeof(la.fa_mailbox)));
        }
        assert(a.state.phase==DBDSP_DONE);assert(a.state.mailbox_events==1);
        assert(a.state.transfer_events==!only);
    }
}

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
    check_lifecycle();
    return 0;
}
