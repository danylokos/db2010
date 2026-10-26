/* Differential reference: the sealed pre-migration C mailbox, not another CPU runner.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-fa-mailbox.h"
const char *sealed_dbfm_access(DB2010FaMailbox *,uint32_t,unsigned,bool,uint32_t *,bool);
bool sealed_dbfm_contains(uint32_t,unsigned);
static uint32_t seed=0x750001;
static unsigned next(void) { seed^=seed<<13;seed^=seed>>17;seed^=seed<<5;return seed; }
int main(void)
{
    static const uint32_t values[]={0,1,2,0x100,0x10001,UINT32_MAX};
    static const uint32_t masks[]={0,1,3,15,UINT32_MAX};
    static const uint16_t headers[]={0,0x0802,0x0822,0x1814,0xf802};
    for(unsigned i=0;i<100000;i++) {
        DB2010FaMailbox a={0},b;
        a.busy=next()%2;a.known=masks[next()%5];a.words[0]=headers[next()%5];
        for(unsigned j=1;j<32;j++) { a.words[j]=j*0x101; }
        a.submissions=next()%10;a.payload_words=next()%32;b=a;
        unsigned address=0xfa03fff8+next()%0x90;
        unsigned size=next()%9;bool write=next()%2,commit=next()%2;
        uint32_t x=values[next()%6],y=x;
        const char *p=dbfm_access(&a,address,size,write,&x,commit);
        const char *q=sealed_dbfm_access(&b,address,size,write,&y,commit);
        assert((!p&&!q)||(p&&q&&!strcmp(p,q)));
        assert(x==y && !memcmp(&a,&b,sizeof(a)));
        assert(dbfm_contains(address,size)==sealed_dbfm_contains(address,size));
    }
    return 0;
}
