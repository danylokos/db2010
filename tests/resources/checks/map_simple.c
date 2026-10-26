/* Sealed C parity, including raw-value normalization and repeated previews.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-components.h"
const char *sealed_startup_status_access(DB2010Startup *,uint32_t,unsigned,bool,uint32_t *,bool);
static bool equal_fault(const char *a,const char *b) { return (!a&&!b)||(a&&b&&!strcmp(a,b)); }
const char *sealed_db2010_uart_access(void *,DB2010Access *);
/* This map fixture selects polled TX only; data RX remains disabled. */
void qemu_chr_fe_accept_input(CharFrontend *chr)
{
    assert(!"unexpected data UART receive in polled fixture");
}
static int host_result=1,host_calls;
int qemu_chr_fe_write_all(CharFrontend *f,const uint8_t *buf,int len)
{
    assert(len==1);host_calls++;return host_result;
}
static void check_uart(void)
{
    const uint32_t ports[]={0xfb060000,0xfc040000,0xfc030000,0xfb040000};
    const uint32_t values[]={0,255,256,UINT32_MAX};
    for(unsigned port=0;port<4;port++) for(unsigned off=0;off<0x30;off++)
    for(unsigned size=0;size<9;size++) for(unsigned w=0;w<2;w++)
    for(unsigned c=0;c<2;c++) for(unsigned v=0;v<G_N_ELEMENTS(values);v++)
    for(unsigned fail=0;fail<2;fail++) {
        DB2010Uart a={0},b=a;
        DB2010Access x={.address=ports[port]+off,.size=size,.write=w,.commit=c,.value=values[v]},y=x;
        host_result=fail?0:1;host_calls=0;
        const char *p=db2010_uart_access(&a,&x);int calls=host_calls;host_calls=0;
        const char *q=sealed_db2010_uart_access(&b,&y);
        assert(equal_fault(p,q));assert(calls==host_calls&&x.value==y.value&&!memcmp(a.uart_bytes,b.uart_bytes,sizeof(a.uart_bytes)));
    }
    assert(db2010_uart_map()->claim_count==8);
}

const char *sealed_db2010_power_access(void *,DB2010Access *);
static void check_power(void)
{
    const uint32_t values[]={0,1,0x8d,0x8c,0x1008c,UINT32_MAX};
    const uint32_t masks[]={0,1,0xffff};
    for(unsigned k=0;k<2;k++) for(unsigned allow=0;allow<2;allow++)
    for(unsigned releases=0;releases<2;releases++) for(unsigned old=0;old<6;old++)
    for(unsigned m=0;m<3;m++) for(unsigned v=0;v<6;v++)
    for(unsigned off=0;off<4;off++) for(unsigned size=0;size<6;size++)
    for(unsigned w=0;w<2;w++) for(unsigned c=0;c<2;c++) for(unsigned pc=0;pc<2;pc++) {
        DB2010Power a={.power_known=k,.power_value=values[old],.power_mask=masks[m],
            .allow_startup_release=allow,.startup_releases=releases},b=a;
        DB2010Access x={.address=0xf9050057+off,.size=size,.write=w,.commit=c,
            .value=values[v],.pc=pc?0x44aceab8:0},y=x;
        const char *p=db2010_power_access(&a,&x),*q=sealed_db2010_power_access(&b,&y);
        assert(equal_fault(p,q));assert(!memcmp(&x,&y,sizeof(x))&&!memcmp(&a,&b,sizeof(a)));
    }
}

int main(void)
{
    const uint32_t values[]={0,1,255,256,0xffff0000,UINT32_MAX};
    for(unsigned offset=0;offset<6;offset++) for(unsigned size=0;size<9;size++)
    for(unsigned w=0;w<2;w++) for(unsigned c=0;c<2;c++) for(unsigned v=0;v<G_N_ELEMENTS(values);v++) {
        DB2010Startup a={.startup_status_initial=0x82,.startup_status_value=0x81},b=a;
        uint32_t x=values[v],y=x;
        const char *p=startup_status_access(&a,0xf905005a+offset,size,w,&x,c);
        const char *q=sealed_startup_status_access(&b,0xf905005a+offset,size,w,&y,c);
        assert(equal_fault(p,q));assert(x==y&&!memcmp(&a,&b,sizeof(a)));
    }
    const DB2010RegMap *map=db2010_startup_map();
    assert(map->claim_count==1 && map->claims[0].address==0xf905005c && map->claims[0].span==2);
    check_uart();
    check_power();
    return 0;
}
