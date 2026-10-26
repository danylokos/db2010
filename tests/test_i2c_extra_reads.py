"""Compiled QEMU PMIC model: interleaved result reads and same-register RMW."""
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


def test_interleaved_reads_and_rmw(tmp_path):
    source = tmp_path / 'reads.c'
    source.write_text(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#include "db2010-i2c.h"
static void clone(DB2010I2c *dst,const DB2010I2c *src,DB2010Pmic *pmic,DB2010FmRadio *fm) {
 *pmic=*src->pmic;*fm=*src->fm;*dst=*src;dst->pmic=pmic;dst->fm=fm;
}
static uint64_t now=100;
static const char *op(DB2010I2c*t,unsigned off,uint32_t value) {
 const char*f=dbi_access(t,0xf9040000+off,2,(now)*64,true,&value);
 now+=3;if(!f)dbi_finish(t,(now)*64);return f;
}
static void ok(DB2010I2c*t,unsigned off,uint32_t value){assert(!op(t,off,value));}
static void data(DB2010I2c*t,unsigned value){ok(t,0,value);ok(t,2,0x80);}
static void select_reg(DB2010I2c*t,unsigned reg){
 ok(t,2,0x84);ok(t,4,0x60);ok(t,2,0xa4);data(t,0x94);data(t,reg);
}
static void receive_prefix(DB2010I2c*t){ok(t,2,0xa4);data(t,0x95);}
static unsigned receive(DB2010I2c*t){
 ok(t,2,0x80);uint32_t value=0;
 assert(!dbi_access(t,0xf9040000,2,(now)*64,false,&value));now+=3;ok(t,2,0x90);return value;
}
int main(void){
 DB2010Pmic initial_pmic = {.config.capture_async_power =true,.config.complete_power_init =true,.config.read_power_status =true,.config.startup_register_reads =true,.state.power_writes =27,.state.register_reads =55,.config.register_read_count =55,.state.status_reads =3,.config.followup_count =2,.config.followup_reg ={0x62,0x71},.config.followup_value ={1,64},.config.followup_read ={false,true},.config.followup_known ={true,true},.config.extra_write_count =1,.config.extra_write_reg ={0x73},.config.extra_write_value ={0},.config.extra_read_count =3,.config.extra_read_reg ={0x75,0x73,0x71},.config.extra_read_value ={64,0,64},.config.extra_read_kind ={1,2,1}};
 DB2010FmRadio initial_fm = {0};
 DB2010I2c initial = {.pmic = &initial_pmic, .fm = &initial_fm,.config.capture_async_init=true,.config.complete_async_init=true,.state.async_init_step=3,.state.async_phase=6,.state.staged=-1};
 for(unsigned value=0;value<=255;value+=value==0?64:191){
  DB2010Pmic t_pmic;DB2010FmRadio t_fm;DB2010I2c t;clone(&t,&initial,&t_pmic,&t_fm);t.pmic->config.extra_read_value[0]=value;select_reg(&t,0x75);receive_prefix(&t);
  assert(receive(&t)==value&&t.pmic->state.followup_cursor ==0&&t.pmic->state.extra_reads ==1&&!t.pmic->state.extra_active);
  assert(t.pmic->state.register_reads ==56&&t.pmic->state.power_writes ==27&&t.state.transactions==1);
 }
 for(unsigned channel=0;channel<5;channel++) {
  DB2010Pmic p;DB2010FmRadio fm;DB2010I2c t;clone(&t,&initial,&p,&fm);
  p.config.sampled_battery_adc=true;p.config.sampled_battery_known=true;p.config.sampled_battery_value=136;
  p.state.last_known[0x70]=channel!=4;
  p.state.last_value[0x70]=(uint8_t[]){0xa1,0xab,0xa8,0xac,0xa1}[channel];
  select_reg(&t,0x71);receive_prefix(&t);
  if(channel==0) {
   p.config.sampled_battery_known=false;DB2010I2c before=t;DB2010Pmic before_pmic=p;
   assert(!strcmp(op(&t,2,0x80),"i2c_extra_read_input_unknown"));
   assert(!memcmp(&t,&before,sizeof(t))&&!memcmp(&p,&before_pmic,sizeof(p)));
   p.config.sampled_battery_known=true;
  }
  assert(receive(&t)==(channel==0?136:64));assert(!p.state.followup_cursor);
 }
 DB2010Pmic t_pmic;DB2010FmRadio t_fm;DB2010I2c t;clone(&t,&initial,&t_pmic,&t_fm);select_reg(&t,0x75);receive_prefix(&t);t.pmic->config.extra_read_kind[0]=0;
 DB2010I2c before=t; DB2010Pmic before_pmic=*t.pmic;assert(!strcmp(op(&t,2,0x80),"i2c_extra_read_input_unknown"));
 assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
 clone(&t,&initial,&t_pmic,&t_fm);select_reg(&t,0x73);receive_prefix(&t);before=t; before_pmic=*t.pmic;
 assert(!strcmp(op(&t,2,0x80),"i2c_extra_read_input_unknown"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
 t.pmic->state.last_known[0x73]=true;t.pmic->state.last_value[0x73]=0xa5;
 assert(receive(&t)==0xa5&&t.pmic->state.followup_cursor ==0);
 select_reg(&t,0x73);before=t; before_pmic=*t.pmic;
 assert(!strcmp(op(&t,0,1),"i2c_extra_write_value_unmodeled"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
 data(&t,0);ok(&t,2,0x90);assert(t.pmic->state.last_value[0x73]==0&&t.pmic->state.extra_writes ==1&&t.pmic->state.followup_cursor ==0);
 select_reg(&t,0x73);receive_prefix(&t);assert(receive(&t)==0&&t.pmic->state.extra_reads ==2);
 select_reg(&t,0x62);data(&t,1);ok(&t,2,0x90);assert(t.pmic->state.followup_cursor ==1);
 select_reg(&t,0x75);receive_prefix(&t);assert(receive(&t)==64&&t.pmic->state.followup_cursor ==1);
 select_reg(&t,0x71);receive_prefix(&t);assert(receive(&t)==64&&t.pmic->state.followup_cursor ==2);
 clone(&t,&initial,&t_pmic,&t_fm);ok(&t,2,0x84);ok(&t,4,0x60);ok(&t,2,0xa4);data(&t,0x94);before=t; before_pmic=*t.pmic;
 assert(!strcmp(op(&t,0,0x76),"i2c_async_power_byte_unmodeled"));assert(!memcmp(&t,&before,sizeof(t)));assert(!memcmp(t.pmic,&before_pmic,sizeof(before_pmic)));
 uint32_t value=0;assert(!strcmp(dbi_access(&t,0xf9040000,1,now,false,&value),"i2c_register_access_unmodeled"));
 dbi_reset(&t);assert(t.pmic->config.extra_read_count ==3&&t.pmic->config.extra_read_kind[1]==2&&!t.pmic->state.extra_reads&&!t.pmic->state.extra_active);
 assert(!t.pmic->state.last_known[0x73]);return 0;
}
''')
    exe = tmp_path / 'reads'
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-sign-compare',
                    '-I' + str(ROOT / 'qemu/hw/arm'), str(source), '-o', str(exe), *component_inputs('i2c', 'fm-radio')], check=True)
    subprocess.run([str(exe)], check=True)



@pytest.mark.parametrize('change,expected', [
    ('valid', None), ('battery_unknown', None), ('duplicate', 'duplicate extra PMIC read'),
    ('value', 'invalid extra PMIC read value'),
    ('empty', 'bounded extra PMIC reads require post-startup operations'),
])
def test_prepared_extra_reads_are_bounded(tmp_path, monkeypatch, change, expected):
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        state = json.loads(path.read_text())
        state['instruction_limit'] = 1
        i2c = json.loads((RESOURCES/'experiments/0159.json').read_text())['i2c_controller']
        i2c['completion_ns'] = 64*i2c.pop('completion_instructions')
        i2c['sampled_battery_adc'] = None if change == 'battery_unknown' else 136
        if change == 'duplicate':
            i2c['post_startup_reads'].append(dict(i2c['post_startup_reads'][0]))
        elif change == 'value':
            i2c['post_startup_reads'][0]['value'] = 'unsupported'
        elif change == 'empty':
            i2c['post_startup_reads'] = []
        state['i2c_controller'] = i2c
        path.write_text(json.dumps(state) + '\n')
        return path, {'seconds': 5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path / 'run', diagnostics=True)
    if expected:
        assert failure is not None and expected in (artifact_path((tmp_path) / 'run', 'qemu.log')).read_text()
    else:
        assert failure is None and result['snapshot']['stop']['reason'] == 'instruction_limit'
        assert result['snapshot']['i2c']['sampled_battery_adc'] == (None if change == 'battery_unknown' else 136)
