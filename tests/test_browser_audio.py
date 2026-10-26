"""Browser PCM consumption, bounded transport and independent UI channels."""
import asyncio
import json
import struct
import time

import pytest
from aiohttp import ClientSession, WSServerHandshakeError
from playwright.sync_api import sync_playwright

from db2010.execution.audio import AudioPacket
from db2010.ui.browser import Browser


def packet(sequence=0, frames=960, eos=False):
    header=dict(type='audio',version=1,stream_id='fixture',epoch='1',sequence=str(sequence),
        first_frame=str(sequence*960),virtual_start_ns=str(sequence*20000000),sample_rate=48000,
        channels=2,format='s16le',frame_count=frames,discontinuity=sequence==0,eos=eos)
    return AudioPacket(header,struct.pack('<hh',-32768,32767)*frames)


@pytest.fixture
def view():
    browser=Browser(0)
    try:yield browser
    finally:browser.close()


@pytest.mark.parametrize('engine',['chromium','firefox'])
def test_worklet_queue_sample_conversion_underrun_overflow_and_short_eos(view,engine):
    with sync_playwright()as p:
        browser=getattr(p,engine).launch(headless=True)
        try:
            page=browser.new_page();page.goto(view.url)
            result=page.evaluate('''async () => {
              const {PCMQueue} = await import('/audio-buffer.js');
              const retired = [], q = new PCMQueue(s => retired.push(s));
              const pcm = frames => { const b = new ArrayBuffer(frames*4), d = new DataView(b);
                for (let i=0;i<frames;i++) { d.setInt16(i*4,-32768,true);d.setInt16(i*4+2,32767,true); } return b; };
              const push = (seq, frames=960, eos=false) => q.push({pcm:pcm(frames),frames,sequence:String(seq),epoch:'1',eos});
              push(0); push(1);
              let left=new Float32Array(128),right=new Float32Array(128);q.render(left,right);
              const prebuffer = Array.from(left).every(x=>x===0) && q.frames===1920;
              push(2);q.render(left,right);
              const endpoints=[left[0],right[0]];
              for(let i=0;i<23;i++) q.render(left,right);
              const underrun=q.underrun;
              q.reset();push(3,2,true);q.render(left,right);
              const tail=[left[0],right[0],left[1],right[1],left[2]];
              q.reset(); for(let i=4;i<15;i++)push(i);
              const overflow=q.stats();q.reset('2');
              return {prebuffer,endpoints,underrun,tail,overflow,reset:q.stats(),retired};
            }''')
            assert result['prebuffer'] and result['endpoints']==[-1,32767/32768]
            assert result['underrun']==192
            assert result['tail']==[-1,32767/32768,-1,32767/32768,0]
            assert result['overflow']['buffered']<=2880 and result['overflow']['chunks']<=10
            assert result['overflow']['dropped']>0 and result['reset']['buffered']==0
        finally:browser.close()


def wait_for_audio_listener(page, view):
    deadline=time.monotonic()+5
    while len(view.audio_hub.clients)!=1 and time.monotonic()<deadline:
        page.wait_for_timeout(20)
    assert len(view.audio_hub.clients)==1


def wait_for_initial_render(page):
    page.wait_for_function("() => handset3D?.renders > 0 || document.getElementById('view-state').textContent.startsWith('3D handset unavailable')",timeout=30000)


@pytest.mark.parametrize('engine',['chromium','firefox'])
def test_short_paused_prefix_is_consumed_without_eos_or_client_timeout(view,engine):
    with sync_playwright()as p:
        browser=getattr(p,engine).launch(headless=True)
        try:
            page=browser.new_page();page.goto(view.url);page.locator('header').click()
            page.wait_for_function("() => window.db2010Audio?.context?.state === 'running'")
            wait_for_initial_render(page);wait_for_audio_listener(page,view)
            clients=set(view.audio_hub.clients)
            view.audio(packet(frames=41))
            page.wait_for_function('() => window.db2010Audio.stats.played === 41',timeout=5000)
            page.wait_for_timeout(600)
            assert set(view.audio_hub.clients)==clients
            stats=page.evaluate('({...window.db2010Audio.stats})')
            assert stats['received']==stats['played']==41 and stats['buffered']==0
            assert stats['dropped']==0 and not stats['eos']
        finally:browser.close()


