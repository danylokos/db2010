"""Loopback-only observation server with bounded caches and browser outboxes."""
import asyncio
import base64
from importlib.resources import files
import json
import ipaddress
import socket
import threading
import time
from urllib.parse import urlsplit

from aiohttp import web, WSMsgType
from .keypad import KeypadInput, InputOverflow
from .audio import AudioHub
from .files import FilesAPI

from ..execution.schedule import Schedule, PUBLICATION_INTERVAL

UART_TAIL = 64 * 1024
MAX_CLIENTS = 16
ASSETS = {'/': ('index.html', 'text/html'), '/app.css': ('app.css', 'text/css'),
          '/app.js': ('app.js', 'text/javascript'),
          '/audio-client.js': ('audio-client.js', 'text/javascript'),
          '/audio-transport.js': ('audio-transport.js', 'text/javascript'),
          '/audio-buffer.js': ('audio-buffer.js', 'text/javascript'),
          '/audio-worklet.js': ('audio-worklet.js', 'text/javascript'),
          '/handset3d.js': ('handset3d.js', 'text/javascript'),
          '/files.js': ('files.js', 'text/javascript'),
          '/model/w800i.glb': ('model/w800i.glb', 'model/gltf-binary')}
for _name in ('three.module.js', 'three.core.js', 'GLTFLoader.js', 'OrbitControls.js',
              'BufferGeometryUtils.js', 'SkeletonUtils.js'):
    ASSETS['/vendor/'+_name] = ('vendor/'+_name, 'text/javascript')


def loopback_origin(request):
    """Validate the browser's authority before using it in Origin/CSP policy.

    SSH may remap the local port. Never replace this with our listening address
    or trust forwarding headers; allow just this validated loopback authority.
    """
    try:
        authority = request.host
        parsed = urlsplit(f'{request.scheme}://{authority}')
        if (parsed.netloc != authority or parsed.path or parsed.query or parsed.fragment
                or parsed.username is not None or parsed.password is not None):
            return None
        parsed.port  # Validate syntax/range, including values used in CSP.
        if parsed.hostname != 'localhost' and not ipaddress.ip_address(parsed.hostname).is_loopback:
            return None
        return f'{request.scheme}://{authority}'
    except ValueError:
        return None


class Outbox:
    """One latest message per channel; replacement preserves fair send order."""
    def __init__(self):
        self.pending = {}
        self.ready = asyncio.Event()

    def put(self, channel, message):
        self.pending[channel] = message
        self.ready.set()

    async def get(self):
        await self.ready.wait()
        channel = next(iter(self.pending))
        message = self.pending.pop(channel)
        if not self.pending:
            self.ready.clear()
        return message


def display_message(panel):
    """A single binary WebSocket message: u32be header size, JSON, RGB565be."""
    pixels = b''
    if (panel and panel.get('known_pixels') == 176*220 and panel.get('width') == 176
            and panel.get('height') == 220 and panel.get('pixel_format') == 'rgb565-be'):
        pixels = bytes.fromhex(panel['pixels_hex'])
        if len(pixels) != 176*220*2:
            raise ValueError('RGB565 panel size differs')
    metadata = {k: v for k, v in panel.items() if k != 'pixels_hex'} if panel else None
    header = json.dumps({'type': 'display', 'panel': metadata}, ensure_ascii=True).encode('utf-8')
    return len(header).to_bytes(4, 'big') + header + pixels


