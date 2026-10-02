#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成测试用的音频与图片素材（供 test_end_to_end.py 使用）。

* make_flac：真正用 Python 手写一个含 16bit/44.1kHz 正弦波、可被任何解码器播放的 FLAC
  （verbatim / fixed 子帧 + Rice 残差 + 正确的 CRC-8/CRC-16）。
* make_mp3：MPEG-1 Layer III 帧结构正确的假数据（帧头合法、帧长正确），
  用于验证标签写入不会破坏帧结构；它不是可听音频，仅供结构性测试。
* make_png / make_jpeg：像素数据真实、尺寸可解析的图片。
* make_broken_*：故意损坏的文件，用于测试错误提示。
"""

from __future__ import annotations

import math
import os
import struct
import zlib

SR = 44100
BITS = 16


# --------------------------------------------------------------------------- #
# FLAC 编码（最小实现，只为造出真实可解码的样本）
# --------------------------------------------------------------------------- #

def _crc8_table() -> list:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return table


def _crc16_table() -> list:
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        table.append(crc)
    return table


_CRC8 = _crc8_table()
_CRC16 = _crc16_table()


def flac_crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = _CRC8[crc ^ byte]
    return crc


def flac_crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC16[((crc >> 8) ^ byte) & 0xFF]
    return crc


class BitWriter:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.acc = 0
        self.nbits = 0

    def write(self, value: int, bits: int) -> None:
        if bits <= 0:
            return
        value &= (1 << bits) - 1
        self.acc = (self.acc << bits) | value
        self.nbits += bits
        while self.nbits >= 8:
            self.nbits -= 8
            self.buf.append((self.acc >> self.nbits) & 0xFF)
        self.acc &= (1 << self.nbits) - 1

    def write_signed(self, value: int, bits: int) -> None:
        self.write(value & ((1 << bits) - 1), bits)

    def write_unary(self, zeros: int) -> None:
        self.write(1, zeros + 1)

    def finish(self) -> bytes:
        if self.nbits:
            self.buf.append((self.acc << (8 - self.nbits)) & 0xFF)
            self.acc, self.nbits = 0, 0
        return bytes(self.buf)


def _sine_blocks(seconds: float, freq: float, channels: int) -> list:
    total = int(SR * seconds)
    samples = [int(12000 * math.sin(2 * math.pi * freq * i / SR)) for i in range(total)]
    blocks = []
    for start in range(0, total, 4096):
        chunk = samples[start : start + 4096]
        if len(chunk) < 64:
            break
        blocks.append([[s for s in chunk] for _ in range(channels)])
    return blocks


def _fixed_residuals(sig: list, order: int) -> list:
    coeffs = {
        0: [],
        1: [1],
        2: [2, -1],
        3: [3, -3, 1],
        4: [4, -6, 4, -1],
    }[order]
    out = []
    for i in range(order, len(sig)):
        pred = sum(c * sig[i - 1 - j] for j, c in enumerate(coeffs))
        out.append(sig[i] - pred)
    return out


def _rice_bits(residuals: list, k: int) -> int:
    return sum(((v << 1) ^ (v >> 31)).bit_length() + k for v in residuals)


def _best_order(sig: list) -> tuple:
    """在 verbatim 与 FIXED 0..4 阶之间挑最短的编码方式，返回 (order, residuals, k, mode)。"""
    verbatim_bits = len(sig) * BITS
    best = (None, None, None, "verbatim", verbatim_bits)
    for order in range(5):
        if len(sig) <= order + 8:
            continue
        res = _fixed_residuals(sig, order)
        for k in range(15):
            bits = 4 + 4 + _rice_bits(res, k)
            if bits < best[4]:
                best = (order, res, k, "fixed", bits)
    return best


def _encode_channel(bw: BitWriter, sig: list, bps: int) -> None:
    order, res, k, mode, _bits = _best_order(sig)
    if mode == "verbatim" or res is None:
        bw.write(0b00, 2)          # verbatim 子帧
        bw.write(0, 1)             # 无 wasted bits
        for s in sig:
            bw.write_signed(s, bps)
        return
    bw.write(0b001, 3)             # FIXED 子帧
    bw.write(order, 3)
    bw.write(0, 1)                 # 无 wasted bits
    bw.write(order, 4)             # 4bit 残差编码方式：0 表示 Rice
    bw.write(k, 4)                 # Rice 参数
    for v in res:
        u = (v << 1) ^ (v >> 31)
        bw.write_unary(u >> k)
        if k:
            bw.write(u & ((1 << k) - 1), k)


def _encode_frame(blocks: list, block_index: int, bps: int, channels: int) -> bytes:
    block = blocks[block_index]
    block_size = len(block[0])
    bw = BitWriter()
    bw.write(0b11111111111110, 14)          # 同步码
    bw.write(0, 1)                          # 非固定块
    bw.write(0b01, 2)                       # blocking strategy = 固定块
    bs_code = 0b0111 if block_size == 4096 else 0b0110  # 8/9 位块大小
    bw.write(bs_code, 4)
    bw.write(0b1001, 4)                     # 采样率 44.1kHz
    ch_code = {1: 0b0000, 2: 0b0001}[channels]
    bw.write(ch_code, 4)
    bw.write(0b100, 3)                      # 16 bit
    bw.write(0, 1)                          # 保留位
    if bs_code == 0b0111:
        bw.write(block_size - 1, 8)
    else:
        bw.write(block_size - 1, 9)
    raw = flac_crc8(bw.buf)
    bw.write(raw, 8)
    for ch in range(channels):
        _encode_channel(bw, block[ch], bps)
    data = bw.finish()
    return data + struct.pack(">H", flac_crc16(data))


def make_flac(path: str, seconds: float = 1.0, channels: int = 1, freq: float = 440.0) -> str:
    blocks = _sine_blocks(seconds, freq, channels)
    total = sum(len(b[0]) for b in blocks)
    frames = [_encode_frame(blocks, i, BITS, channels) for i in range(len(blocks))]
    frame_sizes = [len(f) for f in frames]
    payload = b"".join(frames)
    import hashlib

    md5 = hashlib.md5()
    for block in blocks:
        for ch in range(channels):
            md5.update(struct.pack(f"<{len(block[ch])}h", *block[ch]))

    # STREAMINFO 共 34 字节：块大小 2+2、帧大小 3+3、采样率/声道/位深/总采样数 8、MD5 16
    streaminfo = bytearray()
    streaminfo += struct.pack(">HH", 4096, 4096)
    streaminfo += struct.pack(">I", min(frame_sizes))[1:]
    streaminfo += struct.pack(">I", max(frame_sizes))[1:]
    # 20bit 采样率 | 3bit 声道-1 | 5bit 位深-1 | 36bit 总采样数
    field = ((SR & 0xFFFFF) << 44) | (((channels - 1) & 0x7) << 41) | (((BITS - 1) & 0x1F) << 36) | (total & 0xFFFFFFFFF)
    streaminfo += field.to_bytes(8, "big")
    streaminfo += md5.digest()
    assert len(streaminfo) == 34, len(streaminfo)

    header = b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + bytes(streaminfo)
    with open(path, "wb") as fh:
        fh.write(header + payload)
    return path


# --------------------------------------------------------------------------- #
# MP3 帧结构（合法帧头 + 正确帧长），仅供结构性测试
# --------------------------------------------------------------------------- #

def _mp3_frame(bitrate_index: int = 9, padding: int = 0) -> bytes:
    header = b"\xff\xfb" + bytes([(bitrate_index << 4) | 0x00 | (padding << 1), 0x00])
    bitrate = 128000 if bitrate_index == 9 else 64000
    length = (144 * bitrate) // SR + padding
    return header + b"\x00" * (length - 4)


def make_mp3(path: str, frames: int = 40) -> str:
    with open(path, "wb") as fh:
        for i in range(frames):
            fh.write(_mp3_frame(bitrate_index=9 if i % 2 == 0 else 8, padding=i % 2))
    return path


# --------------------------------------------------------------------------- #
# 图片
# --------------------------------------------------------------------------- #

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def make_png(path: str, width: int = 300, height: int = 200, rgba: bool = False) -> str:
    color_type = 6 if rgba else 2
    bpp = 4 if rgba else 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            if rgba:
                raw += bytes((x * 255 // max(width - 1, 1), y * 255 // max(height - 1, 1), 128, 255))
            else:
                raw += bytes((x * 255 // max(width - 1, 1), y * 255 // max(height - 1, 1), 64))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    data = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + _png_chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _jpeg_quant_table() -> bytes:
    return bytes([16] * 64)


def make_jpeg(path: str, width: int = 240, height: int = 160) -> str:
    out = bytearray(b"\xff\xd8")  # SOI
    out += b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00" + struct.pack(">HH", 1, 1) + b"\x00\x00"
    out += b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" + _jpeg_quant_table()
    out += b"\xff\xc0" + struct.pack(">H", 17) + bytes([8]) + struct.pack(">HH", height, width) + bytes([3])
    out += bytes([1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1])
    out += b"\xff\xda" + struct.pack(">H", 12) + bytes([3, 1, 0, 2, 0x11, 3, 0x11, 0, 63, 0])
    out += bytes([0x0A, 0x0B, 0x0C, 0x0D])  # 熵编码数据（非真实图像内容）
    out += b"\xff\xd9"  # EOI
    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return path


def make_broken_png(path: str) -> str:
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
    return path


def make_broken_jpeg(path: str) -> str:
    with open(path, "wb") as fh:
        fh.write(b"\xff\xd8\xff" + b"\x11" * 40)
    return path


# --------------------------------------------------------------------------- #
# 网易云 ncm（自己按真实格式加密造一个，测试就不必依赖用户的下载目录）
# --------------------------------------------------------------------------- #

def _aes_encrypt_block(block: bytes, w: list) -> bytes:
    """AES-128 单块加密（FIPS-197 §5.1），用来造 ncm 测试素材。"""
    from musictag import ncm as _n

    s = _n._add_round_key(list(block), w, 0)
    for rnd in range(1, 10):
        s = [_n.SBOX[b] for b in s]
        t = [0] * 16                      # ShiftRows：第 r 行循环左移 r 位
        for c in range(4):
            for r in range(4):
                t[4 * c + r] = s[4 * ((c + r) % 4) + r]
        s = t
        for c in range(4):                # MixColumns
            a = s[4 * c:4 * c + 4]
            s[4 * c + 0] = _n._mul(a[0], 2) ^ _n._mul(a[1], 3) ^ a[2] ^ a[3]
            s[4 * c + 1] = a[0] ^ _n._mul(a[1], 2) ^ _n._mul(a[2], 3) ^ a[3]
            s[4 * c + 2] = a[0] ^ a[1] ^ _n._mul(a[2], 2) ^ _n._mul(a[3], 3)
            s[4 * c + 3] = _n._mul(a[0], 3) ^ a[1] ^ a[2] ^ _n._mul(a[3], 2)
        s = _n._add_round_key(s, w, rnd)
    s = [_n.SBOX[b] for b in s]
    t = [0] * 16
    for c in range(4):
        for r in range(4):
            t[4 * c + r] = s[4 * ((c + r) % 4) + r]
    return bytes(_n._add_round_key(t, w, 10))


def aes128_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-ECB 加密（数据长度必须是 16 的倍数）。"""
    from musictag import ncm as _n

    w = _n._expand_key(key)
    assert len(data) % 16 == 0, "数据长度必须是 16 的倍数"
    return b"".join(_aes_encrypt_block(data[i:i + 16], w) for i in range(0, len(data), 16))


