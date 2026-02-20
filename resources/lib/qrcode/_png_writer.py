# SPDX-License-Identifier: GPL-3.0-or-later
# Minimal PNG writer using stdlib only (zlib + struct).
# Drop-in replacement for pypng's ``png.Writer`` used by python-qrcode.

import struct
import zlib


class PngWriter:
    """Minimal 1-bit grayscale PNG writer for QR codes."""

    def __init__(self, width, height, greyscale=True, bitdepth=1):
        self.width = width
        self.height = height

    def write(self, stream, rows):
        """Write a 1-bit grayscale PNG to stream from an iterable of rows.

        Each row is a sequence of 0 (black) or 1 (white) values.
        """

        def _chunk(chunk_type, data):
            raw = chunk_type + data
            return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF)

        # PNG signature
        stream.write(b"\x89PNG\r\n\x1a\n")

        # IHDR: width, height, bit_depth=1, color_type=0 (greyscale)
        ihdr_data = struct.pack(">IIBBBBB", self.width, self.height, 1, 0, 0, 0, 0)
        stream.write(_chunk(b"IHDR", ihdr_data))

        # IDAT: pack each row as 1-bit pixels, prepend filter byte (0=None)
        raw_data = bytearray()
        row_bytes = (self.width + 7) // 8
        for row in rows:
            raw_data.append(0)  # filter byte
            row_list = list(row)
            for byte_idx in range(row_bytes):
                byte_val = 0
                for bit in range(8):
                    px = byte_idx * 8 + bit
                    if px < self.width and row_list[px]:
                        byte_val |= 1 << (7 - bit)
                raw_data.append(byte_val)

        stream.write(_chunk(b"IDAT", zlib.compress(bytes(raw_data))))

        # IEND
        stream.write(_chunk(b"IEND", b""))
