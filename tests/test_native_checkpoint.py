"""Native migration controls: no replay, firmware patch, or forced handler calls."""
from support.paths import BASELINE_MODELS
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time

import pytest

from db2010.execution.qmp import QMP
from db2010.diagnostics.artifacts import identity
from db2010.paths import ROOT
from support.fixtures import base_state, freeze_state, words
from test_native_time import sleeping_peripheral_state
from test_display_first_burst import prepare_channel5


def command(qmp, name, arguments=None):
    return qmp.execute(name, time.monotonic()+20, arguments)


def migration(qmp, name, path):
    command(qmp, name, {'channels': [{'channel-type': 'main', 'addr': {
        'transport': 'file', 'filename': str(path), 'offset': 0}}]})
    until = time.monotonic()+20
    while True:
        status = command(qmp, 'query-migrate')
        if status['status'] == 'completed': return
        assert status['status'] not in ('failed', 'cancelled'), status
        assert time.monotonic() < until, status
        time.sleep(0.01)


@contextmanager
def engine(state, directory, *, incoming=False, prepared=None, audio=None, pass_fds=(),
           icount='shift=6,sleep=off,align=off', uart0=None, research_guards=True, extra_args=()):
    directory.mkdir()
    prepared = prepared or freeze_state(state, directory)
    with tempfile.TemporaryDirectory(prefix='dbcheckpoint-') as temporary:
        qmp_path = Path(temporary)/'qmp'
        argv = [str(ROOT/'qemu/build/qemu-system-arm'), '-machine', f'db2010,research-guards={"on" if research_guards else "off"},state={prepared}',
            '-accel', 'tcg,thread=single', '-icount', icount,
            '-nodefaults', '-display', 'none', '-monitor', 'none', '-serial', 'none',
            '-S', '-qmp', f'unix:{qmp_path},server=on,wait=off']
        if state and 'uart_transmit' in state:
            for port in range(4):
                argv += ['-chardev', uart0 if port == 0 and uart0 else
                         f'file,id=uart{port},path={directory}/uart{port}.bin']
        if audio: argv += ['-chardev', audio]
        if incoming: argv += ['-incoming', 'defer']
        argv.extend(extra_args)
        with (directory/'engine.log').open('w') as log:
            process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, pass_fds=pass_fds)
            client = None
            def health():
                assert process.poll() is None, (directory/'engine.log').read_text()
            try:
                deadline = time.monotonic()+10
                while True:
                    health()
                    try:
                        client = QMP(qmp_path, deadline, health)
                        break
                    except (FileNotFoundError, ConnectionRefusedError):
                        assert time.monotonic() < deadline
                        time.sleep(0.005)
                assert client.summary(time.monotonic()+5)['research_guards'] is research_guards
                yield client
            finally:
                if process.poll() is None:
                    if client:
                        client.health = lambda: None
                        try: command(client, 'quit')
                        except (RuntimeError, EOFError, OSError): pass
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill(); process.wait()
                if client: client.close()


def stopped(qmp):
    qmp.events.clear()
    command(qmp, 'cont')
    qmp.wait_stop(time.monotonic()+15)
    return qmp.snapshot(time.monotonic()+5)


def comparable(value):
    """Diagnostic histories belong to capture sessions; device totals persist."""
    value = copy.deepcopy(value)
    for key in ('initial_snapshot','qmp_status','irq_events','recording_history'):
        value.pop(key, None)
    if 'i2c_controller' in value:
        for key in ('captured_writes','captured_writes_dropped'):
            value['i2c_controller'].pop(key,None)
    return value


@pytest.mark.parametrize('kind', ['loop', 'tcm', 'wfi', 'pending-port'])
def test_native_save_restore_continuation(tmp_path, kind):
    if kind == 'loop':
        state=base_state(words(0xe2800001,0xeafffffd),limit=20)
        prefix=7
    elif kind == 'tcm':
        state=base_state(words(0xee090f11,0xe3a0102a,0xe5821000,0xe5923000,0xeafffffe),limit=20)
        state['registers'][0]=0x10001d
        state['registers'][2]=0x100000
        prefix=3
    else:
        state=sleeping_peripheral_state();state['instruction_limit']=20
        prefix=6 if kind=='wfi' else 5
    state['record_diagnostics']=False
    with engine(state,tmp_path/'whole') as vm: whole=stopped(vm)
    first=copy.deepcopy(state);first['instruction_limit']=prefix
    checkpoint=tmp_path/'state.vmstate'
    with engine(first,tmp_path/'prefix') as vm:
        start=stopped(vm)
        layout=json.loads(command(vm,'qom-get',{'path':'/machine','property':'checkpoint-layout'}))
        migration(vm,'migrate',checkpoint)
    continuation=copy.deepcopy(state);continuation['instruction_limit']=20-prefix
    continuation['checkpoint_tcm']=layout
    with engine(continuation,tmp_path/'restored',incoming=True) as vm:
        migration(vm,'migrate-incoming',checkpoint)
        initial=vm.summary(time.monotonic()+5)
        assert initial['observations']==start['observations']
        final=stopped(vm)
    assert comparable(final)==comparable(whole)