def _pad(data: bytes) -> bytes:
    n = 16 - (len(data) % 16)
    return data + bytes([n]) * n


def make_ncm(path: str, audio_path: str, *, metadata: dict = None,
             cover: bytes = b"", key: bytes = b"0123456789abcdef") -> str:
    """按真实的 ncm 容器格式，把 audio_path 的音频加密打包成 .ncm。"""
    import base64
    import json as _json
    import zlib as _zlib

    from musictag import ncm as _n

    with open(audio_path, "rb") as fh:
        audio = fh.read()

    meta = dict(metadata or {})
    blob = _json.dumps(meta, ensure_ascii=False).encode("utf-8")

    key_box = _pad(_n._KEY_PREFIX + key)
    key_data = bytes(b ^ 0x64 for b in aes128_ecb_encrypt(_n._CORE_KEY, key_box))

    meta_ct = aes128_ecb_encrypt(_n._META_KEY, _pad(_n._META_PREFIX + blob))
    meta_body = b"163 key(Don't modify):" + base64.b64encode(meta_ct)
    meta_data = bytes(b ^ 0x63 for b in meta_body)

    out = bytearray()
    out += _n.MAGIC
    out += b"\x01\x70"
    out += struct.pack("<I", len(key_data)) + key_data
    out += struct.pack("<I", len(meta_data)) + meta_data
    out += struct.pack("<I", _zlib.crc32(blob) & 0xFFFFFFFF)
    out += b"\x00" * 5
    out += struct.pack("<I", len(cover)) + cover
    out += _n._xor_at(audio, _n._keystream_pattern(key), 0)

    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return path


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(here, exist_ok=True)
    make_flac(os.path.join(here, "sample.flac"))
    make_mp3(os.path.join(here, "sample.mp3"))
    make_png(os.path.join(here, "cover.png"))
    make_jpeg(os.path.join(here, "cover.jpg"))
    make_broken_png(os.path.join(here, "broken.png"))
    make_broken_jpeg(os.path.join(here, "broken.jpg"))
    print("素材已生成于:", here)


if __name__ == "__main__":
    main()
