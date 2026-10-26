"""Observe real QEMU/GDB sessions and verify CLI ownership/final-view signals."""
from support.guarded import run as guarded_run
import sys
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from types import SimpleNamespace

import pytest
from support.controlled_clock import controlled_clock
from aiohttp import ClientSession, WSMsgType

from db2010 import cli
from db2010.execution import runner
from db2010.execution.debug import DebugOptions
from db2010.paths import ROOT
from db2010.ui.browser import Browser
from db2010.ui.observer import Observers
from test_runner import synthetic_preparation
from test_display_first_burst import prepare_channel5
from test_optional_diagnostics import uart_preparation


async def status_message(ws):
    while True:
        message = await asyncio.wait_for(ws.receive(), 5)
        if message.type == WSMsgType.TEXT:
            payload = json.loads(message.data)
            if payload['type'] == 'status':
                return payload
        elif message.type != WSMsgType.BINARY:
            raise RuntimeError('WebSocket closed before status')


class Samples:
    show_status = show_display = True
    def __init__(self):
        self.progress_samples = []
        self.panels = []
    def phase(self, name): pass
    def uart(self, port, data): pass
    def progress(self, sample): self.progress_samples.append(sample)
    def display(self, panel): self.panels.append(panel)


def test_live_browser_stats_advance_during_qemu_execution_without_lcd(monkeypatch):
    monkeypatch.setattr(runner, 'prepare',
        lambda root, spec, output, **options: synthetic_preparation(root, spec, output))
    view = Browser(0)
    messages = []

    async def check(executor):
        async with ClientSession() as session:
            async with session.ws_connect(view.url+'/ws', headers={'Origin': view.url}) as ws:
                messages.append(await status_message(ws))
                future = executor.submit(runner.run, ROOT, Path('unused'), console=view, seconds=1.5)
                while True:
                    message = await status_message(ws)
                    messages.append(message)
                    if message['status']['phase'] == 'capture':
                        break
                return future.result(timeout=5)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            result, failure = asyncio.run(check(executor))
        assert isinstance(failure, TimeoutError)
        assert result['snapshot']['observations'] > 0
        live = [message['status'] for message in messages
                if message['status']['phase'] == 'run' and message['status'].get('observations', 0) > 0]
        assert len(live) >= 3
        assert live[-1]['observations'] > live[0]['observations']
        assert live[-1]['elapsed'] > live[0]['elapsed']
        assert all(sample['pc'] == 0x1000 and 'lcd_frames' not in sample for sample in live)
        assert all(message.get('image') is None for message in messages)
        assert view.panel['known_pixels'] == 0
    finally:
        view.close()


@pytest.mark.parametrize('variant', ['stream_nonzero','late_unknown','single_frame'])
def test_completed_live_display_matches_stopped_state(tmp_path, monkeypatch, variant):
    def prepare(root,spec,output,**options):
        return prepare_channel5(root,spec,output,'black' if variant == 'single_frame' else variant,
                                frame=True,stream=variant != 'single_frame')
    monkeypatch.setattr(runner,'prepare',prepare)
    samples = Samples()
    result,failure = guarded_run(ROOT,Path('unused'),tmp_path/'capture', diagnostics=True,console=samples)
    assert failure is None
    assert samples.panels and samples.panels[0]['known_pixels'] == 0
    final = samples.panels[-1]
    assert final['pixel_format'] == 'rgb565-be'
    assert final['width'] == 176 and final['height'] == 220
    assert not {'transfers','frames_history','snapshot','registers','update_hex'} & final.keys()
    if variant == 'late_unknown':
        assert final['frames'] == 0 and final['known_pixels'] == 0 and final['pixels_hex'] == ''
    else:
        expected = (result['snapshot']['pdi']['frame'] if variant == 'single_frame' else
                    result['snapshot']['pdi']['frames'][-1])
        assert final['pixels_hex'] == expected['pixels_hex']
        assert final['completed'] == expected['completed']
        assert final['known_pixels'] == 176*220


