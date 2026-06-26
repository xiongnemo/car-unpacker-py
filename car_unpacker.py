#!/usr/bin/env python3
"""
Apple Assets.car unpacker — pure Python implementation.

Parses BOMStore containers, extracts CoreUI renditions, decompresses
LZFSE-compressed deepmap2 image payloads, and outputs PNG files.

Dependencies: none (stdlib only). Uses external `lzfse` binary for decompression.
"""

import struct
import os
import sys
import subprocess
import tempfile
import zlib
from pathlib import Path

# ============================================================
# BOMStore Parser
# ============================================================

class BOMPointer:
    __slots__ = ('address', 'length')
    def __init__(self, address: int, length: int):
        self.address = address
        self.length = length

class BOMVar:
    __slots__ = ('index', 'name')
    def __init__(self, index: int, name: str):
        self.index = index
        self.name = name

class BOM:
    def __init__(self, data: bytes):
        self.data = data
        if len(data) < 512:
            raise ValueError(f"File too small for BOM header: {len(data)} bytes")
        if data[:8] != b'BOMStore':
            raise ValueError(f"Invalid BOM magic: {data[:8]!r}")

        self.version = struct.unpack_from('>I', data, 8)[0]
        self.num_blocks = struct.unpack_from('>I', data, 12)[0]
        self.index_offset = struct.unpack_from('>I', data, 16)[0]
        self.index_length = struct.unpack_from('>I', data, 20)[0]
        self.vars_offset = struct.unpack_from('>I', data, 24)[0]
        self.vars_length = struct.unpack_from('>I', data, 28)[0]

        # Parse block table
        num_ptrs = struct.unpack_from('>I', data, self.index_offset)[0]
        self.pointers = []
        for i in range(num_ptrs):
            off = self.index_offset + 4 + i * 8
            addr = struct.unpack_from('>I', data, off)[0]
            length = struct.unpack_from('>I', data, off + 4)[0]
            self.pointers.append(BOMPointer(addr, length))

        # Parse variables
        var_count = struct.unpack_from('>I', data, self.vars_offset)[0]
        p = self.vars_offset + 4
        self.vars = []
        for _ in range(var_count):
            idx = struct.unpack_from('>I', data, p)[0]
            p += 4
            nlen = data[p]
            p += 1
            name = data[p:p + nlen].decode('ascii')
            p += nlen
            self.vars.append(BOMVar(idx, name))

    def block(self, idx: int) -> bytes:
        if idx >= len(self.pointers):
            return b''
        ptr = self.pointers[idx]
        if ptr.address == 0 and ptr.length == 0:
            return b''
        return self.data[ptr.address:ptr.address + ptr.length]

    def named_block(self, name: str):
        for v in self.vars:
            if v.name == name:
                return v.index, self.block(v.index)
        return None, b''


# ============================================================
# B+ Tree Parser
# ============================================================

class TreeHeader:
    def __init__(self, data: bytes):
        if len(data) < 21 or data[:4] != b'tree':
            raise ValueError("Invalid tree header")
        self.version = struct.unpack_from('>I', data, 4)[0]
        self.child = struct.unpack_from('>I', data, 8)[0]
        self.block_size = struct.unpack_from('>I', data, 12)[0]
        self.path_count = struct.unpack_from('>I', data, 16)[0]


def parse_tree_node(data: bytes):
    """Returns (is_leaf, count, forward, backward, entries) where entries = [(value_idx, key_idx), ...]"""
    if len(data) < 12:
        return None
    is_leaf, count = struct.unpack_from('>HH', data, 0)
    fwd = struct.unpack_from('>I', data, 4)[0]
    bwd = struct.unpack_from('>I', data, 8)[0]
    entries = []
    p = 12
    for _ in range(count):
        val_idx = struct.unpack_from('>I', data, p)[0]
        key_idx = struct.unpack_from('>I', data, p + 4)[0]
        entries.append((val_idx, key_idx))
        p += 8
    return is_leaf, count, fwd, bwd, entries


# ============================================================
# CSI Header Parser
# ============================================================

