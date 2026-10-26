/* Alias knowledge, permission/dependency priority, normalization and preview.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-components.h"
static const char *bank_check(DB2010RegisterBank *b,uint32_t addr,unsigned size,
                          bool w,uint32_t *value,bool commit)
{
    DB2010Access a={.address=addr,.size=size,.write=w,.value=*value,.commit=commit};
    const char *fault=db2010_bank_access(b,&a);*value=a.value;return fault;
}
int main(void)
{
    DB2010Register r={.address=0xf500081c,.width=2,.f500_lowbyte_alias=true};
    DB2010Register marker={.address=0xf9050044,.width=1,.rtc_halfword_alias=true};
    DB2010Register gpio={.address=0xf900000a,.width=2,.write_only=true};
    DB2010RegisterBank b={.registers=g_ptr_array_new(),.has_serial=true,.has_f500=true};
    g_ptr_array_add(b.registers,&r);g_ptr_array_add(b.registers,&marker);g_ptr_array_add(b.registers,&gpio);
    uint32_t v=0x1234;
    assert(!bank_check(&b,r.address,1,true,&v,false));assert(!r.known_mask&&!b.f500_commands);
    assert(!bank_check(&b,r.address,1,true,&v,true));assert(r.value==0x34&&r.known_mask==255&&b.f500_commands==1);
    v=0;assert(!bank_check(&b,r.address,1,false,&v,true)&&v==0x34);
    assert(!strcmp(bank_check(&b,r.address,2,false,&v,true),"device_register_initial_state_unknown"));
    v=0xabcd;assert(!bank_check(&b,r.address,2,true,&v,true));v=0;
    assert(!bank_check(&b,r.address,2,false,&v,true)&&v==0xabcd);
    v=0x100;assert(!strcmp(bank_check(&b,marker.address,2,true,&v,true),"rtc_marker_upper_byte_unmodeled"));
    assert(!bank_check(&b,marker.address,1,true,&v,true)&&marker.value==0&&marker.known_mask==255);
    assert(!strcmp(bank_check(&b,gpio.address,2,false,&v,true),"serial_gpio_direction_unknown"));
    b.has_serial=false;assert(!strcmp(bank_check(&b,gpio.address,2,false,&v,true),"device_register_read_unmodeled"));
    const DB2010RegMap *map=dbreg_map(&marker);assert(map->claim_count==1&&map->claims[0].span==2);
    for(unsigned i=0;i<b.registers->len;i++) {DB2010Register *p=g_ptr_array_index(b.registers,i);db2010_regmap_destroy(&p->map);}
    g_ptr_array_unref(b.registers);return 0;
}
