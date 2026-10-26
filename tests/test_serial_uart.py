import copy
import json
import socket
import subprocess
import time

from support.component_test import ROOT, component_inputs
from support.fixtures import base_state, words
from test_native_checkpoint import engine, stopped, command, migration, comparable


def test_fifo_status_preview_bounds_and_guards(tmp_path):
    source = tmp_path/'serial.c'
    source.write_text(r'''
#include "qemu/osdep.h"
#include "db2010-components.h"
#include <assert.h>
void qemu_chr_fe_accept_input(CharFrontend *c) {}
int qemu_chr_fe_write_all(CharFrontend *c, const uint8_t *p, int n) { return n; }
static const char *uart_access(DB2010Uart *s, uint32_t offset, bool write, uint32_t *value, bool commit) {
 DB2010Access a={.address=0xfb060000+offset,.size=2,.write=write,.value=*value,.commit=commit};
 const char *fault=db2010_uart_access(s,&a); *value=a.value; return fault;
}
int main(void) {
 DB2010Uart s={.data_enabled=true}; db2010_uart_reset(&s); uint32_t v=0;
 assert(!strcmp(uart_access(&s,0xa,false,&v,true),"data_uart_mode_unknown"));
 assert(!strcmp(uart_access(&s,0,false,&v,true),"data_uart_rx_empty"));
 v=0x102; assert(!uart_access(&s,0xa,true,&v,true));
 v=0x61; assert(!uart_access(&s,0x12,true,&v,true));
 db2010_data_uart_receive(&s,(const uint8_t *)"AT\r",3);
 assert(s.data_autobauds==1 && s.data_received==3);
 v=0;assert(!uart_access(&s,0x14,false,&v,true) && v==0x61);
 assert(!uart_access(&s,0,false,&v,false) && v=='A' && s.data_count==3);
 const char *expected="AT\r";
 for(unsigned i=0;i<3;i++) {assert(!uart_access(&s,0,false,&v,true) && v==expected[i]);}
 assert(s.data_rx_bytes==3);
 assert(!uart_access(&s,0x14,false,&v,true) && v==0x60);
 v=0x60;assert(!uart_access(&s,0x14,true,&v,true));
 assert(!uart_access(&s,0x14,false,&v,true) && v==0);
 DB2010Uart before=s;
 v=0x200;assert(!strcmp(uart_access(&s,0x14,true,&v,true),"data_uart_ack_unmodeled"));
 assert(!memcmp(&before,&s,sizeof(s)));
 v=0x2000;assert(!strcmp(uart_access(&s,0x12,true,&v,true),"data_uart_mask_unmodeled"));
 assert(!memcmp(&before,&s,sizeof(s)));
 v=3;assert(!strcmp(uart_access(&s,0xa,true,&v,true),"data_uart_mode_unmodeled"));
 assert(!memcmp(&before,&s,sizeof(s)));
 uint8_t bulk[256]; for(unsigned i=0;i<256;i++)bulk[i]=i;
 db2010_data_uart_receive(&s,bulk,256);assert(s.data_count==256);
 for(unsigned i=0;i<128;i++)assert(!uart_access(&s,0,false,&v,true)&&v==i);
 db2010_data_uart_receive(&s,bulk,128);assert(s.data_count==256);
 for(unsigned i=128;i<256;i++)assert(!uart_access(&s,0,false,&v,true)&&v==i);
 for(unsigned i=0;i<128;i++)assert(!uart_access(&s,0,false,&v,true)&&v==i);
 v=0x180;assert(!uart_access(&s,0x12,true,&v,true));
 db2010_data_uart_cable(&s,false);assert(!s.data_present && s.data_cable_transitions==1);
 v=65;assert(!uart_access(&s,2,true,&v,true) && s.uart_bytes[0]==0);
 assert(!uart_access(&s,0x14,false,&v,true) && v==0x100);
 v=0x100;assert(!uart_access(&s,0x14,true,&v,true));
 db2010_data_uart_cable(&s,true);assert(s.data_present && s.data_cable_transitions==2);
 v=65;assert(!uart_access(&s,2,true,&v,true) && s.uart_bytes[0]==1);
 assert(!uart_access(&s,0x14,false,&v,true) && v==0x80);
 v=0x80;assert(!uart_access(&s,0x14,true,&v,true));
 assert(!s.data_count);
 return 0;
}
''')
    executable = tmp_path/'serial'
    subprocess.run(['cc', '-I'+str(ROOT/'qemu/hw/arm'), str(source), '-o', str(executable),
                    *component_inputs('simple')], check=True)
    subprocess.run([str(executable)], check=True)


