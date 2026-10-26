from support.guarded import run as guarded_run
import asyncio
import json
import time
from io import StringIO

import pytest
from aiohttp import ClientSession, WSMsgType
from playwright.sync_api import sync_playwright

from db2010.ui.browser import Browser
from db2010.ui.keypad import KeypadInput, KeypadService, InputOverflow


def command(button='1',pressed=True):
    return dict(type='keypad',button=button,pressed=pressed)


class Qmp:
    def __init__(self):self.sets=[];self.events=[]
    def execute(self,name,deadline,args):
        if name=='qom-set':self.sets.append(json.loads(args['value']))
        else:return json.dumps({'applied_at':100+len(self.sets)})


def test_quick_taps_duplicates_fifo_recording_and_overflow():
    channel=KeypadInput(capacity=4)
    channel.submit(command())
    assert not channel.take()
    channel.enable(True)
    stream=StringIO();service=KeypadService(channel,stream);qmp=Qmp()
    for down in [True,True,False,False]:channel.submit(command(pressed=down))
    with pytest.raises(InputOverflow):channel.submit(command('2'))
    service.poll(qmp,time.monotonic()+1)
    assert qmp.sets==[['1'],[]]
    rows=[json.loads(x) for x in stream.getvalue().splitlines()]
    assert [r['virtual_ns'] for r in rows]==[101,102]
    channel.submit(command('2'));service.poll(qmp,time.monotonic()+1)
    channel.release_all();channel.submit(command('3'));service.poll(qmp,time.monotonic()+1)
    assert qmp.sets[-3:]==[['2'],[],['3']]
    service.close(qmp,time.monotonic()+1)
    assert qmp.sets[-1]==[] and not channel.enabled


@pytest.mark.parametrize('message',[{},[],{'type':'foo'},command('unknown'),command(pressed=1),
                                    {**command(),'extra':1},command(None)])
def test_invalid_commands(message):
    with pytest.raises((ValueError,TypeError)):KeypadInput().submit(message)


def test_websocket_shared_fifo_invalid_overflow_and_disconnect():
    view=Browser(0);view.input=KeypadInput(capacity=4);view.input.enable(True);view.phase('run')
    async def check():
        async with ClientSession() as session:
            async with session.ws_connect(view.url+'/ws',headers={'Origin':view.url}) as a:
                await a.receive_json()
                async with session.ws_connect(view.url+'/ws',headers={'Origin':view.url}) as b:
                    await b.receive_json()
                    await a.send_json(command('1'));await asyncio.sleep(.03)
                    await b.send_json(command('2'));await asyncio.sleep(.03)
                    await a.send_json(command('1',False));await asyncio.sleep(.03)
                    assert [(b,p) for b,p,t in view.input.take()]==[('1',True),('2',True),('1',False)]
                await asyncio.sleep(.03)
                assert view.input.take()[0][0] is None
                for _ in range(6):await a.send_json(command())
                while (await a.receive()).type in (WSMsgType.TEXT,WSMsgType.BINARY):pass
                rows=view.input.take();assert len(rows)==5 and rows[-1][0] is None
            async with session.ws_connect(view.url+'/ws',headers={'Origin':view.url}) as a:
                await a.receive_json();await a.send_str('{')
                while (message:=await a.receive()).type in (WSMsgType.TEXT,WSMsgType.BINARY):pass
                assert message.type in (WSMsgType.CLOSE,WSMsgType.CLOSED)
    try:asyncio.run(check())
    finally:view.close()


