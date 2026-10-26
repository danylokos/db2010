"""Independent PMIC writes preserve ADC position and reject unknown byte effects."""
from support.component_test import component_inputs

import subprocess

from db2010.paths import ROOT


def test_native_interleaved_writes_and_fail_before_effect(tmp_path):
    source = tmp_path / 'extra.c'
    source.write_text(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#include "db2010-i2c.h"
static uint64_t now=100;
static const char *op(DB2010I2c*t,unsigned off,uint32_t value) {
 const char*f=dbi_access(t,0xf9040000+off,2,(now)*64,true,&value);
 now+=3;if(!f)dbi_finish(t,(now)*64);return f;
}
static void ok(DB2010I2c*t,unsigned off,uint32_t value){assert(!op(t,off,value));}
static void prefix(DB2010I2c*t){ok(t,2,0x84);ok(t,4,0x60);ok(t,2,0xa4);}
static void data(DB2010I2c*t,unsigned value){ok(t,0,value);ok(t,2,0x80);}
int main(void){
 DB2010Pmic t_pmic = {.config.capture_async_power =true,.config.complete_power_init =true,.config.read_power_status =true,.config.startup_register_reads =true,.state.power_writes =27,.state.register_reads =55,.config.register_read_count =55,.state.status_reads =3,.config.followup_count =2,.config.followup_reg ={0x62,0x71},.config.followup_value ={1,64},.config.followup_read ={false,true},.config.followup_known ={true,true},.config.extra_write_count =4,.config.extra_write_reg ={0xc7,0xcc,0xcc,0xc5},.config.extra_write_value ={0x40,0x40,0x50,0x18}};
 DB2010FmRadio t_fm = {0};
 DB2010I2c t = {.pmic = &t_pmic, .fm = &t_fm,.config.capture_async_init=true,.config.complete_async_init=true,.state.async_init_step=3,.state.async_phase=6,.state.staged=-1};
 for(unsigned n=0;n<4;n++){
  prefix(&t);DB2010I2c before=t; DB2010Pmic before_pmic=*t.pmic;
  assert(!strcmp(op(&t,0,0x95),"i2c_async_power_byte_unmodeled"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
  data(&t,0x94);before=t; before_pmic=*t.pmic;
  assert(!strcmp(op(&t,0,0xc8),"i2c_async_power_byte_unmodeled"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
  data(&t,t.pmic->config.extra_write_reg[n]);assert(t.pmic->state.extra_active&&t.pmic->state.followup_cursor ==0);
  before=t; before_pmic=*t.pmic;
  assert(!strcmp(op(&t,0,0xff),"i2c_extra_write_value_unmodeled"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
  uint32_t value=0x40;
  assert(!strcmp(dbi_access(&t,0xf9040000,1,now,true,&value),"i2c_register_access_unmodeled"));
  assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
  data(&t,t.pmic->config.extra_write_value[n]);ok(&t,2,0x90);
  assert(!t.pmic->state.extra_active&&t.pmic->state.extra_writes ==n+1&&t.pmic->state.followup_cursor ==0);
  assert(t.pmic->state.last_known[t.pmic->config.extra_write_reg[n]]&&t.pmic->state.last_value[t.pmic->config.extra_write_reg[n]]==t.pmic->config.extra_write_value[n]);
  assert(t.pmic->state.write_count ==n+1&&t.pmic->state.register_reads ==55&&t.pmic->state.power_writes ==28+n);
 }
 assert(t.pmic->state.last_value[0xcc]==0x50);
 prefix(&t);data(&t,0x94);data(&t,0x62);data(&t,1);ok(&t,2,0x90);
 assert(t.pmic->state.followup_cursor ==1&&t.pmic->state.extra_writes ==4&&t.pmic->state.last_value[0x62]==1);
 /* Interleave another known write while the ADC's next operation is a read. */
 prefix(&t);data(&t,0x94);data(&t,0xc7);data(&t,0x40);ok(&t,2,0x90);
 assert(t.pmic->state.followup_cursor ==1&&t.pmic->state.extra_writes ==5);
 prefix(&t);data(&t,0x94);data(&t,0x71);ok(&t,2,0xa4);data(&t,0x95);ok(&t,2,0x80);
 uint32_t value=0;assert(!dbi_access(&t,0xf9040000,2,(now)*64,false,&value)&&value==64);now+=3;
 ok(&t,2,0x90);assert(t.pmic->state.followup_cursor ==2&&t.pmic->state.register_reads ==56&&t.pmic->state.extra_writes ==5);
 dbi_reset(&t);assert(t.pmic->config.extra_write_count ==4&&!t.pmic->state.extra_active&&!t.pmic->state.extra_writes&&!t.pmic->state.last_known[0xc7]);
 assert(t.pmic->config.extra_write_reg[2]==0xcc&&t.pmic->config.extra_write_value[2]==0x50);
 /* Without the opt-in list, the legacy unknown-register guard is unchanged. */
 t.pmic->config.extra_write_count =0;t.pmic->state.power_writes =27;t.pmic->state.register_reads =55;t.pmic->state.status_reads =3;
 t.state.async_init_step=3;t.state.async_phase=6;prefix(&t);data(&t,0x94);DB2010I2c before=t; DB2010Pmic before_pmic=*t.pmic;
 assert(!strcmp(op(&t,0,0xc7),"i2c_async_power_byte_unmodeled"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
 return 0;
}
''')
    exe = tmp_path / 'extra'
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-sign-compare',
                    '-I' + str(ROOT / 'qemu/hw/arm'), str(source), '-o', str(exe), *component_inputs('i2c', 'fm-radio')], check=True)
    subprocess.run([str(exe)], check=True)
