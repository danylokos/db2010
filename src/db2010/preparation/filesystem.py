"""Read and provision the recovered W800i NOR/FTL FAT16 storage layout.

This is host-side image construction, not a filesystem service in the emulator.
Other mounted volumes and unsupported source geometries remain untouched.
"""
import math
import struct

FS_BASE = 0x45400000
FS_SIZE = 11*1024*1024


def padded_fs(payload):
    if len(payload) != 0xa23cc0:
        raise ValueError('unsupported stock W800i FS payload size')
    raw = bytearray(payload+b'\xff'*(FS_SIZE-len(payload)))
    for span in range(0xa00000, FS_SIZE, 0x40000):
        raw[span+0x3fffe:span+0x40000] = b'\x54\x12'
    return bytes(raw)


class Fat16:
    """Bounded reader for this authenticated source's first FAT16 volume."""
    def __init__(self, raw):
        self.raw, self.blocks = raw, {}
        if len(raw) != FS_SIZE:
            raise ValueError('FS must be a complete 11 MiB raw image')
        self.marker = None
        for span in range(0, len(raw), 0x40000):
            if raw[span+0x3fffe:span+0x40000] != b'\x54\x12':
                raise ValueError('invalid FS span footer')
            for slot in range(255):
                off = span+slot*1028
                header = struct.unpack_from('<I', raw, off+1024)[0]
                if header == 0xabcd1254:
                    self.marker = off
                    break
                if header >> 24 == 0xaa:
                    logical = header & 0xffffff
                    if logical in self.blocks:
                        raise ValueError('duplicate committed logical block')
                    self.blocks[logical] = off
            if self.marker is not None:
                break
        if self.marker is None:
            raise ValueError('missing FS end marker')
        self.changed = {}
        self.base = 11
        boot = self.read(0, 512)
        if boot[3:11] != b'MSWIN4.1' or boot[510:512] != b'\x55\xaa':
            raise ValueError('unsupported FAT boot sector')
        self.bps = struct.unpack_from('<H', boot, 11)[0]
        self.spc, self.reserved, self.fats = boot[13], struct.unpack_from('<H', boot, 14)[0], boot[16]
        self.roots, self.spf = struct.unpack_from('<H', boot, 17)[0], struct.unpack_from('<H', boot, 22)[0]
        self.total = struct.unpack_from('<I', boot, 32)[0]
        if (self.bps, self.spc, self.reserved, self.fats, self.roots, self.spf, self.total) != (512, 2, 1, 1, 512, 159, 81262):
            raise ValueError('unsupported W800i FAT16 geometry')
        self.root_sector = self.reserved+self.fats*self.spf
        self.data_sector = self.root_sector+self.roots*32//self.bps

    def read(self, offset, size):
        result = bytearray()
        while size:
            logical, within = self.base+offset//1024, offset % 1024
            n = min(size, 1024-within)
            if logical in self.changed:
                result.extend(self.changed[logical][within:within+n])
            elif logical in self.blocks:
                off = self.blocks[logical]+within
                result.extend(self.raw[off:off+n])
            else:
                raise ValueError(f'unknown logical FS block {logical}')
            offset += n; size -= n
        return bytes(result)

    def chain(self, cluster):
        seen, result = set(), bytearray()
        while cluster < 0xfff8:
            if not 2 <= cluster < 0xfff0 or cluster in seen:
                raise ValueError('invalid FAT chain')
            seen.add(cluster)
            result.extend(self.read((self.data_sector+(cluster-2)*self.spc)*self.bps, self.spc*self.bps))
            cluster = struct.unpack('<H', self.read(self.reserved*self.bps+2*cluster, 2))[0]
        return bytes(result)

    def entries(self, cluster=0):
        raw = self.chain(cluster) if cluster else self.read(self.root_sector*self.bps, self.roots*32)
        result, parts, checksum = [], {}, None
        for offset in range(0, len(raw), 32):
            entry = raw[offset:offset+32]
            if entry[0] == 0:
                break
            if entry[0] == 0xe5:
                parts, checksum = {}, None
                continue
            if entry[11] == 15:
                if entry[0] & 0x40:
                    parts, checksum = {}, entry[13]
                if checksum != entry[13]:
                    raise ValueError('inconsistent FAT long filename checksum')
                parts[entry[0] & 31] = (entry[1:11]+entry[14:26]+entry[28:32]).decode('utf-16-le')
                continue
            base, ext = entry[:8].decode('ascii').rstrip(), entry[8:11].decode('ascii').rstrip()
            short = base+('.'+ext if ext else '')
            if parts:
                actual = 0
                for byte in entry[:11]:
                    actual = (((actual & 1) << 7)+(actual >> 1)+byte) & 255
                if checksum != actual or sorted(parts) != list(range(1, len(parts)+1)):
                    raise ValueError('invalid FAT long filename sequence/checksum')
                name = ''.join(parts[k] for k in sorted(parts)).split('\0')[0].rstrip('\uffff')
            else:
                name = (base.lower() if entry[12] & 8 else base)+('.'+(ext.lower() if entry[12] & 16 else ext) if ext else '')
            if name not in ('.', '..') and not entry[11] & 8:
                result.append(dict(name=name, short_name=short, attributes=entry[11],
                    cluster=struct.unpack_from('<H', entry, 26)[0], size_bytes=struct.unpack_from('<I', entry, 28)[0],
                    directory_offset=offset, lfn_checksum=checksum))
            parts, checksum = {}, None
        return result

    def find(self, path):
        directory = self.entries()
        for part in path.strip('/').split('/'):
            entry = next((e for e in directory if part.casefold() in (e['name'].casefold(), e['short_name'].casefold())), None)
            if entry is None:
                return None
            directory = self.entries(entry['cluster']) if entry['attributes'] & 16 else []
        return entry

    def write(self, offset, data):
        while data:
            logical, within = self.base+offset//1024, offset % 1024
            n = min(len(data), 1024-within)
            if logical not in self.changed:
                self.changed[logical] = bytearray(self.raw[self.blocks[logical]:self.blocks[logical]+1024]
                    if logical in self.blocks else b'\xff'*1024)
            self.changed[logical][within:within+n] = data[:n]
            data = data[n:]; offset += n

    def fat(self, cluster, value=None):
        offset = self.reserved*self.bps+2*cluster
        if value is None:
            return struct.unpack('<H', self.read(offset, 2))[0]
        self.write(offset, struct.pack('<H', value))

    def allocate(self, count):
        clusters = []
        maximum = (self.total-self.data_sector)//self.spc+2
        for cluster in range(2, maximum):
            if self.fat(cluster) == 0:
                clusters.append(cluster)
                if len(clusters) == count:
                    break
        if len(clusters) != count:
            raise ValueError('not enough free FAT16 clusters')
        for i, cluster in enumerate(clusters):
            self.fat(cluster, clusters[i+1] if i+1 < count else 0xffff)
            self.write(self.cluster_offset(cluster), bytes(self.spc*self.bps))
        return clusters

    def cluster_offset(self, cluster):
        return (self.data_sector+(cluster-2)*self.spc)*self.bps

    def cluster_list(self, first):
        result = []
        while first < 0xfff8:
            if not 2 <= first < 0xfff0 or first in result:
                raise ValueError('invalid FAT chain')
            result.append(first); first = self.fat(first)
        return result

    @staticmethod
    def short_alias(name, entries):
        base, dot, ext = name.rpartition('.')
        if not dot:
            base, ext = name, ''
        clean = lambda s: ''.join(c if c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_' else '_' for c in s.upper())
        base, ext = clean(base), clean(ext)[:3]
        used = {e['short_name'].upper() for e in entries}
        for n in range(100000):
            tail = '~'+str(n) if n else ''
            candidate = base[:8-len(tail)]+tail
            if (n or len(base) <= 8) and candidate+('.'+ext if ext else '') not in used:
                return candidate.ljust(8).encode('ascii')+ext.ljust(3).encode('ascii')
        raise ValueError('no unique FAT alias')

    @staticmethod
    def directory_records(name, alias, cluster, size):
        units = name.encode('utf-16-le')+b'\x00\x00'
        units += b'\xff\xff'*((-len(units)//2) % 13)
        if len(units)//2 > 260:
            raise ValueError('FAT long filename too long')
        checksum = 0
        for byte in alias:
            checksum = (((checksum & 1) << 7)+(checksum >> 1)+byte) & 255
        records = bytearray()
        for n in range(len(units)//26, 0, -1):
            part = units[(n-1)*26:n*26]
            entry = bytearray(32)
            entry[0] = n | (0x40 if n == len(units)//26 else 0)
            entry[11], entry[13] = 15, checksum
            entry[1:11], entry[14:26], entry[28:32] = part[:10], part[10:22], part[22:]
            records.extend(entry)
        entry = bytearray(32); entry[:11] = alias; entry[11] = 0x20
        # Fixed 2005-01-01 timestamp keeps image construction deterministic.
        struct.pack_into('<H', entry, 24, (2005-1980)*512+33)
        struct.pack_into('<H', entry, 26, cluster)
        struct.pack_into('<I', entry, 28, size)
        return bytes(records+entry)

    def install(self, path, data):
        parts = path.strip('/').split('/')
        if len(parts) != 4 or [p.lower() for p in parts[:3]] != ['tpa', 'preset', 'custom']:
            raise ValueError('minimal provisioning only supports tpa/preset/custom files')
        parent = self.find('/tpa/preset/custom')
        if not parent or not parent['attributes'] & 16:
            raise ValueError('stock customization directory not found')
        entries = self.entries(parent['cluster'])
        old = next((e for e in entries if e['name'].casefold() == parts[-1].casefold()), None)
        if old:
            raise ValueError('refusing to replace an existing customization file: '+path)
        clusters = self.allocate(max(1, math.ceil(len(data)/(self.bps*self.spc))))
        for i, cluster in enumerate(clusters):
            part = data[i*1024:(i+1)*1024]
            self.write(self.cluster_offset(cluster), part.ljust(1024, b'\0'))
        alias = self.short_alias(parts[-1], entries)
        records = self.directory_records(parts[-1], alias, clusters[0], len(data))
        chain = self.cluster_list(parent['cluster'])
        directory = bytearray(self.chain(parent['cluster']))
        needed, start, run = len(records)//32, None, 0
        for off in range(0, len(directory), 32):
            if directory[off] in (0, 0xe5):
                run += 1
            else:
                run = 0
            if run == needed:
                start = off-(needed-1)*32; break
        if start is None:
            start = len(directory)-run*32
            extra = self.allocate(math.ceil((start+len(records)-len(directory))/1024))
            self.fat(chain[-1], extra[0]); chain += extra
            directory.extend(bytes(1024*len(extra)))
        directory[start:start+len(records)] = records
        for i, cluster in enumerate(chain):
            self.write(self.cluster_offset(cluster), directory[i*1024:(i+1)*1024])

    def render(self):
        result = bytearray(self.raw)
        new = [logical for logical in sorted(self.changed) if logical not in self.blocks]
        slots = [span+slot*1028 for span in range(0, len(result), 0x40000) for slot in range(255)]
        free = [off for off in slots if off < self.marker and result[off:off+1028] == b'\xff'*1028]
        marker = self.marker
        if len(new) > len(free):
            # The original marker invalidates every later slot. Move it only
            # when extending into an explicitly erased remainder of the seed.
            result[self.marker:] = b'\xff'*(len(result)-self.marker)
            free += [off for off in slots if off >= self.marker]
            if len(new) > len(free):
                raise ValueError('provisioned data exceeds 11 MiB NOR capacity')
            last = free[len(new)-1]
            marker = (last//0x40000+1)*0x40000
            if marker >= FS_SIZE:
                raise ValueError('provisioned data exceeds 11 MiB NOR capacity')
        mapping = {**self.blocks, **dict(zip(new, free))}
        for logical, data in self.changed.items():
            off = mapping[logical]
            result[off:off+1024] = data
            struct.pack_into('<I', result, off+1024, 0xaa000000 | logical)
        struct.pack_into('<I', result, marker+1024, 0xabcd1254)
        for span in range(0, FS_SIZE, 0x40000):
            result[span+0x3fffe:span+0x40000] = b'\x54\x12'
        return bytes(result)
