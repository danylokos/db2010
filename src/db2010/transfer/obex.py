"""W800i AT/OBEX folder service; files move only through native GET/PUT."""
import time
import xml.etree.ElementTree as ET

TARGET = bytes.fromhex('f9ec7bc4953c11d2984e525400dc9e09')
LISTING = b'x-obex/folder-listing\0'


class ProtocolError(Exception):
    pass


class RemoteError(ProtocolError):
    def __init__(self, status):
        self.status = status
        super().__init__({0xc0:'Phone rejected the request',0xc1:'Phone requires authorization',
            0xc3:'Phone denied access',0xc4:'File or folder not found',0xcd:'Phone has insufficient space',
            0xd0:'Phone reported a transfer error'}.get(status,f'Phone returned OBEX status {status:02X}'))


class TransferCancelled(Exception):
    pass


def header(tag, value):
    kind = tag & 0xc0
    if kind in (0,0x40):
        if len(value)+3 > 65535:
            raise ValueError('OBEX header is too large')
        return bytes([tag])+(len(value)+3).to_bytes(2,'big')+value
    size = 1 if kind == 0x80 else 4
    if len(value) != size:
        raise ValueError('Invalid fixed-size OBEX header')
    return bytes([tag])+value


def headers(data):
    result = []
    while data:
        tag = data[0]
        kind = tag & 0xc0
        if kind in (0,0x40):
            if len(data) < 3:
                raise ProtocolError('Truncated OBEX header')
            size = int.from_bytes(data[1:3],'big')
            if not 3 <= size <= len(data):
                raise ProtocolError('Invalid OBEX header length')
            value = data[3:size]
        else:
            size = 2 if kind == 0x80 else 5
            if len(data) < size:
                raise ProtocolError('Truncated fixed-size OBEX header')
            value = data[1:size]
        result.append((tag,value))
        data = data[size:]
    return result


def path_segments(path):
    parts = tuple(path)
    for part in parts:
        if (not isinstance(part,str) or not part or part in ('.','..') or
                any(c in part for c in ('/','\\','\0')) or len(part.encode('utf-16-be')) > 510):
            raise ValueError('Invalid phone path component')
    if len(parts) > 32:
        raise ValueError('Phone path is too deep')
    return parts


def name_header(name):
    return header(1,name.encode('utf-16-be')+b'\0\0' if name else b'')