def test_native_partial_display_dma_continuation(tmp_path):
    prep=tmp_path/'display';prep.mkdir()
    path,_=prepare_channel5(ROOT,Path('unused'),prep,'stream_nonzero',frame=True,stream=True)
    state=json.loads(path.read_text());state['record_diagnostics']=False
    # Limit immediately after the original channel-enable STR and fixture BX.
    state['instruction_limit']=110
    path.write_text(json.dumps(state))
    checkpoint=tmp_path/'display.vmstate'
    with engine(None,tmp_path/'prefix',prepared=path) as vm:
        first=stopped(vm)
        assert first['pdi']['frame']['bytes']>0
        assert first['pdi']['completed_frames']==0
        migration(vm,'migrate',checkpoint)
    state['instruction_limit']=6000-110;path.write_text(json.dumps(state))
    with engine(None,tmp_path/'resumed',prepared=path,incoming=True) as vm:
        migration(vm,'migrate-incoming',checkpoint)
        final=stopped(vm)
    state['instruction_limit']=6000;path.write_text(json.dumps(state))
    with engine(None,tmp_path/'whole',prepared=path) as vm: whole=stopped(vm)
    assert comparable(final)==comparable(whole)


@pytest.mark.parametrize('opcode,size,logical,selector', [(0x0800,46,11,0), (0x090e,16,12,0), (0x0912,36,12,6), (0x0914,46,12,2)])
def test_native_pending_audio_reply_continuation(tmp_path, opcode, size, logical, selector):
    from support.audio_control_fixtures import make, inline, iir, transport_prefix, ACK, POLL, CLEAR
    programmed = iir(selector,True)+POLL[:20]+ACK+CLEAR if opcode == 0x0914 else []
    prefix = transport_prefix()+programmed+inline(opcode,size,logical,selector)+POLL[:20]
    count = 2 if programmed else 1
    state = make(prefix+ACK+CLEAR)
    state['record_diagnostics'] = False
    with engine(state,tmp_path/'whole') as vm:
        whole = stopped(vm)
    first = copy.deepcopy(state)
    first['instruction_limit'] = len(prefix)*3
    checkpoint = tmp_path/'audio.vmstate'
    with engine(first,tmp_path/'prefix') as vm:
        start = stopped(vm)
        assert start['dsp_peer']['rx_inline'] and start['dsp_peer']['rx_mailbox_ready']
        assert start['dsp_peer']['audio_requests'] == count and start['dsp_peer']['audio_replies'] == count-1
        layout = json.loads(command(vm,'qom-get',{'path':'/machine','property':'checkpoint-layout'}))
        migration(vm,'migrate',checkpoint)
    continuation = copy.deepcopy(state)
    continuation['instruction_limit'] -= first['instruction_limit']
    continuation['checkpoint_tcm'] = layout
    with engine(continuation,tmp_path/'restored',incoming=True) as vm:
        migration(vm,'migrate-incoming',checkpoint)
        restored = vm.snapshot(time.monotonic()+5)
        assert restored['dsp_peer'] == start['dsp_peer']
        final = stopped(vm)
    assert final['dsp_peer']['audio_replies'] == count
    assert comparable(final) == comparable(whole)


@pytest.mark.parametrize('value',[3,4])
def test_native_pending_mma_mode_reply_continuation(tmp_path,value):
    from support.mma_mode_fixtures import make,mode,transport_prefix,ACK,POLL,CLEAR
    prefix=transport_prefix()+mode(value)+POLL[:20]
    state=make(prefix+ACK+CLEAR)
    state['record_diagnostics']=False
    with engine(state,tmp_path/'whole') as vm:whole=stopped(vm)
    first=copy.deepcopy(state);first['instruction_limit']=len(prefix)*3
    checkpoint=tmp_path/'mma.vmstate'
    with engine(first,tmp_path/'prefix') as vm:
        start=stopped(vm)
        assert start['dsp_peer']['rx_mma'] and start['dsp_peer']['rx_mailbox_ready']
        assert start['dsp_peer']['mma_mode']==value and start['dsp_peer']['mma_replies']==0
        layout=json.loads(command(vm,'qom-get',{'path':'/machine','property':'checkpoint-layout'}))
        migration(vm,'migrate',checkpoint)
    continuation=copy.deepcopy(state)
    continuation['instruction_limit']-=first['instruction_limit']
    continuation['checkpoint_tcm']=layout
    with engine(continuation,tmp_path/'restored',incoming=True) as vm:
        migration(vm,'migrate-incoming',checkpoint)
        assert vm.snapshot(time.monotonic()+5)['dsp_peer']==start['dsp_peer']
        final=stopped(vm)
    assert final['dsp_peer']['mma_replies']==1 and final['dsp_peer']['audio_replies']==0
    assert comparable(final)==comparable(whole)


