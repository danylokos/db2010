"""QOM instance variants, connected peers, owned MMIO and cold reset."""
from support.fixtures import qemu_run
from support.startup_status_fixtures import fixture
from support.dsp_peer_fixtures import fixture as dsp_fixture
from support.fm_radio_fixtures import configured


def test_f500_qom_instances_and_reset(tmp_path):
    state = fixture([('w', 0xf50005d6, 0x19, 1), ('w', 0xf50005ce, 15, 1),
                     ('w', 0xf50005d8, 0x25a, 2), ('w', 0xf50005da, 0x689, 2),
                     ('r', 0xf50005e6, 0, 1), ('r', 0xf50005e8, 0, 1)], 0)
    state.pop('startup_status')
    state['f500_command_port'] = {'profile': 'word-ready-strobe-latency', 'latency_ns': 8}
    state['f500_aux_port'] = {'profile': 'lowtag-ready-strobe-latency', 'latency_ns': 2}
    properties = ('realized', 'active', 'variant', 'latency-ns')
    result = qemu_run(state, tmp_path/'ports', reset=True, inspect='info qom-tree /machine',
        qom_inspect={'/machine/f500-primary': properties, '/machine/f500-auxiliary': properties})
    assert result['stop']['reason'] == 'instruction_limit'
    assert {k:v for k,v in result['after_reset'].items() if k!='virtual_ns'} == {k:v for k,v in result['initial'].items() if k!='virtual_ns'}
    assert result['qom_inspection'] == result['qom_after_reset']
    primary = result['qom_inspection']['/machine/f500-primary']
    auxiliary = result['qom_inspection']['/machine/f500-auxiliary']
    assert primary == {'realized': True, 'active': True, 'variant': 'word-ready-strobe-latency', 'latency-ns': 8}
    assert auxiliary == {'realized': True, 'active': True, 'variant': 'lowtag-ready-strobe-latency', 'latency-ns': 2}
    assert result['memory_inspection'].count('(db2010-f500-port)') == 2


def test_connected_peer_lifecycle(tmp_path):
    state = dsp_fixture([], limit=8)
    fm = configured([])
    state['i2c_controller'] = fm['i2c_controller']
    state['fm_radio'] = fm['fm_radio']
    state['pdi'] = {'profile': 'r61500-write-packets-retained-latches'}
    peers = {'/machine/i2c/pmic': 'pmic', '/machine/i2c/fm': 'fm',
             '/machine/pdi/panel': 'panel', '/machine/fa-mailbox/dsp': 'dsp'}
    result = qemu_run(state, tmp_path/'peers', reset=True,
        qom_inspect={path: ('contract', 'realized', 'active') for path in peers})
    assert result['stop']['reason'] == 'instruction_limit'
    assert {k:v for k,v in result['after_reset'].items() if k!='virtual_ns'} == {k:v for k,v in result['initial'].items() if k!='virtual_ns'}
    assert result['qom_inspection'] == result['qom_after_reset']
    for path, name in peers.items():
        assert result['qom_inspection'][path] == {'contract': name, 'realized': True, 'active': True}