class CSIHeader:
    def __init__(self, data: bytes):
        if len(data) < 184:
            raise ValueError(f"CSI data too small: {len(data)}")
        self.tag = struct.unpack_from('<I', data, 0)[0]
        self.version = struct.unpack_from('<I', data, 4)[0]
        self.flags = struct.unpack_from('<I', data, 8)[0]
        self.width = struct.unpack_from('<I', data, 12)[0]
        self.height = struct.unpack_from('<I', data, 16)[0]
        self.scale = struct.unpack_from('<I', data, 20)[0]
        self.pixel_format = struct.unpack_from('<I', data, 24)[0]
        self.color_space = struct.unpack_from('<I', data, 28)[0]
        self.mod_time = struct.unpack_from('<I', data, 32)[0]
        self.layout = struct.unpack_from('<H', data, 36)[0]
        name_bytes = data[40:168]
        self.name = name_bytes.split(b'\x00')[0].decode('ascii', errors='replace')
        self.tv_length = struct.unpack_from('<I', data, 168)[0]
        self.bitmap_count = struct.unpack_from('<I', data, 172)[0]
        self.reserved = struct.unpack_from('<I', data, 176)[0]
        self.rendition_length = struct.unpack_from('<I', data, 180)[0]

    @property
    def pixel_format_str(self):
        b = struct.pack('<I', self.pixel_format)
        return b.decode('ascii', errors='replace')


# ============================================================
# Deepmap2 Decoder
# ============================================================

# Decode types
DM2_NONE = 1
DM2_DEFAULT = 2
DM2_LOSSLESS = 3
DM2_PALETTE = 4

# Pixel formats
DM2_G8 = 1
DM2_GA88 = 2
DM2_RGB888 = 3
DM2_RGBA8888 = 4

PREDICTOR_GROUP_SIZE = 3


class Dm2Header:
    def __init__(self, data: bytes):
        if len(data) < 12 or data[:4] != b'dmp2':
            raise ValueError(f"Invalid dmp2 header: {data[:4]!r}")
        self.decode_type = data[4]
        self.version = data[5]
        self.predictor_type = data[6]
        self.pixel_format = data[7]
        self.width = struct.unpack_from('<H', data, 8)[0]
        self.height = struct.unpack_from('<H', data, 10)[0]
        self.palette_size = 0
        self.palette_type = 0
        self.palette = []
        if self.decode_type == DM2_PALETTE and len(data) >= 16:
            self.palette_size = struct.unpack_from('<H', data, 12)[0]
            self.palette_type = struct.unpack_from('<H', data, 14)[0]
            for i in range(self.palette_size):
                self.palette.append(struct.unpack_from('<I', data, 16 + i * 4)[0])

    @property
    def header_size(self):
        if self.decode_type == DM2_PALETTE:
            return 16 + self.palette_size * 4
        return 12

    @property
    def chroma_scale(self):
        return 1 if self.version != 0 else 0

    @property
    def bytes_per_pixel(self):
        return {DM2_G8: 1, DM2_GA88: 2, DM2_RGB888: 3, DM2_RGBA8888: 4}.get(self.pixel_format, self.pixel_format)

    @property
    def has_alpha(self):
        return self.pixel_format in (DM2_GA88, DM2_RGBA8888)

    @property
    def is_color(self):
        return self.pixel_format in (DM2_RGB888, DM2_RGBA8888)

    @property
    def split_stream_components(self):
        return 3 if self.is_color else 1


def clamp_u8(v: int) -> int:
    return max(0, min(255, v))


def trunc_div2(v: int) -> int:
    # Truncate toward zero (matches Go integer division)
    return int(v / 2)


def wrap_i16(v: int) -> int:
    v = v & 0xFFFF
    if v >= 0x8000:
        return v - 0x10000
    return v


def ycocg_to_rgb(y, co, cg, scale):
    co_s = co << scale
    cg_s = cg << scale
    co_h = trunc_div2(co_s)
    cg_h = trunc_div2(cg_s)
    temp = y - cg_h
    return clamp_u8(temp + co_s - co_h), clamp_u8(temp + cg_s), clamp_u8(temp - co_h)


def apply_predictor(pred_type, row, prev_row, stride=PREDICTOR_GROUP_SIZE):
    count = len(row)
    if pred_type == 0:
        return list(row)
    elif pred_type == 1:
        return _unpredict_paeth(row, prev_row, count, stride)
    elif pred_type == 2:
        return _unpredict_left(row, prev_row, count, stride)
    elif pred_type == 3:
        return _unpredict_up(row, prev_row, count)
    elif pred_type == 4:
        return _unpredict_mean(row, prev_row, count, stride)
    return list(row)


