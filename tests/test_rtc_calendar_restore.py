"""RTC restoration must preserve ticking and real calendar boundaries."""
from support.component_test import component_inputs

from pathlib import Path
import shlex
import subprocess

from db2010.paths import ROOT


def test_calendar_restore_clock_and_guards(tmp_path):
    source = tmp_path / 'rtc.c'
    source.write_text(r'''
#include "qemu/osdep.h"
#include "hw/core/clock.h"
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <glib.h>
#include "db2010-rtc.h"
static const char *rtc_write(DB2010Rtc *r,unsigned off,uint32_t value,uint64_t count) {
 return dbrtc_access(r,count,0,0xf9050000+off,2,true,&value);
}
static uint32_t rtc_read(DB2010Rtc *r,unsigned off,uint64_t count) {
 uint32_t value=999;assert(!dbrtc_access(r,count,0,0xf9050000+off,2,false,&value));return value;
}
int main(void) {
 Clock clk={.period=CLOCK_PERIOD_FROM_HZ(32768)};
 DB2010Rtc r={.clock=&clk,.config.calendar_writable=true,.config.status=0xc0};
 assert(!rtc_write(&r,16,99,0));assert(!rtc_write(&r,18,19,0));
 assert(!rtc_write(&r,14,12,0));assert(!rtc_write(&r,12,31,0));
 assert(!rtc_write(&r,8,23,0));assert(!rtc_write(&r,6,59,0));assert(!rtc_write(&r,4,59,0));
 assert(rtc_read(&r,16,0)==99&&rtc_read(&r,18,0)==19);
 assert(rtc_read(&r,4,1000000000)==0&&rtc_read(&r,6,1000000000)==0);
 assert(rtc_read(&r,8,1000000000)==0&&rtc_read(&r,12,1000000000)==1);
 assert(rtc_read(&r,14,1000000000)==1&&rtc_read(&r,16,1000000000)==0&&rtc_read(&r,18,1000000000)==20);
 DB2010Rtc before=r;
 assert(!strcmp(rtc_write(&r,14,13,1000000000),"rtc_calendar_value_unmodeled"));assert(!memcmp(&r,&before,sizeof(r)));
 assert(!strcmp(rtc_write(&r,2,1,1000000000),"rtc_counter_write_unmodeled"));assert(!memcmp(&r,&before,sizeof(r)));
 assert(!strcmp(rtc_write(&r,0,0,1000000000),"rtc_counter_write_unmodeled"));assert(!memcmp(&r,&before,sizeof(r)));
 r=(DB2010Rtc){.clock=&clk,.config.calendar_writable=true};
 assert(rtc_read(&r,2,500000000)==64);assert(!rtc_write(&r,2,0,500000000));
 assert(rtc_read(&r,2,500000000)==0);assert(rtc_read(&r,2,1000000000)==64);
 r=(DB2010Rtc){.clock=&clk};assert(!strcmp(rtc_write(&r,16,0,0),"rtc_counter_write_unmodeled"));
 return 0;
}
''')
    flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'glib-2.0'], text=True))
    binary = tmp_path / 'rtc'
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-I' + str(ROOT / 'qemu/hw/arm'), str(source), '-o', str(binary), *flags, *component_inputs('rtc')], check=True)
    subprocess.run([str(binary)], check=True)