def test_chromium_every_binding_modifier_pointer_focus_and_final():
    view=Browser(0);view.input.enable(True);view.phase('run')
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
            page=browser.new_page();page.goto(view.url)
            page.wait_for_function("() => !document.querySelector('[data-key]').disabled")
            assert page.locator('#handset').evaluate('(e)=>e===document.activeElement')
            bindings=[('ArrowUp','up'),('ArrowDown','down'),('ArrowLeft','left'),('ArrowRight','right'),
                      ('Enter','select'),('NumpadEnter','select'),('Escape','back'),('Backspace','clear'),
                      ('*','star'),('NumpadMultiply','star'),('#','hash'),('Shift+ArrowLeft','soft-left'),
                      ('Shift+ArrowRight','soft-right'),('Shift+Enter','walkman'),
                      ('PageUp','volume-up'),('PageDown','volume-down'),('Space','play-pause'),
                      ('c','camera'),('Shift+KeyC','camera-focus'),('p','power')]
            bindings += [(str(n),str(n)) for n in range(10)]+[(f'Numpad{n}',str(n)) for n in range(10)]
            for binding,action in bindings:
                page.keyboard.press(binding);page.wait_for_timeout(30)
                assert [(b,d) for b,d,t in view.input.take()]==[(action,True),(action,False)],binding
            for physical,action in [('ArrowLeft','soft-left'),('ArrowRight','soft-right'),('Enter','walkman'),
                                    ('KeyC','camera-focus')]:
                page.keyboard.down('Shift');page.keyboard.down(physical);page.keyboard.up('Shift')
                page.keyboard.down(physical) # repeated keydown
                page.keyboard.up(physical);page.wait_for_timeout(30)
                assert [(b,d) for b,d,t in view.input.take()]==[(action,True),(action,False)]
            second=browser.new_page();second.goto(view.url)
            second.wait_for_function("() => !document.querySelector('[data-key]').disabled")
            for client,button,pressed in [(page,'1',True),(second,'2',True),(page,'1',False)]:
                client.evaluate('(c)=>inputSocket.send(JSON.stringify(c))',command(button,pressed))
                client.wait_for_timeout(30)
            assert [(b,d) for b,d,t in view.input.take()]==[('1',True),('2',True),('1',False)]
            second.close();page.wait_for_timeout(50)
            assert any(b is None for b,d,t in view.input.take())
            page.bring_to_front();page.locator('#handset').focus()
            from test_handset3d import project
            page.wait_for_function('() => handset3D?.renders > 0', timeout=30000)
            button=page.locator('[data-key="5"]');point=project(page, '5')
            page.mouse.move(**point);page.mouse.down()
            page.wait_for_timeout(50)
            assert button.get_attribute('aria-pressed')=='true'
            page.keyboard.down('5');page.mouse.move(1,1);page.mouse.up();page.wait_for_timeout(30)
            assert [(b,d) for b,d,t in view.input.take()]==[('5',True)]
            page.keyboard.up('5');page.wait_for_timeout(30)
            assert [(b,d) for b,d,t in view.input.take()]==[('5',False)]
            page.locator('#handset').focus();page.keyboard.down('1');page.locator('#uart').focus();page.keyboard.up('1')
            page.keyboard.press('ArrowDown');page.wait_for_timeout(30)
            assert [(b,d) for b,d,t in view.input.take()]==[('1',True),('1',False)]
            page.locator('#handset').focus();page.keyboard.down('2')
            page.evaluate("window.dispatchEvent(new Event('blur'))");page.keyboard.up('2');page.wait_for_timeout(30)
            assert [(b,d) for b,d,t in view.input.take()]==[('2',True),('2',False)]
            page.mouse.move(**project(page, '5'));page.mouse.down()
            page.locator('#three-stage canvas').dispatch_event('pointercancel',{'pointerId':1});page.mouse.up();page.wait_for_timeout(30)
            assert [(b,d) for b,d,t in view.input.take()]==[('5',True),('5',False)]
            page.locator('#handset').focus();page.keyboard.down('3')
            page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'))")
            page.keyboard.up('3');page.wait_for_timeout(30)
            assert [(b,d) for b,d,t in view.input.take()]==[('3',True),('3',False)]
            page.keyboard.down('4');page.evaluate('inputSocket.close()');page.keyboard.up('4')
            page.wait_for_function("() => document.getElementById('connection').getAttribute('aria-label') === 'Connected'")
            page.wait_for_timeout(1200)
            rows=view.input.take();assert rows[0][:2]==('4',True) and any(b is None for b,d,t in rows)
            assert not page.locator('.pressed').count()
            view.input.enable(False);view.finish({'success':True,'phases':{}},None)
            page.wait_for_function("() => document.querySelector('[data-key]').disabled")
            page.keyboard.press('3');page.wait_for_timeout(30)
            assert all(b is None for b,d,t in view.input.take())
            browser.close()
    finally:view.close()


def test_stop_race_discards_later_commands_and_releases():
    channel=KeypadInput();channel.enable(True)
    for button in ('1','2','3'):channel.submit(command(button))
    class StoppingQmp(Qmp):
        def execute(self,*args):
            result=super().execute(*args)
            self.events.append({'event':'STOP'})
            return result
    qmp=StoppingQmp();service=KeypadService(channel,stop_on_event=True)
    service.poll(qmp,time.monotonic()+1)
    assert qmp.sets==[['1']]
    service.close(qmp,time.monotonic()+1)
    assert qmp.sets==[['1'],[]]