def _unpredict_left(data, _, count, stride):
    out = [0] * count
    head = min(stride, count)
    out[:head] = data[:head]
    for i in range(stride, count):
        out[i] = wrap_i16(data[i] + out[i - stride])
    return out


def _unpredict_up(data, prev_row, count):
    out = [0] * count
    for i in range(count):
        up = prev_row[i] if prev_row else 0
        out[i] = wrap_i16(data[i] + up)
    return out


def _unpredict_mean(data, prev_row, count, stride):
    out = [0] * count
    for i in range(min(stride, count)):
        up = prev_row[i] if prev_row else 0
        out[i] = wrap_i16(data[i] + up)
    for i in range(stride, count):
        left = out[i - stride]
        up = prev_row[i] if prev_row else 0
        pred = trunc_div2(left + up + 1)
        out[i] = wrap_i16(data[i] + pred)
    return out


def _paeth_predictor(left, up, up_left):
    dist_left = abs(up - up_left)
    dist_up = abs(left - up_left)
    return left if dist_left <= dist_up else up


def _unpredict_paeth(data, prev_row, count, stride):
    out = [0] * count
    for i in range(min(stride, count)):
        up = prev_row[i] if prev_row else 0
        out[i] = wrap_i16(data[i] + up)
    i = stride
    while i < count:
        group_size = min(stride, count - i)
        left0 = out[i - stride]
        up0 = prev_row[i] if prev_row else 0
        up_left0 = prev_row[i - stride] if prev_row and i >= stride else 0
        predicted_first = _paeth_predictor(left0, up0, up_left0)
        use_left = predicted_first == left0
        for offset in range(group_size):
            if i + offset >= count:
                break
            left = out[i + offset - stride]
            up = prev_row[i + offset] if prev_row else 0
            base = left if use_left else up
            out[i + offset] = wrap_i16(data[i + offset] + base)
        i += PREDICTOR_GROUP_SIZE
    return out


def row_to_rgba(pixel_format, decoded_row, alpha_row, chroma_scale, width):
    """Convert one decoded row to RGBA bytes."""
    rgba = bytearray(width * 4)
    for px in range(width):
        sb = px * PREDICTOR_GROUP_SIZE
        rb = px * 4
        lum = decoded_row[sb]
        if pixel_format == DM2_G8:
            g = lum & 0xFF
            rgba[rb:rb + 4] = bytes([g, g, g, 0xFF])
        elif pixel_format == DM2_GA88:
            g = lum & 0xFF
            a = alpha_row[px] if alpha_row else 0xFF
            rgba[rb:rb + 4] = bytes([g, g, g, a])
        elif pixel_format == DM2_RGB888:
            r, g, b = ycocg_to_rgb(lum, decoded_row[sb + 1], decoded_row[sb + 2], chroma_scale)
            rgba[rb:rb + 4] = bytes([b, g, r, 0xFF])
        elif pixel_format == DM2_RGBA8888:
            r, g, b = ycocg_to_rgb(lum, decoded_row[sb + 1], decoded_row[sb + 2], chroma_scale)
            a = alpha_row[px] if alpha_row else 0xFF
            rgba[rb:rb + 4] = bytes([b, g, r, a])
    return bytes(rgba)


def output_bytes_to_rgba(pixel_format, width, height, data):
    """Convert raw pixel bytes to RGBA."""
    rgba = bytearray(width * height * 4)
    n = width * height
    if pixel_format == DM2_G8:
        for i in range(n):
            g = data[i]
            rgba[i * 4:i * 4 + 4] = bytes([g, g, g, 0xFF])
    elif pixel_format == DM2_GA88:
        for i in range(n):
            g = data[i * 2]
            a = data[i * 2 + 1]
            rgba[i * 4:i * 4 + 4] = bytes([g, g, g, a])
    elif pixel_format == DM2_RGB888:
        for i in range(n):
            b, g, r = data[i * 3], data[i * 3 + 1], data[i * 3 + 2]
            rgba[i * 4:i * 4 + 4] = bytes([r, g, b, 0xFF])
    elif pixel_format == DM2_RGBA8888:
        for i in range(n):
            b, g, r, a = data[i * 4], data[i * 4 + 1], data[i * 4 + 2], data[i * 4 + 3]
            rgba[i * 4:i * 4 + 4] = bytes([r, g, b, a])
    return bytes(rgba)


