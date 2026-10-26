import socket
import threading
import time

import pytest

from db2010.execution.serial import SerialLink


def test_backpressure_drains_in_order_and_close_wakes_receiver():
    link = SerialLink(capacity=4)
    worker = threading.Thread(target=link.feed, args=(b'abcdefghij',))
    worker.start()
    received = bytearray()
    for _ in range(3):
        received.extend(link.read(4, time.monotonic()+2))
        assert len(link.pending) <= 4
    worker.join(2)
    assert not worker.is_alive() and received == b'abcdefghij'
    link.close()
    with pytest.raises(EOFError):
        link.read(1, time.monotonic()+1)


def test_close_releases_a_full_capture_queue():
    link = SerialLink(capacity=1)
    worker = threading.Thread(target=link.feed, args=(b'abc',))
    worker.start()
    link.close()
    worker.join(2)
    assert not worker.is_alive()
    with pytest.raises(RuntimeError):
        link.attach(object())


def test_partial_socket_writes_retry_without_duplicate_bytes():
    class Connection:
        def __init__(self):
            self.bytes = bytearray()
            self.calls = 0
        def send(self, data):
            self.calls += 1
            if self.calls == 2:
                raise socket.timeout()
            count = min(2, len(data))
            self.bytes.extend(data[:count])
            return count
    link = SerialLink()
    connection = Connection()
    link.attach(connection)
    link.write(b'abcdefg', time.monotonic()+1)
    assert connection.bytes == b'abcdefg'
    link.close()
    with pytest.raises(EOFError):
        link.write(b'x', time.monotonic()+1)


def test_input_bounds_and_deadlines():
    link = SerialLink(capacity=4)
    for size in (0, 5):
        with pytest.raises(ValueError):
            link.read(size, time.monotonic()+1)
    with pytest.raises(ValueError):
        link.write(b'abcde', time.monotonic()+1)
    with pytest.raises(TimeoutError):
        link.read(1, time.monotonic())
    with pytest.raises(TimeoutError):
        link.write(b'a', time.monotonic())
    link.writer.acquire()
    try:
        with pytest.raises(TimeoutError):
            link.write(b'a',time.monotonic())
    finally:
        link.writer.release()
