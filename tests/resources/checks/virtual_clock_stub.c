/* Deterministic component-test clock; native integration uses real QEMU. */
#include "qemu/osdep.h"
#include "qemu/timer.h"
uint64_t db2010_test_virtual_ns;
int64_t qemu_clock_get_ns(QEMUClockType type)
{
    g_assert(type == QEMU_CLOCK_VIRTUAL);
    return db2010_test_virtual_ns;
}
