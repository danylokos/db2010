"""Ported dummy-card APDUs and native SIMIF reset/IRQ/guard contracts."""
import subprocess

from support.component_test import ROOT, component_inputs


def test_card_profile_protocol_and_controller_irq_guards(tmp_path):
    source = tmp_path/'sim.c'
    source.write_text(r'''
#include "qemu/osdep.h"
#include "hw/core/clock.h"
#include "db2010-simif.h"
#include "db2010-timer.h"
#include <assert.h>
static void drain(DB2010SimCard *c) { while(c->output_len)dbsim_card_pop(c); }
static void command(DB2010SimCard *c,const uint8_t *a,unsigned n) {
 for(unsigned i=0;i<n;i++) {
  assert(!dbsim_card_input(c,a[i]));
  if(i==4 && n>5)assert(dbsim_card_pop(c)==a[1]);
 }
}
static void select_file(DB2010SimCard *c,unsigned id,unsigned length) {
 uint8_t a[]={0xa0,0xa4,0,0,2,id>>8,id};command(c,a,7);
 assert(dbsim_card_pop(c)==0x9f&&dbsim_card_pop(c)==length);
}
static void rx(void *p,bool level) { dbtimer_set_line(p,DBIRQ_SIM_RX,level); }
static void tx(void *p,bool level) { dbtimer_set_line(p,DBIRQ_SIM_TX,level); }
static const char *sim_access(DB2010Simif *s,unsigned off,bool write,unsigned value,uint64_t now) {
 DB2010Access a={.address=0xf9060000+off,.size=1,.write=write,.value=value,.now=now,.commit=true};
 return dbsim_access(s,&a);
}
int main(void) {
 DB2010SimCard c;dbsim_card_init(&c);dbsim_card_activate(&c);
 const uint8_t atr[]={0x3b,0x90,0x11,0};for(unsigned i=0;i<4;i++)assert(dbsim_card_pop(&c)==atr[i]);
 select_file(&c,0x3f00,22);
 const uint8_t get[]={0xa0,0xc0,0,0,22};command(&c,get,5);
 assert(dbsim_card_pop(&c)==0xc0);uint8_t response[22];
 for(unsigned i=0;i<22;i++)response[i]=dbsim_card_pop(&c);
 assert(response[4]==0x3f&&response[6]==1&&response[12]==9&&(response[13]&128));
 assert(response[18]==0x83&&response[21]==0x8a);
 assert(dbsim_card_pop(&c)==0x90&&dbsim_card_pop(&c)==0);
 select_file(&c,0x2fe2,15);
 const uint8_t read_iccid[]={0xa0,0xb0,0,0,10};command(&c,read_iccid,5);
 assert(dbsim_card_pop(&c)==0xb0&&dbsim_card_pop(&c)==0x98);drain(&c);
 select_file(&c,0x7f20,22);select_file(&c,0x6f07,15);
 const uint8_t read_imsi[]={0xa0,0xb0,0,0,9};command(&c,read_imsi,5);
 assert(dbsim_card_pop(&c)==0xb0&&dbsim_card_pop(&c)==8&&dbsim_card_pop(&c)==0x10);drain(&c);
 const uint8_t bad_read[]={0xa0,0xb0,0,8,2};command(&c,bad_read,5);assert(dbsim_card_pop(&c)==0xb0);
 assert(dbsim_card_pop(&c)==0x94&&dbsim_card_pop(&c)==2);
 const uint8_t update[]={0xa0,0xd6,0,8,1,0xa5};command(&c,update,6);drain(&c);
 const uint8_t one[]={0xa0,0xb0,0,8,1};command(&c,one,5);
 assert(dbsim_card_pop(&c)==0xb0&&dbsim_card_pop(&c)==0xa5);drain(&c);
 const uint8_t absent[]={0xa0,0xa4,0,0,2,0x6f,0xff};command(&c,absent,7);
 assert(dbsim_card_pop(&c)==0x94&&dbsim_card_pop(&c)==4);
 assert(!dbsim_card_input(&c,0xff)&&!dbsim_card_input(&c,0)&&!dbsim_card_input(&c,0xff));
 assert(dbsim_card_pop(&c)==0xff&&dbsim_card_pop(&c)==0&&dbsim_card_pop(&c)==0xff);
 const uint8_t iso_probe[]={0,0xa4,0,0,2};command(&c,iso_probe,5);
 assert(dbsim_card_pop(&c)==0x6e&&dbsim_card_pop(&c)==0);
 Clock clock={.period=CLOCK_PERIOD_FROM_HZ(32768)};
 DB2010Timer timer={.clock=&clock,.config={.simif_route=true,.max_irqs=100}};dbtimer_reset(&timer);
 dbtimer_observe(&timer,0xfb020104,~0u);dbtimer_observe(&timer,0xfb020204,~0u);
 DB2010Simif s={.byte_ns=100,.rx_irq={&timer,rx},.tx_irq={&timer,tx}};dbsim_reset(&s);
 assert(sim_access(&s,2,false,0,0)&&sim_access(&s,12,false,0,0));
 assert(!sim_access(&s,4,true,0x84,0)&&!sim_access(&s,4,true,0x85,0));
 assert(s.activations==1&&dbsim_deadline(&s)==100&&!s.rx_ready);
 dbsim_advance(&s,99);assert(!s.rx_ready);dbsim_advance(&s,100);
 assert(s.rx_ready&&timer.state.sim_rx_pending&&!dbirq_any_routed(&timer));
 dbtimer_observe(&timer,0xfb020104,~(1u<<5));assert(dbirq_any_routed(&timer));
 assert(!dbtimer_select(&timer)&&timer.state.sim_rx_selected);
 uint32_t value=0;assert(!dbtimer_access(&timer,0xfb020110,4,false,&value)&&value==5);
 assert(dbtimer_observe(&timer,0xfb020218,1)); /* RX cannot ack secondary */
 DB2010Access read={.address=0xf9060000,.size=1,.now=100};
 DB2010Simif before=s;assert(!dbsim_access(&s,&read)&&read.value==0x3b&&!memcmp(&before,&s,sizeof(s)));
 read.commit=true;assert(!dbsim_access(&s,&read)&&!s.rx_ready&&!timer.state.sim_rx_pending);
 assert(!dbtimer_observe(&timer,0xfb020118,1));
 for(unsigned i=0;i<3;i++) { dbsim_advance(&s,200+100*i);read.now=200+100*i;assert(!dbsim_access(&s,&read)); }
 before=s;assert(sim_access(&s,0,true,0x80,600)&&!memcmp(&before,&s,sizeof(s)));
 assert(sim_access(&s,10,false,0,600));assert(sim_access(&s,8,true,0,600));
 assert(!sim_access(&s,0,true,0xa0,600));assert(sim_access(&s,0,true,0xa4,601)); /* pending TX */
 dbsim_advance(&s,700);assert(timer.state.sim_tx_pending);
 dbtimer_observe(&timer,0xfb020104,~(1u<<28));dbtimer_observe(&timer,0xfb020204,~(1u<<6));
 assert(!dbtimer_select(&timer)&&timer.state.sim_tx_selected&&!timer.state.sim_tx_pending);
 value=0;assert(!dbtimer_access(&timer,0xfb020110,4,false,&value)&&value==28);
 assert(!dbtimer_access(&timer,0xfb020210,4,false,&value)&&value==6);
 assert(dbtimer_observe(&timer,0xfb020118,1));assert(!dbtimer_observe(&timer,0xfb020218,1));
 assert(!dbtimer_observe(&timer,0xfb020118,1));assert(!dbirq_any_routed(&timer));
 /* A completed TX must not fire a second edge when its clock is resumed. */
 assert(!sim_access(&s,4,true,0x81,701)&&!sim_access(&s,4,true,0x01,702));
 assert(!sim_access(&s,4,true,0x81,703)&&!sim_access(&s,4,true,0x85,704));
 assert(!timer.state.sim_tx_pending&&!dbirq_any_routed(&timer));
 for(unsigned i=0;i<600;i++)assert(!sim_access(&s,8,false,0,800+i));
 assert(s.events>600); /* history capacity cannot stop hardware */
 DB2010SimEvent saved[256];memcpy(saved,s.history,sizeof(saved));
 uint64_t events=s.events;s.omit_diagnostics=true;
 for(unsigned i=0;i<600;i++)assert(!sim_access(&s,8,false,0,1400+i));
 assert(s.events==events+600&&!memcmp(saved,s.history,sizeof(saved)));
 assert(!sim_access(&s,4,true,0x84,1500)&&!s.active&&dbsim_deadline(&s)<0);
 assert(!sim_access(&s,12,true,0x102,1501)&&s.regs[6]==2); /* STRB truncation */
 before=s;DB2010Access bad={.address=0xf9060008,.size=2,.commit=true};
 assert(dbsim_access(&s,&bad)&&!memcmp(&before,&s,sizeof(s)));
 dbsim_reset(&s);assert(s.omit_diagnostics&&!s.events&&!s.activations&&!s.card.apdus);
 return 0;
}
''')
    binary = tmp_path/'sim'
    subprocess.run(['cc', '-I'+str(ROOT/'qemu/hw/arm'), str(source), '-o', str(binary),
                    *component_inputs('sim-card', 'simif', 'timer')], check=True)
    subprocess.run([str(binary)], check=True)
