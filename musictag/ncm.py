"""NCM（网易云音乐加密下载格式）解密。

网易云会员下载的歌曲是 ncm 格式，里面的音频数据被一层流密码加密，
文件头里还藏着原始格式（mp3/flac）、歌曲元信息和内嵌封面。

本模块只做解密与解析，不写标签 —— 标签部分交给 core。

文件结构（little-endian）::

    offset  size        内容
    0       8           magic "CTENFDAM"
    8       2           固定 0x01 0x70
    10      4           key_length
    14      key_length  RC4 密钥（每字节 ^0x64 后 AES-128-ECB 解密，去 PKCS7，跳过 "neteasecloudmusic"）
    ...     4           meta_length
    ...     meta_length 元信息（每字节 ^0x63，去掉前 22 字符 "163 key(Don't modify):"，
                        base64 解码后 AES-128-ECB 解密，去 PKCS7，跳过 "music:"，是 JSON）
    ...     4           CRC32
    ...     5           固定间隔
    ...     4           image_size
    ...     image_size  内嵌封面（jpg/png）
    ...     剩余        音频数据

本机没有 pycryptodome，所以 AES-128 是用纯 Python 实现的（只在文件头那几百字节上用，
慢一点无所谓）；音频部分的流密码则用周期 256 的密钥流做整数异或，速度足够。

音频流密码的密钥流只跟 `i % 256` 有关（因为下标 `j = (i + 1) & 0xff`），
所以可以先把 256 字节的密钥流算好，再整块异或，不必逐字节跑 Python 循环。
"""

import base64
import io
import json
import os
import struct

from . import core

MAGIC = b"CTENFDAM"
_CORE_KEY = b"hzHRAmso5kInbaxW"
_META_KEY = b"#14ljk_!\\]&0U<'("
_KEY_PREFIX = b"neteasecloudmusic"
_META_PREFIX = b"music:"
_META_B64_PREFIX_LEN = 22

_HEADER_CHUNK = 1 << 20


class NcmError(core.TagError):
    """ncm 文件损坏或不是 ncm 文件。"""


# --------------------------------------------------------------------------
# 纯 Python AES-128 解密（ECB），只为解 ncm 文件头
# --------------------------------------------------------------------------

def _build_sbox():
    sbox = [0] * 256
    p = q = 1
    while True:
        p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        q &= 0xFF
        x = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6))
        x ^= ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


SBOX = _build_sbox()
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1B) & 0xFF
    return a


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r & 0xFF


def _expand_key(key):
    assert len(key) == 16
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return w


def _add_round_key(s, w, rnd):
    base = rnd * 4
    return [s[i] ^ w[base + i // 4][i % 4] for i in range(16)]


def _inv_shift_rows(s):
    t = list(s)
    # 状态按列存放：s[4*列 + 行]；第 r 行循环「右」移 r 位
    # FIPS-197 §5.3.1: s'[r][c] = s[r][(c - shift(r)) mod 4]
    for c in range(4):
        for r in range(4):
            s[4 * c + r] = t[4 * ((c - r) % 4) + r]
    return s


def _inv_mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        out[4 * c + 0] = _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9)
        out[4 * c + 1] = _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13)
        out[4 * c + 2] = _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11)
        out[4 * c + 3] = _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14)
    return out


def _decrypt_block(block, w):
    # 等价逆密码：先加第 10 轮密钥，再逐轮 InvShiftRows / InvSubBytes / AddRoundKey / InvMixColumns
    s = _add_round_key(list(block), w, 10)
    for rnd in range(9, 0, -1):
        s = _inv_mix_columns(_add_round_key([INV_SBOX[b] for b in _inv_shift_rows(s)], w, rnd))
    s = _add_round_key([INV_SBOX[b] for b in _inv_shift_rows(s)], w, 0)
    return bytes(s)


def aes128_ecb_decrypt(key, data):
    """AES-128-ECB 解密。data 长度必须是 16 的倍数。"""
    if len(data) % 16:
        raise NcmError("ncm 文件头长度不对（不是 16 的倍数），文件可能已损坏。")
    w = _expand_key(key)
    return b"".join(
        _decrypt_block(data[i:i + 16], w) for i in range(0, len(data), 16)
    )


def _unpad(data):
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and n <= len(data):
        return data[:-n]
    return data


# --------------------------------------------------------------------------
# 音频流密码
# --------------------------------------------------------------------------

def _keystream_pattern(key):
    """算出周期 256 的密钥流。"""
    box = list(range(256))
    c = 0
    last = 0
    off = 0
    for i in range(256):
        swap = box[i]
        c = (swap + last + key[off]) & 0xFF
        off += 1
        if off >= len(key):
            off = 0
        box[i] = box[c]
        box[c] = swap
        last = c
    return bytes(
        box[(box[(i + 1) & 0xFF] + box[(box[(i + 1) & 0xFF] + ((i + 1) & 0xFF)) & 0xFF]) & 0xFF]
        for i in range(256)
    )


def _xor_at(data, pattern, start):
    """把 data 与「从绝对偏移 start 起的密钥流」异或。"""
    n = len(data)
    if not n:
        return data
    off = start & 0xFF
    reps = (n + off) // 256 + 2
    ks = (pattern * reps)[off:off + n]
    return (
        int.from_bytes(data, "big") ^ int.from_bytes(ks, "big")
    ).to_bytes(n, "big")


# --------------------------------------------------------------------------
# ncm 解析
# --------------------------------------------------------------------------

