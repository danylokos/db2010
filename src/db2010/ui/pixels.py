"""Host interpretation of native big-endian RGB565 panel bytes."""
import struct
import zlib


def rgb565(raw, width, height, byteorder='big'):
    if byteorder not in ('big', 'little'):
        raise ValueError('RGB565 byte order must be big or little')
    if len(raw) != width * height * 2:
        raise ValueError('RGB565 panel size differs')
    rgb = bytearray()
    for offset in range(0, len(raw), 2):
        word = int.from_bytes(raw[offset:offset+2], byteorder)
        rgb.extend((((word >> 11) & 31)*255//31, ((word >> 5) & 63)*255//63, (word & 31)*255//31))
    return bytes(rgb)


def png_rgb565(raw, width, height, byteorder='big'):
    rgb = rgb565(raw, width, height, byteorder)
    rows = b''.join(b'\0'+rgb[y*width*3:(y+1)*width*3] for y in range(height))
    def chunk(kind, data):
        return struct.pack('>I', len(data))+kind+data+struct.pack('>I', zlib.crc32(kind+data))
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            +chunk(b'IDAT', zlib.compress(rows))+chunk(b'IEND', b''))