def test_input_cadence_independent_of_observations():
    from db2010.execution.qmp import QMP
    qmp=QMP.__new__(QMP);qmp.events=[]
    serviced=[];observed=[]
    def receive(deadline):
        time.sleep(max(0,deadline-time.monotonic()))
        if len(serviced)>=6:return {'event':'STOP'}
        raise TimeoutError
    qmp.receive=receive;qmp.observe=lambda *args, **kwargs:observed.append(time.monotonic())
    qmp.wait_stop(time.monotonic()+1,progress=lambda s:None,
                  service=lambda *args:serviced.append(time.monotonic()))
    assert len(observed)==1 and len(serviced)==6
    assert max(b-a for a,b in zip(serviced,serviced[1:]))<.05


def test_gdb_paused_input_recording_and_cleanup(tmp_path,monkeypatch):
    import threading
    from pathlib import Path
    from db2010.execution import runner
    from db2010.execution.debug import DebugOptions
    from db2010.paths import ROOT
    from db2010.diagnostics.artifacts import artifact
    from test_runner import synthetic_preparation
    def prepare(root,spec,output,**options):
        path,spec=synthetic_preparation(root,spec,output)
        s=json.loads(path.read_text());s['keypad']={'profile':'w800-front-ideal-contacts'}
        path.write_text(json.dumps(s));return path,spec
    monkeypatch.setattr(runner,'prepare',prepare)
    channel=KeypadInput()
    class Samples:
        show_status=True;show_display=False
        def phase(self,name):
            if name=='run':
                def press():
                    time.sleep(.15)
                    channel.submit(command('1'));time.sleep(.12)
                    channel.submit(command('1',False));time.sleep(.12)
                    channel.submit(command('2'))
                self.thread=threading.Thread(target=press);self.thread.start()
        def uart(self,*args):pass
        def progress(self,s):pass
    samples=Samples()
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True,keypad_input=channel,console=samples,
        debug=DebugOptions(interactive=False,actions=[('command','shell sleep 0.8'),('command','stepi')]),seconds=5)
    samples.thread.join()
    assert failure is None and result['snapshot']['keypad']['pressed']==[]
    rows=[json.loads(x) for x in artifact(tmp_path/'run','keypad-input.log').read_text().splitlines()]
    assert [r['held'] for r in rows]==[['1'],[],['2'],[]]
    assert [r['virtual_ns'] for r in rows[:3]]==[0,0,0]
    assert max(r['applied_monotonic']-r['requested_monotonic'] for r in rows)<.05


def test_chromium_contacts_reach_native_cpu_scan_and_release(tmp_path,monkeypatch):
    import threading
    from pathlib import Path
    from db2010.execution import runner
    from db2010.paths import ROOT
    from db2010.diagnostics.artifacts import artifact
    from support.fixtures import base_state,words,freeze_state
    # Original controlled fixture: enable contacts, select row3, poll until 1
    # closes, then until it opens. A neighboring read ends at the normal guard.
    code=[0xe59f1030,0xe59f0030,0xe1c100b0,0xe2811002,0xe3a00037,0xe1c100b0,
          0xe1d120b0,0xe3120040,0x1afffffc,0xe1d120b0,0xe3120040,0x0afffffc,
          0xe3a0002a,0xe1d120b2,0xfb010000,0x3a1]
    def prepare(root,spec,output,**options):
        s=base_state(words(*code),limit=1000000)
        s['keypad']={'profile':'w800-front-ideal-contacts'}
        return freeze_state(s,output),{'seconds':10}
    monkeypatch.setattr(runner,'prepare',prepare)
    view=Browser(0);result=[]
    worker=threading.Thread(target=lambda:result.append(guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True,
        seconds=10,config_limits=False,keypad_input=view.input,console=view)))
    try:
        worker.start()
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
            page=browser.new_page();page.goto(view.url)
            page.wait_for_function("() => !document.querySelector('[data-key]').disabled")
            page.keyboard.down('1');page.wait_for_timeout(200);page.keyboard.up('1')
            worker.join(timeout=10);assert not worker.is_alive()
            outcome,failure=result[0]
            assert failure is None and outcome['snapshot']['registers'][0]==42
            assert outcome['snapshot']['stop']['reason']=='device_register_access_unmodeled'
            assert outcome['snapshot']['keypad']['pressed']==[]
            rows=[json.loads(s) for s in artifact(tmp_path/'run','keypad-input.log').read_text().splitlines()]
            assert [s['held'] for s in rows]==[['1'],[]]
            assert rows[1]['virtual_ns']>rows[0]['virtual_ns']
            page.wait_for_function("() => document.querySelector('[data-key]').disabled")
            browser.close()
    finally:
        worker.join(timeout=12)
        view.close()