class NcmFile(object):
    """解析好的 ncm 文件。`audio_offset` 之后就是（加密的）音频数据。"""

    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path)
        self.key = b""
        self.metadata = {}
        self.cover = b""
        self.audio_offset = 0
        self._pattern = b""
        self._parse()

    def _parse(self):
        with open(self.path, "rb") as f:
            magic = f.read(8)
            if magic != MAGIC:
                raise NcmError(
                    "不是网易云 ncm 文件（文件头是 %s，应该是 CTENFDAM）。"
                    % magic[:8].hex(" ")
                )
            f.read(2)
            raw = f.read(4)
            if len(raw) < 4:
                raise NcmError("ncm 文件不完整：读不到密钥长度。")
            key_length = struct.unpack("<I", raw)[0]
            if key_length <= 0 or key_length > 1 << 20:
                raise NcmError("ncm 文件损坏：密钥长度不合理（%d）。" % key_length)
            key_data = f.read(key_length)
            if len(key_data) != key_length:
                raise NcmError("ncm 文件不完整：密钥数据被截断。")
            key_data = bytes(b ^ 0x64 for b in key_data)
            key_data = _unpad(aes128_ecb_decrypt(_CORE_KEY, key_data))
            if not key_data.startswith(_KEY_PREFIX):
                raise NcmError("ncm 解密失败：密钥段内容不对，文件可能已损坏。")
            self.key = key_data[len(_KEY_PREFIX):]

            raw = f.read(4)
            if len(raw) < 4:
                raise NcmError("ncm 文件不完整：读不到元信息长度。")
            meta_length = struct.unpack("<I", raw)[0]
            if meta_length < 0 or meta_length > 1 << 22:
                raise NcmError("ncm 文件损坏：元信息长度不合理（%d）。" % meta_length)
            meta_data = f.read(meta_length)
            if len(meta_data) != meta_length:
                raise NcmError("ncm 文件不完整：元信息被截断。")
            meta_data = bytes(b ^ 0x63 for b in meta_data)
            try:
                meta_data = base64.b64decode(meta_data[_META_B64_PREFIX_LEN:])
                meta_data = _unpad(aes128_ecb_decrypt(_META_KEY, meta_data))
            except NcmError:
                raise
            except Exception:
                raise NcmError("ncm 解密失败：元信息段无法解码，文件可能已损坏。")
            if not meta_data.startswith(_META_PREFIX):
                raise NcmError("ncm 解密失败：元信息段内容不对，文件可能已损坏。")
            try:
                self.metadata = json.loads(meta_data[len(_META_PREFIX):].decode("utf-8"))
            except Exception:
                raise NcmError("ncm 解密失败：元信息不是合法的 JSON。")

            f.read(4)   # crc32
            f.read(5)   # 固定间隔
            raw = f.read(4)
            if len(raw) < 4:
                raise NcmError("ncm 文件不完整：读不到封面长度。")
            image_size = struct.unpack("<I", raw)[0]
            if image_size:
                self.cover = f.read(image_size)
            self.audio_offset = f.tell()

        self._pattern = _keystream_pattern(self.key)

    # -- 元信息 ----------------------------------------------------------
    @property
    def format(self):
        """原始音频格式：'mp3' 或 'flac'（来自文件内元信息）。"""
        fmt = str(self.metadata.get("format", "")).strip().lower()
        if fmt in ("mp3", "flac"):
            return fmt
        return ""

    @property
    def music_name(self):
        return str(self.metadata.get("musicName", "") or "")

    @property
    def artists(self):
        out = []
        for item in self.metadata.get("artist") or []:
            if isinstance(item, (list, tuple)) and item:
                name = str(item[0]).strip()
            else:
                name = str(item).strip()
            if name and name not in out:
                out.append(name)
        return out

    @property
    def album(self):
        return str(self.metadata.get("album", "") or "")

    @property
    def cover_mime(self):
        if self.cover.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if self.cover.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        return ""

    @property
    def audio_size(self):
        return max(0, self.size - self.audio_offset)

    # -- 解密 ------------------------------------------------------------
    def iter_audio(self, chunk_size=_HEADER_CHUNK):
        """按块产出解密后的音频字节。"""
        pos = 0
        with open(self.path, "rb") as f:
            f.seek(self.audio_offset)
            while True:
                block = f.read(chunk_size)
                if not block:
                    break
                yield _xor_at(block, self._pattern, pos)
                pos += len(block)


def read_info(path):
    """只解析文件头，不写任何东西。"""
    try:
        return NcmFile(path)
    except NcmError:
        raise
    except OSError as exc:
        raise core.TagError("无法读取文件：%s（%s）" % (path, exc))


def requires_ncm(path):
    """文件是不是 ncm（按文件头判断，不看扩展名）。"""
    try:
        with open(path, "rb") as f:
            return f.read(8) == MAGIC
    except OSError:
        return False


def decrypt(src, dst, on_progress=None):
    """把 ncm 解密成 dst。返回 (info, 实际格式)。"""
    info = read_info(src)
    total = info.audio_size or 1
    done = 0
    tmp = dst + ".part"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
        with open(tmp, "wb") as out:
            for block in info.iter_audio():
                out.write(block)
                done += len(block)
                if on_progress:
                    on_progress(min(1.0, done / float(total)))
        if os.path.getsize(tmp) == 0:
            raise NcmError("解密出来的音频是空的，文件可能已损坏。")
        os.replace(tmp, dst)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    if on_progress:
        on_progress(1.0)
    return info
