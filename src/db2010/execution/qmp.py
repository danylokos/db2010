"""Deadline-aware QMP framing without timeout-poisoned buffered socket reads."""
import base64
import json
import select
import socket
import time

from .schedule import Schedule, LCD_INTERVAL, PUBLICATION_INTERVAL

# Guard every individual QMP reply. Stopped snapshots use bounded chunks.
MAX_FRAME_BYTES = 64*1024*1024


class QMP:
    def __init__(self, path, deadline, health):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.connect(str(path))
        self.socket.setblocking(False)
        self.pending = bytearray()
        self.events = []
        self.sequence = 0
        self.health = health
        try:
            self.greeting = self.receive(deadline)
            if 'QMP' not in self.greeting:
                raise ValueError('QMP greeting missing')
            self.execute('qmp_capabilities', deadline)
        except BaseException:
            self.socket.close()
            raise

    def receive(self, deadline):
        while True:
            if b'\n' in self.pending:
                line, _, remaining = self.pending.partition(b'\n')
                if len(line) > MAX_FRAME_BYTES:
                    raise ValueError('QMP frame exceeds limit')
                self.pending = bytearray(remaining)
                return json.loads(line)
            self.health()
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('QMP deadline exceeded')
            if not select.select([self.socket], [], [], min(remaining, 0.1))[0]:
                continue
            data = self.socket.recv(65536)
            if not data:
                raise EOFError('QMP disconnected')
            self.pending.extend(data)
            if len(self.pending) > MAX_FRAME_BYTES:
                raise ValueError('QMP frame exceeds limit')

    def execute(self, name, deadline, arguments=None):
        self.sequence += 1
        request = {'execute': name, 'id': self.sequence}
        if arguments is not None:
            request['arguments'] = arguments
        message = memoryview(json.dumps(request).encode()+b'\n')
        while message:
            self.health()
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('QMP send deadline exceeded')
            if select.select([], [self.socket], [], min(remaining, 0.1))[1]:
                sent = self.socket.send(message)
                if not sent:
                    raise EOFError('QMP send disconnected')
                message = message[sent:]
        while True:
            response = self.receive(deadline)
            if 'event' in response:
                self.events.append(response)
                continue
            if response.get('id') != self.sequence:
                raise ValueError('unexpected QMP response id')
            if 'error' in response:
                raise RuntimeError(f'QMP error: {response["error"]}')
            if 'return' not in response:
                raise ValueError('QMP response lacks return value')
            return response['return']

    def observe(self, deadline, progress=None, display=None):
        if progress is not None:
            progress(json.loads(self.execute('qom-get', deadline,
                {'path': '/machine', 'property': 'progress'})))
        if display is not None:
            display(json.loads(self.execute('qom-get', deadline,
                {'path': '/machine', 'property': 'display'})))

    def summary(self, deadline):
        return json.loads(self.execute('qom-get', deadline,
            {'path': '/machine', 'property': 'summary'}))

    def snapshot(self, deadline):
        arguments = {'path': '/machine', 'property': 'snapshot-chunk'}
        # Bound each request's lack of progress; a large, progressing transfer
        # must not exhaust the former single-reply capture deadline.
        request_seconds = max(0, deadline-time.monotonic())
        self.execute('qom-set', deadline, {**arguments, 'value': 'start'})
        data = bytearray()
        total = None
        while True:
            chunk = json.loads(self.execute('qom-get', time.monotonic()+request_seconds, arguments))
            if (type(chunk.get('offset')) is not int or chunk['offset'] != len(data)
                    or type(chunk.get('total')) is not int or chunk['total'] <= 0
                    or (total is not None and total != chunk['total'])):
                raise ValueError('invalid QMP snapshot chunk position')
            total = chunk['total']
            payload = base64.b64decode(chunk['data_base64'], validate=True)
            if not 0 < len(payload) <= 256*1024 or len(data)+len(payload) > total:
                raise ValueError('invalid QMP snapshot chunk length')
            data.extend(payload)
            if len(data) == total:
                return json.loads(data)

    def observations(self, progress=None, display=None, interval=PUBLICATION_INTERVAL):
        schedule = Schedule()
        if progress is not None:
            schedule.add('progress', interval, lambda until: self.observe(until, progress=progress))
        if display is not None:
            schedule.add('display', LCD_INTERVAL, lambda until: self.observe(until, display=display))
        return schedule

    def wait_stop(self, deadline, progress=None, interval=PUBLICATION_INTERVAL, display=None,
                  service=None, observations=None):
        schedule = observations if observations is not None else self.observations(progress, display, interval)
        if service:
            schedule.add('keypad', 0.025, lambda until: service(self, until))
        while not any(event.get('event') == 'STOP' for event in self.events):
            schedule.run_due(deadline)
            refresh = schedule.next_deadline(deadline)
            while not any(event.get('event') == 'STOP' for event in self.events):
                try:
                    response = self.receive(refresh)
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        raise
                    break
                if 'event' not in response:
                    raise ValueError('unsolicited QMP response')
                self.events.append(response)

    def close(self):
        self.socket.close()
