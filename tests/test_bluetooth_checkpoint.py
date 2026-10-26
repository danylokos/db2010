"""Preserve an in-flight UART command and pending/partially drained failure RX."""
import copy
import json
import pytest
from test_native_checkpoint import engine, stopped, migration, comparable, command
from support.startup_status_fixtures import fixture


@pytest.mark.parametrize('peer,packet,rx', [('command-error-03', '01030c00', 7),
    ('setup-ack-errors', '010cfd0105', 7), ('init-ack-errors', '01091000', 13),
    ('init-ack-errors', '01130cf85738303069'+'00'*243, 7),
    ('zero-capacity-errors', '01051000', 14),
    ('init-ack-errors', '01240c03140242', 7), ('init-ack-errors', '01240c03040200', 7)])
@pytest.mark.parametrize('prefix', [9, 18, 42])
def test_bluetooth_transport_checkpoint(tmp_path, prefix, peer, packet, rx):
    if len(packet) > 98 and prefix == 42:
        prefix = 600  # Preserve a long command beyond the previous49-byte bound.
    base = 0xfc030000
    steps = [('w', base+0x12, 0x41, 2)]
    steps += [('w', base+2, b, 2) for b in bytes.fromhex(packet)]
    steps += [('r', base, 0, 2) for _ in range(rx)]
    steps += [('w', base+0x14, 0x40, 2)]
    state = fixture(steps, 0)
    state.pop('startup_status')
    code_size = len(state['regions'][0]['seeds'][0]['data'])
    region_size = max(0x1000, (code_size+0xfff)&~0xfff)
    state['regions'][0]['size'] = region_size
    state['executable_ranges'][0]['end'] = 0x1000+region_size
    state['uart_transmit'] = {'profile': 'polled-tx-immediate-drain', 'free_slots': 1}
    state['bluetooth_transport'] = {'profile': 'uart3-h4-init-failure', 'peer': peer}
    state['timer_irq'] = {'profile': 'tick1-upcounter-irq-assumption', 'frequency_hz': 32768, 'max_irqs': 100}
    state['device_registers'] = [{'address': a, 'width': 4, 'initial_value': 0,
        'read_or_mask': 0, 'name': 'mask'} for a in (0xfb020104, 0xfb020204, 0xfb020118, 0xfb020218)]
    state['record_diagnostics'] = False
    with engine(state, tmp_path/'whole') as vm:
        whole = stopped(vm)
    first = copy.deepcopy(state)
    first['instruction_limit'] = prefix
    checkpoint = tmp_path/'transport.vmstate'
    with engine(first, tmp_path/'prefix') as vm:
        start = stopped(vm)
        layout = json.loads(command(vm, 'qom-get', {'path': '/machine', 'property': 'checkpoint-layout'}))
        migration(vm, 'migrate', checkpoint)
    continuation = copy.deepcopy(state)
    continuation['instruction_limit'] -= prefix
    continuation['checkpoint_tcm'] = layout
    with engine(continuation, tmp_path/'restored', incoming=True) as vm:
        migration(vm, 'migrate-incoming', checkpoint)
        final = stopped(vm)
    assert comparable(final) == comparable(whole)
    assert final['registers'][2] == (3 if peer == 'command-error-03' or packet == '01240c03040200' else 0)
    assert final['bluetooth_transport']['rx_bytes'] == rx
    assert final['bluetooth_transport']['status'] == 0
    assert final['bluetooth_transport']['commands'] == 1
    written = min(prefix//3-1, len(bytes.fromhex(packet)))
    assert start['bluetooth_transport']['tx_position'] == (written if written < len(bytes.fromhex(packet)) else 0)
