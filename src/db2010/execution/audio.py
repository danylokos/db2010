"""Bounded PCM envelope reader; samples are observations, never guest input."""
from dataclasses import dataclass
import hashlib
import json
import socket
import threading
import wave

MAX_HEADER = 4096
MAX_FRAMES = 960
KEYS = {'type', 'version', 'stream_id', 'epoch', 'sequence', 'first_frame',
        'virtual_start_ns', 'sample_rate', 'channels', 'format', 'frame_count',
        'discontinuity', 'eos'}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate audio header key')
        result[key] = value
    return result


def counter(value):
    if (not isinstance(value, str) or not value or len(value) > 20
            or not value.isascii() or not value.isdecimal() or str(int(value)) != value
            or int(value) > 2**64-1):
        raise ValueError('audio counter must be canonical uint64 text')
    return int(value)


@dataclass(frozen=True)
class AudioPacket:
    header: dict
    pcm: bytes

    @classmethod
    def decode(cls, header, pcm):
        h = json.loads(header, object_pairs_hook=_object)
        if (not isinstance(h, dict) or set(h) != KEYS or h['type'] != 'audio'
                or type(h['version']) is not int or h['version'] != 1
                or type(h['sample_rate']) is not int or h['sample_rate'] != 48000
                or type(h['channels']) is not int or h['channels'] != 2
                or h['format'] != 's16le' or type(h['frame_count']) is not int
                or not 0 <= h['frame_count'] <= MAX_FRAMES
                or type(h['discontinuity']) is not bool or type(h['eos']) is not bool
                or (not h['frame_count'] and not h['eos'])
                or not isinstance(h['stream_id'], str) or not 1 <= len(h['stream_id']) <= 64
                or not h['stream_id'].isascii()):
            raise ValueError('invalid audio format/header')
        for field in ('epoch', 'sequence', 'first_frame', 'virtual_start_ns'):
            counter(h[field])
        if len(pcm) != h['frame_count'] * 4:
            raise ValueError('audio PCM length differs')
        return cls(h, bytes(pcm))

    def wire(self):
        header = json.dumps(self.header, separators=(',', ':')).encode('utf-8')
        if len(header) > MAX_HEADER:
            raise ValueError('audio header too large')
        return len(header).to_bytes(4, 'big') + header + self.pcm


class AudioFramer:
    """At most one bounded incomplete envelope; yield packets immediately."""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        # The caller reads <=8192 bytes; reject accidental unbounded ingress.
        if len(data) > 8192:
            raise ValueError('audio ingress read too large')
        self.buffer.extend(data)
        while len(self.buffer) >= 4:
            size = int.from_bytes(self.buffer[:4], 'big')
            if not 1 <= size <= MAX_HEADER:
                raise ValueError('audio header length outside bound')
            if len(self.buffer) < size+4:
                break
            header = bytes(self.buffer[4:4+size])
            # Validate all fields before trusting payload length.
            h = json.loads(header, object_pairs_hook=_object)
            frames = h.get('frame_count') if isinstance(h, dict) else None
            if type(frames) is not int or not 0 <= frames <= MAX_FRAMES:
                raise ValueError('audio frame count outside bound')
            length = 4+size+frames*4
            if len(self.buffer) < length:
                break
            packet = AudioPacket.decode(header, self.buffer[4+size:length])
            del self.buffer[:length]
            yield packet


class AudioCapture:
    """Continuous bounded drain; optional recording is independent of live sinks."""
    def __init__(self, destination=None, observer=None):
        self.destination = destination
        self.observer = observer
        self.last = None
        self.ready = threading.Event()
        self.metrics = dict(packets=0, frames=0, epochs=0, gaps=0, truncated_connections=0,
                            disconnects=0, eos=0, sample_rate=48000, channels=2, format='s16le')
        self.digest = hashlib.sha256()
        self.hash = 14695981039346656037
        self.pcm = self.metadata = self.wav = None
        if destination:
            destination.mkdir(parents=True, exist_ok=True)
            self.pcm = (destination/'audio.pcm').open('wb')
            self.metadata = (destination/'packets.jsonl').open('w')
            self.wav = wave.open(str(destination/'audio.wav'), 'wb')
            self.wav.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))

    def accept(self, packet):
        h = packet.header
        key = h['stream_id'], h['epoch']
        sequence, first = counter(h['sequence']), counter(h['first_frame'])
        if self.last and key == self.last[0]:
            _, before_seq, end, ended = self.last
            if sequence <= before_seq or first < end or ended:
                raise ValueError('stale, overlapping or post-EOS audio packet')
            if (sequence != before_seq+1 or first != end) and not h['discontinuity']:
                raise ValueError('unannounced audio sample gap')
            if sequence != before_seq+1 or first != end:
                self.metrics['gaps'] += 1
        else:
            self.metrics['epochs'] += 1
            restart = sequence == 0 and first == 0 and h['discontinuity']
            if self.last and first != self.last[2] and not restart:
                self.metrics['gaps'] += 1
        self.last = key, sequence, first+h['frame_count'], h['eos']
        offset = self.metrics['frames'] * 4
        self.metrics['packets'] += 1
        self.metrics['frames'] += h['frame_count']
        self.metrics['eos'] += h['eos']
        self.digest.update(packet.pcm)
        if self.destination:
            for byte in packet.pcm:
                self.hash = ((self.hash ^ byte)*1099511628211) & (2**64-1)
            self.pcm.write(packet.pcm)
            self.wav.writeframesraw(packet.pcm)
            self.metadata.write(json.dumps(dict(h, pcm_offset=offset))+'\n')
        if self.observer:
            self.observer(packet)

    def collect(self, path, stopping, connections, lock):
        while not stopping.is_set():
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(.2)
            try:
                connection.connect(str(path))
            except (FileNotFoundError, ConnectionRefusedError, socket.timeout):
                connection.close()
                stopping.wait(.01)
                continue
            with lock:
                connections.append(connection)
            self.ready.set()
            framer = AudioFramer()
            try:
                with connection:
                    while True:
                        try:
                            data = connection.recv(8192)
                        except socket.timeout:
                            continue
                        if not data:
                            break
                        for packet in framer.feed(data):
                            self.accept(packet)
            finally:
                with lock:
                    connections.remove(connection)
                self.metrics['disconnects'] += 1
                if framer.buffer:
                    self.metrics['truncated_connections'] += 1
                if self.observer:
                    self.observer(None)  # Flush presentation on pause/disconnect.
            # An EOF may be a VM pause/restore, not process termination.
            stopping.wait(.01)

    def close(self):
        if self.pcm:
            self.pcm.close(); self.metadata.close(); self.wav.close()
            self.pcm = self.metadata = self.wav = None
        self.metrics['sha256'] = self.digest.hexdigest()
        if self.destination:
            self.metrics['fnv1a64'] = f'{self.hash:016x}'
            (self.destination/'audio.json').write_text(json.dumps(self.metrics, indent=2)+'\n')
        return dict(self.metrics)
