"""Native MMIO contacts, controller masking/acknowledgement and QOM validation."""
from support.guarded import run as guarded_run
import json
import pytest
from support.fixtures import qemu_run
from support.startup_status_fixtures import fixture

MAPPING = {'1':(3,1),'2':(2,1),'3':(0,1),'4':(3,2),'5':(2,2),'6':(0,2),
 '7':(3,4),'8':(2,4),'9':(0,4),'0':(2,8),'star':(3,8),'hash':(0,8),
 'soft-left':(1,1),'soft-right':(1,2),'back':(1,4),'clear':(1,8),'walkman':(5,2),
 'up':(4,0x1a),'right':(4,0x19),'down':(4,0x0d),'left':(4,0x0e),'select':(4,8),
 'volume-up':(3,0x10),'volume-down':(2,0x10),'play-pause':(1,0x10),
 'camera-focus':(5,4),'camera':(5,0x0c),'power':(6,1)}


def state(steps):
    s = fixture(steps,0)
    s.pop('startup_status')
    s['keypad'] = {'profile':'w800-front-ideal-contacts'}
    return s


@pytest.mark.parametrize('button',MAPPING)
def test_every_key_selected_and_idle(tmp_path,button):
    row,low=MAPPING[button]
    for rows in ((63,62,31) if row == 6 else (63,63 ^ (1<<row),63 ^ (1<<((row+1)%6)))):
        s=state([('w',0xfb010002,0x3fc0|rows,2),('r',0xfb010002,0,2)])
        r=qemu_run(s,tmp_path/str(rows),qom_set={'/machine/keypad':{'pressed':json.dumps([button])}}, reset=True)
        assert r['stop']['reason']=='instruction_limit'
        expected=255 ^ low if row == 6 or not rows & (1<<row) else 255
        assert r['registers'][2] == rows | (expected<<6)
        assert r['keypad']['pressed']==[button]
        assert r['keypad']['transitions']==1
        assert r['after_reset']['keypad']['pressed']==[]
        assert {k:v for k,v in r['after_reset'].items() if k!='virtual_ns'} == {k:v for k,v in r['initial'].items() if k!='virtual_ns'}


def test_simultaneous_shared_row_and_joystick(tmp_path):
    for keys, rows, columns in [(['1','4'],55,252),(['1','2'],55,254),
                                 (['up','right'],47,228),(['select','left'],47,241),
                                 (['camera-focus','camera'],31,243),
                                 (['power','volume-up'],55,238),(['power','1'],63,254)]:
        r=qemu_run(state([('w',0xfb010002,rows,2),('r',0xfb010002,0,2)]),
            tmp_path/('-'.join(keys)),qom_set={'/machine/keypad':{'pressed':json.dumps(keys)}})
        assert r['registers'][2] == rows | columns<<6


@pytest.mark.parametrize('step,reason',[
 (('r',0xfb010000,0,2),'keypad_control_unknown'),
 (('w',0xfb010000,0,2),'keypad_control_write_unmodeled'),
 (('r',0xfb010000,0,4),'keypad_access_unmodeled'),
 (('w',0xfb010002,0x3ffc,2),'keypad_rows_write_unmodeled'),
 (('w',0xfb010002,0xffff,2),'keypad_rows_write_unmodeled'),
 (('r',0xfb010004,0,2),'device_register_access_unmodeled')])
def test_guard_before_effect(tmp_path,step,reason):
    r=qemu_run(state([step]),tmp_path/'guard',trace=True)
    assert r['stop']['reason']==reason
    assert r['keypad']['column_reads']==r['keypad']['transitions']==0


def test_irq60_mask_and_ack(tmp_path):
    # Controller status sees tertiary0 through cascade30. CPU IRQ masked here;
    # explicit selection/ack path covered by unchanged-firmware research runs.
    s=state([('r',0xfb020300,0,4),('r',0xfb020100,0,4)])
    s['timer_irq']={'profile':'tick1-upcounter-irq-assumption','frequency_hz':131072,'max_irqs':8}
    s['device_registers']=[{'address':a,'width':4,'initial_value':v,'read_or_mask':0,'name':'fixture.mask'}
                           for a,v in [(0xfb020104,0),(0xfb020204,0),(0xfb020304,0),(0xfb020118,0),(0xfb020218,0)]]
    # Enable the keypad natively before a CPU-thread input. Press at reset is
    # acknowledged by initialization, so this fixture checks quiet init/reset.
    r=qemu_run(s,tmp_path/'masked')
    assert r['stop']['reason']=='instruction_limit'
    assert r['registers'][2]==0


