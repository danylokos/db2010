"""Independent bounded live audio subscriptions; never replay retained samples."""
import asyncio
from collections import deque
import json
import secrets
import threading
import time

from aiohttp import web, WSMsgType

from ..execution.audio import counter


class AudioClient:
    def __init__(self, ws):
        self.ws = ws
        self.epoch = str(secrets.randbits(64))
        self.pending = asyncio.Queue(maxsize=10)
        self.outstanding = deque()
        self.changed = asyncio.Event()
        self.last_ack = time.monotonic()
        self.last_sent = self.last_consumed = None
        self.source = None
        self.first = True
        self.closing = False
        self.ready_pending = True

    def close(self):
        if not self.closing:
            self.closing = True
            asyncio.create_task(self.ws.close(code=1013, message=b'audio presentation reset'))
            self.changed.set()

    def put(self, packet):
        if self.closing:
            return
        source = packet.header['stream_id'], packet.header['epoch']
        if self.source is not None and source != self.source:
            # A new native stream invalidates pending presentation, not its
            # connection. Reconnecting would miss its first live packets.
            while not self.pending.empty():
                self.pending.get_nowait()
            self.outstanding.clear()
            self.epoch = str(secrets.randbits(64))
            self.last_sent = self.last_consumed = None
            self.last_ack = time.monotonic()
            self.first = True
            self.ready_pending = True
        self.source = source
        try:
            self.pending.put_nowait(packet)
            self.changed.set()
        except asyncio.QueueFull:
            self.close()

    def ack(self, data):
        if (not isinstance(data, dict) or set(data) != {'type', 'epoch', 'sequence'}
                or data['type'] != 'audio-ack'):
            raise ValueError('invalid audio acknowledgement')
        counter(data['epoch'])
        if data['epoch'] != self.epoch:
            return  # Worklet retirement from the stream just replaced.
        value = data['sequence']
        if value is None:
            if self.last_sent is not None:
                return  # Heartbeat before any worklet chunk has finished.
            self.last_ack = time.monotonic()
            return
        sequence = counter(value)
        if (self.last_sent is None or sequence > self.last_sent
                or (self.last_consumed is not None and sequence < self.last_consumed)):
            raise ValueError('audio acknowledgement outside sent range')
        if self.last_consumed == sequence:
            if not self.outstanding:
                self.last_ack = time.monotonic()
            return
        if not any(item == sequence for item in self.outstanding):
            raise ValueError('audio acknowledgement was never sent')
        while self.outstanding and self.outstanding[0] <= sequence:
            self.outstanding.popleft()
        self.last_consumed = sequence
        self.last_ack = time.monotonic()
        self.changed.set()

    async def send(self):
        while not self.ws.closed:
            self.changed.clear()
            if self.ready_pending:
                epoch = self.epoch
                await self.ws.send_json(dict(type='audio-ready', epoch=epoch, sample_rate=48000))
                if epoch == self.epoch:
                    self.ready_pending = False
                continue
            if not self.pending.empty() and not self.outstanding:
                # Start a consumption deadline with new work, rather than
                # charging an idle heartbeat interval to the first PCM block.
                self.last_ack = time.monotonic()
            if time.monotonic()-self.last_ack > .5:
                self.close()
                return
            if not self.pending.empty() and len(self.outstanding) < 10:
                packet = self.pending.get_nowait()
                header = dict(packet.header, epoch=self.epoch,
                              discontinuity=self.first or packet.header['discontinuity'])
                self.first = False
                self.last_sent = counter(header['sequence'])
                self.outstanding.append(self.last_sent)
                from ..execution.audio import AudioPacket
                await asyncio.wait_for(self.ws.send_bytes(AudioPacket(header, packet.pcm).wire()), .1)
                continue
            try:
                await asyncio.wait_for(self.changed.wait(), .05)
            except TimeoutError:
                pass


class AudioHub:
    """At most ten ingress packets and one event-loop wake across threads."""
    def __init__(self, browser):
        self.browser = browser
        self.lock = threading.Lock()
        self.pending = deque()
        self.scheduled = False
        self.clients = set()
        self.dropped = 0

    def publish(self, packet):
        with self.lock:
            if packet is None or len(self.pending) >= 10:
                self.dropped += sum(p.header['frame_count'] for p in self.pending if p is not None)
                self.pending.clear()
                self.pending.append(None)
            if packet is not None:
                self.pending.append(packet)
            if self.scheduled:
                return
            self.scheduled = True
        self.browser.loop.call_soon_threadsafe(self.deliver)

    def deliver(self):
        with self.lock:
            pending = tuple(self.pending)
            self.pending.clear()
            self.scheduled = False
        for packet in pending:
            for client in tuple(self.clients):
                if packet is None:
                    client.close()
                else:
                    client.put(packet)

    async def endpoint(self, request, origin, max_clients):
        if origin is None or request.headers.get('Origin') != origin:
            raise web.HTTPForbidden(text='same-origin loopback browser required')
        if len(self.clients) >= max_clients:
            raise web.HTTPServiceUnavailable(text='too many audio listeners')
        ws = web.WebSocketResponse(max_msg_size=1024, timeout=.1)
        await ws.prepare(request)
        client = AudioClient(ws)
        self.clients.add(client)
        sender = asyncio.create_task(client.send())
        # Ensure send failures/timeouts close the connection and unblock receive.
        def ended(task):
            if not task.cancelled():
                task.exception()
            client.close()
        sender.add_done_callback(ended)
        try:
            async for message in ws:
                try:
                    if message.type != WSMsgType.TEXT:
                        raise ValueError('text acknowledgement required')
                    client.ack(json.loads(message.data))
                except (ValueError, TypeError):
                    await ws.close(code=1008, message=b'invalid audio acknowledgement')
        finally:
            self.clients.discard(client)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        return ws

    async def close(self):
        await asyncio.gather(*(c.ws.close(code=1001) for c in tuple(self.clients)), return_exceptions=True)
