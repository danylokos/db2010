"""Real loopback/WebSocket/Chromium views of bounded execution observations."""
import asyncio
import json
import socket
import time

import pytest
from aiohttp import ClientSession, WSServerHandshakeError, WSMsgType
from playwright.sync_api import sync_playwright, expect

from db2010.ui.browser import Browser, UART_TAIL, Outbox, display_message
from db2010.ui.pixels import rgb565


@pytest.fixture
def view():
    browser = Browser(0)
    try:
        yield browser
    finally:
        browser.close()
    assert not browser.thread.is_alive()


def panel(raw, frames=1, known=176*220):
    return dict(width=176, height=220, pixel_format='rgb565-be', known_pixels=known,
                frames=frames, completed=frames*10, pixels_hex=raw.hex())


def test_rgb565_validates_and_converts():
    assert rgb565(bytes.fromhex('f80007e0001ff79e'),4,1) == bytes([255,0,0,0,255,0,0,0,255,246,242,246])
    with pytest.raises(ValueError):
        rgb565(b'',176,220)
    with pytest.raises(ValueError):
        rgb565(b'\0\0',1,1,'native')


def test_browser_canvas_uart_refresh_reconnect_and_multiple_clients(view, tmp_path):
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            first = chromium.new_page()
            first.goto(view.url)
            first.wait_for_function("() => document.getElementById('connection').getAttribute('aria-label') === 'Connected'")
            assert first.locator('#unknown').is_visible()
            assert first.locator('#cpu-frequency').count() == 0
            view.display(panel(b'',known=8))
            first.wait_for_timeout(350)
            assert first.locator('#unknown').is_visible()
            # Native byte order, including non-primary gray conversion.
            raw = bytes.fromhex('f80007e0001ff79e')+bytes(176*220*2-8)
            view.phase('run')
            view.display(panel(raw,frames=2))
            view.progress(dict(observations=123456, pc=0x44020f50, lcd_frames=2, uart_bytes=[0,0,0,0], cpu_hz=104000000))
            view.uart(0,b'<script>bad()</script>\x1b[31m\r\nUTF-8: \xe2')
            view.uart(0,b'\x82\xac\xff\npartial')
            view.uart(1,b'port one')
            view.uart(2,b'\x00\x09\x0a\x0d\x1b\x7f\x80\xff<>&A0123~')
            view.uart(3,b'port three')
            first.wait_for_function("() => document.getElementById('unknown').hidden")
            assert first.locator('#cpu-frequency').count() == 0
            pixels = first.evaluate("Array.from(document.getElementById('lcd').getContext('2d').getImageData(0,0,4,1).data)")
            assert pixels == [255,0,0,255,0,255,0,255,0,0,255,255,246,242,246,255]
            first.wait_for_function("() => document.getElementById('uart').textContent.includes('partial')")
            assert first.locator('#uart').text_content() == '<script>bad()</script>\\x1b[31m\nUTF-8: €�\npartial'
            assert first.locator('#uart script').count() == 0
            first.select_option('#port','1')
            assert first.locator('#uart').text_content() == 'port one'
            first.select_option('#port','2')
            assert first.locator('#uart').text_content().splitlines() == [
                '00000000  00 09 0a 0d 1b 7f 80 ff  3c 3e 26 41 30 31 32 33  |........<>&A0123|',
                '00000010  7e                                                |~|',
            ]
            assert first.locator('#uart').evaluate("box => getComputedStyle(box).whiteSpace") == 'pre'
            view.uart(2,b'x'*UART_TAIL)
            expect(first.locator('#truncated')).to_contain_text('Earlier bytes omitted')
            assert first.locator('#truncated').is_visible()
            assert 'Earlier bytes omitted' in first.locator('#truncated').text_content()
            lines = first.locator('#uart').text_content().splitlines()
            assert len(lines) == UART_TAIL//16
            assert lines[0] == '00000011  78 78 78 78 78 78 78 78  78 78 78 78 78 78 78 78  |xxxxxxxxxxxxxxxx|'
            assert lines[-1].startswith('00010001  ')
            first.select_option('#port','3')
            assert first.locator('#uart').text_content() == 'port three'
            assert first.locator('#uart').evaluate("box => getComputedStyle(box).whiteSpace") == 'pre-wrap'
            second = chromium.new_page()
            second.goto(view.url)
            second.wait_for_function("() => document.getElementById('unknown').hidden")
            assert second.locator('#cpu-frequency').count() == 0
            assert second.evaluate("Array.from(document.getElementById('lcd').getContext('2d').getImageData(0,0,1,1).data)") == [255,0,0,255]
            first.reload()
            first.wait_for_function("() => document.getElementById('uart').textContent.includes('partial')")
            assert first.locator('#cpu-frequency').count() == 0
            async def disconnect():
                await asyncio.gather(*(ws.close() for ws in list(view.clients)))
            asyncio.run_coroutine_threadsafe(disconnect(), view.loop).result(5)
            first.wait_for_function("() => document.getElementById('connection').getAttribute('aria-label') === 'Connected'")
            view.finish({'success':True,'phases':{'emulation_seconds':2},'snapshot':{
                'cpu_hz':52000000, 'stop':{'reason':'instruction_limit'}}},None)
            second.wait_for_function("() => document.getElementById('outcome').textContent === 'instruction_limit'")
            assert second.locator('#cpu-frequency').count() == 0
            first.screenshot(path=str(tmp_path/'browser.png'))
        finally:
            chromium.close()


