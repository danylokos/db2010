"""Observed compact28/60 routing, masks and acknowledgement order."""
from support.component_test import component_inputs

import subprocess
from db2010.paths import ROOT


def test_display_interrupt_routing(tmp_path):
    code=tmp_path/'irq.c'
    code.write_text(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#define G_N_ELEMENTS(a) (sizeof(a)/sizeof((a)[0]))
#include "db2010-timer.h"
int main(void){
 DB2010Timer t={.config.display_route=true,.state.pdi_pending=true,.state.dma_pending=true,
  .state.masks_known={true,true,true},.state.masks={0,0,1u<<17},.config.max_irqs=10};
 assert(dbirq_pdi_routed(&t)&&!dbirq_dma_routed(&t));
 assert(!dbtimer_select(&t)&&t.state.pdi_selected&&!t.state.dma_selected);
 uint32_t value=0;
 assert(!dbtimer_access(&t,0xfb020110,4,false,&value)&&value==28);
 assert(!dbtimer_access(&t,0xfb020210,4,false,&value)&&value==13);
 assert(value+28==41); /* Original native selector offset. */
 assert(!strcmp(dbtimer_observe(&t,0xfb020118,1),"irq_primary_ack_before_secondary"));
 t.state.pdi_pending=false;
 assert(!dbtimer_observe(&t,0xfb020218,1)&&!dbtimer_observe(&t,0xfb020118,1));
 assert(!t.state.selected&&!dbirq_any_routed(&t));
 t.state.masks[2]=0;assert(dbirq_dma_routed(&t));
 assert(!dbtimer_select(&t)&&t.state.dma_selected&&!t.state.pdi_selected);
 assert(!dbtimer_access(&t,0xfb020110,4,false,&value)&&value==30);
 assert(!dbtimer_access(&t,0xfb020310,4,false,&value)&&value==17);
 assert(value+60==77); /* Compact77 maps to native vector128. */
 assert(!strcmp(dbtimer_observe(&t,0xfb020218,1),"irq_ack_wrong_bank"));
 assert(!dbtimer_observe(&t,0xfb020318,1)&&!dbtimer_observe(&t,0xfb020118,1));
 assert(dbirq_dma_routed(&t)); /* Level remains until peripheral clears TC. */
 t.state.dma_pending=false;t.state.pending=true;t.state.pdi_pending=true;
 assert(!dbtimer_select(&t)&&!t.state.pdi_selected&&!t.state.dma_selected&&!t.state.i2c_selected);
 assert(!dbtimer_access(&t,0xfb020210,4,false,&value)&&value==25);
 return 0;
}
''')
    exe=tmp_path/'irq'
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-I'+str(ROOT/'qemu/hw/arm'),str(code),'-o',str(exe), *component_inputs('timer')],check=True)
    subprocess.run([str(exe)],check=True)
