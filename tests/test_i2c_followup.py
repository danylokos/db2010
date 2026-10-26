"""Check ordered native PMIC follow-ups and unknown-result fail-before-effect."""
from support.component_test import component_inputs

import subprocess
from db2010.paths import ROOT


def test_followup_sequence(tmp_path):
    code = tmp_path/'follow.c'
    code.write_text(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#include "db2010-i2c.h"
static uint64_t now=100;
static const char *op(DB2010I2c *t,unsigned off,bool write,uint32_t value) {
 const char *fault=dbi_access(t,0xf9040000+off,2,(now)*64,write,&value);
 now+=3; if(!fault)dbi_finish(t,(now)*64);return fault;
}
static void ok(DB2010I2c*t,unsigned off,uint32_t value){assert(!op(t,off,true,value));}
int main(void){
 DB2010Pmic t_pmic = {.config.capture_async_power =true,.config.complete_power_init =true,.config.read_power_status =true,.config.startup_register_reads =true,.state.power_writes =27,.state.register_reads =55,.config.register_read_count =55,.state.status_reads =3,.config.followup_count =2,.config.followup_reg ={0x70,0x71},.config.followup_value ={0xa8,64},.config.followup_read ={false,true},.config.followup_known ={true,true}};
 DB2010FmRadio t_fm = {0};
 DB2010I2c t = {.pmic = &t_pmic, .fm = &t_fm,.config.capture_async_init=true,.config.complete_async_init=true,.state.async_init_step=3,.state.async_phase=6,.state.staged=-1};
 ok(&t,2,0x84);ok(&t,4,0x60);ok(&t,2,0xa4);
 DB2010I2c before=t; DB2010Pmic before_pmic=*t.pmic;
 assert(!strcmp(op(&t,0,true,0x95),"i2c_async_power_byte_unmodeled"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
 ok(&t,0,0x94);ok(&t,2,0x80);ok(&t,0,0x70);ok(&t,2,0x80);
 ok(&t,0,0xa8);ok(&t,2,0x80);ok(&t,2,0x90);
 assert(t.pmic->state.followup_cursor ==1&&t.pmic->state.power_writes ==28&&t.state.reg==0x70);
 assert(t.pmic->state.write_count ==1&&t.pmic->state.write_reg[0]==0x70&&t.pmic->state.write_value[0]==0xa8);
 ok(&t,2,0x84);ok(&t,4,0x60);ok(&t,2,0xa4);
 ok(&t,0,0x94);ok(&t,2,0x80);ok(&t,0,0x71);ok(&t,2,0x80);
 ok(&t,2,0xa4);ok(&t,0,0x95);ok(&t,2,0x80);
 t.pmic->config.followup_known[1]=false;before=t; before_pmic=*t.pmic;
 assert(!strcmp(op(&t,2,true,0x80),"i2c_followup_input_unknown"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
 t.pmic->config.followup_known[1]=true;ok(&t,2,0x80);assert(t.state.received==64);
 assert(!op(&t,0,false,0));ok(&t,2,0x90);
 assert(t.pmic->state.followup_cursor ==2&&t.pmic->state.register_reads ==56&&t.state.transactions==2);
 before=t; before_pmic=*t.pmic;assert(!strcmp(op(&t,2,true,0x84),"i2c_followup_next_dependency_unmodeled"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
 dbi_reset(&t);assert(t.pmic->state.followup_cursor ==0&&t.pmic->config.followup_count ==2&&t.pmic->config.followup_value[1]==64);
 return 0;
}
''')
    exe=tmp_path/'follow'
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-sign-compare','-I'+str(ROOT/'qemu/hw/arm'),str(code),'-o',str(exe), *component_inputs('i2c', 'fm-radio')],check=True)
    subprocess.run([str(exe)],check=True)
