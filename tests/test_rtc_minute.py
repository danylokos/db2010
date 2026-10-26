"""Recovered RTC minute enable, read-clear status and native cascade behavior."""
from support.paths import RESOURCES
import json
import subprocess
from support.component_test import ROOT, component_inputs
from support.fixtures import base_state, qemu_run, words


def test_minute_phase_status_ack_masks_reload_and_reset(tmp_path):
    source=tmp_path/'minute.c'
    source.write_text(r'''
#include "qemu/osdep.h"
#include "hw/core/clock.h"
#include "db2010-rtc.h"
#include "db2010-timer.h"
#include <assert.h>
extern uint64_t db2010_test_virtual_ns;
static void line(void *opaque,bool level) { dbtimer_set_line(opaque,DBIRQ_RTC,level); }
int main(void) {
 Clock clk={.period=CLOCK_PERIOD_FROM_HZ(32768)};
 DB2010Timer timer={.clock=&clk,.config={.rtc_route=true,.max_irqs=10}};
 DB2010Rtc rtc={.clock=&clk,.config={.minute_irq=true,.calendar_writable=true},.irq={&timer,line}};
 dbtimer_reset(&timer);dbrtc_reset(&rtc);
 dbtimer_observe(&timer,0xfb020104,0);dbtimer_observe(&timer,0xfb020204,0x8000);
 uint32_t value=0x80;
 assert(!dbrtc_access(&rtc,0,0,0xf9050018,2,true,&value));
 assert(dbrtc_deadline(&rtc)==60000000000LL);
 dbrtc_advance(&rtc,59999999999ULL);assert(!rtc.state.minute_status);
 dbrtc_advance(&rtc,60000000000ULL);dbrtc_update_irq(&rtc);
 assert(rtc.state.minute_status==128&&rtc.state.minute_events==1);
 assert(timer.state.rtc_pending&&!dbirq_any_routed(&timer));
 dbtimer_observe(&timer,0xfb020204,0);assert(dbirq_any_routed(&timer));
 assert(!dbtimer_select(&timer)&&timer.state.rtc_selected&&!timer.state.rtc_pending);
 value=0;assert(!dbtimer_access(&timer,0xfb020110,4,false,&value)&&value==28);
 value=0;assert(!dbtimer_access(&timer,0xfb020210,4,false,&value)&&value==15);
 value=1;assert(dbtimer_observe(&timer,0xfb020118,value)); /* secondary first */
 DB2010Rtc preview=rtc;
 value=0;assert(!dbrtc_access(&preview,60000000000ULL,0,0xf905001c,2,false,&value)&&value==128);
 assert(rtc.state.minute_status==128); /* preview did not acknowledge */
 value=0;assert(!dbrtc_access(&rtc,60000000000ULL,0,0xf905001c,2,false,&value)&&value==128);
 dbrtc_update_irq(&rtc);assert(!timer.state.rtc_input_level);
 value=1;assert(!dbtimer_observe(&timer,0xfb020218,value));assert(!dbtimer_observe(&timer,0xfb020118,value));
 assert(!dbirq_any_routed(&timer)&&timer.state.rtc_irqs==1);
 /* A masked source coalesces multiple minutes without losing status. */
 dbrtc_advance(&rtc,180000000000ULL);assert(rtc.state.minute_events==3&&rtc.state.minute_status==128);
 dbrtc_update_irq(&rtc);assert(timer.state.rtc_pending);
 value=0;assert(!dbrtc_access(&rtc,180000000000ULL,0,0xf9050018,2,true,&value));
 dbrtc_update_irq(&rtc);assert(dbrtc_deadline(&rtc)==-1&&!timer.state.rtc_input_level);
 dbrtc_advance(&rtc,200000000000ULL);assert(rtc.state.minute_events==3);
 value=0;assert(!dbrtc_access(&rtc,200000000000ULL,0,0xf905001c,2,false,&value)&&value==128);
 /* Calendar writes rephase the next rollover without creating an event. */
 value=59;assert(!dbrtc_access(&rtc,200000000000ULL,0,0xf9050004,2,true,&value));
 value=128;assert(!dbrtc_access(&rtc,200000000000ULL,0,0xf9050018,2,true,&value));
 assert(dbrtc_deadline(&rtc)==201000000000LL);
 value=4;assert(dbrtc_access(&rtc,200000000000ULL,0,0xf9050018,2,true,&value));
 assert(rtc.state.minute_control==128&&!rtc.state.minute_status);
 db2010_test_virtual_ns=200000000000ULL;dbrtc_reset(&rtc);
 assert(!rtc.state.minute_events&&!rtc.state.minute_status&&dbrtc_deadline(&rtc)==-1);
 /* Midnight rollover reaches the next day through the ordinary calendar. */
 value=59;assert(!dbrtc_access(&rtc,200000000000ULL,0,0xf9050004,2,true,&value));
 value=59;assert(!dbrtc_access(&rtc,200000000000ULL,0,0xf9050006,2,true,&value));
 value=23;assert(!dbrtc_access(&rtc,200000000000ULL,0,0xf9050008,2,true,&value));
 value=128;assert(!dbrtc_access(&rtc,200000000000ULL,0,0xf9050018,2,true,&value));
 dbrtc_advance(&rtc,201000000000ULL);assert(rtc.state.minute_events==1);
 value=0;assert(!dbrtc_access(&rtc,201000000000ULL,0,0xf905000c,2,false,&value)&&value==2);
 value=1;assert(!dbrtc_access(&rtc,201000000000ULL,0,0xf9050008,2,false,&value)&&value==0);
 /* Gated input retains a half-minute phase; restore resumes its remainder. */
 db2010_test_virtual_ns=201000000000ULL;dbrtc_reset(&rtc);
 value=128;assert(!dbrtc_access(&rtc,201000000000ULL,0,0xf9050018,2,true,&value));
 dbrtc_advance(&rtc,231000000000ULL);clk.period=0;
 dbrtc_advance(&rtc,331000000000ULL);assert(!rtc.state.minute_events&&dbrtc_deadline(&rtc)==-1);
 clk.period=CLOCK_PERIOD_FROM_HZ(32768);assert(dbrtc_deadline(&rtc)==361000000000LL);
 dbrtc_advance(&rtc,361000000000ULL);assert(rtc.state.minute_events==1);
 /* Simultaneous expiries preserve TIMER1 preference and RTC's pending edge. */
 db2010_test_virtual_ns=0;dbtimer_reset(&timer);dbrtc_reset(&rtc);
 dbtimer_observe(&timer,0xfb020104,0);dbtimer_observe(&timer,0xfb020204,0);
 timer.state.control=1;timer.state.load_known=true;timer.state.load=60*32768-1;
 value=128;assert(!dbrtc_access(&rtc,0,0,0xf9050018,2,true,&value));
 dbtimer_advance(&timer,60000000000ULL);dbrtc_advance(&rtc,60000000000ULL);dbrtc_update_irq(&rtc);
 assert(!dbtimer_select(&timer)&&!timer.state.rtc_selected&&timer.state.rtc_pending);
 value=1;assert(!dbtimer_observe(&timer,0xfb020218,value));assert(!dbtimer_observe(&timer,0xfb020118,value));
 assert(!dbtimer_select(&timer)&&timer.state.rtc_selected&&!timer.state.pending);
 return 0;
}
''')
    exe=tmp_path/'minute'
    subprocess.run(['cc','-I'+str(ROOT/'qemu/hw/arm'),str(source),'-o',str(exe),
                    *component_inputs('rtc','timer')],check=True)
    subprocess.run([str(exe)],check=True)


