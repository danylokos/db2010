"""Drain child output with optional recording and a bounded error tail."""
import select
import threading


class ProcessLog:
    def __init__(self, path=None):
        self.path = path
        self.file = self.stream = self.thread = None
        self.tail = bytearray()
        self.error = None
        self.stopping = threading.Event()

    def __enter__(self):
        if self.path is not None:
            self.file = self.path.open('wb')
        return self

    def start(self, stream):
        self.stream = stream
        self.thread = threading.Thread(target=self._drain, name='db2010-log', daemon=True)
        self.thread.start()

    def _drain(self):
        try:
            while not self.stopping.is_set():
                if not select.select([self.stream], [], [], 0.1)[0]:
                    continue
                data = self.stream.read1(65536)
                if not data:
                    break
                self.tail.extend(data)
                del self.tail[:-4096]
                if self.file and self.error is None:
                    try:
                        self.file.write(data)
                        self.file.flush()
                    except OSError as exc:
                        self.error = exc
        except OSError as exc:
            self.error = self.error or exc

    def check(self):
        if self.error:
            raise self.error

    def __exit__(self, kind, value, traceback):
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                self.error = self.error or TimeoutError('child output did not close')
                self.stopping.set()
                self.thread.join()
        if self.stream:
            self.stream.close()
        if self.file:
            self.file.close()
        if kind is None:
            self.check()

    def text(self):
        return bytes(self.tail).decode('utf-8', errors='backslashreplace').strip()
