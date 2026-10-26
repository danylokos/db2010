"""Native format0x110 supplies high RGB565 byte first, independent of ARM endian."""
import struct
import zlib

from db2010.ui.pixels import png_rgb565


def pixels(png):
    cursor=8;data=bytearray()
    while cursor<len(png):
        size=struct.unpack_from('>I',png,cursor)[0]
        if png[cursor+4:cursor+8]==b'IDAT':
            data.extend(png[cursor+8:cursor+8+size])
        cursor+=size+12
    return zlib.decompress(data)


def test_native_gray_and_primary_colors():
    raw=bytes.fromhex('0000 f79e f800 07e0 001f')
    assert pixels(png_rgb565(raw,5,1))==bytes([0,0,0,0,246,242,246,255,0,0,0,255,0,0,0,255])
    # Retain an explicit interpretation for comparing historical previews.
    assert pixels(png_rgb565(bytes.fromhex('f79e'),1,1,'little'))==bytes([0,156,222,189])