class Browser:
    show_status = show_display = True

    def __init__(self, port=8765):
        # Bind synchronously: port errors happen before firmware preparation.
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind(('127.0.0.1', port))
            self.socket.listen(16)
            self.socket.setblocking(False)
        except BaseException:
            self.socket.close()
            raise
        self.url = f'http://127.0.0.1:{self.socket.getsockname()[1]}'
        self.input = KeypadInput()
        self.audio_hub = AudioHub(self)
        self.files = FilesAPI(loopback_origin)
        self.lock = threading.Lock()
        self.status = {'phase': 'prepare', 'elapsed': 0.0, 'rate': 0.0}
        self.tails = [bytearray() for _ in range(4)]
        self.totals = [0]*4
        self.panel = None
        self.display_data = display_message(None)
        self.frame = None
        self.dirty = {'status', 'uart', 'display'}
        self.wake_pending = False
        self.flush_pending = False
        self.started = self.previous = None
        self.clients = {}
        self.ready = threading.Event()
        self.error = None
        self.loop = None
        self.thread = threading.Thread(target=self._thread, name='db2010-browser')
        self.thread.start()
        self.ready.wait()
        if self.error:
            self.thread.join()
            raise self.error

    def audio(self, packet):
        self.audio_hub.publish(packet)

    async def _audio(self, request):
        return await self.audio_hub.endpoint(request, loopback_origin(request), MAX_CLIENTS)

    def phase(self, name):
        now = time.monotonic()
        with self.lock:
            if name == 'run':
                self.started = now
                self.previous = (now, 0)
            elif self.status['phase'] == 'run' and self.started is not None:
                self.status['elapsed'] = now-self.started
            self.status['phase'] = name
            self.dirty.add('status')

    def progress(self, sample):
        now = time.monotonic()
        with self.lock:
            if self.previous:
                before, count = self.previous
                self.status['rate'] = max(0, sample['observations']-count)/max(now-before, 1e-9)
            self.previous = (now, sample['observations'])
            self.status.update({key: value for key, value in sample.items()
                                if key not in ('cpu_hz', 'lcd_frames')})
            if self.status['phase'] == 'run' and self.started is not None:
                self.status['elapsed'] = now-self.started
            self.dirty.add('status')

    def uart(self, port, data):
        with self.lock:
            self.totals[port] += len(data)
            self.tails[port].extend(data[-UART_TAIL:])
            del self.tails[port][:-UART_TAIL]
            self.dirty.add('uart')

    def _notify(self):
        # At most one cross-thread wake is pending, even during a fast producer
        # burst. No frames or UART bytes accumulate on the event loop queue.
        with self.lock:
            if self.wake_pending:
                return
            self.wake_pending = True
        self.loop.call_soon_threadsafe(self._wake)

    def _wake(self):
        with self.lock:
            self.wake_pending = False
        self.changed.set()

    def display(self, panel):
        frame = tuple(panel.get(key) for key in
                      ('frames', 'completed', 'known_pixels', 'width', 'height', 'pixel_format'))
        if frame == self.frame:
            return
        message = display_message(panel)
        with self.lock:
            self.frame = frame
            self.panel = {k: v for k, v in panel.items() if k != 'pixels_hex'}
            self.display_data = message
            self.dirty.add('display')
        self._notify()

    def finish(self, result, failure):
        self.input.enable(False)
        with self.lock:
            if self.status['phase'] == 'run' and self.started is not None:
                self.status['elapsed'] = time.monotonic()-self.started
            self.status.update(phase='interrupted' if isinstance(failure, KeyboardInterrupt) else
                               ('error' if failure else 'stopped'), running=False,
                               success=result['success'], error=result.get('error'),
                               stop=result.get('snapshot', {}).get('stop'), phases=result['phases'])
            self.dirty.add('status')
            self.flush_pending = True
        self._notify()

    def _messages(self, channels, full=False):
        messages = {}
        with self.lock:
            for channel in channels:
                running = self.status['phase'] == 'run' and self.started is not None
                if not full and channel not in self.dirty and not (channel == 'status' and running):
                    continue
                if channel == 'display':
                    messages[channel] = self.display_data
                elif channel == 'status':
                    # Elapsed time ticks even while the QMP owner waits.
                    if running:
                        self.status['elapsed'] = time.monotonic()-self.started
                    messages[channel] = json.dumps({'type': 'status', 'input_enabled': self.input.enabled,
                                                    'status': dict(self.status)}, ensure_ascii=True)
                elif channel == 'uart':
                    messages[channel] = json.dumps({'type': 'uart', 'uart': [
                        {'port': port, 'total': self.totals[port],
                         'truncated': self.totals[port] > len(tail),
                         'data': base64.b64encode(tail).decode('ascii')}
                        for port, tail in enumerate(self.tails)]})
                else:
                    raise ValueError('unknown observation channel: '+channel)
                if not full:
                    self.dirty.discard(channel)
        return messages

    def _broadcast(self, channels):
        for channel, message in self._messages(channels).items():
            for outbox in self.clients.values():
                outbox.put(channel, message)

    async def _asset(self, request):
        origin = loopback_origin(request)
        if origin is None:
            raise web.HTTPForbidden(text='loopback browser required')
        # 'self' alone does not cover WebSocket schemes in every browser.
        # Explicitly allow only the browser's own host/port, including SSH remaps.
        websocket_origin = origin.replace('http', 'ws', 1)
        name, mime = ASSETS[request.path]
        # FileResponse streams sizeable model assets without reading them on
        # the publication loop. Wheels install these resources as normal files.
        return web.FileResponse(files(__package__).joinpath('assets', name),
                            headers={'Content-Type': mime, 'Cache-Control': 'no-store',
                                     'X-Content-Type-Options': 'nosniff',
                                     'Content-Security-Policy': f"default-src 'self'; connect-src 'self' {websocket_origin} blob:; img-src 'self' data: blob:; object-src 'none'; frame-ancestors 'none'"})

    async def _ws(self, request):
        # SSH forwarding preserves the browser's Host/Origin, including its
        # local port. Validate against that authority, not our listening port.
        origin = loopback_origin(request)
        if origin is None or request.headers.get('Origin') != origin:
            raise web.HTTPForbidden(text='same-origin loopback browser required')
        if len(self.clients) >= MAX_CLIENTS:
            raise web.HTTPServiceUnavailable(text='too many viewers')
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=1024, timeout=0.5)
        await ws.prepare(request)
        queue = Outbox()
        for channel, message in self._messages(('status', 'uart', 'display'), full=True).items():
            queue.put(channel, message)
        sender = asyncio.create_task(self._send(ws, queue))
        self.clients[ws] = queue
        try:
            async for message in ws:
                try:
                    if message.type != WSMsgType.TEXT:
                        raise ValueError('text command required')
                    self.input.submit(json.loads(message.data))
                except (ValueError, TypeError) as exc:
                    await self._close_client(ws, 1013 if isinstance(exc, InputOverflow) else 1008,
                                             b'invalid or full keypad input')
                    break
        finally:
            self.input.release_all()
            self.clients.pop(ws, None)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        return ws

    async def _close_client(self, ws, code, message=b''):
        try:
            # Closing a stalled viewer must not wait for a network drain or an
            # unbounded close handshake when the user shuts down the runner.
            await asyncio.wait_for(ws.close(code=code, message=message, drain=False), timeout=1)
        except (TimeoutError, ConnectionError, RuntimeError):
            pass

    async def _send(self, ws, queue):
        try:
            while True:
                message = await queue.get()
                send = ws.send_bytes if isinstance(message, bytes) else ws.send_str
                await asyncio.wait_for(send(message), timeout=2)
        except (TimeoutError, ConnectionError, RuntimeError):
            self.clients.pop(ws, None)
            await self._close_client(ws, 1013, b'viewer too slow')

    async def _publish(self):
        schedule = Schedule()
        for channel in ('status', 'uart'):
            schedule.add(channel, PUBLICATION_INTERVAL,
                         lambda until, channel=channel: self._broadcast((channel,)))
        while True:
            self.changed.clear()
            schedule.run_due(float('inf'))
            with self.lock:
                flush = self.flush_pending
                self.flush_pending = False
            self._broadcast(('status', 'uart', 'display') if flush else ('display',))
            try:
                await asyncio.wait_for(self.changed.wait(), schedule.delay(float('inf')))
            except TimeoutError:
                pass

    async def _serve(self):
        app = web.Application()
        for path in ASSETS:
            app.router.add_get(path, self._asset)
        app.router.add_get('/ws', self._ws)
        app.router.add_get('/audio', self._audio)
        self.files.routes(app)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
        try:
            await runner.setup()
            await web.SockSite(runner, self.socket).start()
            self.stopping = asyncio.Event()
            self.changed = asyncio.Event()
            self.ready.set()
            publisher = asyncio.create_task(self._publish())
            try:
                await self.stopping.wait()
            finally:
                await self.audio_hub.close()
                await asyncio.to_thread(self.files.service.close)
                publisher.cancel()
                await asyncio.gather(publisher, return_exceptions=True)
                await asyncio.gather(*(self._close_client(ws, 1001) for ws in list(self.clients)), return_exceptions=True)
        finally:
            await runner.cleanup()

    def _thread(self):
        try:
            with asyncio.Runner() as runner:
                self.loop = runner.get_loop()
                runner.run(self._serve())
        except BaseException as exc:
            self.error = exc
        finally:
            self.socket.close()
            self.ready.set()

    def wait(self):
        # Main thread retains signal ownership; timing was finalized by run().
        while self.thread.is_alive():
            self.thread.join(timeout=0.25)

    def close(self):
        self.input.enable(False)
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.stopping.set)
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError('browser server did not close')