@pytest.mark.parametrize('stage',['announcement','mode_offer','mode_response','registered'])
def test_native_pending_mma_ring_continuation(tmp_path,stage):
    from support.mma_ring_fixtures import make,stages
    parts=stages()
    state=make(parts['registered'],latency=512)
    state['record_diagnostics']=False
    with engine(state,tmp_path/'whole') as vm:whole=stopped(vm)
    first=copy.deepcopy(state);first['instruction_limit']=len(parts[stage])*3
    checkpoint=tmp_path/'ring.vmstate'
    with engine(first,tmp_path/'prefix') as vm:
        start=stopped(vm)
        peer=start['dsp_peer']
        if stage=='announcement':assert peer['rx_ring4'] and not peer['ring4_announced']
        elif stage=='mode_offer':assert peer['phase']==9 and peer['ring4_announced'] and not peer['rx_mailbox_ready']
        elif stage=='mode_response':assert peer['rx_mma'] and peer['ring4_announced']
        else:assert peer['ring4_registered'] and peer['ring4_threshold']==240
        layout=json.loads(command(vm,'qom-get',{'path':'/machine','property':'checkpoint-layout'}))
        migration(vm,'migrate',checkpoint)
    continuation=copy.deepcopy(state)
    continuation['instruction_limit']-=first['instruction_limit']
    continuation['checkpoint_tcm']=layout
    with engine(continuation,tmp_path/'restored',incoming=True) as vm:
        migration(vm,'migrate-incoming',checkpoint)
        assert vm.snapshot(time.monotonic()+5)['dsp_peer']==start['dsp_peer']
        final=stopped(vm)
    assert final['dsp_peer']['ring4_registrations']==final['dsp_peer']['mma_replies']==1
    assert comparable(final)==comparable(whole)


@pytest.mark.parametrize('prefix', [3, 6, 9])
def test_pending_nor_program_commands_restore_without_committing_early(tmp_path, prefix):
    from support.nor_fixtures import program
    target=0x45f00000
    operations=[('write',2,target,0x40),('write',2,target,0x1234),('write',2,target,0xff),
                ('read',2,target,0)]
    code,limit,_=program(operations)
    state=base_state(code,limit=limit)
    state['nor_model']=copy.deepcopy(json.loads((BASELINE_MODELS).read_text())['nor_model'])
    state['nor_model']['initial_locked']=False
    state['regions'].append({'name':'fixture.nor','address':0x44000000,'size':0x2000000,
        'readonly':True,'seeds':[{'address':target,'data':b'\xff\xff'}]})
    with engine(state,tmp_path/'whole') as vm: whole=stopped(vm)
    first=copy.deepcopy(state);first['instruction_limit']=prefix
    checkpoint=tmp_path/'nor.vmstate'
    with engine(first,tmp_path/'prefix') as vm:
        stopped(vm)
        migration(vm,'migrate',checkpoint)
    continuation=copy.deepcopy(state);continuation['instruction_limit']=limit-prefix
    with engine(continuation,tmp_path/'resumed',incoming=True) as vm:
        migration(vm,'migrate-incoming',checkpoint)
        final=stopped(vm)
    assert comparable(final)==comparable(whole)
    assert final['registers'][2]==0x1234