def decode_default_decompressed(header: Dm2Header, decompressed: bytes, width: int, height: int) -> bytes:
    """Decode Default type decompressed data to RGBA."""
    has_alpha = header.has_alpha
    components = header.split_stream_components
    pixel_count = width * height
    alpha_size = pixel_count if has_alpha else 0
    split_count = pixel_count * components

    pred_off = alpha_size
    pred_end = pred_off + height
    high_off = pred_end
    high_end = high_off + split_count
    low_off = high_end
    low_end = low_off + split_count

    if len(decompressed) < low_end:
        raise ValueError(f"Default decompressed data too short: need {low_end}, got {len(decompressed)}")

    alpha_plane = decompressed[:alpha_size] if has_alpha else None
    predictor_bytes = decompressed[pred_off:pred_end]
    high_stream = decompressed[high_off:high_end]
    low_stream = decompressed[low_off:low_end]

    chroma_scale = header.chroma_scale
    result = bytearray()
    prev_row = None
    split_row_width = width * components

    for row in range(height):
        ro = row * split_row_width
        high_row = high_stream[ro:ro + split_row_width]
        low_row = low_stream[ro:ro + split_row_width]

        # Zigzag decode
        decoded = []
        for i in range(split_row_width):
            combined = low_row[i] | (high_row[i] << 8)
            magnitude = combined >> 1
            # Convert unsigned magnitude to signed i16
            if magnitude >= 0x8000:
                mag_signed = magnitude - 0x10000
            else:
                mag_signed = magnitude
            if combined & 1:
                decoded.append(-mag_signed)
            else:
                decoded.append(mag_signed)

        # Expand grayscale
        if not header.is_color:
            expanded = []
            for v in decoded:
                expanded.extend([v, 0, 0])
            decoded = expanded

        # Apply predictor
        pred_type = predictor_bytes[row]
        predicted = apply_predictor(pred_type, decoded, prev_row)

        # Alpha row
        alpha_row = alpha_plane[row * width:(row + 1) * width] if has_alpha else None

        # To RGBA
        result.extend(row_to_rgba(header.pixel_format, predicted, alpha_row, chroma_scale, width))
        prev_row = predicted

    return bytes(result)


def decode_dm2(header: Dm2Header, payload: bytes, lzfse_bin: str) -> bytes:
    """Decode a deepmap2 payload to RGBA pixels."""
    w, h = header.width, header.height
    if header.decode_type == DM2_NONE:
        if len(payload) < w * h * header.bytes_per_pixel:
            raise ValueError("None type data too short")
        return output_bytes_to_rgba(header.pixel_format, w, h, payload[:w * h * header.bytes_per_pixel])
    elif header.decode_type == DM2_DEFAULT:
        decompressed = decompress_lzfse(lzfse_bin, payload)
        return decode_default_decompressed(header, decompressed, w, h)
    elif header.decode_type == DM2_LOSSLESS:
        decompressed = decompress_lzfse(lzfse_bin, payload)
        expected = w * h * header.bytes_per_pixel
        if len(decompressed) < expected:
            raise ValueError(f"Lossless data too short: need {expected}, got {len(decompressed)}")
        return output_bytes_to_rgba(header.pixel_format, w, h, decompressed[:expected])
    elif header.decode_type == DM2_PALETTE:
        decompressed = decompress_lzfse(lzfse_bin, payload)
        return _decode_palette(header, decompressed, w, h)
    else:
        raise ValueError(f"Unsupported decode type: {header.decode_type}")


def _decode_palette(header, decompressed, width, height):
    n = width * height
    rgba = bytearray(n * 4)
    if header.palette_type == 3:
        for i in range(n):
            idx = decompressed[n + i]
            entry = header.palette[idx]
            a = decompressed[i]
            r = (entry >> 16) & 0xFF
            g = (entry >> 8) & 0xFF
            b = entry & 0xFF
            rgba[i * 4:i * 4 + 4] = bytes([r, g, b, a])
    elif header.palette_type == 4:
        for i in range(n):
            entry = header.palette[decompressed[i]]
            r = (entry >> 16) & 0xFF
            g = (entry >> 8) & 0xFF
            b = entry & 0xFF
            a = (entry >> 24) & 0xFF
            rgba[i * 4:i * 4 + 4] = bytes([r, g, b, a])
    return bytes(rgba)


