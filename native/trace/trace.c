/* Minimal out-of-tree ARM snapshot producer; host owns storage and analysis.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include <glib.h>
#include <qemu-plugin.h>
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define RECORD_SIZE 92
#define MEMORY_SIZE 52
#define BATCH_ROWS 8192
QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

typedef struct {
    uint32_t pc, size;
    uint8_t bytes[4];
} Instruction;

static int connection = -1;
static uint8_t buffer[RECORD_SIZE * BATCH_ROWS];
static size_t pending;
static uint64_t observations, write_ns, write_calls;
static bool synthetic_kgen, synthetic_kgen_final, synthetic_etx, synthetic_sha;
static uint64_t synthetic_etx_observations;
static uint64_t synthetic_api_observations;
static char *metrics;
static int memory_connection = -1;
static uint8_t memory_buffer[MEMORY_SIZE * BATCH_ROWS];
static size_t memory_pending;
static uint64_t memory_events;
static char *memory_metrics;
static struct qemu_plugin_register *registers[17];
static GByteArray *register_buffer;
static GPtrArray *translations;

static void put32(uint8_t *destination, uint32_t value)
{
    uint32_t little = GUINT32_TO_LE(value);
    memcpy(destination, &little, 4);
}

static uint64_t now_ns(void)
{
    struct timespec value;
    clock_gettime(CLOCK_MONOTONIC, &value);
    return (uint64_t)value.tv_sec * 1000000000 + value.tv_nsec;
}

static void send_to(int socket_fd, const void *data, size_t length)
{
    const uint8_t *bytes = data;
    uint64_t start = now_ns();
    while (length) {
        ssize_t count = send(socket_fd, bytes, length, MSG_NOSIGNAL);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            fprintf(stderr, "db2010 trace stream failed: %s\n", strerror(errno));
            exit(EXIT_FAILURE);
        }
        bytes += count;
        length -= count;
        write_calls++;
    }
    write_ns += now_ns() - start;
}

static void send_bytes(const void *data, size_t length)
{
    send_to(connection, data, length);
}

static void put64(uint8_t *destination, uint64_t value)
{
    uint64_t little = GUINT64_TO_LE(value);
    memcpy(destination, &little, 8);
}

static void memory_access(unsigned int index, qemu_plugin_meminfo_t info,
                          uint64_t address, void *userdata)
{
    Instruction *insn = userdata;
    struct qemu_plugin_hwaddr *hardware = qemu_plugin_get_hwaddr(info, address);
    qemu_plugin_mem_value value = qemu_plugin_mem_get_value(info);
    uint64_t data;
    switch (value.type) {
    case QEMU_PLUGIN_MEM_VALUE_U8: data = value.data.u8; break;
    case QEMU_PLUGIN_MEM_VALUE_U16: data = value.data.u16; break;
    case QEMU_PLUGIN_MEM_VALUE_U32: data = value.data.u32; break;
    case QEMU_PLUGIN_MEM_VALUE_U64: data = value.data.u64; break;
    default:
        fprintf(stderr, "db2010 memory stream: unsupported value size\n");
        exit(EXIT_FAILURE);
    }
    g_assert(hardware && index == 0);
    uint8_t *row = memory_buffer + memory_pending;
    put64(row, ++memory_events);
    put64(row + 8, observations);
    put64(row + 16, address);
    put64(row + 24, qemu_plugin_hwaddr_phys_addr(hardware));
    put64(row + 32, data);
    put32(row + 40, insn->pc);
    put32(row + 44, 1u << qemu_plugin_mem_size_shift(info));
    put32(row + 48, qemu_plugin_mem_is_store(info) |
        (qemu_plugin_hwaddr_is_io(hardware) << 1) |
        (qemu_plugin_mem_is_big_endian(info) << 2));
    memory_pending += MEMORY_SIZE;
    if (memory_pending == sizeof(memory_buffer)) {
        send_to(memory_connection, memory_buffer, memory_pending);
        memory_pending = 0;
    }
}

static void flush(void)
{
    if (pending) {
        send_bytes(buffer, pending);
        pending = 0;
    }
}

static void vcpu_init(unsigned int index, void *userdata)
{
    GArray *descriptors = qemu_plugin_get_registers();
    bool found[17] = {0};
    static const char *names[] = {"r0", "r1", "r2", "r3", "r4", "r5", "r6",
        "r7", "r8", "r9", "r10", "r11", "r12", "sp", "lr", "pc", "cpsr"};
    g_assert(index == 0);
    for (unsigned i = 0; i < descriptors->len; i++) {
        qemu_plugin_reg_descriptor *descriptor = &g_array_index(descriptors, qemu_plugin_reg_descriptor, i);
        for (unsigned n = 0; n < 17; n++) {
            if (!strcmp(descriptor->name, names[n])) {
                registers[n] = descriptor->handle;
                found[n] = true;
            }
        }
    }
    g_array_free(descriptors, true);
    for (unsigned n = 0; n < 17; n++) {
        if (!found[n]) {
            fprintf(stderr, "db2010 trace: missing ARM register %s\n", names[n]);
            exit(EXIT_FAILURE);
        }
    }
    register_buffer = g_byte_array_sized_new(4);
}

static void instruction(unsigned int index, void *userdata)
{
    Instruction *insn = userdata;
    /* These are declared synthetic API entries, not original ROM instructions.
     * The board returns from its guard before the decoded placeholder executes.
     * Keep attempts separate from instruction records, including rejected calls. */
    if (synthetic_kgen && (insn->pc == 0xffff12a2 || insn->pc == 0xffff12be ||
                           (synthetic_kgen_final && insn->pc == 0xffff12c2))) {
        synthetic_api_observations++;
        return;
    }
    if (synthetic_sha && (insn->pc == 0xffff0f48 || insn->pc == 0xffff112c || insn->pc == 0xffff129e)) {
        synthetic_api_observations++;
        return;
    }
    if (synthetic_etx && insn->pc == 0xffff0ddc) {
        synthetic_api_observations++; synthetic_etx_observations++;
        return;
    }
    uint8_t *row = buffer + pending;
    uint64_t ordinal = GUINT64_TO_LE(++observations);
    uint32_t cpsr = 0;
    memcpy(row, &ordinal, 8);
    put32(row + 8, insn->pc);
    put32(row + 12, insn->size);
    memset(row + 20, 0, 4);
    memcpy(row + 20, insn->bytes, insn->size);
    for (unsigned n = 0; n < 17; n++) {
        g_byte_array_set_size(register_buffer, 0);
        if (!qemu_plugin_read_register(registers[n], register_buffer) || register_buffer->len != 4) {
            fprintf(stderr, "db2010 trace: ARM register read failed\n");
            exit(EXIT_FAILURE);
        }
        memcpy(row + 24 + 4 * n, register_buffer->data, 4);
        if (n == 16) {
            memcpy(&cpsr, register_buffer->data, 4);
            cpsr = GUINT32_FROM_LE(cpsr);
        }
    }
    /* The API supplies instruction identity; env PC may describe a TB boundary. */
    put32(row + 24 + 15 * 4, insn->pc);
    put32(row + 16, !!(cpsr & 0x20));
    pending += RECORD_SIZE;
    if (pending == sizeof(buffer)) {
        flush();
    }
}

