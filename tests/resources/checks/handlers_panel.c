/* Indexed packet partial faults, pure DMA preflight and cursor wrap/knowledge.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-panel.h"
const char *sealed_dbpanel_packet(DB2010Panel *,DB2010Pdi *);
const char *sealed_dbpanel_dma_fault(DB2010Panel *,unsigned);
void sealed_dbpanel_pixels(DB2010Panel *,const uint8_t *,unsigned);
void sealed_dbpanel_begin_stream(DB2010Panel *);
static uint32_t seed=0x740012;
static unsigned next(void) {seed^=seed<<13;seed^=seed>>17;seed^=seed<<5;return seed;}
int main(void)
{
    for(unsigned i=0;i<10000;i++) {
        DB2010Panel a={0},b;DB2010Pdi bus={0};
        a.index=next()%0x46;a.index_known=next()%2;a.cursor=((next()%220)<<8)|(next()%176);a.cursor_known=next()%2;
        a.known[3]=next()%2;a.regs[3]=0x7230;a.known[0x44]=next()%2;a.regs[0x44]=0xaf00;a.known[0x45]=next()%2;a.regs[0x45]=0xdb00;b=a;
        bus.state.packet_size=next()%25;
        for(unsigned j=0;j<24;j++) {bus.state.packet[j]=next()%0x50;bus.state.rs[j]=next()%2;}
        const char *p=dbpanel_packet(&a,&bus),*q=sealed_dbpanel_packet(&b,&bus);
        assert((!p&&!q)||(p&&q&&!strcmp(p,q)));assert(!memcmp(&a,&b,sizeof(a)));
        unsigned bytes=next()%65;p=dbpanel_dma_fault(&a,bytes);q=sealed_dbpanel_dma_fault(&b,bytes);
        assert((!p&&!q)||(p&&q&&!strcmp(p,q)));assert(!memcmp(&a,&b,sizeof(a)));
        a.cursor=b.cursor=(219<<8)|172;
        dbpanel_begin_stream(&a);sealed_dbpanel_begin_stream(&b);
        uint8_t pixels[32];memset(pixels,0xa5,sizeof(pixels));
        dbpanel_pixels(&a,pixels,sizeof(pixels));sealed_dbpanel_pixels(&b,pixels,sizeof(pixels));
        assert(!memcmp(&a,&b,sizeof(a)));
    }
    return 0;
}
