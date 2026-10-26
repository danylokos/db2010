/* Delayed read/write completion with independent peer memories and previews.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-fa-transfer.h"
const char *sealed_dbfa_access(DB2010FaTransfer *,uint32_t,unsigned,uint64_t,bool,uint32_t *,bool);
void sealed_dbfa_finish(DB2010FaTransfer *,uint64_t);
static uint32_t seed=0x740007;
static unsigned next(void) {seed^=seed<<13;seed^=seed>>17;seed^=seed<<5;return seed;}
static void equal(DB2010FaTransfer *a,DB2010FaTransfer *b)
{
    DB2010FaTransfer c=*b;c.memory=a->memory;c.initial=a->initial;
    assert(!memcmp(a,&c,sizeof(c)));
    assert(g_hash_table_size(a->memory)==g_hash_table_size(b->memory));
    GHashTableIter it;gpointer k,v;g_hash_table_iter_init(&it,a->memory);
    while(g_hash_table_iter_next(&it,&k,&v))assert(g_hash_table_lookup(b->memory,k)==v);
}
int main(void)
{
    const uint32_t values[]={0,1,2,0x800000,0x12345678,UINT32_MAX};
    for(unsigned i=0;i<30000;i++) {
        DB2010FaTransfer a={0},b;
        a.address=1;a.address_known=next()%2;a.data=0x12345678;a.data_known=next()%2;
        a.command=next()%3;a.due=10;b=a;
        a.memory=g_hash_table_new(g_direct_hash,g_direct_equal);b.memory=g_hash_table_new(g_direct_hash,g_direct_equal);
        dbfa_set_word(&a,0,0xabcd);dbfa_set_word(&a,1,0x1234);dbfa_set_word(&b,0,0xabcd);dbfa_set_word(&b,1,0x1234);
        uint32_t addr=0xfa040020+next()%20,x=values[next()%G_N_ELEMENTS(values)],y=x;
        unsigned size=next()%9;bool w=next()%2,c=next()%2;uint64_t now=9+next()%3;
        const char *p=dbfa_access(&a,addr,size,now,w,&x,c),*q=sealed_dbfa_access(&b,addr,size,now,w,&y,c);
        assert((!p&&!q)||(p&&q&&!strcmp(p,q)));assert(x==y);equal(&a,&b);
        dbfa_finish(&a,now);sealed_dbfa_finish(&b,now);equal(&a,&b);
        g_hash_table_unref(a.memory);g_hash_table_unref(b.memory);
    }
    return 0;
}
