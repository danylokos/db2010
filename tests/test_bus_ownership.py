"""Ownership is distinct from valid test_access; collisions fail before registration."""
import subprocess
from support.component_test import ROOT, component_inputs


def test_page_registry(tmp_path):
    source = tmp_path/'registry.c'
    source.write_text(r'''
#include "qemu/osdep.h"
#include <assert.h>
#include "db2010-bus.h"
static const char *test_access(void *opaque, DB2010Access *a) { return NULL; }
int main(void) {
 DB2010Bus b;db2010_bus_init(&b);
 DB2010RegisterSpec primary[]={{0x1fff,2,1u<<2,1,true,true}};
 DB2010RegisterSpec adjacent[]={{0x2001,2,1u<<1,1,true,true}};
 DB2010RegisterSpec conflict[]={{0x5000,4,0,0,true,true},{0x2000,1,0,0,true,true}};
 assert(!db2010_bus_add(&b,"primary",primary,1,test_access,NULL,1));
 assert(!db2010_bus_add(&b,"adjacent",adjacent,1,test_access,NULL,2));
 assert(!strcmp(db2010_bus_owner(&b,0x1fff,1)->name,"primary"));
 assert(!db2010_register_spec(primary,1,0x1fff,1)); /* owned, wrong width */
 assert(db2010_register_spec(primary,1,0x1fff,2));
 assert(!strcmp(db2010_bus_owner(&b,0x1ffe,4)->name,"primary"));
 assert(!strcmp(db2010_bus_owner(&b,0x2000,4)->name,"primary"));
 assert(!db2010_bus_owner(&b,0x2003,1));assert(!db2010_bus_owner(&b,0x1fff,0));
 assert(db2010_bus_add(&b,"conflict",conflict,2,test_access,NULL,0));
 assert(!db2010_bus_owner(&b,0x5000,4)); /* no partial registration */
 DB2010RegisterSpec overflow={0xffffffff,2,0,0,true,true};
 assert(db2010_bus_add(&b,"overflow",&overflow,1,test_access,NULL,0));
 db2010_bus_destroy(&b);return 0;
}
''')
    exe = tmp_path/'registry'
    subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'qemu/hw/arm'),
                    str(source), '-o', str(exe), *component_inputs('bus')], check=True)
    subprocess.run([str(exe)], check=True)