def serial_irq_state(limit):
    state = base_state(words(0xe1c010ba, 0xe3a01061, 0xe1c011b2, 0xe121f00c,
                             0xe3570001, 0x0a000001, 0xee070f90, 0xeafffffb, 0xeafffffe), limit=limit)
    state['registers'][0] = 0xfb060000
    state['registers'][1] = 0x102
    state['registers'][6] = 0xfb020218
    state['registers'][12] = 0x53
    state['uart_transmit'] = dict(profile='polled-tx-immediate-drain', free_slots=1)
    state['serial_lines'] = dict(profile='port1-rx-levels',uart0_rx_level=1,uart1_rx_level=1)
    state['serial_transport'] = dict(profile='uart0-9600-fifo-assumption')
    state['timer_irq'] = dict(profile='tick1-paired-upcounter-irq-assumption', frequency_hz=32768, max_irqs=10)
    state['device_registers'] = [dict(address=a,width=4,initial_value=0,read_or_mask=0,name='fixture.irq-mask')
                                for a in (0xfb020104,0xfb020204,0xfb020118,0xfb020218)]
    state['device_registers'] += [dict(address=a,width=2,initial_value=0,read_or_mask=0,name='fixture.gpio')
                                  for a in (0xf900000a,0xf900000c)]
    handler = words(0xe5168008,0xe1d0a1b4,0xe1d020b0,0xe1d030b0,0xe1d040b0,
                    0xe3a05060,0xe1c051b4,0xe3a05001,0xe5865000,0xe5065100,
                    0xe2877001,0xe25ef004)
    state['regions'].append(dict(name='fixture.irq',address=0,size=4096,readonly=True,
                                seeds=[dict(address=0x18,data=handler)]))
    state['executable_ranges'].append(dict(start=0x18,end=0x18+len(handler)))
    return state


def assert_serial_irq(final):
    assert final['stop']['reason'] == 'instruction_limit'
    assert final['registers'][2:5] == list(b'AT\r')
    assert final['registers'][7:9] == [1,16]
    assert final['registers'][10] == 0x61
    assert final['cpsr'] & 0x80 == 0
    assert final['timer_irq']['data_uart_irqs'] == 1
    assert not final['timer_irq']['data_uart_pending'] and not final['timer_irq']['selected']
    assert final['serial_transport']['rx_count'] == final['serial_transport']['status'] == 0


def test_real_host_rx_checkpoint_native_irq_and_ack(tmp_path):
    state = serial_irq_state(3)
    checkpoint = tmp_path/'serial.vmstate'
    host, guest = socket.socketpair()
    with host, guest:
        with engine(state,tmp_path/'prefix',uart0=f'socket,id=uart0,fd={guest.fileno()}',
                    pass_fds=(guest.fileno(),)) as vm:
            stopped(vm)
            host.sendall(b'AT\r')
            deadline = time.monotonic()+3
            while True:
                before = vm.snapshot(time.monotonic()+3)
                if before['serial_transport']['received'] == 3: break
                assert time.monotonic() < deadline
                time.sleep(.01)
            assert before['timer_irq']['data_uart_pending']
            assert before['serial_transport']['status'] == 0x61
            vm.execute('qom-set',time.monotonic()+3,dict(path='/machine/uart',property='cable-present',value=False))
            before=vm.snapshot(time.monotonic()+3)
            assert not before['serial_transport']['cable_present']
            assert before['serial_transport']['presence_events']==0x100
            assert before['serial_transport']['cable_transitions']==1
            migration(vm,'migrate',checkpoint)
    continuation = copy.deepcopy(state)
    continuation['instruction_limit'] = 40
    with engine(continuation,tmp_path/'restored',incoming=True) as vm:
        migration(vm,'migrate-incoming',checkpoint)
        restored = vm.snapshot(time.monotonic()+3)
        before.pop('stop')
        assert comparable(restored) == comparable(before)
        final = stopped(vm)
    assert_serial_irq(final)


def test_host_input_wakes_native_idle_without_an_internal_timer(tmp_path):
    state = serial_irq_state(43)
    host, guest = socket.socketpair()
    with host, guest:
        with engine(state,tmp_path/'idle',uart0=f'socket,id=uart0,fd={guest.fileno()}',
                    pass_fds=(guest.fileno(),)) as vm:
            command(vm,'cont')
            deadline = time.monotonic()+3
            while True:
                idle = json.loads(command(vm,'qom-get',{'path':'/machine','property':'progress'}))
                assert 'stop' not in idle
                if idle['wfi_waits']: break
                assert time.monotonic() < deadline
                time.sleep(.01)
            host.sendall(b'AT\r')
            vm.wait_stop(time.monotonic()+3)
            final = vm.snapshot(time.monotonic()+3)
    assert_serial_irq(final)
    assert final['timer_irq']['deadline_ns'] == -1