# ============================================================
# LZFSE Decompression (via external binary)
# ============================================================

def find_lzfse_binary() -> str:
    candidates = [
        '_tools/lzfse/lzfse.exe',
        '_tools/lzfse/lzfse',
        '_tools/lzfse/build/lzfse',
        '_tools/lzfse/build/lzfse.exe',
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    import shutil
    p = shutil.which('lzfse')
    if p:
        return p
    return ''


def decompress_lzfse(lzfse_bin: str, data: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(suffix='.lzfse', delete=False) as f_in:
        f_in.write(data)
        in_path = f_in.name
    out_path = in_path + '.raw'
    try:
        result = subprocess.run(
            [lzfse_bin, '-decode', '-i', in_path, '-o', out_path],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"lzfse failed: {result.stderr.decode(errors='replace')}")
        with open(out_path, 'rb') as f:
            return f.read()
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


# ============================================================
# PNG Writer (stdlib only)
# ============================================================

def write_png(path: str, width: int, height: int, rgba: bytes):
    """Write RGBA data as PNG using stdlib zlib."""
    # Check if grayscale
    is_gray = True
    for i in range(width * height):
        r, g, b = rgba[i * 4], rgba[i * 4 + 1], rgba[i * 4 + 2]
        if r != g or g != b:
            is_gray = False
            break

    if is_gray:
        bpp = 1
        raw = bytearray()
        for y in range(height):
            raw.append(0)  # filter: None
            for x in range(width):
                raw.append(rgba[(y * width + x) * 4])
        color_type = 0
    else:
        bpp = 3
        raw = bytearray()
        for y in range(height):
            raw.append(0)  # filter: None
            for x in range(width):
                off = (y * width + x) * 4
                raw.extend([rgba[off], rgba[off + 1], rgba[off + 2]])
        color_type = 2

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xFFFFFFFF)

    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n')
        ihdr = struct.pack('>IIBBBBB', width, height, 8, color_type, 0, 0, 0)
        f.write(_chunk(b'IHDR', ihdr))
        compressed = zlib.compress(bytes(raw), 9)
        f.write(_chunk(b'IDAT', compressed))
        f.write(_chunk(b'IEND', b''))


# ============================================================
# Main Extraction Logic
# ============================================================

LAYOUT_NAMES = {
    10: 'OnePartFixedSize', 11: 'OnePartTile', 12: 'OnePartScale',
    20: 'ThreePartHTile', 21: 'ThreePartHScale', 22: 'ThreePartHUniform',
    23: 'ThreePartVTile', 24: 'ThreePartVScale', 25: 'ThreePartVUniform',
    30: 'NinePartTile', 31: 'NinePartScale',
    1000: 'Data', 1001: 'ExternalLink', 1002: 'LayerStack',
    1004: 'PackedImage', 1005: 'NamedContent', 1006: 'ThinningPlaceholder',
    1007: 'Texture', 1008: 'TextureImage', 1009: 'Color',
    1010: 'MultisizeImageSet', 1011: 'LayerReference',
    1012: 'ContentRendition', 1013: 'RecognitionObject',
}


def parse_tlv(data: bytes, length: int):
    entries = []
    p = 0
    while p + 8 <= length:
        t = struct.unpack_from('<I', data, p)[0]
        l = struct.unpack_from('<I', data, p + 4)[0]
        p += 8
        if p + l > length:
            break
        entries.append((t, data[p:p + l]))
        p += l
    return entries


def sanitize_filename(s: str) -> str:
    for c in r'\/:*?"<>|':
        s = s.replace(c, '_')
    return s or 'unnamed'


def extract_assets(car_path: str, out_dir: str):
    with open(car_path, 'rb') as f:
        data = f.read()

    bom = BOM(data)
    print(f"BOMStore: version={bom.version}, blocks={bom.num_blocks}")

    print("\nNamed blocks:")
    for v in bom.vars:
        ptr = bom.pointers[v.index]
        print(f"  {v.name:<20} -> block[{v.index}] addr=0x{ptr.address:X} len={ptr.length}")

    # CARHEADER
    idx, cdata = bom.named_block('CARHEADER')
    if cdata:
        print(f"\nCARHEADER (block[{idx}], {len(cdata)} bytes):")
        tag = cdata[:4].decode('ascii', errors='replace')
        coreui_ver = struct.unpack_from('<I', cdata, 4)[0]
        storage_ver = struct.unpack_from('<I', cdata, 8)[0]
        rend_count = struct.unpack_from('<I', cdata, 16)[0]
        main_ver = cdata[20:148].split(b'\x00')[0].decode('ascii', errors='replace')
        ver_str = cdata[148:404].split(b'\x00')[0].decode('ascii', errors='replace')
        print(f"  Tag: {tag}")
        print(f"  CoreUIVersion: {coreui_ver}, StorageVersion: {storage_ver}")
        print(f"  RenditionCount: {rend_count}")
        print(f"  MainVersion: {main_ver}")
        print(f"  VersionString: {ver_str}")

    # EXTENDED_METADATA
    idx, mdata = bom.named_block('EXTENDED_METADATA')
    if mdata:
        platform = mdata[516:772].split(b'\x00')[0].decode('ascii', errors='replace')
        platform_ver = mdata[260:516].split(b'\x00')[0].decode('ascii', errors='replace')
        print(f"\nEXTENDED_METADATA: {platform} {platform_ver}")

    # KEYFORMAT
    idx, kdata = bom.named_block('KEYFORMAT')
    if kdata:
        max_tokens = struct.unpack_from('<I', kdata, 8)[0]
        print(f"\nKEYFORMAT: {max_tokens} tokens")
        for i in range(max_tokens):
            token = struct.unpack_from('<I', kdata, 12 + i * 4)[0]
            print(f"  Token[{i}]: {token}")

    # FACETKEYS
    idx, fdata = bom.named_block('FACETKEYS')
    if fdata:
        th = TreeHeader(fdata)
        print(f"\nFACETKEYS: PathCount={th.path_count}")
        node_data = bom.block(th.child)
        while node_data:
            result = parse_tree_node(node_data)
            if not result:
                break
            _, _, fwd, _, entries = result
            for val_idx, key_idx in entries:
                key_data = bom.block(key_idx)
                name = key_data.split(b'\x00')[0].decode('ascii', errors='replace')
                val_data = bom.block(val_idx)
                print(f"  '{name}' ({len(val_data)} bytes)")
            if fwd == 0:
                break
            node_data = bom.block(fwd)

    # RENDITIONS — extract images
    idx, rdata = bom.named_block('RENDITIONS')
    if not rdata:
        print("\nNo RENDITIONS tree found.")
        return

    th = TreeHeader(rdata)
    print(f"\nRENDITIONS: PathCount={th.path_count}")

    os.makedirs(out_dir, exist_ok=True)
    lzfse_bin = find_lzfse_binary()
    if not lzfse_bin:
        print("ERROR: lzfse binary not found. Build _tools/lzfse first.")
        return

    rend_idx = 0
    node_data = bom.block(th.child)
    while node_data:
        result = parse_tree_node(node_data)
        if not result:
            break
        _, _, fwd, _, entries = result
        for val_idx, key_idx in entries:
            csi_data = bom.block(val_idx)
            if not csi_data:
                continue
            print(f"\n  Rendition[{rend_idx}] (block[{val_idx}], {len(csi_data)} bytes):")
            try:
                _process_rendition(csi_data, out_dir, rend_idx, lzfse_bin)
            except Exception as e:
                print(f"    Error: {e}")
            rend_idx += 1
        if fwd == 0:
            break
        node_data = bom.block(fwd)


def _process_rendition(csi_data: bytes, out_dir: str, idx: int, lzfse_bin: str):
    csi = CSIHeader(csi_data)
    lay_name = LAYOUT_NAMES.get(csi.layout, f'Unknown({csi.layout})')
    print(f"    Name: {csi.name}")
    print(f"    Size: {csi.width}x{csi.height}, Scale: {csi.scale}")
    print(f"    PixelFormat: {csi.pixel_format_str} (0x{csi.pixel_format:08X})")
    print(f"    Layout: {lay_name} ({csi.layout})")
    print(f"    BitmapCount: {csi.bitmap_count}, TVLength: {csi.tv_length}")

    # Parse TLV
    tlv_data = csi_data[184:184 + csi.tv_length]
    tlvs = parse_tlv(tlv_data, csi.tv_length)
    for t, d in tlvs:
        print(f"    TLV type={t} len={len(d)}")

    # Bitmap data
    bitmap_start = 184 + csi.tv_length
    if bitmap_start + 4 > len(csi_data):
        print("    (no bitmap data)")
        return

    bmp_tag = csi_data[bitmap_start:bitmap_start + 4]
    print(f"    Bitmap tag: {bmp_tag.decode('ascii', errors='replace')}")

    # Find dmp2 magic
    dm2_off = csi_data.find(b'dmp2', bitmap_start)
    if dm2_off < 0:
        print("    (no dmp2 data found)")
        return

    _extract_dm2_image(csi_data, dm2_off, out_dir, idx, csi.name, lzfse_bin)


def _extract_dm2_image(csi_data: bytes, dm2_off: int, out_dir: str, idx: int, name: str, lzfse_bin: str):
    header = Dm2Header(csi_data[dm2_off:])
    print(f"    dmp2: type={header.decode_type} ver={header.version} pred={header.predictor_type} "
          f"pixfmt={header.pixel_format} {header.width}x{header.height}")

    payload = csi_data[dm2_off + header.header_size:]

    # Check for size-prefixed banded streams
    if len(payload) < 4:
        raise ValueError("Payload too small")

    first_size = struct.unpack_from('<I', payload, 0)[0]
    if first_size > 0 and 4 + first_size <= len(payload):
        if payload[4:8] in (b'bvx2', b'bvxn', b'bvx-'):
            _extract_banded(payload, header, lzfse_bin, out_dir, idx, name)
            return

    # Single stream
    rgba = decode_dm2(header, payload, lzfse_bin)
    out_path = os.path.join(out_dir, f"{idx:03d}_{sanitize_filename(name)}.png")
    write_png(out_path, header.width, header.height, rgba)
    print(f"    Wrote: {out_path} ({header.width}x{header.height})")


def _extract_banded(payload: bytes, first_header: Dm2Header, lzfse_bin: str, out_dir: str, idx: int, name: str):
    bands = []
    cursor = 0
    band_idx = 0
    while cursor + 4 <= len(payload):
        stream_size = struct.unpack_from('<I', payload, cursor)[0]
        if stream_size == 0 or cursor + 4 + stream_size > len(payload):
            break
        stream_data = payload[cursor + 4:cursor + 4 + stream_size]
        cursor += 4 + stream_size

        print(f"    Band {band_idx}: {stream_size} compressed bytes")
        decompressed = decompress_lzfse(lzfse_bin, stream_data)

        # Calculate band height from decompressed size
        comp = first_header.split_stream_components
        alpha_size = 1 if first_header.has_alpha else 0
        denom = first_header.width * (alpha_size + comp * 2) + 1
        band_height = len(decompressed) // denom
        if band_height == 0:
            band_height = first_header.height

        print(f"      Decompressed: {len(decompressed)} bytes, height={band_height}")
        bands.append((decompressed, band_height, first_header.width))
        band_idx += 1

    if not bands:
        raise ValueError("No bands found")

    # Decode each band and stitch
    width = bands[0][2]
    all_rgba = bytearray()
    for i, (decompressed, band_height, bw) in enumerate(bands):
        h = Dm2Header(b'dmp2' + struct.pack('<BBBBHH',
            first_header.decode_type, first_header.version, first_header.predictor_type,
            first_header.pixel_format, bw, band_height))
        rgba = decode_default_decompressed(h, decompressed, bw, band_height)
        all_rgba.extend(rgba)

    total_height = sum(b[1] for b in bands)
    out_path = os.path.join(out_dir, f"{idx:03d}_{sanitize_filename(name)}.png")
    write_png(out_path, width, total_height, bytes(all_rgba))
    print(f"    Wrote: {out_path} ({width}x{total_height})")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <Assets.car> [output_dir]", file=sys.stderr)
        sys.exit(1)

    car_path = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) >= 3 else 'car_output'
    extract_assets(car_path, out_dir)


if __name__ == '__main__':
    main()
