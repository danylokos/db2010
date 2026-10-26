"""Original A0 control fields, guarded native I2C, state and retention independence."""
import subprocess

import pytest

from support.component_test import ROOT, component_inputs
from support.fixtures import qemu_run
from support.i2c_interleave_fixtures import state, write, read


def test_vibrator_control_fields_and_state_are_independent_of_retained_history(tmp_path):
    source = tmp_path/'vibrator.c'
    source.write_text(r'''
#include "qemu/osdep.h"
#include "db2010-i2c.h"
#include <assert.h>
int main(void) {
 DB2010Pmic p={.config={.vibrator_write_latch=true,.bounded_history=true}};
 DB2010I2c t={.pmic=&p};
 assert(!p.state.last_known[0xa0]);
 unsigned supported=0;
 for(unsigned value=0;value<256;value++) {
  /* Enumerate the Cartesian product emitted by the original two masks. */
  bool original=false;
  for(unsigned rate=0;rate<8;rate++)for(unsigned mode=0;mode<4;mode++)
   if(value==((rate<<3)|mode))original=true;
  assert(dbi_extra_write_known(&t,0xa0,value)==original);
  supported+=original;
 }
 assert(supported==32);
 assert(!dbi_extra_write_known(&t,0xa0,256));
 assert(!dbi_extra_write_known(&t,0x9f,0x2a));
 assert(!dbi_extra_write_known(&t,0xa1,0x2a));
 assert(dbi_extra_read_index(&t,0xa0)==-1);
 for(unsigned n=0;n<1024;n++)dbi_capture_write(&t,0xa0,n%2?0:0x2a);
 assert(p.state.write_count==1024&&dbi_retained_writes(&t)==128);
 assert(p.state.last_known[0xa0]&&p.state.last_value[0xa0]==0);
 p.config.omit_diagnostics=true;dbi_capture_write(&t,0xa0,0x2a);
 assert(dbi_retained_writes(&t)==0&&p.state.last_value[0xa0]==0x2a);
 assert(!p.state.followup_cursor&&!p.state.register_reads);
 dbpmic_reset(&p);assert(!p.state.last_known[0xa0]&&!p.state.write_count);
 assert(p.config.vibrator_write_latch);
 p.config.vibrator_write_latch=false;
 assert(!dbi_extra_write_known(&t,0xa0,0x2a));
 return 0;
}
''')
    executable = tmp_path/'vibrator'
    subprocess.run(['cc','-I'+str(ROOT/'qemu/hw/arm'),str(source),'-o',str(executable),
                    *component_inputs('i2c','fm-radio')],check=True)
    subprocess.run([str(executable)],check=True)


def configured(steps, *, enabled=True, readback=None):
    spec = state(steps)
    if enabled:
        spec['i2c_controller']['vibrator_write_latch'] = True
    if readback is not None:
        spec['i2c_controller']['post_startup_reads'].append({'register':0xa0,'value':readback})
    return spec


def test_native_vibrator_pulses_stop_and_sampler_are_independent(tmp_path):
    values = [((rate<<3)|mode) for rate in range(8) for mode in range(4)]*8+[0x2a,0]
    spec = configured([step for value in values for step in write(0xa0,value)]+write(0x70,0xa8)+read(0x71))
    result = qemu_run(spec,tmp_path/'run',trace=False)
    assert result['stop']['reason']=='instruction_limit'
    peer=result['i2c']
    assert peer['vibrator_write_latch'] and peer['last_write_registers']['a0']==0
    assert peer['extra_writes_completed']==len(values)
    assert peer['post_startup_completed']==2 and result['registers'][2]==64
    assert peer['captured_writes_dropped']>0


@pytest.mark.parametrize('value',[4,0x2e,0x40,0x80,0xff,0x100])
def test_native_vibrator_reserved_bits_stop_before_control_changes(tmp_path,value):
    result=qemu_run(configured(write(0xa0,0x2a)+write(0xa0,value)),tmp_path/'run',trace=False)
    assert result['stop']['reason']=='i2c_extra_write_value_unmodeled'
    assert result['i2c']['last_write_registers']['a0']==0x2a
    assert result['i2c']['extra_writes_completed']==1


def test_native_vibrator_disabled_and_unknown_readback_remain_guarded(tmp_path):
    result=qemu_run(configured(write(0xa0,0x2a),enabled=False),tmp_path/'disabled',trace=False)
    assert result['stop']['reason']=='i2c_async_power_byte_unmodeled'
    # The shared native fixture runs the real initializer's A0=0 write first.
    assert result['i2c']['last_write_registers']['a0']==0
    spec=configured(read(0xa0))
    spec['i2c_controller']['post_startup_reads'].append({'register':0xa0,'value':None})
    result=qemu_run(spec,tmp_path/'unknown-read',trace=False)
    assert result['stop']['reason']=='i2c_extra_read_input_unknown'
    assert result['i2c']['last_write_registers']['a0']==0


def test_native_vibrator_readback_requires_explicit_contract(tmp_path):
    result=qemu_run(configured(write(0xa0,0x2a)+read(0xa0),readback='last-write'),tmp_path/'explicit',trace=False)
    assert result['stop']['reason']=='instruction_limit' and result['registers'][2]==0x2a
    result=qemu_run(configured(write(0xa0,0x2a)+read(0xa0)),tmp_path/'implicit',trace=False)
    assert result['stop']['reason']=='i2c_extra_write_value_unmodeled'
    assert result['i2c']['last_write_registers']['a0']==0x2a
