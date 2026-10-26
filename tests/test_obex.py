import io
import json
from pathlib import Path
import threading

import pytest

from db2010.transfer.obex import ObexSession, ProtocolError, RemoteError, TransferCancelled, headers, path_segments

WIRE = json.loads((Path(__file__).parent/'oracles/serial-obex-native.json').read_text())


class Peer:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.pending = bytearray()
        self.requests = []
    def write(self,data,deadline):
        assert not self.pending
        self.requests.append(bytes(data))
        self.pending.extend(self.replies.pop(0))
    def read(self,size,deadline):
        if not self.pending: raise EOFError('test peer closed')
        data = bytes(self.pending[:min(size,2)])
        del self.pending[:len(data)]
        return data


def connected(peer, maximum=4096):
    session = ObexSession(peer)
    session.connected = session.binary_mode = True
    session.connection = bytes.fromhex('cb00000001')
    session.peer_max = maximum
    return session


def test_native_connect_listing_and_empty_name_reset_golden_wire():
    peer = Peer(bytes.fromhex(WIRE['connect_response_hex']),b'\xa0\0\x03',
                bytes.fromhex(WIRE['listing_response_hex']))
    session = ObexSession(peer)
    session.connect(binary=True)
    assert peer.requests == [bytes.fromhex(WIRE['connect_request_hex'])]
    assert session.peer_max == 4096
    assert session.list() == [dict(name='Память телефона',directory=True,size=None,modified=None,created=None)]
    assert peer.requests[1] == bytes.fromhex('85000d0200cb00000001010003')
    assert peer.requests[2] == bytes.fromhex(WIRE['listing_request_hex'])


def test_at_handover_prefers_cprot_and_consumes_echo_before_binary(monkeypatch):
    peer = Peer(b'+CPROT=0\r\r\nCONNECT\r\n',bytes.fromhex(WIRE['connect_response_hex']))
    session = ObexSession(peer)
    pauses = []
    monkeypatch.setattr('db2010.transfer.obex.time.sleep', pauses.append)
    session.connect()
    assert peer.requests[0] == b'AT+CPROT=0\r'
    assert pauses == [.025]
    assert session.connected


@pytest.mark.parametrize('refusal', [b'ERROR', b'NO CARRIER', b'BUSY', b'+CME ERROR: 3'])
def test_explicit_modem_refusal_falls_back_to_eobex(refusal):
    peer = Peer(b'\r\n'+refusal+b'\r\n',b'*EOBEX\r\r\nCONNECT\r\n',
                bytes.fromhex(WIRE['connect_response_hex']))
    session = ObexSession(peer)
    session.connect()
    assert peer.requests[:2] == [b'AT+CPROT=0\r', b'AT*EOBEX\r']
    assert session.connected


def test_lost_modem_reply_does_not_send_another_command():
    class TimeoutPeer(Peer):
        def read(self, size, deadline):
            raise TimeoutError('No response')
    peer = TimeoutPeer(b'')
    session = ObexSession(peer)
    with pytest.raises(TimeoutError): session.connect()
    assert peer.requests == [b'AT+CPROT=0\r']
    assert session.failed and not session.binary_mode
    with pytest.raises(ProtocolError): session.connect()


def test_two_framed_refusals_allow_later_retry_without_binary_bytes():
    peer = Peer(b'\r\nERROR\r\n', b'\r\nNO CARRIER\r\n',
                b'\r\nCONNECT\r\n', bytes.fromhex(WIRE['connect_response_hex']))
    session = ObexSession(peer)
    with pytest.raises(ProtocolError): session.connect()
    assert not session.failed and not session.binary_mode
    session.connect()
    assert peer.requests[:3] == [b'AT+CPROT=0\r', b'AT*EOBEX\r', b'AT+CPROT=0\r']
    assert session.connected


def test_not_ready_can_retry_connect_in_existing_binary_mode():
    peer = Peer(b'\xc3\0\x07\x10\0\x10\0',bytes.fromhex(WIRE['connect_response_hex']))
    session = ObexSession(peer)
    with pytest.raises(RemoteError) as error:
        session.connect(binary=True)
    assert error.value.status == 0xc3
    session.connect()
    assert len(peer.requests) == 2 and all(r[0] == 0x80 for r in peer.requests)


def test_stream_upload_has_bounded_packets_one_name_and_final_body():
    peer = Peer(b'\xa0\0\x03',b'\x90\0\x03',b'\x90\0\x03',b'\xa0\0\x03')
    session = connected(peer,255)
    data = bytes(range(250))*2
    progress = []
    assert session.upload(('x.bin',),io.BytesIO(data),len(data),lambda n,t:progress.append((n,t))) == 500
    packets = peer.requests[1:]
    assert [p[0] for p in packets] == [2,2,0x82]
    assert all(len(p) <= 255 and int.from_bytes(p[1:3],'big') == len(p) for p in packets)
    fields = [headers(p[3:]) for p in packets]
    assert sum(tag == 1 for f in fields for tag,_ in f) == 1
    assert [value for f in fields for tag,value in f if tag == 0xc3] == [(500).to_bytes(4,'big')]
    assert b''.join(value for f in fields for tag,value in f if tag in (0x48,0x49)) == data
    assert fields[-1][-1][0] == 0x49 and progress[-1] == (500,500)