@pytest.mark.parametrize('engine',['chromium','firefox'])
@pytest.mark.parametrize('gesture',['pointer','keyboard'])
def test_default_audio_unlocks_on_normal_input_and_reconnects(view,engine,gesture):
    with sync_playwright()as p:
        options={'args':['--autoplay-policy=document-user-activation-required']} if engine=='chromium' else {
            'firefox_user_prefs':{'media.autoplay.default':1,'media.autoplay.block-webaudio':True}}
        browser=getattr(p,engine).launch(headless=True,**options)
        try:
            page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(view.url)
            page.wait_for_function("() => window.db2010Audio?.context !== undefined")
            assert page.evaluate('window.db2010Audio.enabled')
            assert page.locator('.audio-controls, #audio-enable, #audio-mute, #audio-volume, #audio-status').count()==0
            assert page.evaluate('window.db2010Audio.context.state')=='suspended'
            view.audio(packet(0)) # Blocked contexts do not subscribe/replay old samples.
            if gesture=='pointer':page.locator('header').click()
            else:page.keyboard.press('Shift')
            page.wait_for_function("() => window.db2010Audio.context.state === 'running'",timeout=10000)
            # Initial 3D setup can block the main thread beyond the audio
            # deadline. Exercise live playback once that unrelated load settles.
            wait_for_initial_render(page)
            wait_for_audio_listener(page,view)
            assert page.evaluate('window.db2010Audio.context.sampleRate')==48000
            for i in range(30):
                view.audio(packet(i,eos=i==29));page.wait_for_timeout(20)
            page.wait_for_function('() => window.db2010Audio.stats.played > 0',timeout=5000)
            assert page.evaluate('window.db2010Audio.stats.received')<=30*960
            wait_for_audio_listener(page,view)
            # A presentation disconnect reconnects automatically with no UI toggle.
            async def disconnect():
                await asyncio.gather(*(c.ws.close()for c in tuple(view.audio_hub.clients)))
            asyncio.run_coroutine_threadsafe(disconnect(),view.loop).result(5)
            wait_for_audio_listener(page,view)
            assert page.evaluate('window.db2010Audio.enabled')
            assert not errors,errors
        finally:browser.close()


@pytest.mark.parametrize('engine',['chromium','firefox'])
def test_audio_starts_without_interaction_when_autoplay_is_allowed(view,engine):
    with sync_playwright()as p:
        options={'args':['--autoplay-policy=no-user-gesture-required']} if engine=='chromium' else {
            'firefox_user_prefs':{'media.autoplay.default':0,'media.autoplay.block-webaudio':False}}
        browser=getattr(p,engine).launch(headless=True,**options)
        try:
            page=browser.new_page();page.goto(view.url)
            page.wait_for_function("() => window.db2010Audio?.context?.state === 'running'",timeout=10000)
            wait_for_initial_render(page)
            wait_for_audio_listener(page,view)
            for i in range(5):
                view.audio(packet(i,eos=i==4));page.wait_for_timeout(20)
            page.wait_for_function('() => window.db2010Audio.stats.played > 0',timeout=5000)
        finally:browser.close()


def test_main_thread_stall_does_not_interrupt_worker_audio(view):
    from concurrent.futures import ThreadPoolExecutor
    with sync_playwright()as p:
        browser=p.chromium.launch(headless=True,args=['--autoplay-policy=no-user-gesture-required'])
        try:
            page=browser.new_page();page.goto(view.url);wait_for_initial_render(page)
            page.wait_for_function("() => window.db2010Audio?.context?.state === 'running'")
            wait_for_audio_listener(page,view);previous=set(view.audio_hub.clients)
            def produce():
                for i in range(30):
                    view.audio(packet(i,eos=i==29));time.sleep(.02)
            with ThreadPoolExecutor(max_workers=1)as executor:
                future=executor.submit(produce)
                page.evaluate("() => { const end=performance.now()+700; while(performance.now()<end) {} }")
                future.result()
            page.wait_for_function('() => window.db2010Audio.stats.played === 28800',timeout=5000)
            assert set(view.audio_hub.clients)==previous
            assert page.evaluate('window.db2010Audio.stats.received')==28800
            assert page.evaluate('window.db2010Audio.stats.dropped')==0
        finally:browser.close()


def test_audio_origin_and_unconsumed_client_deadline(view):
    async def check():
        async with ClientSession()as session:
            with pytest.raises(WSServerHandshakeError):
                await session.ws_connect(view.url+'/audio',headers={'Origin':'http://evil.test'})
            async with session.ws_connect(view.url+'/audio',headers={'Origin':view.url})as ws:
                hello=await ws.receive_json();assert hello['type']=='audio-ready'
                view.audio(packet())
                message=await ws.receive();assert isinstance(message.data,bytes)
                # No worklet acknowledgement: this listener must be disconnected.
                until=time.monotonic()+2
                while not ws.closed and time.monotonic()<until:
                    await asyncio.wait_for(ws.receive(),2)
                assert ws.closed
            # Status/UART/display remain independently available.
            async with session.ws_connect(view.url+'/ws',headers={'Origin':view.url})as ws:
                assert (await ws.receive_json())['type']=='status'
    asyncio.run(check())


