"""Conditional SHA-1 state candidate, NOT an identified DB2010 ROM implementation.

Diagnostic layout assumption: five little-endian chaining words, a little-endian
64-bit bit count, then 64 buffered bytes. The native caller reserves 92 bytes
and reads the first 20; neither this layout nor SHA-1 semantics is recovered.
Specification: NIST FIPS 180-4 (August 2015), sections 5 and 6.1.
"""
import struct

MASK = 0xffffffff
IV = (0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0)


def rol(value, count):
    return ((value << count) | (value >> (32-count))) & MASK


def compress(state, block):
    words = list(struct.unpack('>16I', block))
    for i in range(16, 80):
        words.append(rol(words[i-3] ^ words[i-8] ^ words[i-14] ^ words[i-16], 1))
    a, b, c, d, e = state
    for i, word in enumerate(words):
        if i < 20:
            f, k = (b & c) | (~b & d), 0x5a827999
        elif i < 40:
            f, k = b ^ c ^ d, 0x6ed9eba1
        elif i < 60:
            f, k = (b & c) | (b & d) | (c & d), 0x8f1bbcdc
        else:
            f, k = b ^ c ^ d, 0xca62c1d6
        a, b, c, d, e = (rol(a, 5)+f+e+k+word) & MASK, a, rol(b, 30), c, d
    return tuple((x+y) & MASK for x, y in zip(state, (a,b,c,d,e)))


def initialize():
    return struct.pack('<5IQ64s', *IV, 0, bytes(64))


def update(context, data):
    *state, bits, buffer = struct.unpack('<5IQ64s', context)
    assert bits % 8 == 0
    used = (bits // 8) % 64
    pending = buffer[:used] + data
    complete = len(pending) // 64 * 64
    for offset in range(0, complete, 64):
        state = compress(state, pending[offset:offset+64])
    # Zero unused buffer bytes is an explicit diagnostic assumption.
    return struct.pack('<5IQ64s', *state, (bits+8*len(data)) & ((1<<64)-1), pending[complete:])


def finalize(context):
    bits = struct.unpack_from('<Q', context, 20)[0]
    padding = b'\x80' + bytes((55-bits//8) % 64) + struct.pack('>Q', bits)
    final_context = update(context, padding)
    digest = struct.pack('>5I', *struct.unpack_from('<5I', final_context))
    return final_context, digest
