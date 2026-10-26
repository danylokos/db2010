"""Exercise the exact QEMU PDI owner: framing, busy lifecycle and fail-before-effect."""
from support.component_test import component_inputs

import subprocess
from pathlib import Path

from db2010.paths import ROOT


def test_pdi_owner(tmp_path):
    code = tmp_path / 'pdi.c'
    code.write_text(r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include "db2010-pdi.h"
static const uint8_t program[]={0x1f,0x1d,9,0,0x1f,0x20,0x90,0x99,0x1f,0x28,0x9c,0x99,0x17,0x98,12,0,0x17,5,0x15,5,0x17,0xf8,0x1f,0,0x1f,8};
static const char *access(DB2010Pdi *p,unsigned off,bool w,uint32_t v,uint64_t t) {
 return dbpdi_access(p,0xf7000100+off,4,t,w,&v);
}
int main(void) {
 DB2010Pdi p={0}; uint32_t v=99;
 assert(!dbpdi_access(&p,0xf70001f4,4,0,false,&v)&&v==0);
 for(unsigned i=0;i<sizeof(program);i++)p.state.program[i/4]|=(uint32_t)program[i]<<(8*(i%4));
 assert(!access(&p,0xf8,true,0x200,0));
 assert(!access(&p,0x7c,true,0x1f101300,0));
 assert(!access(&p,0x78,true,0x00001200,0));
 assert(!access(&p,0x74,true,0x04000700,0));
 assert(!access(&p,0x8c,true,0xccc00000,0));
 DB2010Pdi before=p;
 DB2010Pdi after=before;after.state.config=0x208;DB2010Pdi guarded=after;
 assert(!strcmp(access(&after,0xdc,true,1,100),"pdi_transfer_config_unmodeled"));
 assert(!memcmp(&after,&guarded,sizeof(after)));
 after.state.stop_commands=1;assert(!access(&after,0xdc,true,1,100));
 assert(after.state.config==0x208&&after.state.transfers==1&&after.state.busy_until==2148);
 DB2010Pdi stop={0};stop.state.config=0x180208;stop.state.dma_complete=true;
 assert(!access(&stop,0xdc,true,5,100));
 assert(stop.state.stop_commands==1&&!stop.state.dma_complete&&stop.state.transfers==0);
 guarded=stop;assert(!strcmp(access(&stop,0xdc,true,5,100),"pdi_dma_selector_unmodeled"));
 assert(!memcmp(&stop,&guarded,sizeof(stop)));
 assert(!access(&p,0xdc,true,1,100));
 uint8_t expected[]={0,7,0,4,0,0x12,0,0,0,0x13,0x10,0x1f};
 uint8_t rs[]={0,0,1,1,0,0,1,1,0,0,1,1};
 assert(p.state.packet_size==12&&!memcmp(p.state.packet,expected,12)&&!memcmp(p.state.rs,rs,12));
 assert(p.state.transfers==1&&p.state.busy_until==2148);
 assert(!dbpdi_access(&p,0xf70001f4,4,2147,false,&v)&&v==1);
 assert(!strcmp(access(&p,0xdc,true,1,2147),"pdi_trigger_while_busy"));
 assert(!dbpdi_access(&p,0xf70001f4,4,2148,false,&v)&&v==0);
 assert(!strcmp(access(&p,0xdc,true,1,2148),"pdi_transfer_data_unknown"));
 assert(p.state.transfers==1);
 p=before;p.state.program[0]^=1;before=p;
 assert(!strcmp(access(&p,0xdc,true,1,100),"pdi_program_execution_unmodeled"));
 assert(!memcmp(&p,&before,sizeof(p)));
 p=before;p.state.program[0]^=1;p.state.program[3]=(p.state.program[3]&0xffff)|(25u<<16);before=p;
 assert(!strcmp(access(&p,0xdc,true,1,100),"pdi_transfer_count_unmodeled"));
 assert(!memcmp(&p,&before,sizeof(p)));
 p=before;p.state.program[3]=(p.state.program[3]&0xffff)|(6u<<16);
 p.state.data[0]=0x34122200;p.state.data[1]=0xcdab0000;p.state.data_known=3;p.state.data_dirty=3;p.state.mask_known=true;p.state.mask_dirty=true;p.state.mask=0x30000000;
 assert(!access(&p,0xdc,true,1,200));
 uint8_t shortpacket[]={0xab,0xcd,0,0x22,0x12,0x34};
 assert(!memcmp(p.state.packet,shortpacket,6));
 assert(!strcmp(access(&p,0xdc,true,3,250),"pdi_trigger_unmodeled"));
 assert(!strcmp(access(&p,0xf4,true,0,250),"pdi_status_write_unmodeled"));
 assert(!access(&p,0x7c,false,0,250));
 DB2010Pdi empty={0};assert(!strcmp(access(&empty,0x7c,false,0,250),"pdi_data_state_unknown"));
 assert(!strcmp(access(&p,0x80,true,0,250),"pdi_register_unmodeled"));
 assert(!strcmp(dbpdi_access(&p,0xf70001dc,2,250,true,&v),"pdi_access_width_unmodeled"));
 return 0;
}
''')
    exe = tmp_path / 'pdi'
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'qemu/hw/arm'), str(code), '-o', str(exe), *component_inputs('pdi')], check=True)
    subprocess.run([str(exe)], check=True)