def test_keypad_irq_mask_ack_and_edge_consumption(tmp_path):
    import subprocess
    from db2010.paths import ROOT
    from support.component_test import component_inputs
    code=tmp_path/'keypad-irq.c'
    code.write_text(r'''
#include <assert.h>
#include <string.h>
#include "db2010-timer.h"
int main(void) {
 DB2010Timer t={.config.keypad_route=true,.config.unlimited_irqs=true,
  .state.masks_known={true,true,true},.state.masks={1u<<30,0,0}};
 dbtimer_set_line(&t,DBIRQ_KEYPAD,true);
 assert(!dbirq_any_routed(&t));
 t.state.masks[0]=0;t.state.masks[2]=1;assert(!dbirq_any_routed(&t));
 t.state.masks[2]=0;assert(dbirq_any_routed(&t));
 uint32_t value=0;
 assert(!dbtimer_access(&t,0xfb020100,4,false,&value)&&value==(1u<<30));
 assert(!dbtimer_access(&t,0xfb020300,4,false,&value)&&value==1);
 for(unsigned i=0;i<2048;i++) {
  dbtimer_set_line(&t,DBIRQ_KEYPAD,true);
  assert(!dbtimer_select(&t)&&t.state.keypad_selected&&!t.state.keypad_pending);
  assert(!dbtimer_access(&t,0xfb020110,4,false,&value)&&value==30);
  assert(!dbtimer_access(&t,0xfb020310,4,false,&value)&&value==0);
  assert(!strcmp(dbtimer_access(&t,0xfb020210,4,false,&value),"irq_selection_wrong_bank"));
  assert(!strcmp(dbtimer_observe(&t,0xfb020118,1),"irq_primary_ack_before_secondary"));
  assert(!strcmp(dbtimer_observe(&t,0xfb020218,1),"irq_ack_wrong_bank"));
  assert(!strcmp(dbtimer_observe(&t,0xfb020318,2),"irq_ack_value_unmodeled"));
  assert(!dbtimer_observe(&t,0xfb020318,1)&&!dbtimer_observe(&t,0xfb020118,1));
  assert(!dbirq_any_routed(&t));
 }
 assert(t.state.keypad_irqs==2048);
 dbtimer_reset(&t);assert(!t.state.keypad_pending&&!t.state.keypad_selected&&!t.state.keypad_irqs);
 return 0;
}
''')
    exe=tmp_path/'keypad-irq'
    subprocess.run(['cc','-Wall','-Wextra','-Werror','-I'+str(ROOT/'qemu/hw/arm'),str(code),
                    '-o',str(exe),*component_inputs('timer')],check=True)
    subprocess.run([str(exe)],check=True)


def test_qom_pressed_validation_is_atomic_on_cpu_thread(tmp_path,monkeypatch):
    from pathlib import Path
    from db2010.execution import runner
    from db2010.paths import ROOT
    from test_runner import synthetic_preparation
    def prepare(root,spec,output,**options):
        path,spec=synthetic_preparation(root,spec,output)
        s=json.loads(path.read_text());s['keypad']={'profile':'w800-front-ideal-contacts'}
        path.write_text(json.dumps(s));return path,spec
    monkeypatch.setattr(runner,'prepare',prepare)
    original=runner.QMP.observe;checked=[]
    def observe(qmp,deadline,progress=None,display=None):
        if not checked:
            def set_keys(value):return qmp.execute('qom-set',deadline,
                {'path':'/machine/keypad','property':'pressed','value':value})
            def get_keys():return json.loads(qmp.execute('qom-get',deadline,
                {'path':'/machine/keypad','property':'pressed'}))
            set_keys('["1"]')
            for value in ['["2","bad"]','["1","1"]','{}','[true]','["1\\u0000bad"]','x'*1025]:
                with pytest.raises(RuntimeError,match='QMP error'):set_keys(value)
                assert get_keys()==['1']
            set_keys('[]');assert get_keys()==[]
            checked.append(True)
        return original(qmp,deadline,progress,display)
    monkeypatch.setattr(runner.QMP,'observe',observe)
    class Samples:
        show_status=True;show_display=False
        def phase(self,name):pass
        def progress(self,sample):pass
        def uart(self,*args):pass
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'capture', diagnostics=True,instruction_limit=2000000,seconds=5,console=Samples())
    assert failure is None and checked and result['snapshot']['keypad']['transitions']==2