def test_gdb_observations_poll_with_terminal_progress_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(runner,'prepare',lambda root,spec,output,**options: synthetic_preparation(root,spec,output))
    samples = Samples()
    debug = DebugOptions(interactive=False, actions=[('command','shell sleep 0.8'),('command','stepi')])
    result,failure = guarded_run(ROOT,Path('unused'),debug=debug,console=samples,seconds=5)
    assert failure is None and result['success']
    assert len(samples.panels) >= 3 and len(samples.progress_samples) >= 3
    assert all(not panel['pixels_hex'] for panel in samples.panels)
    assert result['qemu_returncode'] is not None
    with pytest.raises(ProcessLookupError):
        os.kill(result['debugger']['pid'],0)


@pytest.mark.parametrize('trace', [False,True])
def test_browser_recorded_and_temporary_uart_bytes_remain_exact(tmp_path,monkeypatch,trace,controlled_clock):
    monkeypatch.setattr(runner,'prepare',uart_preparation)
    snapshots=[]
    for recording in ([True] if trace else [False,True]):
        browser=Browser(0)
        try:
            destination=tmp_path/('trace' if trace else 'recorded') if recording else None
            result,failure=guarded_run(ROOT,Path('unused'),destination, diagnostics=recording,console=browser,trace=trace)
            assert failure is None
            assert result['diagnostics_enabled']==recording
            assert (result['run_directory'] is not None)==recording
            snapshots.append(result['snapshot'])
            assert browser.totals == result['snapshot']['uart_bytes']
            if recording:
                from db2010.diagnostics.artifacts import artifact
                for port in range(4):
                    assert bytes(browser.tails[port]) == artifact(destination,f'uart{port}.bin').read_bytes()
        finally:
            browser.close()
    assert all({k:s[k] for k in ('stop','observations','virtual_ns','uart_bytes')} ==
               {k:snapshots[0][k] for k in ('stop','observations','virtual_ns','uart_bytes')}
               for s in snapshots)


def test_cli_port_error_precedes_preparation(monkeypatch):
    listener=socket.socket()
    listener.bind(('127.0.0.1',0));listener.listen()
    try:
        monkeypatch.setattr('sys.argv',['db2010', '--research-guards','--ui',str(listener.getsockname()[1])])
        monkeypatch.setattr(cli,'run',lambda *a,**kw: pytest.fail('runner started before bind'))
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 2
    finally:
        listener.close()


