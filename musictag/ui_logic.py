# -*- coding: utf-8 -*-
"""
musictag UI 的纯逻辑层。

职责：把「界面表单」翻译成 `musictag.core.Changes`，并且**只把用户真正改动过的
字段**放进去（未改动的字段留 `None`，对应 core「别动这个字段」的语义）。

本模块**不含任何界面库、网络、服务端依赖**，因此可以脱离浏览器单独测试。
真正的标签读写、图片校验、日期归一化全部由 `musictag.core` 完成，这里不重复实现。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

if __package__ in (None, ""):  # 支持直接运行
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from musictag import core
    from musictag.core import Changes, CoverChange, LyricsChange, TagError
else:
    from . import core
    from .core import Changes, CoverChange, LyricsChange, TagError

DEFAULT_SUFFIX = "_tagged"


# --------------------------------------------------------------------------- #
# 路径与字段清洗
# --------------------------------------------------------------------------- #


def default_output(src: str, suffix: str = DEFAULT_SUFFIX) -> str:
    """默认输出路径：原文件名 + 后缀 + 原扩展名（与原文件同目录，且绝不等于原文件）。"""
    root, ext = os.path.splitext(os.path.abspath(src))
    return f"{root}{suffix}{ext}"


def resolve_date(text: Optional[str]) -> str:
    """把界面上的日期文字交给 core 归一化；空字符串原样返回空（表示不写日期）。

    core.normalize_date 认不出来时会抛 TagError，由调用方展示给用户。
    """
    text = (text or "").strip()
    if not text:
        return ""
    return core.normalize_date(text)


def clean_artists(values: Optional[Sequence[str]]) -> List[str]:
    """去掉空白项、按不区分大小写去重（保持顺序）。"""
    out: List[str] = []
    seen = set()
    for raw in values or []:
        name = (raw or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _text(form_value: Optional[str]) -> str:
    return (form_value or "").strip()


# --------------------------------------------------------------------------- #
# 表单往返
# --------------------------------------------------------------------------- #


def initial_form(
    src: str,
    tags: Any,
    audio_text: str = "",
    output: Optional[str] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """用文件里已有的标签填满表单。用户不动这些值，就等于「什么都不改」。"""
    return {
        "src": os.path.abspath(src),
        "title": tags.title or "",
        "artists": list(tags.artists),
        "album": tags.album or "",
        "date": tags.date or "",
        # 封面：keep 表示沿用文件里原有的封面；set / clear 由前端在用户操作后设置
        "cover": {"action": "keep", "token": None},
        "lyrics": {
            "action": "keep",
            "text": tags.lyrics or "",
            "kind": tags.lyrics_kind or "text",
        },
        "output": output or default_output(src),
        "overwrite": bool(overwrite),
        # 只读的展示信息
        "audio_text": audio_text,
        "existing_count": int(getattr(tags, "existing_count", 0) or 0),
        "original": {
            "title": tags.title or "",
            "artists": list(tags.artists),
            "album": tags.album or "",
            "date": tags.date or "",
            "has_cover": tags.cover is not None,
            "cover_text": tags.cover.summary if tags.cover else "",
            "lyrics": tags.lyrics or "",
            "lyrics_kind": tags.lyrics_kind or "",
        },
    }


# --------------------------------------------------------------------------- #
# 表单 -> Changes（核心：差异比对）
# --------------------------------------------------------------------------- #


def build_changes(form: Dict[str, Any], original: Any) -> Changes:
    """把表单与文件原有标签比对，生成 Changes。

    规则（与 core 的语义对齐）：
      * 字段没变        -> None      「别动它」
      * 字段变了        -> 新值
      * 字段被清空      -> ""/(空列表) 「清除它」

    这样「只更新用户指定的内容」在界面上自动成立：用户不动的地方一律不写。
    """
    # ---- 文本字段 ----
    title = _diff_text(form.get("title"), getattr(original, "title", None))
    album = _diff_text(form.get("album"), getattr(original, "album", None))

    # ---- 作者（多位）----
    new_artists = clean_artists(form.get("artists"))
    old_artists = clean_artists(getattr(original, "artists", None))
    artists: Optional[List[str]] = None if new_artists == old_artists else new_artists

    # ---- 发行日期 ----
    raw_date = _text(form.get("date"))
    old_date = _text(getattr(original, "date", None))
    date: Optional[str] = None
    if raw_date:
        normalized = core.normalize_date(raw_date)  # 认不出来会抛 TagError
        if normalized != old_date:
            date = normalized
    elif old_date:
        date = ""  # 原来有日期、现在被清空 -> 清除

    # ---- 封面 ----
    cover_form = form.get("cover") or {}
    cover_action = (cover_form.get("action") or "keep").lower()
    cover = CoverChange("keep")
    if cover_action == "clear":
        cover = CoverChange("clear")
    elif cover_action == "set":
        image = cover_form.get("image")
        if image is None:
            raise TagError("封面图片已失效（可能是服务重启了），请重新选择一次。")
        cover = CoverChange("set", image)

    # ---- 歌词 ----
    lyrics_form = form.get("lyrics") or {}
    lyrics_action = (lyrics_form.get("action") or "keep").lower()
    lyrics = LyricsChange("keep")
    if lyrics_action == "clear":
        lyrics = LyricsChange("clear")
    elif lyrics_action == "set":
        text = core.normalize_lyrics(lyrics_form.get("text") or "")
        if not text.strip():
            # 用户把歌词内容删干净了，等同于「清除歌词」
            lyrics = LyricsChange("clear")
        elif text == core.normalize_lyrics(getattr(original, "lyrics", None) or ""):
            lyrics = LyricsChange("keep")  # 改来改去还是原样，别动它
        else:
            kind = (lyrics_form.get("kind") or "").strip() or core.detect_lyrics_kind(text)
            lyrics = LyricsChange("set", text, kind)

    return Changes(
        title=title,
        artists=artists,
        album=album,
        date=date,
        cover=cover,
        lyrics=lyrics,
    )


def _diff_text(form_value: Optional[str], original_value: Optional[str]) -> Optional[str]:
    """返回 None（没改）/ 新值 / ""（被清空，即清除）。"""
    new = _text(form_value)
    old = _text(original_value)
    if new == old:
        return None
    return new


# --------------------------------------------------------------------------- #
# 给界面看的「将要做什么」说明
# --------------------------------------------------------------------------- #


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        return " / ".join(value) if value else "—"
    text = str(value).strip()
    return text or "—"


def describe_changes(changes: Changes, original: Any) -> List[Dict[str, str]]:
    """列出「旧值 -> 新值」，供界面上的试运行/确认区展示。"""
    rows: List[Dict[str, str]] = []
    old_lyrics_len = len(getattr(original, "lyrics", None) or "")

    if changes.title is not None:
        rows.append({"label": "标题", "before": _fmt(getattr(original, "title", None)), "after": _fmt(changes.title)})
    if changes.artists is not None:
        rows.append({"label": "作者", "before": _fmt(list(getattr(original, "artists", None) or [])), "after": _fmt(list(changes.artists))})
    if changes.album is not None:
        rows.append({"label": "专辑", "before": _fmt(getattr(original, "album", None)), "after": _fmt(changes.album)})
    if changes.date is not None:
        rows.append({"label": "发行日期", "before": _fmt(getattr(original, "date", None)), "after": _fmt(changes.date)})
    if changes.cover.action != "keep":
        image = changes.cover.image
        old_image = getattr(original, "cover", None)
        rows.append(
            {
                "label": "封面",
                "before": old_image.summary if old_image is not None else "—",
                "after": image.summary if image else "（清除）",
            }
        )
    if changes.lyrics.action != "keep":
        if changes.lyrics.text:
            after = f"{changes.lyrics.kind}，{len(changes.lyrics.text)} 字符"
        else:
            after = "（清除）"
        before = f"{old_lyrics_len} 字符" if old_lyrics_len else "—"
        rows.append({"label": "歌词", "before": before, "after": after})
    return rows