def test_send_deadline_and_unconsumed_packet_credit_are_bounded():
    from db2010.ui.audio import AudioClient
    class Socket:
        closed=False
        sent=0
        delay=0
        async def send_json(self,data):pass
        async def send_bytes(self,data):
            self.sent+=1
            await asyncio.sleep(self.delay)
        async def close(self,**kwargs):self.closed=True
    async def check():
        slow=Socket();slow.delay=1
        client=AudioClient(slow);client.put(packet())
        started=time.monotonic()
        with pytest.raises(TimeoutError):await client.send()
        assert time.monotonic()-started<.5
        normal=Socket();client=AudioClient(normal)
        for i in range(10):client.put(packet(i))
        sender=asyncio.create_task(client.send())
        await asyncio.sleep(.02)
        assert normal.sent==10 and len(client.outstanding)==10
        for i in range(10,20):client.put(packet(i))
        assert client.pending.qsize()==10
        client.put(packet(20))
        await asyncio.sleep(.02)
        assert normal.closed and normal.sent==10
        await sender
        with pytest.raises(ValueError):
            client.ack(dict(type='audio-ack',epoch=client.epoch,sequence='99999'))
    asyncio.run(check())


def test_ingress_burst_schedules_one_wake_and_keeps_at_most_ten_packets():
    from db2010.ui.audio import AudioHub
    class Loop:
        def __init__(self):self.callbacks=[]
        def call_soon_threadsafe(self,callback):self.callbacks.append(callback)
    class View:
        loop=Loop()
    hub=AudioHub(View())
    for i in range(1000):hub.publish(packet(i))
    assert len(hub.pending)<=10 and len(hub.browser.loop.callbacks)==1 and hub.dropped>0
    hub.deliver()
    assert not hub.pending and not hub.scheduled


@pytest.mark.parametrize('engine',['chromium','firefox'])
def test_new_native_stream_starts_at_first_sample_without_reconnecting(view,engine):
    with sync_playwright()as p:
        options={'args':['--autoplay-policy=no-user-gesture-required']} if engine=='chromium' else {
            'firefox_user_prefs':{'media.autoplay.default':0,'media.autoplay.block-webaudio':False}}
        browser=getattr(p,engine).launch(headless=True,**options)
        try:
            page=browser.new_page();page.goto(view.url);wait_for_initial_render(page)
            page.wait_for_function("() => window.db2010Audio?.context?.state === 'running'")
            wait_for_audio_listener(page,view)
            original=set(view.audio_hub.clients)
            for epoch in range(1,4):
                for sequence in range(5):
                    source=packet(sequence,eos=sequence==4)
                    view.audio(AudioPacket(dict(source.header,epoch=str(epoch)),source.pcm))
                    page.wait_for_timeout(20)
                page.wait_for_function('(n) => window.db2010Audio.stats.played === n',arg=epoch*4800,timeout=5000)
            assert set(view.audio_hub.clients)==original
            assert page.evaluate('window.db2010Audio.stats.received')==14400
            assert page.evaluate('window.db2010Audio.stats.dropped')==0
            assert not page.evaluate('window.db2010Audio.stats.error')
        finally:browser.close()


def test_epoch_switch_discards_old_pending_pcm_and_ignores_its_late_ack():
    from db2010.ui.audio import AudioClient
    class Socket:
        closed=False
    client=AudioClient(Socket())
    client.put(packet())
    old=client.epoch
    changed=packet()
    client.put(AudioPacket(dict(changed.header,epoch='2'),changed.pcm))
    assert client.pending.qsize()==1 and client.epoch!=old and client.first
    client.ack(dict(type='audio-ack',epoch=old,sequence='0'))
    assert not client.closing and client.last_consumed is None


def test_first_packet_gets_its_own_consumption_deadline_after_idle():
    from db2010.ui.audio import AudioClient
    class Socket:
        closed=False
        async def send_json(self,data):pass
        async def send_bytes(self,data):pass
        async def close(self,**kwargs):self.closed=True
    async def check():
        socket=Socket();client=AudioClient(socket)
        client.last_ack=time.monotonic()-10
        client.put(packet())
        sender=asyncio.create_task(client.send())
        try:
            await asyncio.sleep(.01)
            assert not socket.closed and len(client.outstanding)==1
            assert time.monotonic()-client.last_ack<.5
        finally:
            sender.cancel();await asyncio.gather(sender,return_exceptions=True)
    asyncio.run(check())