def launch_fixture(tmp_path, *, failure=False, long=False, json_summary=False, recording=False, debugging=False):
    port_socket=socket.socket();port_socket.bind(('127.0.0.1',0))
    port=port_socket.getsockname()[1];port_socket.close()
    script=tmp_path/'launch.py'
    arguments=['db2010', '--research-guards','--ui',str(port),'--limit',str(200000000000 if long else 10)]
    if json_summary:
        arguments.append('--json-summary')
    if recording:
        arguments += ['--diagnostics','--artifacts-dir',str(tmp_path),'--label','capture']
    if debugging:
        arguments.append('--debug')
    script.write_text("""import sys
from pathlib import Path
sys.path[:0] = [str(Path.cwd()), str(Path.cwd()/'tests')]
from db2010.execution import runner
from db2010 import cli
from test_runner import synthetic_preparation
"""+ ("""def prepare(*args,**kwargs):
    raise ValueError('fixture preparation failure')
runner.prepare=prepare
""" if failure else 'runner.prepare=lambda root,spec,output,**options: synthetic_preparation(root,spec,output)\n')
        + f'sys.argv={arguments!r}\ncli.main()\n')
    process=subprocess.Popen([sys.executable,'-B',str(script)],cwd=ROOT,
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    return process,f'http://127.0.0.1:{port}'


def wait_phase(url, phase):
    async def read():
        deadline=time.monotonic()+15
        async with ClientSession() as session:
            while True:
                try:
                    async with session.ws_connect(url+'/ws',headers={'Origin':url}) as ws:
                        while True:
                            message=await status_message(ws)
                            if message['status']['phase'] == phase:
                                return message
                except (OSError,TimeoutError):
                    if time.monotonic() > deadline:
                        raise
                    await asyncio.sleep(.05)
    return asyncio.run(read())


@pytest.mark.parametrize('failure,json_summary,recording',[(False,False,False),(False,True,True),(True,True,False)])
def test_final_view_outlives_execution_and_preserves_exit_status(tmp_path,failure,json_summary,recording):
    process,url=launch_fixture(tmp_path,failure=failure,json_summary=json_summary,recording=recording)
    try:
        message=wait_phase(url,'error' if failure else 'stopped')
        assert process.poll() is None
        phases=message['status']['phases']
        time.sleep(.4)
        later=wait_phase(url,'error' if failure else 'stopped')
        assert later['status']['phases'] == phases
        assert later['status']['elapsed'] == message['status']['elapsed']
        # No owned QEMU/GDB remains while the final view waits.
        children=subprocess.check_output(['ps','-o','comm=','--ppid',str(process.pid)],text=True) if Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().strip() else ''
        assert 'qemu' not in children and 'gdb' not in children
        process.send_signal(signal.SIGINT)
        out,err=process.communicate(timeout=8)
        assert process.returncode == (1 if failure else 0)
        if json_summary:
            summary=json.loads(err)
            assert summary['ui_url'] == url
            assert summary['success'] == (not failure)
            assert out == b''
        else:
            assert url.encode() in err
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGKILL)
            process.wait()


@pytest.mark.parametrize('debugging',[False,True])
def test_ctrl_c_during_execution_closes_server_and_reaps_qemu(tmp_path,debugging):
    process,url=launch_fixture(tmp_path,long=True,json_summary=True,debugging=debugging)
    try:
        wait_phase(url,'run')
        process.send_signal(signal.SIGINT)
        out,err=process.communicate(timeout=8)
        assert process.returncode == 130
        assert json.loads(err)['error']['type'] == 'KeyboardInterrupt'
        port=int(url.rsplit(':',1)[1])
        with socket.socket() as connection:
            assert connection.connect_ex(('127.0.0.1',port)) != 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGKILL)
            process.wait()


def test_display_property_is_read_only_and_does_not_change_stopped_state(tmp_path,monkeypatch):
    monkeypatch.setattr(runner,'prepare',lambda root,spec,output,**options: synthetic_preparation(root,spec,output))
    original=runner.QMP.observe
    checked=[]
    def observe(qmp,deadline,progress=None,display=None):
        if not checked:
            before=json.loads(qmp.execute('qom-get',deadline,{'path':'/machine','property':'display'}))
            with pytest.raises(RuntimeError,match='QMP error'):
                qmp.execute('qom-set',deadline,{'path':'/machine','property':'display','value':'{}'})
            after=json.loads(qmp.execute('qom-get',deadline,{'path':'/machine','property':'display'}))
            assert before == after
            checked.append(True)
        return original(qmp,deadline,progress,display)
    monkeypatch.setattr(runner.QMP,'observe',observe)
    result,failure=guarded_run(ROOT,Path('unused'),instruction_limit=10,console=Samples())
    assert failure is None and checked and result['snapshot']['observations'] == 10


