"""One bounded UART0 stream, owned by a live runner and its protocol client."""
import socket
import threading
import time


class SerialLink:
    def __init__(self, capacity=65536):
        if not 0 < capacity <= 1048576:
            raise ValueError('serial capacity must be between1 and1048576')
        self.capacity = capacity
        self.pending = bytearray()
        self.condition = threading.Condition()
        self.writer = threading.Lock()
        self.connection = None
        self.closed = False
        self.cable_requests = []
        self.present = True
        self.generation = 0
        self.discarded_bytes = 0

    def observe_cable(self, present):
        """Apply an observed physical edge to host stream ownership, not firmware."""
        if type(present) is not bool:
            raise ValueError('cable state must be boolean')
        with self.condition:
            if self.present == present:
                return
            self.present = present
            self.generation += 1
            self.discarded_bytes += len(self.pending)
            self.pending.clear()
            self.condition.notify_all()

    def _check(self, generation):
        if not self.present or generation != self.generation:
            raise EOFError('Serial cable changed; reconnect the file-transfer session')

    def open_channel(self):
        with self.condition:
            self._check(self.generation)
            if self.closed:
                raise EOFError('serial session ended')
            return SerialChannel(self, self.generation)

    def attach(self, connection):
        with self.condition:
            if self.closed or self.connection is not None:
                raise RuntimeError('serial link cannot be reused')
            self.connection = connection
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            for request in self.cable_requests:
                request[2] = EOFError('serial session ended')
                request[1].set()
            self.cable_requests.clear()
            self.condition.notify_all()

    def feed(self, data):
        """The UART capture thread blocks when its consumer applies backpressure."""
        offset = 0
        with self.condition:
            generation = self.generation
            while offset < len(data) and not self.closed:
                if not self.present or generation != self.generation:
                    self.discarded_bytes += len(data)-offset
                    return
                available = self.capacity-len(self.pending)
                if not available:
                    self.condition.wait(.2)
                    continue
                chunk = data[offset:offset+available]
                self.pending.extend(chunk)
                offset += len(chunk)
                self.condition.notify_all()

    def read(self, size, deadline, *, generation=None):
        if not 0 < size <= self.capacity:
            raise ValueError('serial read exceeds capacity')
        with self.condition:
            generation = self.generation if generation is None else generation
            self._check(generation)
            while not self.pending:
                if self.closed:
                    raise EOFError('serial session ended')
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('serial receive deadline exceeded')
                self.condition.wait(remaining)
                self._check(generation)
            data = bytes(self.pending[:size])
            del self.pending[:len(data)]
            self.condition.notify_all()
            return data

    def write(self, data, deadline, *, generation=None):
        if len(data) > self.capacity:
            raise ValueError('serial write exceeds capacity')
        with self.condition:
            generation = self.generation if generation is None else generation
            self._check(generation)
        if not self.writer.acquire(timeout=max(0,deadline-time.monotonic())):
            raise TimeoutError('serial writer deadline exceeded')
        try:
            with self.condition:
                self._check(generation)
                while self.connection is None and not self.closed:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('serial connection deadline exceeded')
                    self.condition.wait(remaining)
                    self._check(generation)
                connection = self.connection
            remaining = memoryview(data)
            while remaining:
                with self.condition:
                    self._check(generation)
                if self.closed:
                    raise EOFError('serial session ended')
                if time.monotonic() >= deadline:
                    raise TimeoutError('serial send deadline exceeded')
                try:
                    count = connection.send(remaining)
                except socket.timeout:
                    continue
                if not count:
                    raise EOFError('serial peer disconnected')
                remaining = remaining[count:]
        finally:
            self.writer.release()

    def set_cable(self, present, deadline):
        """Request a physical cable edge from the QMP owner, then await delivery."""
        if type(present) is not bool:
            raise ValueError('cable state must be boolean')
        request = [present, threading.Event(), None]
        with self.condition:
            if self.closed:
                raise EOFError('serial session ended')
            if len(self.cable_requests) >= 8:
                raise RuntimeError('cable input queue is full')
            self.cable_requests.append(request)
        if not request[1].wait(max(0,deadline-time.monotonic())):
            with self.condition:
                if request in self.cable_requests: self.cable_requests.remove(request)
            raise TimeoutError('cable input deadline exceeded')
        if request[2] is not None: raise request[2]

    def poll(self, qmp, deadline):
        with self.condition:
            requests = self.cable_requests[:]
            if requests: self.cable_requests.clear()
        for request in requests:
            try:
                qmp.execute('qom-set', deadline, {'path':'/machine/uart',
                    'property':'cable-present','value':request[0]})
                self.observe_cable(request[0])
            except BaseException as exc:
                request[2] = exc
            finally:
                request[1].set()

    def discard_received(self):
        """Discard obsolete stream bytes only after the cable has been removed."""
        with self.condition:
            self.pending.clear()
            self.condition.notify_all()


class SerialChannel:
    """One cable generation of a live UART stream; old owners cannot reconnect."""
    def __init__(self, link, generation):
        self.link = link
        self.generation = generation

    def check(self):
        with self.link.condition:
            self.link._check(self.generation)
            if self.link.closed:
                raise EOFError('serial session ended')

    def read(self, size, deadline):
        return self.link.read(size, deadline, generation=self.generation)

    def write(self, data, deadline):
        return self.link.write(data, deadline, generation=self.generation)
