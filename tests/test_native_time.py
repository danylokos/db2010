"""Native timeline phase math and CPU/peripheral sleep integration."""
from support.guarded import run as guarded_run
from support.paths import RESOURCES
import json
import os
import signal
import struct
import subprocess
import time
from pathlib import Path

from support.component_test import ROOT, component_inputs
from support.fixtures import base_state, qemu_run, words


def periodic_wfi_state():
    # Count 256 real IRQ entries from a 131/32768-second periodic timer.
    # Main executes WFI; modeled pins and native CPU exception/ack own each tick.
    s = base_state(words(0xe5832000, 0xe5810000, 0xe121f00c, 0xee070f90,
                         0xe3570c01, 0x3afffffc, 0xeafffffe), limit=2051)
    s['registers'][:7] = [1, 0xf9010000, 130, 0xf9010004, 0, 0, 0xfb020218]
    s['registers'][12] = 0x53
    s['timer_irq'] = {'profile':'tick1-upcounter-irq-assumption',
                      'frequency_hz':32768, 'max_irqs':300}
    s['device_registers'] = [dict(address=a, width=4, initial_value=0,
                                 read_or_mask=0, name='fixture.irq-mask')
                            for a in (0xfb020104, 0xfb020204, 0xfb020118, 0xfb020218)]
    s['regions'].append(dict(name='fixture.irq', address=0, size=4096, readonly=True,
                            seeds=[dict(address=0x18, data=words(
                                0xe2877001, 0xe3a05001, 0xe5865000,
                                0xe5065100, 0xe25ef004))]))
    s['executable_ranges'].append(dict(start=0x18, end=0x2c))
    return s


def test_paced_wfi_preserves_periodic_deadlines_after_host_stall(tmp_path):
    def stall(process):
        # A host scheduling delay must not skip guest timer deadlines.
        time.sleep(.05)
        os.kill(process.pid, signal.SIGSTOP)
        try:
            time.sleep(.05)
        finally:
            os.kill(process.pid, signal.SIGCONT)

    controls = [qemu_run(periodic_wfi_state(), tmp_path/name, trace=False,
                        icount=icount, after_cont=callback)
                for name, icount, callback in [
                    ('unpaced', 'shift=6,sleep=off,align=off', None),
                    ('paced-stalled', 'shift=6,sleep=on,align=on', stall)]]
    for result in controls:
        assert result['stop']['reason'] == 'instruction_limit'
        assert result['registers'][7] == result['timer_irq']['irqs'] == 256
        # Paced QEMU may establish its initial epoch a few source edges later.
        # Measure every interval from actual IRQ1 rather than assuming epoch0;
        # a host stall must not skip or shift any subsequent guest deadline.
        entries = result['irq_events']
        assert len(entries) == 256
        first = entries[0]['virtual_ns']
        for index, event in enumerate(entries):
            expected_ns = index * 131 * 1_000_000_000 / 32768
            assert abs(event['virtual_ns'] - first - expected_ns) < 2
        assert 0 <= result['virtual_ns'] - entries[-1]['virtual_ns'] < 1000


def test_fractional_clock_reload_gating_and_overflow(tmp_path):
    source = tmp_path/'phase.c'
    source.write_text(r'''#include "qemu/osdep.h"
#include "hw/core/clock.h"
#include "db2010-timer.h"
#include <assert.h>
extern uint64_t db2010_test_virtual_ns;
int main(void) {
 Clock clk={.period=CLOCK_PERIOD_FROM_HZ(32768)};
 DB2010Timer t={.clock=&clk,.config.frequency_hz=32768};
 dbtimer_reset(&t);
 t.state.control=1;t.state.load_known=true;t.state.load=130;
 assert(!dbtimer_advance(&t,30517)&&t.state.counter==0);
 assert(!dbtimer_advance(&t,30518)&&t.state.counter==1);
 assert(t.state.phase.fraction==13824);
 assert(dbtimer_deadline(&t)==3997803);
 dbtimer_advance(&t,3997802);assert(!t.state.pending&&t.state.counter==130);
 dbtimer_advance(&t,3997803);assert(t.state.pending&&t.state.counter==0);
 /* Reload retains fractional source phase and the live counter. */
 t.state.pending=false;dbtimer_advance(&t,5000000);
 assert(t.state.counter==32);uint64_t fraction=t.state.phase.fraction;
 uint32_t load=60;assert(!dbtimer_access(&t,0xf9010004,4,true,&load));
 assert(t.state.counter==32&&t.state.phase.fraction==fraction);
 int64_t due=dbtimer_deadline(&t);assert(due>5000000);
 dbtimer_advance(&t,due-1);assert(!t.state.pending);
 dbtimer_advance(&t,due);assert(t.state.pending);
 /* Stop the input clock after settling the old rate: no elapsed ticks while
  * gated, but sub-tick phase survives and resumes at the new rate. */
 dbtimer_advance(&t,due+10);uint32_t before=t.state.counter;
 fraction=t.state.phase.fraction;clk.period=0;
 dbtimer_advance(&t,due+1000000000);assert(t.state.counter==before);
 assert(t.state.phase.fraction==fraction&&dbtimer_deadline(&t)==-1);
 clk.period=CLOCK_PERIOD_FROM_HZ(131072);
 due=dbtimer_deadline(&t);dbtimer_advance(&t,due);
 assert(t.state.counter==0);
 /* Full 32-bit inclusive reload must not wrap its period to zero. */
 db2010_test_virtual_ns=0;dbtimer_reset(&t);
 t.state.control=1;t.state.load_known=true;t.state.load=UINT32_MAX;
 dbtimer_advance(&t,1000000000);assert(t.state.counter==131072&&!t.state.pending);
 uint32_t disable=0;assert(!dbtimer_access(&t,0xf9010000,4,true,&disable));
 assert(!t.state.counter&&dbtimer_deadline(&t)==-1);
 return 0;
}
''')
    exe = tmp_path/'phase'
    subprocess.run(['cc','-I'+str(ROOT/'qemu/hw/arm'),str(source),'-o',str(exe),
                    *component_inputs('timer')],check=True)
    subprocess.run([str(exe)],check=True)