@pytest.mark.parametrize('kind',['unknown','complete','partial','inflight'])
@pytest.mark.parametrize('recording',[False,True])
def test_completed_panel_knowledge_and_partial_updates(tmp_path,monkeypatch,kind,recording):
    from support.lcd_partial_fixtures import setup,dma,state,w
    from support.fixtures import freeze_state
    full=bytes.fromhex('1234')*(176*220)
    update=bytes(range(32))
    stop=[w(0xf2000008,32),w(0xf70001f8,0x180208),w(0xf70001dc,5)]
    if kind=='unknown':
        steps=setup()+dma(0xa800,32)
        raw=update; limit=None
    elif kind=='complete':
        steps=setup()+dma(0,77440)
        raw=full;limit=6000
    else:
        steps=setup()+dma(0,77440)+[('wait',0,0,4)]+stop+dma(0xa800,32,0x4c012e80)
        # With only the first burst known, the next burst is guarded; GRAM has
        # begun changing but the browser must still acquire the previous panel.
        raw=full+(update[:16] if kind=='inflight' else update);limit=None
    fixture=state(steps,raw,limit=limit)
    def prepare(root,spec,output,**options):
        return freeze_state(fixture,output),{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    observations=Samples()
    run_result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'capture' if recording else None, diagnostics=recording,console=observations)
    assert failure is None
    result=run_result['snapshot']
    panel=observations.panels[-1]
    if kind=='unknown':
        assert panel['known_pixels']==16 and panel['pixels_hex']==''
    elif kind in ('complete','inflight'):
        assert bytes.fromhex(panel['pixels_hex'])==full
        assert panel['frames']==1
        if kind=='inflight':
            assert result['stop']['reason']=='display_dma_source_unknown'
            if recording: assert result['pdi']['frame']['bytes']==16
    else:
        expected=bytearray(full);expected[168*176*2:168*176*2+32]=update
        assert bytes.fromhex(panel['pixels_hex'])==expected
        assert panel['frames']==2 and panel['known_pixels']==176*220
    if not recording: assert 'pdi' not in result and 'registers' not in result


@pytest.mark.parametrize('arguments,expected', [(['--ui'],8765),(['--ui','0'],0)])
def test_ui_default_and_ephemeral_port_cli(monkeypatch,arguments,expected):
    import db2010.ui.browser as module
    seen=[]
    class View:
        show_status=show_display=True
        url='http://127.0.0.1:12345'
        def __init__(self,port):
            seen.append(port)
            self.files = SimpleNamespace(service=object())
        def finish(self,result,failure): pass
        def wait(self): pass
        def close(self): seen.append('closed')
    monkeypatch.setattr(module,'Browser',View)
    def run(*args, **options):
        assert options['serial_service'] is not None
        assert args[1] == cli.DEFAULT_EXPERIMENT.resolve()
        return {'success':True,'phases':{},'uart':{}}, None
    monkeypatch.setattr(cli,'run',run)
    monkeypatch.setattr('sys.argv',['db2010', '--research-guards',*arguments])
    cli.main()
    assert seen == [expected,'closed']


@pytest.mark.parametrize('fixture', ['uart', 'panel'])
def test_headless_and_browser_fixture_guest_state_is_exact(tmp_path, monkeypatch, fixture,controlled_clock):
    prepare = uart_preparation if fixture == 'uart' else (
        lambda r,s,o,**kw: prepare_channel5(r,s,o,'stream_nonzero',frame=True,stream=True))
    monkeypatch.setattr(runner, 'prepare', prepare)
    results = []
    view = Browser(0)
    try:
        for name, console in [('headless', None), ('browser', view)]:
            result, failure = guarded_run(ROOT, Path('unused'), tmp_path/name,
                                         diagnostics=True, console=console)
            assert failure is None
            results.append(result)
        assert results[0]['initial_snapshot'] == results[1]['initial_snapshot']
        assert results[0]['snapshot'] == results[1]['snapshot']
        from db2010.diagnostics.artifacts import artifact
        for port in range(4):
            expected = artifact(tmp_path/'headless', f'uart{port}.bin').read_bytes()
            assert artifact(tmp_path/'browser', f'uart{port}.bin').read_bytes() == expected
            assert bytes(view.tails[port]) == expected
        if fixture == 'panel':
            assert view.display_data.endswith(bytes.fromhex(
                results[1]['snapshot']['pdi']['frames'][-1]['pixels_hex']))
    finally:
        view.close()