@pytest.mark.parametrize('known', [None, 0, 8])
def test_stats_tick_before_first_lcd_and_between_samples(view, known):
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = chromium.new_page()
            page.goto(view.url)
            page.wait_for_function("() => document.getElementById('connection').getAttribute('aria-label') === 'Connected'")
            view.phase('run')
            if known is not None:
                view.display(panel(b'', frames=0, known=known))
            # No progress callback and no complete LCD: the execution clock
            # still advances at the server's publication cadence.
            page.wait_for_function("() => parseFloat(document.getElementById('elapsed').textContent) >= 0.5", timeout=3000)
            assert page.locator('#unknown').is_visible()
            view.progress(dict(observations=100000000, pc=0x44020f50, lcd_frames=0, uart_bytes=[0]*4))
            page.wait_for_function("() => document.getElementById('observations').textContent === '100.0 M'")
            assert page.locator('#pc').text_content() == '44020F50'
            view.progress(dict(observations=100900000, pc=0x44020f54, lcd_frames=0, uart_bytes=[0]*4))
            page.wait_for_function("() => document.getElementById('observations').textContent === '100.9 M'")
            assert page.locator('#pc').text_content() == '44020F54'
            assert float(page.locator('#rate').text_content().split()[0]) > 0
            assert page.locator('#unknown').is_visible()
            # Final-view waiting must not extend execution time.
            view.finish({'success': True, 'phases': {}, 'snapshot': {'stop': {'reason': 'instruction_limit'}}}, None)
            page.wait_for_function("() => document.getElementById('outcome').textContent === 'instruction_limit'")
            elapsed = page.locator('#elapsed').text_content()
            page.wait_for_timeout(600)
            assert page.locator('#elapsed').text_content() == elapsed
            assert page.locator('.uart summary').text_content() == 'UART console'
        finally:
            chromium.close()


def test_server_assets_origin_and_inbound_commands(view):
    async def check():
        async with ClientSession() as session:
            for path in ('/', '/app.css', '/app.js'):
                async with session.get(view.url+path) as response:
                    assert response.status == 200
                    assert "default-src 'self'" in response.headers['Content-Security-Policy']
            for path in ('/etc/passwd', '/cli.py', '/assets/../cli.py'):
                async with session.get(view.url+path) as response:
                    assert response.status == 404
            for origin in (None, 'http://evil.test', view.url+'.evil.test'):
                with pytest.raises(WSServerHandshakeError) as error:
                    await session.ws_connect(view.url+'/ws', headers={'Origin':origin} if origin else {})
                assert error.value.status == 403
            async with session.ws_connect(view.url+'/ws', headers={'Origin':view.url}) as ws:
                assert (await ws.receive_json())['type'] == 'status'
                assert len((await ws.receive_json())['uart']) == 4
                frame = await ws.receive()
                assert frame.type == WSMsgType.BINARY
                assert decode_frame(frame.data) == ({'type': 'display', 'panel': None}, b'')
                await ws.send_str('continue')
                await ws.receive()
                assert ws.close_code == 1008
    asyncio.run(check())


def decode_frame(message):
    length = int.from_bytes(message[:4], 'big')
    return json.loads(message[4:4+length]), message[4+length:]


