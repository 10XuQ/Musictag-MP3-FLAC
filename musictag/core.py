"""musictag 核心实现：读取 / 写入 / 校验 MP3(ID3v2) 与 FLAC(Vorbis + PICTURE) 标签。

对外主要接口
------------
read_tags(path)                     -> Tags        读取现有标签
read_image(path)                    -> Image       读取并校验封面图片（损坏会抛错）
read_lyrics_text(path)              -> str
normalize_date(text)                -> str         统一日期的规范写法 YYYY-MM-DD
write_tags(src, dst, changes, ...)  -> WriteReport 把 changes 合入 src 后写出 dst（原子替换）
verify(dst, expected)               -> list[Check] 重新打开 dst 逐项核对
describe(tags)                      -> str         人类可读的标签摘要

约束
----
* 只支持 .mp3 与 .flac，其余格式直接报错（不做任何尝试性写入）。
* 音频数据始终保持原样：保存后会用 STREAMINFO / MPEG 帧信息再次确认。
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import shutil
import struct
import tempfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence

from mutagen import id3
from mutagen.flac import FLAC, Picture
from mutagen.mp3 import MP3 as MP3File

__all__ = [
    "TagError",
    "FormatError",
    "ImageError",
    "Tags",
    "Image",
    "CoverChange",
    "LyricsChange",
    "Changes",
    "Check",
    "WriteReport",
    "detect_format",
    "read_tags",
    "read_image",
    "read_lyrics_text",
    "normalize_date",
    "write_tags",
    "verify",
    "describe",
]

# --------------------------------------------------------------------------- #
# 错误类型：CLI 直接把 str(exc) 展示给用户，所以信息必须自解释、可操作
# --------------------------------------------------------------------------- #


class TagError(Exception):
    """所有可预期的、应当以友好方式展示给用户的错误。"""


class FormatError(TagError):
    """文件格式不受支持或文件已损坏。"""


class ImageError(TagError):
    """封面图片不存在 / 无法读取 / 数据损坏。"""


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass
class Image:
    data: bytes
    mime: str
    width: int
    height: int
    depth: int
    source: str = ""

    @property
    def summary(self) -> str:
        return f"{self.mime} {self.width}x{self.height} {self.depth}bit {len(self.data)}B"


@dataclass
class Tags:
    title: Optional[str] = None
    artists: list[str] = field(default_factory=list)
    album: Optional[str] = None
    date: Optional[str] = None
    cover: Optional[Image] = None
    lyrics: Optional[str] = None
    lyrics_kind: str = ""  # "lrc" 或 "text"
    # 原始文件里已经存在的标签总数（用于报告“保留了多少原有标签”）
    existing_count: int = 0

    def describe(self) -> str:
        lines = [
            f"标题     : {self.title or '—'}",
            f"作者     : {' / '.join(self.artists) if self.artists else '—'}",
            f"专辑     : {self.album or '—'}",
            f"发行日期 : {self.date or '—'}",
            f"封面     : {self.cover.summary if self.cover else '—'}",
        ]
        if self.lyrics:
            head = self.lyrics.strip().splitlines()[:3]
            lines.append(f"歌词     : {self.lyrics_kind or 'text'}，{len(self.lyrics)} 字符，开头 " + " ⏎ ".join(head))
        else:
            lines.append("歌词     : —")
        return "\n".join(lines)


@dataclass
class CoverChange:
    """action: keep / set / clear —— set 时 image 必须存在。"""

    action: str = "keep"
    image: Optional[Image] = None


@dataclass
class LyricsChange:
    """action: keep / set / clear —— set 时 text 非空。"""

    action: str = "keep"
    text: Optional[str] = None
    kind: str = "text"


@dataclass
class Changes:
    """None 表示“不要动这个字段”。空字符串 / 空序列表示“清除这个字段”。"""

    title: Optional[str] = None
    artists: Optional[Sequence[str]] = None
    album: Optional[str] = None
    date: Optional[str] = None
    cover: CoverChange = field(default_factory=CoverChange)
    lyrics: LyricsChange = field(default_factory=LyricsChange)

    def is_empty(self) -> bool:
        return (
            self.title is None
            and self.artists is None
            and self.album is None
            and self.date is None
            and self.cover.action == "keep"
            and self.lyrics.action == "keep"
        )


@dataclass
class Check:
    label: str
    ok: bool
    expected: str = ""
    actual: str = ""


@dataclass
class WriteReport:
    output: str
    fmt: str
    preserved: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    audio_before: dict = field(default_factory=dict)
    audio_after: dict = field(default_factory=dict)

    @property
    def audio_unchanged(self) -> bool:
        if not self.audio_before or not self.audio_after:
            return False
        shared = set(self.audio_before) & set(self.audio_after)
        return bool(shared) and all(self.audio_before[k] == self.audio_after[k] for k in shared)


# --------------------------------------------------------------------------- #
# 日期：统一格式 YYYY-MM-DD
# --------------------------------------------------------------------------- #

_DATE_PATTERNS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%Y-%m",
    "%Y/%m/%d",
    "%Y/%m",
    "%Y.%m.%d",
    "%Y.%m",
    "%Y年%m月%d日",
    "%Y年%m月",
    "%Y%m%d",
    "%Y",
)


def normalize_date(text: str) -> str:
    """把用户写的日期整理成 YYYY-MM-DD（给出月份/日则一并保留，避免丢失信息）。"""
    raw = (text or "").strip()
    if not raw:
        raise TagError("发行日期不能是空字符串；要删除日期请使用 --clear-date")
    candidate = re.sub(r"[./年月]", "-", raw).replace("日", "").replace("T", " ")
    candidate = re.sub(r"\s+", " ", candidate).strip()
    for fmt in _DATE_PATTERNS:
        for value in (raw, candidate):
            try:
                dt = datetime.strptime(value, fmt)
            except ValueError:
                continue
            if fmt == "%Y":
                return f"{dt.year:04d}"
            if fmt in ("%Y-%m", "%Y/%m", "%Y.%m", "%Y年%m月"):
                return f"{dt.year:04d}-{dt.month:02d}"
            if fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
    if re.fullmatch(r"\d{4}", candidate):
        return candidate
    raise TagError(
        f"无法识别的发行日期：{text!r}。请使用 年-月-日 写法，例如 2019-03-01 或 2019-03。"
    )


# --------------------------------------------------------------------------- #
# 图片：格式识别 + 结构校验 + 尺寸解析
# --------------------------------------------------------------------------- #


def _png_info(data: bytes) -> tuple[int, int, int]:
    size = len(data)
    if size < 33 or data[12:16] != b"IHDR":
        raise ImageError("PNG 文件损坏：缺少 IHDR 数据块。")
    width, height, depth, color_type = struct.unpack(">IIBB", data[16:26])
    if width == 0 or height == 0:
        raise ImageError("PNG 文件损坏：宽高为 0。")
    if data[24] not in (1, 2, 4, 8, 16):
        raise ImageError(f"PNG 文件损坏：无法识别的位深 {data[24]}。")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise ImageError(f"PNG 文件损坏：无法识别的颜色类型 {color_type}。")
    # 逐块走完整个文件，确认结构完整、且以 IEND 结束
    pos = 8
    saw_end = False
    while pos + 8 <= size:
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        tag = data[pos + 4 : pos + 8]
        if pos + 12 + length > size:
            raise ImageError("PNG 文件损坏：数据块长度越界（文件可能被截断）。")
        body = data[pos + 8 : pos + 8 + length]
        stored = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])[0]
        if zlib.crc32(tag + body) & 0xFFFFFFFF != stored:
            raise ImageError(f"PNG 文件损坏：数据块 {tag.decode('ascii', 'replace')} 的校验和不匹配。")
        pos += 12 + length
        if tag == b"IEND":
            saw_end = True
            break
    if not saw_end:
        raise ImageError("PNG 文件损坏：找不到 IEND 结束块（文件可能被截断）。")
    return width, height, depth * channels


_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_info(data: bytes) -> tuple[int, int, int]:
    pos, size = 2, len(data)
    while pos + 3 < size:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker == 0xD9:
            break
        if pos + 4 > size:
            break
        seg_len = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        if seg_len < 2:
            break
        if marker in _JPEG_SOF:
            if pos + 9 > size:
                break
            height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
            comps = data[pos + 9] if pos + 9 < size else 3
            if width == 0 or height == 0:
                raise ImageError("JPEG 文件损坏：宽高为 0。")
            return width, height, max(comps, 1) * 8
        pos += 2 + seg_len
    raise ImageError("JPEG 文件损坏：找不到有效的图像尺寸信息（SOF 段）。")


_OTHER_IMAGE_MAGIC = (
    (b"GIF87a", "image/gif", 1),
    (b"GIF89a", "image/gif", 1),
    (b"BM", "image/bmp", 1),
)


def sniff_image(data: bytes, where: str = "图片") -> tuple[str, int, int, int]:
    """返回 (mime, 宽, 高, 位深)。无法识别或结构损坏时抛 ImageError。"""
    if not data:
        raise ImageError(f"{where}是空文件（0 字节）。")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        width, height, depth = _png_info(data)
        return "image/png", width, height, depth
    if data.startswith(b"\xff\xd8\xff"):
        width, height, depth = _jpeg_info(data)
        return "image/jpeg", width, height, depth
    for magic, mime, depth in _OTHER_IMAGE_MAGIC:
        if data.startswith(magic):
            raise ImageError(
                f"{where}是 {mime}，本工具只写入常见格式；请转换为 JPG 或 PNG（部分播放器不识别 {mime} 封面）。"
            )
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        raise ImageError(f"{where}是 WebP；兼容性差，请先转换为 JPG 或 PNG。")
    head = data[:8].hex(" ")
    raise ImageError(
        f"{where}不是可识别的图片（头部字节：{head}），或文件已损坏。支持 JPG / PNG。"
    )


def read_image(path: str, where: str = "封面图片") -> Image:
    if not os.path.exists(path):
        raise ImageError(f"{where}不存在：{path}")
    if os.path.isdir(path):
        raise ImageError(f"{where}是一个目录，不是文件：{path}")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise ImageError(f"无法读取{where} {path}：{exc.strerror or exc}") from exc
    mime, width, height, depth = sniff_image(data, f"{where}（{os.path.basename(path)}）")
    return Image(data=data, mime=mime, width=width, height=height, depth=depth, source=path)


# --------------------------------------------------------------------------- #
# 歌词：纯文本 / LRC 识别
# --------------------------------------------------------------------------- #

_LRC_RE = re.compile(r"^\s*\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]")


def _looks_like_lrc(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    hits = sum(1 for ln in lines if _LRC_RE.match(ln))
    return hits >= max(1, len(lines) // 2)


def _decode_text(data: bytes, path: str) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise TagError(
        f"无法识别歌词文件的文本编码：{path}。请另存为 UTF-8 编码后重试。"
    )


def read_lyrics_text(path: str) -> str:
    if not os.path.exists(path):
        raise TagError(f"歌词文件不存在：{path}")
    if os.path.isdir(path):
        raise TagError(f"歌词路径是一个目录，不是文件：{path}")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise TagError(f"无法读取歌词文件 {path}：{exc.strerror or exc}") from exc
    return _decode_text(data, path).replace("\r\n", "\n").replace("\r", "\n")


def detect_lyrics_kind(text: str) -> str:
    return "lrc" if _looks_like_lrc(text) else "text"


def normalize_lyrics(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n"))


# --------------------------------------------------------------------------- #
# 格式识别
# --------------------------------------------------------------------------- #


def _read_head(path: str, size: int = 16) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(size)


def _id3v2_total_size(head: bytes) -> int:
    """根据 ID3v2 头算出整个标签的长度（用于跳过前置标签找 FLAC 标记）。"""
    if len(head) < 10 or head[:3] != b"ID3":
        return 0
    size = (head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14 | (head[8] & 0x7F) << 7 | (head[9] & 0x7F)
    return size + 10 + (10 if head[5] & 0x10 else 0)


def detect_format(path: str) -> str:
    """返回 'mp3' 或 'flac'；其它一律抛出 FormatError。"""
    if not os.path.exists(path):
        raise TagError(f"文件不存在：{path}")
    if os.path.isdir(path):
        raise TagError(f"这是一个目录，不是音频文件：{path}")
    try:
        head = _read_head(path, 64)
    except OSError as exc:
        raise TagError(f"无法读取文件 {path}：{exc.strerror or exc}") from exc
    if not head:
        raise FormatError(f"文件是空的（0 字节）：{path}")
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"ID3"):
        skip = _id3v2_total_size(head)
        try:
            with open(path, "rb") as fh:
                fh.seek(skip)
                if fh.read(4) == b"fLaC":
                    return "flac"
        except OSError:
            pass
        return "mp3"
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    ext = os.path.splitext(path)[1].lower()
    raise FormatError(
        f"不支持的音频格式：{path}（扩展名 {ext or '无'}，文件头 {head[:4].hex(' ')}）。"
        "本工具只处理 MP3 与 FLAC。"
    )


# --------------------------------------------------------------------------- #
# 音频参数（保存前后对比，证明没有重新编码）
# --------------------------------------------------------------------------- #


def _audio_signature(path: str, fmt: str) -> dict:
    try:
        if fmt == "mp3":
            from mutagen.mp3 import MP3

            info = MP3(path).info
            if info is None or info.length is None:
                return {}
            keys = {
                "时长(秒)": round(info.length, 3),
                "比特率": info.bitrate,
                "采样率": info.sample_rate,
                "声道": info.channels,
                "音频帧数": getattr(info, "frame_count", None),
            }
            return {k: v for k, v in keys.items() if v is not None}
        flac = FLAC(path)
        info = flac.info
        md5 = (info.md5_signature or 0).to_bytes(16, "big").hex()
        return {
            "时长(秒)": round(info.length, 3),
            "采样率": info.sample_rate,
            "声道": info.channels,
            "位深": info.bits_per_sample,
            "总采样数": info.total_samples,
            "STREAMINFO MD5": md5,
        }
    except Exception:  # 参数读取失败不应阻断主流程
        return {}


def _id3v2_size_of(data: bytes) -> int:
    """给一段以 ID3v2 头开头的数据，算出整个标签的长度；不是 ID3v2 则返回 0。"""
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
    return size + 10 + (10 if data[5] & 0x10 else 0)


def _audio_bytes_fingerprint(path: str, fmt: str) -> str:
    """对「去掉标签后的音频数据」做 SHA256，用于证明音频字节逐字节未变。"""
    with open(path, "rb") as fh:
        data = fh.read()
    if fmt == "flac":
        start = 4
        while start + 4 <= len(data):
            header = data[start]
            length = int.from_bytes(data[start + 1 : start + 4], "big")
            is_last = header & 0x80
            block_type = header & 0x7F
            start += 4 + length
            if is_last or block_type == 127:
                break
        audio = data[start:]
    else:
        audio = data[_id3v2_size_of(data[:10]) :]
        if len(audio) >= 128 and audio[-128:-125] == b"TAG":
            audio = audio[:-128]
    return hashlib.sha256(audio).hexdigest()


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #

# 多位作者在标签里的常见分隔符（ID3v2.4 用 \x00，很多软件也用 / ; 、 , 分隔）
_ARTIST_SPLIT = r"[\x00/;、,，]"


def _id3_texts(frame: Any) -> list[str]:
    """取一个文本帧里的全部文本值（ID3v2.4 的 TPE1 可能是多值）。"""
    if frame is None:
        return []
    text = getattr(frame, "text", None)
    if text:
        return [str(v) for v in text]
    raw = bytes(frame)
    for off in (1, 0, 2):
        try:
            return [raw[off:].decode("utf-8").strip("\x00").strip()]
        except UnicodeDecodeError:
            continue
    return []


def _id3_text(frame: Any) -> str:
    values = _id3_texts(frame)
    return values[0].strip() if values else ""


def _apic_image(frame: Any, where: str) -> Optional[Image]:
    data = getattr(frame, "data", None)
    mime = (getattr(frame, "mime", "") or "").strip()
    if data is None:
        raw = bytes(frame)
        for sep in (b"\x00", b"\xff\xfe", b"\xfe\xff"):
            idx = raw.find(sep)
            if 0 < idx < 32:
                data = raw[idx + len(sep) :]
                break
    if data is None:
        return None
    try:
        sniffed, width, height, depth = sniff_image(data, where)
    except ImageError:
        return Image(data=data, mime=mime or "application/octet-stream", width=0, height=0, depth=0)
    if mime and mime.lower() != sniffed:
        mime = mime  # 保留原文件声明，仅用于展示
    return Image(data=data, mime=mime or sniffed, width=width, height=height, depth=depth)


def _front_cover_id3(audio: Any) -> Optional[Image]:
    tags = getattr(audio, "tags", None)
    if not tags:
        return None
    frames = [f for f in tags.getall("APIC") if getattr(f, "type", 3) == 3] or list(tags.getall("APIC"))
    return _apic_image(frames[0], "音频内已有封面") if frames else None


def _lyrics_id3(audio: Any) -> Optional[str]:
    tags = getattr(audio, "tags", None)
    if not tags:
        return None
    texts = [str(f) for f in tags.getall("USLT")]
    texts += [str(f) for f in tags.getall("SYLT")]
    texts = [t for t in texts if t.strip()]
    return max(texts, key=len) if texts else None


def read_tags(path: str) -> tuple[Tags, str, Any]:
    """读取现有标签。返回 (Tags, 格式, mutagen 对象)。

    格式以**文件头内容**为准（detect_format 已经嗅探过），这里按嗅探结果显式
    选用 mutagen 的类，不依赖扩展名 —— 否则「MP3 内容 + .flac 扩展名」这种
    输出路径会让 mutagen 按扩展名挑错解析器而读取失败。
    """
    fmt = detect_format(path)
    try:
        audio = FLAC(path) if fmt == "flac" else MP3File(path)
    except Exception as exc:
        raise FormatError(f"无法解析音频文件 {path}：{exc}") from exc
    if audio is None:
        raise FormatError(
            f"无法解析音频文件 {path}：内容与扩展名不符或文件已损坏。本工具只处理 MP3 与 FLAC。"
        )

    if fmt == "flac":
        artists = [a.strip() for a in audio.get("artist", []) if a.strip()]
        multi = [a.strip() for a in audio.get("artists", []) if a.strip()]
        lyrics = next((v.strip() for v in audio.get("lyrics", []) if v and v.strip()), None)
        date = next((v.strip() for v in audio.get("date", []) if v and v.strip()), None)
        cover = None
        for pic in audio.pictures:
            if pic.type == 3 and pic.data:
                try:
                    mime, w, h, d = sniff_image(pic.data, "音频内已有封面")
                except ImageError:
                    continue
                cover = Image(pic.data, pic.mime or mime, w, h, d)
                break
        if cover is None and audio.pictures:
            pic = audio.pictures[0]
            try:
                mime, w, h, d = sniff_image(pic.data, "音频内已有封面")
                cover = Image(pic.data, pic.mime or mime, w, h, d)
            except ImageError:
                cover = None
        tags = Tags(
            title=next((v.strip() for v in audio.get("title", []) if v and v.strip()), None),
            artists=artists,
            album=next((v.strip() for v in audio.get("album", []) if v and v.strip()), None),
            date=date,
            cover=cover,
            lyrics=lyrics,
            lyrics_kind=detect_lyrics_kind(lyrics or ""),
            existing_count=sum(len(audio.get(k, [])) for k in audio.keys()),
        )
        return tags, fmt, audio

    artists = []
    if audio.tags:
        # 优先读 TXXX:ARTISTS（多位作者时本工具额外写入的一份），再退回 TPE1
        for frame in audio.tags.getall("TXXX:ARTISTS"):
            for value in getattr(frame, "text", []) or []:
                artists += [p.strip() for p in re.split(_ARTIST_SPLIT, value) if p.strip()]
        if not artists:
            for frame in audio.tags.getall("TPE1"):
                for value in _id3_texts(frame):
                    artists += [p.strip() for p in re.split(_ARTIST_SPLIT, value) if p.strip()]
    seen: set = set()
    artists = [a for a in artists if not (a in seen or seen.add(a))]
    title = _id3_text(audio.tags.get("TIT2")) if audio.tags else ""
    album = _id3_text(audio.tags.get("TALB")) if audio.tags else ""
    date = ""
    if audio.tags:
        for fid in ("TDRC", "TDOR", "TYER"):
            date = _id3_text(audio.tags.get(fid))
            if date:
                break
    lyrics = _lyrics_id3(audio)
    tags = Tags(
        title=title or None,
        artists=artists,
        album=album or None,
        date=date or None,
        cover=_front_cover_id3(audio),
        lyrics=lyrics,
        lyrics_kind=detect_lyrics_kind(lyrics or ""),
        existing_count=len(audio.tags) if audio.tags else 0,
    )
    return tags, fmt, audio


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #

_MP3_V23_FRAMES = (
    "TIT2", "TPE1", "TPE2", "TALB", "TRCK", "TPOS", "TCON", "TCOM", "TEXT",
    "TCOP", "TPUB", "TLAN", "TSSE", "TENC", "TDAT", "TIME", "TYER", "APIC", "USLT",
)
_MP3_DROP_FRAMES = ("GEOB",)  # 加密/带签名的帧在版本迁移后可能失效，直接丢弃


def _text_frame(fid: str, values: Sequence[str]) -> Any:
    """按帧 ID 造一个文本帧。

    注意：不能用 id3.Frame(fid, ...)——那是基类，FrameID 会变成 "Frame" 而不是 fid，
    写进文件后播放器无法识别（会变成一堆垃圾字节）。
    """
    cls = id3.Frames.get(fid)
    if cls is None:
        raise TagError(f"不支持的 ID3 帧类型：{fid}")
    return cls(encoding=3, text=list(values))


def _build_id3(existing: Any, changes: Changes) -> tuple[id3.ID3, list[str]]:
    version = 4
    version_note = "ID3v2.4"
    if isinstance(existing, id3.ID3):
        raw = bytes(existing._version[:2]) if getattr(existing, "_version", None) else b"\x04\x00"
        version = 3 if raw[:1] == b"\x03" else 4
        version_note = f"ID3v2.{version}"
        tags = existing
        dropped = []
        for fid in _MP3_DROP_FRAMES:
            for frame in list(tags.getall(fid)):
                tags.pop(frame.HashKey)
                dropped.append(fid)
        for fid in list(tags.keys()):
            if fid.startswith("PRIV"):
                for frame in list(tags.getall(fid)):
                    tags.pop(frame.HashKey)
                dropped.append(fid)
    else:
        tags, dropped = id3.ID3(), []
    tags.v2_version = 3 if version == 3 else 4

    removed: list[str] = []

    def set_text(fid: str, value: Optional[str], label: str) -> None:
        if value is None:
            return
        for frame in list(tags.getall(fid)):
            tags.pop(frame.HashKey)
        if value.strip() != "":
            tags.add(_text_frame(fid, [value]))

    set_text("TIT2", changes.title, "标题")
    set_text("TALB", changes.album, "专辑")
    if changes.date is not None:
        for fid in ("TDRC", "TYER", "TDAT", "TIME", "TDOR"):
            for frame in list(tags.getall(fid)):
                tags.pop(frame.HashKey)
        if changes.date != "":
            if version == 3:
                # ID3v2.3 没有 TDRC，用 TYER(+TDAT) 表达，兼容性最好
                year, month, day = changes.date[:4], "", ""
                m = re.match(r"^\d{4}-(\d{2})(?:-(\d{2}))?", changes.date)
                if m:
                    month, day = m.group(1) or "", m.group(2) or ""
                tags.add(_text_frame("TYER", [year]))
                if month and day:
                    tags.add(_text_frame("TDAT", [f"{day}{month}"]))
            else:
                tags.add(_text_frame("TDRC", [changes.date]))
    if changes.artists is not None:
        for frame in list(tags.getall("TPE1")):
            tags.pop(frame.HashKey)
        clean = [a.strip() for a in changes.artists if a and a.strip()]
        if clean:
            # ID3v2.4 用 \x00 分隔多位作者；同时写入 TXXX:ARTISTS 供部分软件读取
            tags.add(_text_frame("TPE1", ["\x00".join(clean)]))
            for frame in list(tags.getall("TXXX:ARTISTS")):
                tags.pop(frame.HashKey)
            if len(clean) > 1:
                tags.add(id3.TXXX(encoding=3, desc="ARTISTS", text=clean))
    if changes.lyrics.action == "set":
        text = changes.lyrics.text or ""
        if text.strip():
            tags.delall("USLT")
            tags.add(id3.USLT(encoding=3, lang="XXX", desc="", text=text))
            if changes.lyrics.kind == "lrc":
                tags.delall("SYLT")
        else:
            tags.delall("USLT")
    elif changes.lyrics.action == "clear":
        tags.delall("USLT")
        removed.append("歌词")
    if changes.cover.action == "set" and changes.cover.image is not None:
        _set_apic(tags, changes.cover.image)
    elif changes.cover.action == "clear":
        if tags.getall("APIC"):
            tags.delall("APIC")
            removed.append("封面")
    return tags, removed


def _set_apic(tags: id3.ID3, image: Image) -> None:
    """替换“正面封面”，保留其它类型（封底、艺人照片等）的图片。"""
    for frame in list(tags.getall("APIC")):
        if getattr(frame, "type", 3) == 3:
            tags.pop(frame.HashKey)
    tags.add(
        id3.APIC(
            encoding=0,
            mime=image.mime,
            type=3,
            desc="Cover",
            data=image.data,
        )
    )


def _vorbis_set(audio: FLAC, key: str, value: str) -> None:
    """按惯例用大写键名写入 Vorbis 注释（查找不区分大小写，但大写兼容性最好）。"""
    audio.pop(key, None)
    audio[key.upper()] = [value]


def _apply_flac(audio: FLAC, changes: Changes) -> list[str]:
    """把 changes 合入 FLAC 对象；返回被清除的字段名列表。"""
    removed: list[str] = []
    if changes.title is not None:
        if changes.title.strip():
            _vorbis_set(audio, "title", changes.title)
        else:
            audio.pop("title", None)
    if changes.album is not None:
        if changes.album.strip():
            _vorbis_set(audio, "album", changes.album)
        else:
            audio.pop("album", None)
    if changes.date is not None:
        if changes.date.strip():
            _vorbis_set(audio, "date", changes.date)
        else:
            audio.pop("date", None)
    if changes.artists is not None:
        clean = [a.strip() for a in changes.artists if a and a.strip()]
        audio.pop("artist", None)
        audio.pop("artists", None)
        if clean:
            audio["ARTIST"] = clean
            if len(clean) > 1:
                audio["ARTISTS"] = clean
    if changes.lyrics.action == "set":
        text = changes.lyrics.text or ""
        if text.strip():
            _vorbis_set(audio, "lyrics", text)
        else:
            if "lyrics" in audio:
                audio.pop("lyrics", None)
                removed.append("歌词")
    elif changes.lyrics.action == "clear":
        if "lyrics" in audio:
            audio.pop("lyrics", None)
            removed.append("歌词")
    if changes.cover.action == "set" and changes.cover.image is not None:
        image = changes.cover.image
        others = [p for p in audio.pictures if p.type != 3]
        audio.clear_pictures()
        for pic in others:
            audio.add_picture(pic)
        picture = Picture()
        picture.type = 3
        picture.mime = image.mime
        picture.desc = "Cover"
        picture.width = image.width
        picture.height = image.height
        picture.depth = image.depth
        picture.colors = 0
        picture.data = image.data
        audio.add_picture(picture)
    elif changes.cover.action == "clear":
        if audio.pictures:
            others = [p for p in audio.pictures if p.type != 3]
            audio.clear_pictures()
            for pic in others:
                audio.add_picture(pic)
            removed.append("封面")
    return removed


# --------------------------------------------------------------------------- #
# 保存（原子替换）
# --------------------------------------------------------------------------- #


def _atomic_target(dst: str) -> tuple[str, str]:
    directory = os.path.dirname(os.path.abspath(dst)) or "."
    if not os.path.isdir(directory):
        raise TagError(f"输出目录不存在：{directory}")
    suffix = os.path.splitext(dst)[1] or ".tmp"
    try:
        fd, tmp = tempfile.mkstemp(prefix=".musictag-", suffix=suffix, dir=directory)
    except OSError as exc:
        raise TagError(f"无法在输出目录创建临时文件（{directory}）：{exc.strerror or exc}") from exc
    os.close(fd)
    return tmp, directory


def _has_id3v1(path: str) -> bool:
    try:
        size = os.path.getsize(path)
        if size < 128:
            return False
        with open(path, "rb") as fh:
            fh.seek(-128, os.SEEK_END)
            return fh.read(3) == b"TAG"
    except OSError:
        return False


def write_tags(src: str, dst: str, changes: Changes, overwrite: bool = False) -> WriteReport:
    tags, fmt, audio = read_tags(src)
    if changes.is_empty():
        raise TagError("没有需要写入的内容：请至少指定一个元数据、封面或歌词参数。")

    src_abs, dst_abs = os.path.abspath(src), os.path.abspath(dst)
    if src_abs == dst_abs and not overwrite:
        raise TagError(
            f"输出路径与原文件相同：{dst}\n"
            "默认不覆盖原文件。请换一个输出路径，或显式加上 --force（会覆盖原文件，请先备份）。"
        )
    if os.path.exists(dst_abs):
        if os.path.isdir(dst_abs):
            raise TagError(f"输出路径是一个目录：{dst}")
        if not overwrite:
            raise TagError(f"输出文件已存在：{dst}\n默认不覆盖，请加 --force 覆盖，或换一个输出路径。")

    audio_before = _audio_signature(src, fmt)
    fingerprint_before = _audio_bytes_fingerprint(src, fmt)

    if fmt == "flac":
        removed = _apply_flac(audio, changes)

        def save_into(target: str) -> None:
            audio.save(target, padding=None)
    else:
        new_tags, removed = _build_id3(audio.tags, changes)
        v1 = 1 if _has_id3v1(src) else 0
        v2_version = getattr(new_tags, "v2_version", 4)

        def save_into(target: str) -> None:
            new_tags.save(target, v1=v1, v2_version=v2_version)

    # mutagen 的 MP3/FLAC 保存都是「就地改写文件里的标签区」，
    # 所以输出的临时文件必须先是原文件的完整副本，音频数据才会被保留。
    tmp, _ = _atomic_target(dst_abs)
    try:
        shutil.copyfile(src, tmp)
        save_into(tmp)
    except Exception as exc:
        _safe_unlink(tmp)
        raise TagError(f"写入标签失败（原文件未改动）：{exc}") from exc

    fingerprint_after = _audio_bytes_fingerprint(tmp, fmt)
    if fingerprint_before != fingerprint_after:
        _safe_unlink(tmp)
        raise TagError(
            "检测到音频数据在写入后发生变化，已放弃输出以免损坏音频。"
            f"（原文件未受影响：{src}）"
        )

    try:
        os.replace(tmp, dst_abs)
    except OSError as exc:
        _safe_unlink(tmp)
        raise TagError(f"无法写入输出文件 {dst}：{exc.strerror or exc}") from exc

    return WriteReport(
        output=dst_abs,
        fmt=fmt,
        removed=removed,
        audio_before=audio_before,
        audio_after=_audio_signature(dst_abs, fmt),
    )


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 校验：把写进去的内容重新读出来对比
# --------------------------------------------------------------------------- #


def _cmp(a: Optional[str], b: Optional[str]) -> bool:
    return (a or "").strip() == (b or "").strip()


def verify(dst: str, expected: Changes) -> list[Check]:
    got, fmt, _ = read_tags(dst)
    checks: list[Check] = []

    def add(label: str, ok: bool, exp: Any = "", act: Any = "") -> None:
        checks.append(Check(label, bool(ok), str(exp), str(act)))

    if expected.title is not None:
        if expected.title.strip():
            add("标题", _cmp(got.title, expected.title), expected.title, got.title)
        else:
            add("标题（已清除）", not got.title, "（无）", got.title)
    if expected.artists is not None:
        want = [a.strip() for a in expected.artists if a and a.strip()]
        if want:
            add("作者", [a.lower() for a in got.artists] == [a.lower() for a in want], " / ".join(want), " / ".join(got.artists))
            add("作者数量", len(got.artists) == len(want), len(want), len(got.artists))
        else:
            add("作者（已清除）", not got.artists, "（无）", " / ".join(got.artists))
    if expected.album is not None:
        if expected.album.strip():
            add("专辑", _cmp(got.album, expected.album), expected.album, got.album)
        else:
            add("专辑（已清除）", not got.album, "（无）", got.album)
    if expected.date is not None:
        if expected.date.strip():
            actual = got.date or ""
            same = actual == expected.date or (
                expected.date.count("-") < 2 and actual.startswith(expected.date)
            )
            add("发行日期", same, expected.date, got.date)
        else:
            add("发行日期（已清除）", not got.date, "（无）", got.date)
    if expected.cover.action == "set" and expected.cover.image is not None:
        image = expected.cover.image
        ok = bool(got.cover) and hashlib.sha256(got.cover.data).digest() == hashlib.sha256(image.data).digest()
        add("封面", ok, image.summary, got.cover.summary if got.cover else "（无）")
        add(
            "封面尺寸",
            bool(got.cover) and (got.cover.width, got.cover.height) == (image.width, image.height),
            f"{image.width}x{image.height}",
            f"{got.cover.width}x{got.cover.height}" if got.cover else "—",
        )
        if got.cover:
            add("封面类型声明", got.cover.mime.lower() in (image.mime.lower(), "image/jpg"), image.mime, got.cover.mime)
    elif expected.cover.action == "clear":
        add("封面（已清除）", got.cover is None, "（无）", got.cover.summary if got.cover else "（无）")
    if expected.lyrics.action == "set" and expected.lyrics.text is not None:
        want = normalize_lyrics(expected.lyrics.text).strip()
        actual = normalize_lyrics(got.lyrics or "").strip()
        add("歌词", actual == want, f"{len(want)} 字符（{detect_lyrics_kind(want)}）", f"{len(actual)} 字符（{got.lyrics_kind or '无'}）")
        add("歌词逐行一致", actual.split("\n") == want.split("\n"), "按行比较", "按行比较")
    elif expected.lyrics.action == "clear":
        add("歌词（已清除）", not got.lyrics, "（无）", f"{len(got.lyrics)} 字符" if got.lyrics else "（无）")
    return checks


def describe(tags: Tags) -> str:
    return tags.describe()


def format_audio(sig: dict) -> str:
    return "，".join(f"{k}={v}" for k, v in sig.items()) if sig else "（不可用）"