class ObexSession:
    """Single-owner synchronous session. A caller serializes whole operations."""
    def __init__(self, link, timeout=15, receive_max=1024):
        if not 255 <= receive_max <= 65535:
            raise ValueError('Invalid receive packet size')
        self.link = link.open_channel() if hasattr(link, 'open_channel') else link
        self.timeout = timeout
        self.receive_max = receive_max
        self.peer_max = 255
        self.connection = b''
        self.connected = False
        self.binary_mode = False
        self.failed = False

    def _exact(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            chunk = self.link.read(min(size-len(data),65536), deadline)
            if not chunk: raise EOFError('serial peer disconnected')
            data.extend(chunk)
        return bytes(data)

    def _exchange(self, opcode, data=b'', *, connect=False):
        deadline = time.monotonic()+self.timeout
        size = 3+len(data)
        if size > (65535 if connect else self.peer_max):
            raise ValueError('Request exceeds phone packet size')
        try:
            self.link.write(bytes([opcode])+size.to_bytes(2,'big')+data, deadline)
            prefix = self._exact(3,deadline)
            length = int.from_bytes(prefix[1:3],'big')
            if not 3 <= length <= self.receive_max:
                raise ProtocolError('Invalid OBEX packet size')
            return prefix[0], self._exact(length-3,deadline)
        except (OSError,EOFError,TimeoutError,ProtocolError):
            self.connected = False
            self.failed = True
            raise

    def connect(self, *, binary=False):
        if self.connected:
            self._require()
            return
        if self.failed:
            raise ProtocolError('File-transfer stream requires reconnection')
        if not (binary or self.binary_mode):
            # PC Suite prefers CPROT and falls back only after a framed modem
            # refusal. A lost response leaves the stream ambiguous, so no
            # second command is sent after a timeout or transport failure.
            if not self._handover(b'AT+CPROT=0\r') and not self._handover(b'AT*EOBEX\r'):
                raise ProtocolError('Phone did not enter file-transfer mode; finish any handset dialog and retry')
            # Original host policy, not a modeled DB2010 timer requirement.
            time.sleep(.025)
        self.binary_mode = True
        data = b'\x10\0'+self.receive_max.to_bytes(2,'big')+header(0x46,TARGET)
        status, response = self._exchange(0x80,data,connect=True)
        if status != 0xa0:
            raise RemoteError(status)
        if len(response) < 4 or response[0] >> 4 != 1 or response[1] != 0:
            raise ProtocolError('Invalid OBEX CONNECT response')
        self.peer_max = int.from_bytes(response[2:4],'big')
        if self.peer_max < 255:
            raise ProtocolError('Phone packet size is too small')
        fields = dict(headers(response[4:]))
        if fields.get(0x4a) != TARGET or len(fields.get(0xcb,b'')) != 4:
            raise ProtocolError('Unexpected phone file-service identity')
        self.connection = header(0xcb,fields[0xcb])
        self.connected = True

    def _handover(self, command):
        deadline = time.monotonic()+self.timeout
        response = bytearray()
        line = bytearray()
        try:
            self.link.write(command, deadline)
            while len(response) < 4096:
                chunk = self._exact(1, deadline)
                response.extend(chunk)
                line.extend(chunk)
                if line.endswith(b'\r\n'):
                    result = bytes(line).strip()
                    line.clear()
                    if result == b'CONNECT':
                        return True
                    if (result in (b'ERROR', b'NO CARRIER', b'BUSY') or
                            result.startswith(b'+CME ERROR:')):
                        return False
            raise ProtocolError('Invalid AT handover response')
        except (OSError, EOFError, TimeoutError, ProtocolError):
            self.failed = True
            raise

    def _require(self):
        try:
            if hasattr(self.link, 'check'):
                self.link.check()
        except EOFError:
            self.connected = False
            self.failed = True
            raise
        if not self.connected:
            raise ProtocolError('File-transfer session is not connected')

    def _set_path(self, parts):
        self._require()
        for part in ('',*path_segments(parts)):
            status, response = self._exchange(0x85,b'\x02\0'+self.connection+name_header(part))
            if status != 0xa0:
                raise RemoteError(status)

    def _cancel(self, cancel, *, continuing=False):
        if cancel is not None and cancel.is_set():
            if continuing:
                # Only called after a fully consumed Continue response. A
                # pending handset permission dialog must finish its request;
                # do not overlap it with ABORT. OEM File Manager sends FF0003.
                status, _ = self._exchange(0xff)
                if status != 0xa0:
                    self.connected = False
                    self.failed = True
                    raise RemoteError(status)
            raise TransferCancelled('Transfer cancelled')

    def _get(self, request, destination, progress=None, cancel=None):
        total = None
        count = 0
        continuing = False
        for _ in range(1048576):
            self._cancel(cancel, continuing=continuing)
            status, data = self._exchange(0x83,self.connection+request)
            if status not in (0x90,0xa0):
                raise RemoteError(status)
            end = False
            for tag,value in headers(data):
                if tag == 0xc3:
                    total = int.from_bytes(value,'big')
                if tag in (0x48,0x49):
                    destination.write(value)
                    count += len(value)
                    end |= tag == 0x49
            if progress:
                progress(count,total)
            if status == 0xa0:
                if not end or (total is not None and count != total):
                    raise ProtocolError('Phone completed an incomplete object')
                return count
            request = b''
            continuing = True
        self.connected = False
        raise ProtocolError('Too many OBEX continuation packets')

    def list(self, path=()):
        import io
        self._set_path(path)
        output = io.BytesIO()
        def bound(count,total):
            if count > 4*1024*1024:
                raise ProtocolError('Phone directory listing exceeds limit')
        self._get(header(0x42,LISTING),output,progress=bound)
        try:
            root = ET.fromstring(output.getvalue())
            if root.tag != 'folder-listing':
                raise ValueError('Unexpected listing type')
            entries = []
            for node in root:
                if node.tag not in ('file','folder'):
                    continue
                name = node.attrib['name']
                path_segments((name,))
                entries.append(dict(name=name,directory=node.tag=='folder',
                    size=int(node.attrib['size']) if 'size' in node.attrib else None,
                    modified=node.attrib.get('modified'),created=node.attrib.get('created')))
            return entries
        except (ET.ParseError,ValueError,KeyError) as exc:
            raise ProtocolError('Invalid phone directory listing') from exc

    def download(self, path, destination, progress=None, cancel=None):
        parts = path_segments(path)
        if not parts:
            raise ValueError('File name required')
        self._cancel(cancel)
        self._set_path(parts[:-1])
        return self._get(name_header(parts[-1]),destination,progress,cancel)

    def upload(self, path, source, size, progress=None, cancel=None):
        parts = path_segments(path)
        if not parts or type(size) is not int or not 0 <= size <= 0xffffffff:
            raise ValueError('File name and uint32 size required')
        self._cancel(cancel)
        self._set_path(parts[:-1])
        metadata = name_header(parts[-1])+header(0xc3,size.to_bytes(4,'big'))
        count = 0
        continuing = False
        while True:
            self._cancel(cancel, continuing=continuing)
            capacity = self.peer_max-3-len(self.connection)-len(metadata)-3
            if capacity < 0:
                raise ValueError('File name exceeds phone packet size')
            data = source.read(min(capacity,size-count))
            if not data and count < size:
                raise EOFError('Upload source ended early')
            final = count+len(data) == size
            request = self.connection+metadata+header(0x49 if final else 0x48,data)
            status, _ = self._exchange(0x82 if final else 0x02,request)
            if status != (0xa0 if final else 0x90):
                raise RemoteError(status)
            count += len(data)
            if progress:
                progress(count,size)
            if final:
                return count
            metadata = b''
            continuing = True

    def disconnect(self):
        if self.connected:
            status, _ = self._exchange(0x81,self.connection)
            self.connected = False
            self.binary_mode = False
            self.connection = b''
            if status != 0xa0:
                self.failed = True
                raise RemoteError(status)
