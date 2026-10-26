/* Component tests have no character output frontend. A real frontend request
 * here is an error; native QEMU integration tests exercise connected outputs. */
#include "qemu/osdep.h"
#include "chardev/char-fe.h"
void qemu_chr_fe_disconnect(CharFrontend *chr)
{
    g_assert_not_reached();
}