def test_binary_frame_metadata_and_unknown_pixels():
    raw = bytes.fromhex('f80007e0001ff79e')+bytes(176*220*2-8)
    header, pixels = decode_frame(display_message(panel(raw)))
    assert header['type'] == 'display' and 'pixels_hex' not in header['panel']
    assert pixels == raw and header['panel']['known_pixels'] == 176*220
    for known in (0, 8):
        header, pixels = decode_frame(display_message(panel(b'', known=known)))
        assert header['panel']['known_pixels'] == known and pixels == b''
    with pytest.raises(ValueError, match='size differs'):
        display_message(panel(b'bad'))


def test_outbox_bounds_and_fairness():
    async def check():
        queue = Outbox()
        queue.put('display', b'first')
        queue.put('status', 'status')
        queue.put('uart', 'uart')
        for i in range(10000):
            queue.put('display', str(i).encode())
        assert len(queue.pending) == 3
        assert await queue.get() == b'9999'
        queue.put('display', b'next')
        assert await queue.get() == 'status'
        queue.put('status', 'new status')
        assert await queue.get() == 'uart'
        assert await queue.get() == b'next'
        assert await queue.get() == 'new status'
        assert not queue.ready.is_set()
    asyncio.run(check())


def test_slow_client_send_deadline_isolated_and_pending_bounded(view):
    class SlowSocket:
        async def send_bytes(self, message):
            await asyncio.Future()
        send_str = send_bytes
        async def close(self, code, **kwargs):
            self.code = code
    async def check():
        slow = SlowSocket()
        queue = Outbox()
        view.clients[slow] = queue
        queue.put('display', b'blocked')
        sender = asyncio.create_task(view._send(slow, queue))
        for i in range(180):
            view.display(panel(bytes(176*220*2), frames=i))
            view.uart(0, b'x'*4096)
            await asyncio.sleep(.001)
            assert len(queue.pending) <= 3
        await asyncio.wait_for(sender, 3)
        assert slow.code == 1013 and slow not in view.clients
    asyncio.run_coroutine_threadsafe(check(), view.loop).result(6)
    assert len(view.tails[0]) == UART_TAIL and view.totals[0] == 180*4096


def test_port_is_exclusive_before_runner_preparation(view):
    with pytest.raises(OSError):
        Browser(int(view.url.rsplit(':',1)[1]))


