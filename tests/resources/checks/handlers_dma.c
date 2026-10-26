/* Sparse/repeated DMA shape and competing validations against sealed C.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-dma.h"
const char *sealed_dbdma_access(DB2010Dma *,uint32_t,unsigned,bool,uint32_t *);
void sealed_dbdma_source_advance(DB2010Dma *,unsigned,unsigned);
void sealed_dbdma_word_transferred(DB2010Dma *,unsigned);
void sealed_dbdma_complete(DB2010Dma *,unsigned);
static uint32_t seed=0x750004;
static unsigned next(void) {seed^=seed<<13;seed^=seed>>17;seed^=seed<<5;return seed;}
int main(void)
{
    const uint32_t values[]={0,1,2,255,256,0xc8c7,0xea53,UINT32_MAX};
    for(unsigned i=0;i<100000;i++) {
        DB2010Dma a={.config={.fa_tx=next()%2,.display_first_burst=next()%2}},b;
        a.state.raw_tc=next();a.state.raw_error=next();
        for(unsigned ch=0;ch<8;ch++) for(unsigned f=0;f<5;f++)a.state.channel[ch][f]=next();
        for(unsigned f=0;f<6;f++) { a.state.global[f]=next(); }
        b=a;
        uint32_t addr=0xf2000000+next()%0x220,x=values[next()%G_N_ELEMENTS(values)],y=x;
        unsigned size=next()%9;bool w=next()%2;
        const char *p=dbdma_access(&a,addr,size,w,&x),*q=sealed_dbdma_access(&b,addr,size,w,&y);
        assert((!p&&!q)||(p&&q&&!strcmp(p,q)));assert(x==y&&!memcmp(&a,&b,sizeof(a)));
        unsigned channel=next()%8,bytes=next()%32;
        dbdma_source_advance(&a,channel,bytes);sealed_dbdma_source_advance(&b,channel,bytes);
        dbdma_word_transferred(&a,channel);sealed_dbdma_word_transferred(&b,channel);
        dbdma_complete(&a,channel);sealed_dbdma_complete(&b,channel);
        assert(!memcmp(&a,&b,sizeof(a)));
    }
    const DB2010RegMap *map=dbdma_map();assert(map->claim_count==1&&map->claims[0].span==4096);
    return 0;
}