def test_native_minute_irq_wakes_masked_wfi_and_clears_status(tmp_path):
    # No TIMER1 counter runs. RTC's own event must wake native ARM WFI.
    state=base_state(words(0xe1c100b0,0xee070f90,0xe1d320b0,0xe1d340b0,0xeafffffe),limit=5)
    state['registers'][:4]=[128,0xf9050018,0,0xf905001c]
    state['rtc_counter']=json.loads((RESOURCES/'experiments/0200.json').read_text())['rtc_counter']
    state['timer_irq']={'profile':'tick1-upcounter-irq-assumption','frequency_hz':32768,'max_irqs':8}
    state['device_registers']=[{'address':a,'width':4,'initial_value':0,'read_or_mask':0,'name':'mask'} for a in (0xfb020104,0xfb020204,0xfb020118,0xfb020218)]
    r=qemu_run(state,tmp_path/'minute',trace=True,reset=True)
    assert r['stop']['reason']=='instruction_limit'
    assert r['registers'][2]==128 and r['registers'][4]==0
    assert r['virtual_ns']>=60000000000 and r['cpsr']&128
    assert r['timer_irq']['rtc_irqs']==0 and r['timer_irq']['rtc_pending']
    assert r['rtc']['minute_events']==1 and r['rtc']['minute_status']==0
    assert r['after_reset']['rtc']['minute_events']==0
    assert r['after_reset']['rtc']['minute_deadline_ns']==-1