def test_real_slow_socket_does_not_delay_chromium(view):
    # Stop consuming a real TCP WebSocket after the HTTP upgrade. A deliberately
    # small receive buffer reaches transport backpressure while Chromium reads.
    slow=socket.socket()
    slow.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024)
    slow.connect(('127.0.0.1',int(view.url.rsplit(':',1)[1])))
    slow.sendall((f'GET /ws HTTP/1.1\r\nHost: {view.url[7:]}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                  f'Sec-WebSocket-Key: MDEyMzQ1Njc4OWFiY2RlZg==\r\nSec-WebSocket-Version: 13\r\nOrigin: {view.url}\r\n\r\n').encode())
    slow.settimeout(3)
    assert b'101 Switching Protocols' in slow.recv(4096)
    with sync_playwright() as playwright:
        chromium=playwright.chromium.launch(headless=True,args=['--no-sandbox'])
        try:
            page=chromium.new_page();page.goto(view.url)
            page.wait_for_function("() => document.getElementById('connection').getAttribute('aria-label') === 'Connected'")
            for i in range(35):
                for port in range(4):
                    view.uart(port,b'z'*UART_TAIL)
                view.progress(dict(observations=i,pc=0x1000,lcd_frames=0,uart_bytes=view.totals[:]))
                page.wait_for_timeout(260)
            page.wait_for_function("() => document.getElementById('observations').textContent === '34.0'")
            async def client_count():
                return len(view.clients)
            count=asyncio.run_coroutine_threadsafe(client_count(),view.loop).result(3)
            assert count == 1  # Only healthy Chromium remains registered.
        finally:
            slow.close()
            chromium.close()


def test_shutdown_is_bounded_with_unresponsive_websocket():
    view=Browser(0)
    slow=socket.socket()
    try:
        slow.connect(('127.0.0.1',int(view.url.rsplit(':',1)[1])))
        slow.sendall((f'GET /ws HTTP/1.1\r\nHost: {view.url[7:]}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                      f'Sec-WebSocket-Key: MDEyMzQ1Njc4OWFiY2RlZg==\r\nSec-WebSocket-Version: 13\r\nOrigin: {view.url}\r\n\r\n').encode())
        slow.settimeout(3)
        assert b'101 Switching Protocols' in slow.recv(4096)
        started=time.monotonic()
        view.close()
        assert time.monotonic()-started < 3
        assert not view.thread.is_alive()
    finally:
        slow.close()
        view.close()


def test_closed_listener_can_restart_on_same_port():
    view=Browser(0)
    port=int(view.url.rsplit(':',1)[1])
    async def check():
        async with ClientSession() as session:
            async with session.ws_connect(view.url+'/ws',headers={'Origin':view.url}) as ws:
                await ws.receive_json()
                await asyncio.to_thread(view.close)
    try:
        asyncio.run(check())
        replacement=Browser(port)
        replacement.close()
    finally:
        view.close()


@pytest.mark.parametrize('authority', ['localhost:8080', '127.0.0.1:8080', '[::1]:8080'])
def test_websocket_accepts_matching_forwarded_loopback_origin(view, authority):
    async def check():
        async with ClientSession() as session:
            async with session.ws_connect(view.url+'/ws',
                    headers={'Host':authority,'Origin':'http://'+authority}) as ws:
                assert (await ws.receive_json())['status']['phase'] == 'prepare'
    asyncio.run(check())


@pytest.mark.parametrize('authority,origin', [
    ('localhost:8080', 'http://localhost:8765'),
    ('127.0.0.1:8080', 'http://localhost:8080'),
    ('evil.test:8080', 'http://evil.test:8080'),
    ('localhost.evil.test:8080', 'http://localhost.evil.test:8080'),
    ('localhost:8080', 'null'),
    ('localhost:8080', None),
])
def test_forwarded_websocket_still_rejects_foreign_origins_and_hosts(view, authority, origin):
    async def check():
        headers={'Host':authority}
        if origin is not None:
            headers['Origin']=origin
        async with ClientSession() as session:
            with pytest.raises(WSServerHandshakeError) as error:
                await session.ws_connect(view.url+'/ws',headers=headers)
            assert error.value.status == 403
    asyncio.run(check())


@pytest.mark.parametrize('engine', ['chromium', 'firefox'])
@pytest.mark.parametrize('host', ['localhost', '127.0.0.1'])
def test_browser_connects_through_tcp_port_forward(view, engine, host):
    import select
    import socketserver
    import threading

    backend_port=int(view.url.rsplit(':',1)[1])
    class Forward(socketserver.BaseRequestHandler):
        def handle(self):
            with socket.create_connection(('127.0.0.1',backend_port)) as backend:
                peers=(self.request,backend)
                while True:
                    readable,_,_=select.select(peers,[],[],5)
                    if not readable:
                        return
                    for source in readable:
                        data=source.recv(65536)
                        if not data:
                            return
                        target=backend if source is self.request else self.request
                        target.sendall(data)
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads=True
    with Server(('127.0.0.1',0),Forward) as forward:
        thread=threading.Thread(target=forward.serve_forever)
        thread.start()
        try:
            with sync_playwright() as playwright:
                chromium=getattr(playwright, engine).launch(headless=True)
                try:
                    page=chromium.new_page()
                    forwarded=f'http://{host}:{forward.server_address[1]}'
                    assert int(forwarded.rsplit(':',1)[1]) != backend_port
                    page.goto(forwarded)
                    page.wait_for_function("() => document.getElementById('connection').getAttribute('aria-label') === 'Connected'",timeout=5000)
                    view.input.enable(True);view.phase('run')
                    page.wait_for_function("() => !document.querySelector('[data-key]').disabled")
                    page.keyboard.press('Shift+Enter');page.wait_for_timeout(50)
                    assert [(b,d) for b,d,t in view.input.take()]==[('walkman',True),('walkman',False)]
                    view.uart(0,b'forwarded live UART\n')
                    view.display(panel(bytes.fromhex('f800')*(176*220)))
                    page.wait_for_function("() => document.getElementById('uart').textContent.includes('forwarded live UART')")
                    page.wait_for_function("() => document.getElementById('unknown').hidden")
                    assert page.evaluate("Array.from(document.getElementById('lcd').getContext('2d').getImageData(0,0,1,1).data)") == [255,0,0,255]
                finally:
                    chromium.close()
        finally:
            forward.shutdown()
            thread.join(5)
            assert not thread.is_alive()


@pytest.mark.parametrize('engine', ['chromium', 'firefox'])
@pytest.mark.parametrize('fault', ['handshake', 'first-update'])
def test_stalled_browser_connection_retries_and_receives_cached_state(engine, fault):
    from aiohttp import web
    import threading

    class StalledBrowser(Browser):
        def __init__(self):
            self.attempts = 0
            self.stalled = threading.Event()
            self.release = asyncio.Event()
            super().__init__(0)

        async def _ws(self, request):
            self.attempts += 1
            if fault == 'handshake' and self.attempts == 1:
                self.stalled.set()
                await self.release.wait()
                return web.Response(status=503)
            return await super()._ws(request)

        async def _send(self, ws, queue):
            if fault == 'first-update' and self.attempts == 1:
                self.stalled.set()
                await self.release.wait()
                return
            await super()._send(ws, queue)

    view = StalledBrowser()
    try:
        view.progress(dict(observations=123456, pc=0x44020f50))
        view.uart(0, b'cached before connection\n')
        with sync_playwright() as playwright:
            browser = getattr(playwright, engine).launch(headless=True)
            try:
                page = browser.new_page()
                page.clock.install()
                page.goto(view.url)
                assert view.stalled.wait(5)
                if fault == 'first-update':
                    expect(page.locator('#connection')).to_have_attribute('aria-label', 'Connected')
                assert page.locator('#observations').text_content() == '—'
                page.clock.fast_forward(5001)
                expect(page.locator('#connection')).to_have_attribute('aria-label', 'Connection timed out; reconnecting', timeout=2000)
                page.clock.fast_forward(1001)
                expect(page.locator('#observations')).to_have_text('123.5 k', timeout=5000)
                assert page.locator('#connection').get_attribute('aria-label') == 'Connected'
                assert page.locator('#uart').text_content() == 'cached before connection\n'
                assert view.attempts == 2
                # Late completion of the discarded request cannot replace the
                # current socket or schedule another retry.
                view.loop.call_soon_threadsafe(view.release.set)
                page.clock.fast_forward(10000)
                page.wait_for_timeout(100)
                assert view.attempts == 2
                assert page.locator('#connection').get_attribute('aria-label') == 'Connected'
            finally:
                browser.close()
    finally:
        view.loop.call_soon_threadsafe(view.release.set)
        view.close()


def test_latest_pending_frame_final_flush_and_independent_uart_rendering(view):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(view.url)
            page.wait_for_function('() => inputSocket?.readyState === WebSocket.OPEN')
            # Hold animation callbacks to check that a burst retains one latest
            # frame and schedules one draw. Independent JSON dispatch must not
            # touch UART DOM when only status or display changes.
            page.evaluate("""() => {
              window.rafCallbacks = [];
              window.requestAnimationFrame = cb => rafCallbacks.push(cb);
              window.uartRenders = 0;
              const original = renderUART;
              renderUART = () => { uartRenders++; original(); };
            }""")
            page.wait_for_timeout(50)
            page.evaluate('() => { rafCallbacks.splice(0).forEach(cb => cb()); uartRenders = 0; }')
            page.evaluate("""() => {
              const packet = color => {
                const raw = new Uint8Array(176*220*2);
                raw[0] = color >> 8; raw[1] = color & 255;
                const header = new TextEncoder().encode(JSON.stringify({type:'display', panel:{
                  width:176, height:220, pixel_format:'rgb565-be', known_pixels:176*220}}));
                const buffer = new ArrayBuffer(4+header.length+raw.length);
                new DataView(buffer).setUint32(0, header.length, false);
                new Uint8Array(buffer,4,header.length).set(header);
                new Uint8Array(buffer,4+header.length).set(raw);
                return buffer;
              };
              for (const color of [0xf800, 0x07e0, 0x001f]) receive(packet(color));
              window.pendingCount = rafCallbacks.length;
              rafCallbacks.splice(0).forEach(cb => cb());
            }""")
            assert page.evaluate('pendingCount') == 1
            assert page.evaluate("Array.from(el('lcd').getContext('2d').getImageData(0,0,1,1).data)") == [0,0,255,255]
            assert page.evaluate('pendingDisplay') is None
            assert page.evaluate('uartRenders') == 0
            # Malformed lengths and a missing binary type are rejected by the decoder.
            assert page.evaluate("""() => {
              let rejected = 0;
              for (const bytes of [[0,0,0], [0,0,0,99], [0,0,0,2,123,125]]) {
                try { decodeDisplay(decodeBinary(new Uint8Array(bytes).buffer)); } catch { rejected++; }
              }
              return rejected;
            }""") == 3
            # Unknown metadata supersedes a complete pending frame too.
            view.display(panel(b'', frames=10, known=8))
            page.wait_for_function('() => pendingDisplay?.panel?.frames === 10')
            page.evaluate('() => rafCallbacks.splice(0).forEach(cb => cb())')
            assert page.locator('#unknown').is_visible()
            # Finish flushes the last UART and panel even between periodic ticks.
            view.display(panel(bytes.fromhex('07e0')*(176*220), frames=11))
            view.uart(0, b'last partial')
            view.finish({'success': True, 'phases': {}, 'snapshot': {'stop': {'reason': 'final'}}}, None)
            page.wait_for_function("() => el('outcome').textContent === 'final' && el('uart').textContent === 'last partial' && pendingDisplay?.panel?.frames === 11")
            page.evaluate('() => rafCallbacks.splice(0).forEach(cb => cb())')
            assert page.evaluate("Array.from(el('lcd').getContext('2d').getImageData(0,0,1,1).data)") == [0,255,0,255]
            assert page.evaluate('uartRenders') == 1
        finally:
            browser.close()


@pytest.mark.parametrize('engine', ['chromium', 'firefox'])
def test_websocket_constructor_exception_recovers_without_staying_connecting(view, engine):
    view.progress(dict(observations=500123456, pc=0x44020f50))
    with sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch(headless=True)
        try:
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script("""
                const NativeWebSocket = window.WebSocket;
                window.socketAttempts = 0;
                window.WebSocket = class extends NativeWebSocket {
                  constructor(...args) {
                    if (++window.socketAttempts === 1) {
                      throw new DOMException('Blocked by browser', 'SecurityError');
                    }
                    super(...args);
                  }
                };
            """)
            page.clock.install()
            page.goto(view.url)
            expect(page.locator('#connection')).to_have_attribute(
                'aria-label', 'Connection failed: SecurityError; reconnecting')
            page.clock.fast_forward(1001)
            expect(page.locator('#observations')).to_have_text('500.1 M')
            expect(page.locator('#connection')).to_have_attribute('aria-label', 'Connected')
            assert page.evaluate('window.socketAttempts') == 2
            assert not errors
        finally:
            browser.close()


def test_firefox_native_policy_exception_does_not_leave_connecting():
    class BlockedBrowser(Browser):
        async def _asset(self, request):
            response = await super()._asset(request)
            if request.path == '/':
                response.headers['Content-Security-Policy'] = "default-src 'self'; connect-src 'none'"
            return response
    view = BlockedBrowser(0)
    try:
        with sync_playwright() as playwright:
            browser = playwright.firefox.launch(headless=True)
            try:
                page = browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.clock.install()
                page.goto(view.url)
                expect(page.locator('#connection')).to_have_class('connection-dot disconnected')
                page.clock.fast_forward(1001)
                expect(page.locator('#connection')).to_have_class('connection-dot disconnected')
                assert not errors
                assert not view.clients  # Policy failure occurs before upgrade.
            finally:
                browser.close()
    finally:
        view.close()


@pytest.mark.parametrize('authority', ['localhost:8080', '127.0.0.1:8765', '[::1]:8081'])
def test_asset_policy_explicitly_allows_only_forwarded_websocket_authority(view, authority):
    async def check():
        async with ClientSession() as session:
            async with session.get(view.url, headers={'Host': authority}) as response:
                assert response.status == 200
                policy = response.headers['Content-Security-Policy']
                connect = next(part.strip() for part in policy.split(';') if part.strip().startswith('connect-src'))
                assert connect == "connect-src 'self' ws://"+authority+" blob:"
    asyncio.run(check())


@pytest.mark.parametrize('authority', ['evil.test:8080', 'localhost:8080;foo',
                                      'localhost:8080/path', 'user@localhost:8080'])
def test_asset_policy_rejects_foreign_or_malformed_authorities(view, authority):
    async def check():
        async with ClientSession() as session:
            async with session.get(view.url, headers={'Host': authority}) as response:
                assert response.status == 403
    asyncio.run(check())