def sleeping_peripheral_state():
    # CPU masks IRQ throughout: a native physical TIMER1 pin wakes WFI,
    # without exception entry. F500 completes earlier while no CPU runs.
    code=words(0xe5864000,0xe5853000,0xe5810000,0xe5c87000,0xe1c9a0b0,
               0xee070f90,0xe5db2000,0xeafffffe)
    s=base_state(code,limit=8)
    s['registers'][:12]=[1,0xf9010000,0,1,100,0xf9010000,0xf9010004,
                         0x19,0xf50005d6,0xf50005d8,0x25a,0xf50005e6]
    s['f500_command_port']={'profile':'word-ready-strobe-latency','latency_ns':128}
    s['timer_irq']={'profile':'tick1-upcounter-irq-assumption','frequency_hz':32768,'max_irqs':8}
    s['device_registers']=[{'address':a,'width':4,'initial_value':0,'read_or_mask':0,'name':'irq-mask'}
                            for a in (0xfb020104,0xfb020204,0xfb020118,0xfb020218)]
    return s


def test_peripheral_completion_during_masked_native_wfi(tmp_path):
    r=qemu_run(sleeping_peripheral_state(),tmp_path/'sleep',trace=True)
    assert r['stop']['reason']=='instruction_limit'
    assert r['registers'][2]==1 and r['f500_command_port']['completions']==1
    assert r['timer_irq']['irqs']==0 and r['timer_irq']['pending']
    assert r['virtual_ns']>3000000 and r['cpsr']&0x80
    assert r['virtual_ns']==r['stop']['virtual_ns']


def test_archived_time_profile_rejected_before_preparation(tmp_path):
    from db2010.preparation.inputs import prepare
    import pytest
    with pytest.raises(ValueError,match='frozen runner and QEMU source bundle'):
        prepare(ROOT,RESOURCES/'experiments/0190.json',tmp_path)
    assert not list(tmp_path.iterdir())


def test_qmp_pause_freezes_guest_clock_and_pending_events(tmp_path,monkeypatch):
    from db2010.execution import runner
    from db2010.execution.qmp import QMP
    from db2010.diagnostics.artifacts import artifact
    from test_runner import synthetic_preparation
    captured=[]
    class PauseQMP(QMP):
        def execute(self,name,deadline,arguments=None):
            result=super().execute(name,deadline,arguments)
            if name=='cont' and not captured:
                until=time.monotonic()+5
                while json.loads(super().execute('qom-get',until,{'path':'/machine','property':'progress'}))['observations']<1000:
                    pass
                super().execute('stop',time.monotonic()+5)
                def snapshot():
                    return json.loads(super(PauseQMP,self).execute('qom-get',time.monotonic()+5,
                        {'path':'/machine','property':'snapshot'}))
                first=snapshot()
                assert first['timer_irq']['control']==1 and not first['timer_irq']['pending']
                assert first['rtc']['minute_control']==128 and first['rtc']['minute_status']==0
                super().execute('qom-get',time.monotonic()+5,{'path':'/machine','property':'display'})
                progress=json.loads(super().execute('qom-get',time.monotonic()+5,{'path':'/machine','property':'progress'}))
                assert progress['timer_hz']==32768 and progress['virtual_ns']==first['virtual_ns']
                time.sleep(0.05)
                second=snapshot()
                assert first==second
                captured.append(first)
                self.events=[e for e in self.events if e.get('event')!='STOP']
                super().execute('cont',time.monotonic()+5)
            return result
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        state=json.loads(path.read_text())
        state['instruction_limit']=1000000
        artifact(output,'code.bin').write_bytes(words(0xe5832000,0xe5810000,0xe1cc50b0,0xeafffffe))
        state['executable_ranges'][0]['end']=0x1010
        state['registers'][:4]=[1,0xf9010000,32767,0xf9010004]
        state['timer_irq']={'profile':'tick1-upcounter-irq-assumption','frequency_hz':32768,'max_irqs':8}
        state['rtc_counter']=json.loads((RESOURCES/'experiments/0200.json').read_text())['rtc_counter']
        state['registers'][5]=128
        state['registers'][12]=0xf9050018
        state['device_registers']=[{'address':a,'width':4,'initial_value':0xffffffff,'read_or_mask':0,'name':'mask'} for a in (0xfb020104,0xfb020204,0xfb020118,0xfb020218)]
        path.write_text(json.dumps(state))
        return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    monkeypatch.setattr(runner,'QMP',PauseQMP)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'pause', diagnostics=True,trace=True)
    assert failure is None and captured
    assert result['snapshot']['virtual_ns']>captured[0]['virtual_ns']
    assert result['snapshot']['observations']==1000000
