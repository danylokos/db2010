"""Cable loss ends host ownership without injecting protocol or guest cleanup."""
import io
import threading
import time

import pytest

from db2010.execution.serial import SerialLink
from db2010.transfer.obex import ObexSession
from db2010.transfer.service import FileService, SessionChanged


def test_blocked_old_channel_wakes_even_if_cable_is_reinserted_immediately():
    link = SerialLink()
    channel = link.open_channel()
    started = threading.Event()
    result = []
    def reader():
        started.set()
        try: channel.read(1,time.monotonic()+30)
        except EOFError as exc: result.append(str(exc))
    worker = threading.Thread(target=reader)
    worker.start()
    assert started.wait(1)
    link.observe_cable(False)
    link.observe_cable(True)
    worker.join(1)
    assert not worker.is_alive() and len(result) == 1
    link.feed(b'new')
    with pytest.raises(EOFError): channel.read(1,time.monotonic()+1)
    assert link.open_channel().read(3,time.monotonic()+1) == b'new'


def test_loss_discards_old_buffer_and_releases_capture_backpressure():
    link = SerialLink(capacity=2)
    worker = threading.Thread(target=link.feed,args=(b'abcdef',))
    worker.start()
    with link.condition:
        assert link.condition.wait_for(lambda: len(link.pending)==2,timeout=1)
    link.observe_cable(False)
    worker.join(1)
    assert not worker.is_alive() and not link.pending and link.discarded_bytes == 6
    link.feed(b'late')
    assert link.discarded_bytes == 10
    with pytest.raises(EOFError): link.read(1,time.monotonic()+1)
    with pytest.raises(EOFError): link.open_channel()
    link.observe_cable(True)
    link.feed(b'OK')
    assert link.read(2,time.monotonic()+1) == b'OK'


def test_partial_writer_cannot_finish_old_request_in_new_cable_generation():
    link = SerialLink()
    class Connection:
        output = bytearray()
        def send(self,data):
            count = min(2,len(data))
            self.output.extend(data[:count])
            if len(self.output) == 2:
                link.observe_cable(False)
                link.observe_cable(True)
            return count
    connection = Connection()
    link.attach(connection)
    old = link.open_channel()
    with pytest.raises(EOFError): old.write(b'abcdef',time.monotonic()+1)
    assert connection.output == b'ab'
    with pytest.raises(EOFError): old.write(b'bad',time.monotonic()+1)
    link.open_channel().write(b'new',time.monotonic()+1)
    assert connection.output == b'abnew'


def test_stale_connected_session_cannot_skip_new_handover_or_send_abort():
    link = SerialLink()
    session = ObexSession(link)
    session.connected = True
    link.observe_cable(False)
    link.observe_cable(True)
    with pytest.raises(EOFError): session.connect()
    assert session.failed and not session.connected
    with pytest.raises(EOFError): session._exchange(0xff)
    assert not link.pending


def test_upload_body_reservation_expires_at_cable_boundary():
    service = FileService()
    try:
        link = SerialLink()
        service.attach(link)
        service.progress(dict(serial_input_ready=True,serial_cable_present=True))
        epoch = service.epoch
        service.session.connected = True
        record = service.reserve(epoch,'upload',('file',),size=3)
        job = service.begin_receiving(epoch,record['id'])
        job.file.write_bytes(b'x')
        link.observe_cable(False)
        state = service.state()
        assert state['epoch'] != epoch and not state['connected'] and not state['available']
        assert job.phase == 'error' and not job.file.exists() and not service.operation.locked()
        link.observe_cable(True)
        service.progress(dict(serial_input_ready=True,serial_cable_present=True))
        assert service.state()['available'] and not service.session.connected
        with pytest.raises(SessionChanged): service.commit_upload(epoch,job.id,3,'unused')
    finally: service.close()


def test_cable_loss_ends_active_job_and_releases_owner_before_reconnect():
    link = SerialLink()
    requested = threading.Event()
    class Connection:
        def send(self,data):
            if data[0] == 0x85: link.feed(b'\xa0\0\x03')
            else: requested.set()  # A GET response remains pending.
            return len(data)
    link.attach(Connection())
    service = FileService()
    try:
        service.attach(link)
        service.progress(dict(serial_input_ready=True,serial_cable_present=True))
        epoch = service.epoch
        service.session.connected = True
        service.session.connection = bytes.fromhex('cb00000001')
        service.session.peer_max = 4096
        record = service.reserve(epoch,'download',('file',))
        job = service.job(epoch,record['id'])
        assert requested.wait(1)
        link.observe_cable(False)
        service.progress(dict(serial_input_ready=True,serial_cable_present=False))
        deadline = time.monotonic()+1
        while job.finished is None and time.monotonic() < deadline: time.sleep(.005)
        assert job.finished is not None and job.phase == 'error'
        assert not service.operation.locked() and not job.file.exists()
        assert not service.state()['available']
        link.observe_cable(True)
        service.progress(dict(serial_input_ready=True,serial_cable_present=True))
        assert service.epoch != epoch and service.state()['available'] and not service.session.connected
        with pytest.raises(SessionChanged): service.list(epoch,())
    finally: service.close()
