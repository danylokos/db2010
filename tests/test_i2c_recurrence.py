"""Recurring known ADC cycles preserve unknown-input/order and reset guards."""
from support.guarded import run as guarded_run
from support.paths import RESOURCES
from db2010.diagnostics.artifacts import artifact as artifact_path
from support.component_test import component_inputs

import json
from pathlib import Path
import subprocess

import pytest

from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation


@pytest.mark.parametrize('cycles,bounded,omit', [(5, False, False), (200, True, False), (200, False, True)])
def test_repeated_native_adc_bytes_and_guards(tmp_path, cycles, bounded, omit):
    code=tmp_path/'repeat.c'
    code.write_text(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#include "db2010-i2c.h"
static uint64_t now=100;
static const char *op(DB2010I2c*t,unsigned off,bool write,uint32_t value){
 const char*f=dbi_access(t,0xf9040000+off,2,(now)*64,write,&value);
 now+=3;if(!f)dbi_finish(t,(now)*64);return f;
}
static void ok(DB2010I2c*t,unsigned off,uint32_t v){assert(!op(t,off,true,v));}
int main(void){
 DB2010Pmic t_pmic = {.config.omit_diagnostics=OMIT,.config.capture_async_power =true,.config.complete_power_init =true,.config.read_power_status =true,.config.startup_register_reads =true,.state.power_writes =30,.state.register_reads =59,.config.register_read_count =55,.state.status_reads =3,.config.followup_count =13,.state.followup_cursor =7,.config.repeat_followups =true,.config.followup_repeat_from =7,.config.bounded_history =BOUNDED};
 DB2010FmRadio t_fm = {0};
 DB2010I2c t = {.pmic = &t_pmic, .fm = &t_fm,.config.capture_async_init=true,.config.complete_async_init=true,.state.async_init_step=3,.state.async_phase=6,.state.staged=-1};
 uint8_t regs[]={0x70,0x71,0x70,0x71,0x7a,0x77};
 uint8_t vals[]={0xa8,64,0xac,64,64,136};
 bool reads[]={false,true,false,true,true,true};
 for(unsigned i=0;i<6;i++){
  unsigned k=i+7;t.pmic->config.followup_reg[k]=regs[i];t.pmic->config.followup_value[k]=vals[i];
  t.pmic->config.followup_read[k]=reads[i];t.pmic->config.followup_known[k]=true;
 }
 for(unsigned cycle=0;cycle<CYCLES;cycle++)for(unsigned i=0;i<6;i++){
  assert(dbi_followup_index(&t)==i+7);
  ok(&t,2,0x84);ok(&t,4,0x60);ok(&t,2,0xa4);
  DB2010I2c before=t; DB2010Pmic before_pmic=*t.pmic;
  assert(!strcmp(op(&t,0,true,0x95),"i2c_async_power_byte_unmodeled"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
  ok(&t,0,0x94);ok(&t,2,0x80);
  before=t; before_pmic=*t.pmic;assert(!strcmp(op(&t,0,true,0x7f),"i2c_async_power_byte_unmodeled"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
  ok(&t,0,regs[i]);ok(&t,2,0x80);
  if(reads[i]){
   ok(&t,2,0xa4);ok(&t,0,0x95);ok(&t,2,0x80);
   t.pmic->config.followup_known[i+7]=false;before=t; before_pmic=*t.pmic;
   assert(!strcmp(op(&t,2,true,0x80),"i2c_followup_input_unknown"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
   t.pmic->config.followup_known[i+7]=true;ok(&t,2,0x80);assert(t.state.received==vals[i]);
   assert(!op(&t,0,false,0));
  }else{
   before=t; before_pmic=*t.pmic;assert(!strcmp(op(&t,0,true,0xff),"i2c_async_power_byte_unmodeled"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
   ok(&t,0,vals[i]);ok(&t,2,0x80);
  }
  ok(&t,2,0x90);assert(!t.state.pending&&!t.state.irq_pending&&t.state.status==0xf8);
 }
 assert(t.pmic->state.followup_cursor ==7+6*CYCLES&&t.state.transactions==6*CYCLES&&t.pmic->state.write_count ==2*CYCLES);
 assert(t.pmic->state.register_reads ==59+4*CYCLES&&t.pmic->state.power_writes ==30+2*CYCLES);
 if(OMIT) {
  assert(dbi_retained_writes(&t)==0&&t.pmic->state.last_known[0x70]&&t.pmic->state.last_value[0x70]==0xac);
  for(unsigned i=0;i<10;i++)assert(!t.pmic->state.write_reg[i]&&!t.pmic->state.write_value[i]);
 } else for(unsigned i=0;i<10;i++)assert(t.pmic->state.write_reg[i]==0x70&&t.pmic->state.write_value[i]==(i%2?0xac:0xa8));
 t.pmic->config.repeat_followups =false;DB2010I2c before=t; DB2010Pmic before_pmic=*t.pmic;
 assert(!strcmp(op(&t,2,true,0x84),"i2c_followup_next_dependency_unmodeled"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
 t.pmic->config.repeat_followups =true;dbi_reset(&t);
 assert(t.pmic->state.followup_cursor ==0&&t.pmic->config.followup_count ==13&&t.pmic->config.repeat_followups&&t.pmic->config.followup_repeat_from ==7);
 return 0;
}
''')
    exe=tmp_path/'repeat'
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-sign-compare',
                    '-DCYCLES='+str(cycles), '-DBOUNDED='+str(int(bounded)), '-DOMIT='+str(int(omit)),
                    '-I'+str(ROOT/'qemu/hw/arm'),str(code),'-o',str(exe), *component_inputs('i2c', 'fm-radio')],check=True)
    subprocess.run([str(exe)],check=True)


@pytest.mark.parametrize('change,expected',[
    ('valid',None),('battery',None),('from','unsupported recurring ADC tail'),
    ('count','unsupported recurring ADC tail'),('value','unsupported recurring ADC input'),
    ('unknown','unsupported recurring ADC input'),
    ('battery_unknown','unsupported recurring ADC input'),
])
def test_native_prepared_profile_is_bounded(tmp_path,monkeypatch,change,expected):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        state=json.loads(path.read_text());state['instruction_limit']=1
        i2c=json.loads((RESOURCES/'experiments/0140.json').read_text())['i2c_controller']
        i2c['completion_ns'] = 64*i2c.pop('completion_instructions')
        if change=='from':i2c['post_startup_repeat_from']=6
        if change=='count':i2c['post_startup'].pop()
        if change=='value':i2c['post_startup'][8]['value']=255
        if change=='unknown':i2c['post_startup'][8]['value']=None
        if change=='battery':i2c['post_startup'][12]['value']=136
        if change=='battery_unknown':i2c['post_startup'][12]['value']=None
        state['i2c_controller']=i2c;path.write_text(json.dumps(state)+'\n')
        return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True)
    if expected:
        assert failure is not None and expected in (artifact_path((tmp_path) / 'run', 'qemu.log')).read_text()
    else:
        assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
        assert result['snapshot']['i2c']['post_startup_repeat_from']==7