static void translate(struct qemu_plugin_tb *tb, void *userdata)
{
    size_t count = qemu_plugin_tb_n_insns(tb);
    for (size_t i = 0; i < count; i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        Instruction *meta = g_new0(Instruction, 1);
        g_ptr_array_add(translations, meta);
        meta->pc = qemu_plugin_insn_vaddr(insn);
        meta->size = qemu_plugin_insn_size(insn);
        g_assert(meta->size == 2 || meta->size == 4);
        g_assert(qemu_plugin_insn_data(insn, meta->bytes, 4) >= meta->size);
        qemu_plugin_register_vcpu_insn_exec_cb(insn, instruction,
                                              QEMU_PLUGIN_CB_R_REGS, meta);
        if (memory_connection >= 0) {
            qemu_plugin_register_vcpu_mem_cb(insn, memory_access,
                                            QEMU_PLUGIN_CB_NO_REGS,
                                            QEMU_PLUGIN_MEM_RW, meta);
        }
    }
}

static void cache_flush(void *userdata)
{
    g_ptr_array_set_size(translations, 0);
}

static void finish(void *userdata)
{
    flush();
    close(connection);
    if (memory_connection >= 0) {
        if (memory_pending) {
            send_to(memory_connection, memory_buffer, memory_pending);
        }
        close(memory_connection);
        if (memory_metrics) {
            FILE *file = fopen(memory_metrics, "w");
            if (!file) {
                perror("db2010 memory metrics");
                exit(EXIT_FAILURE);
            }
            fprintf(file, "{\"events\":%"PRIu64",\"record_bytes\":%u,"
                    "\"batch_rows\":%u}\n", memory_events, MEMORY_SIZE, BATCH_ROWS);
            fclose(file);
        }
    }
    g_ptr_array_free(translations, true);
    if (register_buffer) {
        g_byte_array_unref(register_buffer);
    }
    if (metrics) {
        FILE *file = fopen(metrics, "w");
        if (!file) {
            perror("db2010 trace metrics");
            exit(EXIT_FAILURE);
        }
        fprintf(file, "{\"observations\":%"PRIu64",\"record_bytes\":%u,"
                "\"batch_rows\":%u,\"write_calls\":%"PRIu64","
                "\"write_wall_ns\":%"PRIu64,
                observations, RECORD_SIZE, BATCH_ROWS, write_calls, write_ns);
        if (synthetic_kgen) {
            fprintf(file, ",\"synthetic_kgen_profile\":\"%s\"",
                    synthetic_kgen_final ? "sha1-le92-init-update-final-8040" : "sha1-le92-init-update-8040");
        }
        if (synthetic_etx) {
            fprintf(file, ",\"synthetic_etx_profile\":\"gdfs-static-8040-status-success\","
                    "\"synthetic_etx_observations\":%"PRIu64, synthetic_etx_observations);
        }
        if (synthetic_kgen || synthetic_etx || synthetic_sha) {
            fprintf(file, ",\"synthetic_api_observations\":%"PRIu64, synthetic_api_observations);
        }
        fprintf(file, "}\n");
        fclose(file);
    }
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                    const qemu_info_t *info, int argc, char **argv)
{
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    const char *path = NULL;
    const char *memory_path = NULL;
    uint8_t header[16] = "DBTRC001";
    if (!info->system_emulation || strcmp(info->target_name, "arm") ||
        info->system.max_vcpus != 1) {
        fprintf(stderr, "db2010 trace supports one ARM system vCPU\n");
        return -1;
    }
    for (int i = 0; i < argc; i++) {
        if (g_str_has_prefix(argv[i], "socket=")) {
            path = argv[i] + 7;
        } else if (g_str_has_prefix(argv[i], "metrics=")) {
            metrics = g_strdup(argv[i] + 8);
        } else if (g_str_has_prefix(argv[i], "memsocket=")) {
            memory_path = argv[i] + 10;
        } else if (g_str_has_prefix(argv[i], "memmetrics=")) {
            memory_metrics = g_strdup(argv[i] + 11);
        } else if (!strcmp(argv[i], "synthetic-kgen=sha1-le92-init-update-8040")) {
            synthetic_kgen = true;
        } else if (!strcmp(argv[i], "synthetic-kgen=sha1-le92-init-update-final-8040")) {
            synthetic_kgen = synthetic_kgen_final = true;
        } else if (!strcmp(argv[i], "synthetic-sha=sha1-le92-init-update-final-8040")) {
            synthetic_sha = true;
        } else if (!strcmp(argv[i], "synthetic-etx=gdfs-static-8040-status-success")) {
            synthetic_etx = true;
        } else {
            return -1;
        }
    }
    if (!path || strlen(path) >= sizeof(address.sun_path)) {
        fprintf(stderr, "db2010 trace requires a bounded socket= path\n");
        return -1;
    }
    strcpy(address.sun_path, path);
    connection = socket(AF_UNIX, SOCK_STREAM, 0);
    if (connection < 0 || connect(connection, (void *)&address, sizeof(address)) < 0) {
        perror("db2010 trace connect");
        return -1;
    }
    put32(header + 8, 1);
    put32(header + 12, RECORD_SIZE);
    send_bytes(header, sizeof(header));
    if (memory_path) {
        if (strlen(memory_path) >= sizeof(address.sun_path)) {
            return -1;
        }
        strcpy(address.sun_path, memory_path);
        memory_connection = socket(AF_UNIX, SOCK_STREAM, 0);
        if (memory_connection < 0 ||
            connect(memory_connection, (void *)&address, sizeof(address)) < 0) {
            perror("db2010 memory connect");
            return -1;
        }
        memcpy(header, "DBMEM001", 8);
        put32(header + 12, MEMORY_SIZE);
        send_to(memory_connection, header, sizeof(header));
    }
    translations = g_ptr_array_new_with_free_func(g_free);
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init, NULL);
    qemu_plugin_register_vcpu_tb_trans_cb(id, translate, NULL);
    qemu_plugin_register_flush_cb(id, cache_flush, NULL);
    qemu_plugin_register_atexit_cb(id, finish, NULL);
    return 0;
}
