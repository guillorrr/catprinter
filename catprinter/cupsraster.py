'''Reader for CUPS raster streams (what CUPS hands a backend after its filters).

Supports versions 1 and 3 (uncompressed) and 2 / PWG raster (run-length
encoded), 1 or 8 bits per pixel, single channel.
'''
import struct

import numpy as np

HEADER_SIZE = 1796

# cupsColorSpace values where 0 means black (luminance spaces).
LUMINANCE_SPACES = {0, 18}  # CUPS_CSPACE_W, CUPS_CSPACE_SW


def _syncs():
    out = {}
    for version, tag in ((1, b"RaSt"), (2, b"RaS2"), (3, b"RaS3")):
        out[tag] = (version, ">")
        out[tag[::-1]] = (version, "<")
    return out


SYNC_WORDS = _syncs()


def is_cups_raster(data):
    return data[:4] in SYNC_WORDS


def _decode_v2_page(data, pos, height, bytes_per_line, bytes_per_pixel):
    '''Decodes a run-length encoded (v2 / PWG) page. Returns (rows, new position).'''
    rows = []
    while len(rows) < height:
        repeat = data[pos] + 1
        pos += 1
        line = bytearray()
        while len(line) < bytes_per_line:
            n = data[pos]
            pos += 1
            if n < 128:
                # Repeat the next pixel n + 1 times.
                pixel = data[pos:pos + bytes_per_pixel]
                pos += bytes_per_pixel
                line += pixel * (n + 1)
            else:
                # 257 - n literal pixels follow.
                count = (257 - n) * bytes_per_pixel
                line += data[pos:pos + count]
                pos += count
        line = bytes(line[:bytes_per_line])
        rows.extend([line] * min(repeat, height - len(rows)))
    return rows, pos


def read_pages(data):
    '''Returns a list of pages as uint8 grayscale arrays (0 = black, 255 = white).'''
    if not is_cups_raster(data):
        raise ValueError("Not a CUPS raster stream")
    version, endian = SYNC_WORDS[data[:4]]
    pos = 4
    pages = []
    while pos + HEADER_SIZE <= len(data):
        header = data[pos:pos + HEADER_SIZE]
        pos += HEADER_SIZE

        def u32(offset):
            return struct.unpack_from(endian + "I", header, offset)[0]

        width, height = u32(372), u32(376)
        bits_per_color, bits_per_pixel = u32(384), u32(388)
        bytes_per_line, color_space = u32(392), u32(400)
        if bits_per_pixel not in (1, 8) or bits_per_color != bits_per_pixel:
            raise ValueError(
                f"Unsupported raster: {bits_per_pixel} bits per pixel, color space "
                f"{color_space}. The queue must produce 1 or 8-bit grayscale.")

        if version == 2:
            rows, pos = _decode_v2_page(data, pos, height, bytes_per_line,
                                        max(1, bits_per_pixel // 8))
            raw = b"".join(rows)
        else:
            size = bytes_per_line * height
            raw = data[pos:pos + size]
            pos += size
        page = np.frombuffer(raw, dtype=np.uint8).reshape(height, bytes_per_line)

        if bits_per_pixel == 1:
            # 1 bit per pixel: 1 = black for K-type spaces, white for luminance ones.
            bits = np.unpackbits(page, axis=1)[:, :width].astype(bool)
            black = ~bits if color_space in LUMINANCE_SPACES else bits
            gray = np.where(black, 0, 255).astype(np.uint8)
        else:
            gray = page[:, :width]
            if color_space not in LUMINANCE_SPACES:
                gray = 255 - gray
        pages.append(np.ascontiguousarray(gray))
    return pages