def test_empty_upload_and_missing_remote_file():
    peer = Peer(b'\xa0\0\x03',b'\xa0\0\x03',b'\xa0\0\x03',b'\xc4\0\x03')
    session = connected(peer)
    assert session.upload(('empty',),io.BytesIO(),0) == 0
    assert peer.requests[1][-3:] == b'\x49\0\x03'
    with pytest.raises(RemoteError) as error:
        session.download(('missing',),io.BytesIO())
    assert error.value.status == 0xc4 and session.connected


def test_get_continuation_is_streamed_and_cancel_sends_native_abort():
    peer = Peer(b'\xa0\0\x03',bytes.fromhex('90000cc30000000548000441'),b'\xa0\0\x03')
    session = connected(peer)
    cancel = threading.Event()
    output = io.BytesIO()
    with pytest.raises(TransferCancelled):
        session.download(('file',),output,lambda *_:cancel.set(),cancel)
    assert output.getvalue() == b'A'
    assert peer.requests[-1] == bytes.fromhex('ff0003')


@pytest.mark.parametrize('kind', ['upload', 'download'])
def test_cancel_before_transfer_sends_no_abort_or_file_request(kind):
    peer = Peer()
    session = connected(peer)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(TransferCancelled):
        if kind == 'upload': session.upload(('file',),io.BytesIO(b'x'),1,cancel=cancel)
        else: session.download(('file',),io.BytesIO(),cancel=cancel)
    assert peer.requests == [] and session.connected


def test_upload_cancellation_waits_for_pending_response_and_keeps_session_usable():
    cancel = threading.Event()
    class CancelDuringRequest(Peer):
        def write(self, data, deadline):
            super().write(data, deadline)
            if data[0] == 2: cancel.set()
    peer = CancelDuringRequest(b'\xa0\0\x03',b'\x90\0\x03',b'\xa0\0\x03',b'\xa0\0\x03')
    session = connected(peer, 255)
    with pytest.raises(TransferCancelled):
        session.upload(('file',),io.BytesIO(b'x'*500),500,cancel=cancel)
    # Peer rejects any write while the previous response is still unread.
    assert [r[0] for r in peer.requests] == [0x85, 2, 0xff]
    assert peer.requests[-1] == bytes.fromhex('ff0003')
    assert session.connected
    session._set_path(())


def test_rejected_abort_invalidates_session():
    peer = Peer(b'\xa0\0\x03',bytes.fromhex('90000cc30000000548000441'),b'\xc3\0\x03')
    session = connected(peer)
    cancel = threading.Event()
    with pytest.raises(RemoteError):
        session.download(('file',),io.BytesIO(),lambda *_:cancel.set(),cancel)
    assert session.failed and not session.connected
    with pytest.raises(ProtocolError): session.connect()


def test_cancel_after_final_reply_does_not_abort_completed_object():
    peer = Peer(b'\xa0\0\x03',bytes.fromhex('a0000749000441'))
    session = connected(peer)
    cancel = threading.Event()
    assert session.download(('file',),io.BytesIO(),lambda *_:cancel.set(),cancel) == 1
    assert all(r[0] != 0xff for r in peer.requests)


def test_disconnect_releases_identity_and_reconnect_uses_ascii_handover():
    peer = Peer(b'\xa0\0\x03', b'\r\nOK\r\n+CPROT=0\r\r\nCONNECT\r\n',
                bytes.fromhex(WIRE['connect_response_hex']))
    session = connected(peer)
    session.disconnect()
    assert not session.connected and not session.binary_mode and not session.connection
    session.connect()
    assert peer.requests[1] == b'AT+CPROT=0\r' and session.connected


def test_refused_disconnect_cannot_be_reused_as_ascii_session():
    session = connected(Peer(b'\xc3\0\x03'))
    with pytest.raises(RemoteError): session.disconnect()
    assert session.failed and not session.connected
    with pytest.raises(ProtocolError): session.connect()


def test_get_streams_complete_multipart_object_and_checks_total():
    peer = Peer(b'\xa0\0\x03',bytes.fromhex('90000cc30000000548000441'),
                bytes.fromhex('a0000a49000742434445'))
    output = io.BytesIO()
    assert connected(peer).download(('file',),output) == 5
    assert output.getvalue() == b'ABCDE'
    assert peer.requests[-1] == bytes.fromhex('830008cb00000001')


@pytest.mark.parametrize('reply',[b'\xa0\0\x02',b'\xa0\x04\x01',b'\xa0\0\x05\x00'])
def test_invalid_or_truncated_packet_invalidates_session(reply):
    peer = Peer(reply)
    session = connected(peer)
    with pytest.raises((ProtocolError,EOFError)):
        session._exchange(0x83)
    assert session.failed and not session.connected
    with pytest.raises(ProtocolError): session.connect()


@pytest.mark.parametrize('value',[b'\x01\0',b'\x48\0\x02',b'\xcb\0\0'])
def test_truncated_header_is_rejected(value):
    with pytest.raises(ProtocolError): headers(value)


@pytest.mark.parametrize('parts',[('..',),('a/b',),('x\0y',),('a\\b',),('',)])
def test_invalid_path_component(parts):
    with pytest.raises(ValueError): path_segments(parts)
