/* Sparse controller holes, region knowledge/dirty effects and absent boundaries.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-components.h"
#include "db2010-nor.h"
const char *sealed_db2010_controller_access(void *,DB2010Access *);
const char *sealed_db2010_region_access(void *,DB2010Access *);
const char *sealed_db2010_absent_access(void *,DB2010Access *);
static uint32_t seed=0x750009;
static unsigned next(void) {seed^=seed<<13;seed^=seed>>17;seed^=seed<<5;return seed;}
static void equal(const char *p,const char *q,DB2010Access *a,DB2010Access *b)
{
    assert((!p&&!q)||(p&&q&&!strcmp(p,q)));assert(!memcmp(a,b,sizeof(*a)));
}
/* Frozen owner wrapper versus declarative window, using identical native core. */
const char *sealed_db2010_nor_bus_access(void *,DB2010Access *);
static void check_nor_window(void)
{
    CPUARMState env={0};
    DB2010Nor a={.env=&env,.wp_high=true,.has_rcr=true,.rcr=0xbddf,.manufacturer=0x89,.device=0x88},b;
    a.array=g_malloc(DBNOR_SIZE);a.known=g_malloc(DBNOR_SIZE);
    memset(a.array,255,DBNOR_SIZE);memset(a.known,1,DBNOR_SIZE);
    a.known[DBNOR_SIZE-1]=0;a.cfi[0x10]='Q';a.cfi_known[0x10]=true;
    b=a;b.array=g_memdup2(a.array,DBNOR_SIZE);b.known=g_memdup2(a.known,DBNOR_SIZE);
    const uint32_t values[]={0,1,3,0xff,0x70,0x90,0x98,0x50,0x60,0x20,0x40,0x10,0xe8,0xd0,0x2f,0x10098,UINT32_MAX};
    const uint32_t addresses[]={DBNOR_BASE-1,DBNOR_BASE,DBNOR_BASE+0x20,DBNOR_BASE+0x40,
        DBNOR_BASE+0x41,DBNOR_BASE+0x42,DBNOR_BASE+0x200000,DBNOR_BASE+DBNOR_SIZE-1,
        DBNOR_BASE+DBNOR_SIZE,UINT32_MAX};
    for(unsigned i=0;i<4000;i++) {
        DB2010NorPart part={.mode=next()%4,.pending=next()%7,.status=0x80,
            .block=next()%2,.count=1,.used=1};
        part.address=DBNOR_BASE+(part.block?0x20000:0x40);
        part.word_address[0]=part.address;part.word_value[0]=0xabcd;
        a.part[0]=b.part[0]=part;
        a.bridge=b.bridge=next()%2;a.mirrored=b.mirrored=next()%2;
        a.unaligned_array_halfwords=b.unaligned_array_halfwords=next()%2;
        a.locked[0]=b.locked[0]=next()%2;
        bool w=next()%2;unsigned size=next()%9;
        uint32_t addr=w?(next()%2?part.address:part.address+2):addresses[next()%G_N_ELEMENTS(addresses)];
        DB2010Access x={.address=addr,.fault_address=addr,.size=size,.fault_size=size,
            .write=w,.commit=next()%2,.value=values[next()%G_N_ELEMENTS(values)]},y=x;
        const char *p=db2010_nor_bus_access(&a,&x),*q=sealed_db2010_nor_bus_access(&b,&y);
        equal(p,q,&x,&y);
        DB2010Nor copy=b;copy.array=a.array;copy.known=a.known;
        assert(!memcmp(&a,&copy,sizeof(a)));
        assert(!memcmp(a.array,b.array,0x40000)&&!memcmp(a.known,b.known,0x40000));
    }
    g_free(a.array);g_free(a.known);g_free(b.array);g_free(b.known);
}

int main(void)
{
    const uint32_t absent[]={0x45ffffff,0x46000000,0x46000001,0x46001ffc,0x46001ffd,0x46001fff,0x46002000};
    for(unsigned i=0;i<100000;i++) {
        unsigned size=next()%9;bool w=next()%2,c=next()%2;uint32_t v=next();
        DB2010Access a={.size=size,.write=w,.commit=c,.value=v},b;
        DB2010MemoryController x={.initial_bank=0x1234},y;
        for(unsigned k=0;k<11;k++){x.controller_known[k]=next()%2;x.controller_words[k]=next();}y=x;
        a.address=0x13fffffc+next()%0x44;b=a;
        const char *p=db2010_controller_access(&x,&a),*q=sealed_db2010_controller_access(&y,&b);
        equal(p,q,&a,&b);assert(!memcmp(&x,&y,sizeof(x)));
        DB2010RegionController r={.has_section_table=next()%2},s;
        for(unsigned k=0;k<24;k++){r.region_known[k]=next()%2;r.region_words[k]=next();}s=r;
        a.address=0xfe003ffc+next()%0x74;a.value=v;b=a;
        p=db2010_region_access(&r,&a);q=sealed_db2010_region_access(&s,&b);
        equal(p,q,&a,&b);assert(!memcmp(&r,&s,sizeof(r)));
        DB2010Absent d={0},e=d;a.address=absent[next()%G_N_ELEMENTS(absent)];a.value=v;b=a;
        p=db2010_absent_access(&d,&a);q=sealed_db2010_absent_access(&e,&b);
        equal(p,q,&a,&b);assert(!memcmp(&d,&e,sizeof(d)));
    }
    check_nor_window();
    return 0;
}
