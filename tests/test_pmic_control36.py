"""Recovered digital PMIC36/37 field storage, with cold/reserved-bit guards."""
import subprocess
from support.component_test import ROOT, component_inputs


def test_control_fields_preserve_unsupported_bits_and_unknown_state(tmp_path):
    source=tmp_path/'control.c'
    source.write_text(r'''
#include "qemu/osdep.h"
#include "db2010-i2c.h"
#include <assert.h>
int main(void) {
 DB2010Pmic p={.config.control36_37_write_latches=true,.config.bounded_history=true};
 DB2010I2c t={.pmic=&p};
 assert(dbi_extra_write_known(&t,0x36,-1));
 assert(!dbi_extra_write_known(&t,0x36,8)); /* Cold state not invented. */
 assert(!dbi_extra_write_known(&t,0x37,8));
 for(unsigned reg=0x36;reg<=0x37;reg++) {
  unsigned mask=reg==0x36?0x3b:0x38;
  for(unsigned initial=0;initial<256;initial++) {
   dbi_capture_write(&t,reg,initial);
   for(unsigned value=0;value<256;value++) {
    assert(dbi_extra_write_known(&t,reg,value)==!((initial^value)&~mask));
   }
  }
 }
 assert(!dbi_extra_write_known(&t,0x36,256));
 assert(!dbi_extra_write_known(&t,0x35,-1));
 assert(!dbi_extra_write_known(&t,0x38,-1));
 p.config.control36_37_write_latches=false;
 assert(!dbi_extra_write_known(&t,0x36,8));
 return 0;
}
''')
    exe=tmp_path/'control'
    subprocess.run(['cc','-I'+str(ROOT/'qemu/hw/arm'),str(source),'-o',str(exe),
                    *component_inputs('i2c','fm-radio')],check=True)
    subprocess.run([str(exe)],check=True)
