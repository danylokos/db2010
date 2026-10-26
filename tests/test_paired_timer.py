"""Independent paired timer phase/IRQ contracts and native checkpoint state."""
import copy
import subprocess
import time

from support.component_test import ROOT, component_inputs
from support.fixtures import base_state, qemu_run, words
from test_native_checkpoint import engine, stopped, command, migration, comparable


def test_two_counters_masking_selected_ack_and_clock_phase(tmp_path):
    source = tmp_path / 'paired.c'
    source.write_text(r'''
#include "qemu/osdep.h"
#include "hw/core/clock.h"
#include "db2010-timer.h"
#include <assert.h>
int main(void) {
 Clock clock = {.period = CLOCK_PERIOD_FROM_HZ(32768)};
 DB2010Timer t = {.clock = &clock, .config = {.paired_channel = true, .max_irqs = 10}};
 dbtimer_reset(&t);
 uint32_t value = 0;
 assert(!strcmp(dbtimer_access(&t,0xf9010008,4,false,&value),"timer_paired_load_unknown"));
 value = 130; assert(!dbtimer_access(&t,0xf9010004,4,true,&value));
 value = 31; assert(!dbtimer_access(&t,0xf9010008,4,true,&value));
 value = 3; assert(!dbtimer_access(&t,0xf9010000,4,true,&value));
 assert(dbtimer_deadline(&t) == 976563);
 dbtimer_advance(&t,976562); assert(!t.state.paired_pending && t.state.paired_counter==31);
 dbtimer_advance(&t,976563); assert(t.state.paired_pending && !t.state.pending);
 assert(t.state.counter==32 && !t.state.paired_counter);
 t.state.masks_known[0] = t.state.masks_known[1] = true;
 t.state.masks[1] = 1u<<26;
 assert(!dbirq_any_routed(&t));
 t.state.masks[1] = 0; assert(dbirq_any_routed(&t));
 assert(!dbtimer_select(&t) && t.state.paired_selected && t.state.paired_irqs==1);
 value = 0; assert(!dbtimer_access(&t,0xfb020210,4,false,&value) && value==26);
 value = 1; assert(!strcmp(dbtimer_observe(&t,0xfb020118,value),"irq_primary_ack_before_secondary"));
 assert(t.state.paired_pending);
 assert(!dbtimer_observe(&t,0xfb020218,value));
 assert(!t.state.paired_pending && t.state.selected);
 assert(!dbtimer_observe(&t,0xfb020118,value) && !t.state.selected);
 /* Simultaneous expiries retain the other channel through one source ACK. */
 dbtimer_advance(&t,4000000); assert(t.state.pending && t.state.paired_pending);
 assert(!dbtimer_select(&t) && !t.state.paired_selected);
 assert(!dbtimer_observe(&t,0xfb020218,1) && !t.state.pending && t.state.paired_pending);
 assert(!dbtimer_observe(&t,0xfb020118,1));
 assert(dbirq_any_routed(&t));
 assert(!dbtimer_select(&t) && t.state.paired_selected);
 assert(!dbtimer_observe(&t,0xfb020218,1));
 assert(!dbtimer_observe(&t,0xfb020118,1));
 /* Stopping only channel2 leaves channel1's phase/counter/deadline intact. */
 uint32_t first=t.state.counter;
 uint64_t fraction=t.state.phase.fraction;
 value=1; assert(!dbtimer_access(&t,0xf9010000,4,true,&value));
 assert(t.state.counter==first && !t.state.paired_counter && t.state.phase.fraction==fraction);
 /* Unknown mode and wrong width cannot change either counter. */
 DB2010Timer before=t;
 value=4; assert(!strcmp(dbtimer_access(&t,0xf9010000,4,true,&value),"timer_other_channel_unmodeled"));
 assert(!memcmp(&before,&t,sizeof(t)));
 assert(!strcmp(dbtimer_access(&t,0xf9010008,2,true,&value),"timer_irq_access_width_unmodeled"));
 /* Legacy profile still refuses all paired-channel access. */
 t.config.paired_channel=false; value=2;
 assert(!strcmp(dbtimer_access(&t,0xf9010000,4,true,&value),"timer_other_channel_unmodeled"));
 assert(!strcmp(dbtimer_access(&t,0xf9010008,4,true,&value),"timer_paired_channel_unmodeled"));
 return 0;
}
''')
    executable = tmp_path / 'paired'
    subprocess.run(['cc', '-I'+str(ROOT/'qemu/hw/arm'), str(source), '-o', str(executable),
                    *component_inputs('timer')], check=True)
    subprocess.run([str(executable)], check=True)


def paired_wfi_state():
    state = base_state(words(0xe5832000, 0xe5810000, 0xe121f00c, 0xee070f90,
                             0xe3570004, 0x3afffffc, 0xeafffffe), limit=45)
    state['registers'][:7] = [2, 0xf9010000, 31, 0xf9010008, 0, 0, 0xfb020218]
    state['registers'][12] = 0x53
    state['timer_irq'] = dict(profile='tick1-paired-upcounter-irq-assumption',
                              frequency_hz=32768, max_irqs=20)
    state['device_registers'] = [dict(address=a, width=4, initial_value=0,
                                     read_or_mask=0, name='fixture.irq-mask')
                                for a in (0xfb020104, 0xfb020204, 0xfb020118, 0xfb020218)]
    state['regions'].append(dict(name='fixture.irq', address=0, size=4096, readonly=True,
        seeds=[dict(address=0x18, data=words(0xe5168008, 0xe2877001, 0xe3a05001,
            0xe5865000, 0xe5065100, 0xe25ef004))]))
    state['executable_ranges'].append(dict(start=0x18, end=0x30))
    return state


def test_native_paired_irq_selection_and_return(tmp_path):
    result = qemu_run(paired_wfi_state(), tmp_path/'run', trace=True)
    assert result['stop']['reason'] == 'instruction_limit'
    assert result['registers'][7:9] == [4, 26]
    timer = result['timer_irq']
    assert timer['irqs'] == timer['paired']['irqs'] == 4
    assert not timer['pending'] and not timer['paired']['pending'] and not timer['selected']
    assert result['cpsr'] & 0x80 == 0


def test_pending_paired_counter_survives_native_checkpoint(tmp_path):
    # Keep CPU IRQ masked, so a short-running CPU loop leaves the peripheral
    # pending independently of exception delivery. Split after counter advance.
    state = paired_wfi_state()
    state['regions'][0]['seeds'][0]['data'] = words(0xe5832000, 0xe5810000, 0xeafffffe)
    state['instruction_limit'] = 30000
    state['record_diagnostics'] = False
    with engine(state, tmp_path/'whole') as vm:
        whole = stopped(vm)
    prefix = copy.deepcopy(state)
    prefix['instruction_limit'] = 20000
    checkpoint = tmp_path/'paired.vmstate'
    with engine(prefix, tmp_path/'prefix') as vm:
        before = stopped(vm)
        assert before['timer_irq']['paired']['pending']
        migration(vm, 'migrate', checkpoint)
    continuation = copy.deepcopy(state)
    continuation['instruction_limit'] = 10000
    with engine(continuation, tmp_path/'restored', incoming=True) as vm:
        migration(vm, 'migrate-incoming', checkpoint)
        restored = vm.snapshot(time.monotonic()+5)
        expected = copy.deepcopy(before)
        expected.pop('stop')  # Continuation starts with a fresh instruction budget.
        assert comparable(restored) == comparable(expected)
        after = stopped(vm)
    assert comparable(after) == comparable(whole)