def test_halted_wfi_checkpoint_waits_for_real_keypad_irq(tmp_path):
    # No running timer can advance this wait. A modeled physical keypad pin is
    # the wake source; the ARM CPU keeps its own WFI state and IRQ masks.
    state=base_state(words(0xe59f0010,0xe59f1010,0xe1c100b0,0xee070f90,
                           0xe2800001,0xeafffffe,0x3a1,0xfb010000),limit=9)
    state['keypad']={'profile':'w800-front-ideal-contacts'}
    state['timer_irq']={'profile':'tick1-upcounter-irq-assumption','frequency_hz':131072,'max_irqs':8}
    state['device_registers']=[{'address':a,'width':4,'initial_value':0,'read_or_mask':0,'name':'fixture.mask'}
        for a in (0xfb020104,0xfb020204,0xfb020304,0xfb020118,0xfb020218)]
    # Fixture wait() is CP15 WFI, followed by the harmless fixture loop.
    checkpoint=tmp_path/'wfi.vmstate'
    def sleeping(vm):
        command(vm,'cont')
        until=time.monotonic()+5
        while True:
            progress=json.loads(command(vm,'qom-get',{'path':'/machine','property':'progress'}))
            if progress['wfi_waits']: break
            assert time.monotonic()<until
        command(vm,'stop')
        return vm.snapshot(time.monotonic()+5)
    with engine(state,tmp_path/'source') as vm:
        first=sleeping(vm)
        migration(vm,'migrate',checkpoint)
    with engine(state,tmp_path/'resumed',incoming=True) as vm:
        migration(vm,'migrate-incoming',checkpoint)
        initial=vm.snapshot(time.monotonic()+5)
        assert initial==first
        command(vm,'cont')
        # Multiple round trips prove that resume remains halted awaiting input.
        for _ in range(3):
            assert json.loads(command(vm,'qom-get',{'path':'/machine','property':'progress'}))['observations']==first['observations']
        command(vm,'qom-set',{'path':'/machine/keypad','property':'pressed','value':'["1"]'})
        vm.wait_stop(time.monotonic()+5)
        final=vm.snapshot(time.monotonic()+5)
    assert final['observations']>first['observations']
    assert final['keypad']['pressed']==['1']
    assert final['cpsr']&0x80


def test_nor_query_mapping_survives_restore(tmp_path):
    from support.nor_fixtures import program
    target=0x45e00000
    operations=[('write',2,target,0x60),('write',2,target,0xd0),
                ('write',2,target,0x90),('read',2,target,0)]
    code,limit,_=program(operations)
    state=base_state(code,limit=limit)
    state['nor_model']=copy.deepcopy(json.loads((BASELINE_MODELS).read_text())['nor_model'])
    state['regions'].append({'name':'fixture.nor','address':0x44000000,'size':0x2000000,
        'readonly':True,'seeds':[{'address':target,'data':b'\x34\x12'}]})
    with engine(state,tmp_path/'whole') as vm: whole=stopped(vm)
    assert whole['stop']['reason']=='instruction_limit'
    assert whole['registers'][2]==0x89
    first=copy.deepcopy(state);first['instruction_limit']=9
    checkpoint=tmp_path/'query.vmstate'
    with engine(first,tmp_path/'source') as vm:
        prefix=stopped(vm)
        migration(vm,'migrate',checkpoint)
    state['instruction_limit']=limit-9
    with engine(state,tmp_path/'restored',incoming=True) as vm:
        migration(vm,'migrate-incoming',checkpoint)
        final=stopped(vm)
    assert final['registers'][2]==whole['registers'][2] # manufacturer89, not array1234
    assert comparable(final)==comparable(whole)


def test_staged_nand_program_is_resumed(tmp_path,monkeypatch):
    from test_nand import execute, write, program
    from db2010.diagnostics.artifacts import artifact, prepared_references
    result,error=execute(tmp_path,monkeypatch,[write(0x10,0xff),*program(0,3,0)])
    assert error is None
    state=prepared_references(json.loads(artifact(tmp_path/'run','state.json').read_text()),tmp_path/'run',absolute=True)
    total=state['instruction_limit']
    prepared=tmp_path/'prepared.json';prepared.write_text(json.dumps(state))
    # Compare both migration paths under the fixture's identical icount clock.
    # The host runner uses sleep/align=on and may acquire a different time epoch.
    with engine(None,tmp_path/'whole',prepared=prepared) as vm:
        whole=stopped(vm)
    state['instruction_limit']=14;prepared.write_text(json.dumps(state))
    checkpoint=tmp_path/'nand.vmstate'
    with engine(None,tmp_path/'source',prepared=prepared) as vm:
        first=stopped(vm)
        assert first['nand']['mode']=='spare-program-data'
        migration(vm,'migrate',checkpoint)
    state['instruction_limit']=total-14;prepared.write_text(json.dumps(state))
    with engine(None,tmp_path/'restored',prepared=prepared,incoming=True) as vm:
        migration(vm,'migrate-incoming',checkpoint)
        final=stopped(vm)
    # Operation ledgers are per session; actual device state matches exactly.
    assert comparable(final)==comparable(whole)
