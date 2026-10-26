"""Recording rollover must not erase register state or change protocol guards."""
from support.guarded import run as guarded_run
from db2010.diagnostics.artifacts import artifact as artifact_path
from support.component_test import component_inputs

import json
from pathlib import Path
import subprocess

import pytest

from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation


def test_retention_register_state_reset_and_large_counters(tmp_path):
    source=tmp_path/'recording.c'
    source.write_text(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define G_N_ELEMENTS(a) ARRAY_SIZE(a)
#include "db2010-i2c.h"
#include "db2010-timer.h"
int main(void) {
 DB2010Pmic t_pmic = {.config.bounded_history =true};
 DB2010FmRadio t_fm = {0};
 DB2010I2c t = {.pmic = &t_pmic, .fm = &t_fm};
 for(unsigned i=0;i<64;i++)dbi_capture_write(&t,i,i);
 for(unsigned i=64;i<1000;i++)dbi_capture_write(&t,i==100?0x26:0x70,i==100?0xcc:(uint8_t)i);
 assert(t.pmic->state.write_count ==1000&&dbi_retained_writes(&t)==128);
 for(unsigned ordinal=0;ordinal<128;ordinal++) {
  uint64_t index=dbi_write_index(&t,ordinal);
  assert(index==(ordinal<64?ordinal:1000-128+ordinal));
  unsigned slot=dbi_write_slot(&t,index);
  assert(t.pmic->state.write_reg[slot]==(index<64?index:0x70));
  assert(t.pmic->state.write_value[slot]==(uint8_t)index);
 }
 /* Register26's middle record was evicted, but its current value must survive. */
 t.config.capture_async_init=t.config.complete_async_init=t.pmic->config.capture_async_power =true;
 t.pmic->config.complete_power_init =t.pmic->config.read_power_status =t.pmic->config.startup_register_reads =true;
 t.state.async_init_step=3;t.state.async_phase=6;t.state.peer_step =15;t.pmic->state.power_writes =27;
 t.pmic->state.status_reads =3;t.pmic->config.register_read_count =1;t.pmic->config.read_reg[0]=0x26;t.pmic->config.read_kind[0]=2;
 t.state.staged=t.state.received=-1;
 DB2010Pmic ready_pmic=*t.pmic;DB2010FmRadio ready_fm=*t.fm;
 DB2010I2c ready=t;ready.pmic=&ready_pmic;ready.fm=&ready_fm;uint32_t value=0x80;
 assert(!dbi_access(&t,0xf9040002,2,100,true,&value)&&t.state.received==0xcc);
 t=ready;t_pmic=ready_pmic;t_fm=ready_fm;t.pmic=&t_pmic;t.fm=&t_fm;t.pmic->config.read_reg[0]=0x55;
 assert(!strcmp(dbi_access(&t,0xf9040002,2,100,true,&value),"i2c_async_power_input_unknown"));
 t=ready;t_pmic=ready_pmic;t_fm=ready_fm;t.pmic=&t_pmic;t.fm=&t_fm;t.pmic->config.followup_count =13;t.pmic->config.followup_repeat_from =7;t.pmic->config.repeat_followups =true;
 t.pmic->state.followup_cursor =(uint64_t)UINT32_MAX+8;t.pmic->state.register_reads =(uint64_t)UINT32_MAX+8;
 t.pmic->state.power_writes =(uint64_t)UINT32_MAX+8;
 assert(dbi_followup_index(&t)==7+(t.pmic->state.followup_cursor-7)%6);
 uint64_t cursor=t.pmic->state.followup_cursor,writes=t.pmic->state.power_writes;
 t.state.peer_step =11;t.state.pending=DBI_STOP;t.state.due=0;dbi_finish(&t,100);
 assert(t.pmic->state.followup_cursor ==cursor+1&&t.pmic->state.power_writes ==writes+1);
 dbi_reset(&t);assert(t.pmic->config.bounded_history&&t.pmic->state.write_count ==0&&!t.pmic->state.last_known[0x26]);
 DB2010Timer timer={.config.max_irqs=1,.state.irqs=(uint64_t)UINT32_MAX+5,.config.unlimited_irqs=true,
                   .state.pending=true,.state.masks_known={true,true,true}};
 assert(!dbtimer_select(&timer)&&timer.state.irqs==(uint64_t)UINT32_MAX+6);
 timer.state.selected=false;timer.config.unlimited_irqs=false;
 assert(!strcmp(dbtimer_select(&timer),"timer_irq_budget"));
 timer.state.pending=false;timer.config.unlimited_irqs=true;
 assert(!strcmp(dbtimer_select(&timer),"irq_entry_without_routed_source"));
 return 0;
}
''')
    executable=tmp_path/'recording'
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-sign-compare',
                    '-I'+str(ROOT/'qemu/hw/arm'),str(source),'-o',str(executable), *component_inputs('i2c', 'fm-radio', 'timer')],check=True)
    subprocess.run([str(executable)],check=True)


@pytest.mark.parametrize('policy', [None, {}, {'profile': 'unknown'},
                                  {'profile': 'startup64-recent64-irq512', 'extra': 1}])
def test_native_recording_policy_validation(tmp_path,monkeypatch,policy):
    def prepare(root,spec,output):
        path,config=synthetic_preparation(root,spec,output)
        state=json.loads(path.read_text());state['instruction_limit']=1
        if policy is not None:
            state['recording_history']=policy
        path.write_text(json.dumps(state)+'\n')
        return path,config
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True,trace=False)
    if policy is None:
        assert failure is None and 'recording_history' not in result
    else:
        assert failure is not None
        assert 'unsupported recording history policy' in (artifact_path((tmp_path) / 'run', 'qemu.log')).read_text()


def test_host_recording_policy_preserves_frozen_preparation(tmp_path,monkeypatch):
    monkeypatch.setattr(runner,'prepare',synthetic_preparation)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True,trace=False,
                              bounded_history=True,instruction_limit=1)
    assert failure is None
    assert result['snapshot']['stop']['observations']==1
    assert result['recording_history']=={'profile':'startup64-recent64-irq512'}
    assert json.loads((artifact_path((tmp_path) / 'run', 'state.json')).read_text())['recording_history']==result['recording_history']
