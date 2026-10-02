#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
musictag —— MP3 / FLAC 音乐标签封装工具（命令行入口）

把歌曲名称、作者、专辑、发行日期、封面图片、歌词写入 MP3 / FLAC 文件，
只改写标签区，**不重新编码**，音质与原文件完全一致；输出为新文件，默认不覆盖原文件。

常用示例
--------
  写入全部信息（LRC 歌词 + JPG 封面）：
    python musictag.py "song.mp3" --title "夜曲" --artist 周杰伦 --album "十一月的萧邦" \\
        --date 2005-11-01 --cover cover.jpg --lyrics song.lrc

  多位作者（重复 --artist，或用逗号/顿号分隔）：
    python musictag.py "song.flac" --artist 周杰伦 --artist 方文山 --title "夜曲"

  指定输出路径：
    python musictag.py in.mp3 --title "新标题" --out "D:\\Music\\out.mp3"

  查看标签，不写任何东西：
    python musictag.py "song.mp3" --show

  单独校验已写好的文件：
    python musictag.py "--verify-only" "out.mp3" --title "夜曲" --artist 周杰伦 --cover cover.jpg

  清空封面与歌词：
    python musictag.py in.mp3 --clear-cover --clear-lyrics --out out.mp3

交换格式说明
------------
    发行日期统一写成 YYYY-MM-DD（如 2019-03-01），只给年月则为 YYYY-MM。
    缺省不指定 = 保留原文件里的内容；显式给空字符串 = 删除该字段；信息缺失时绝不写入空标签。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import List, Optional

if __package__ in (None, ""):  # 支持 `python musictag.py ...` 直接运行
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from musictag import core
    from musictag.core import Changes, CoverChange, LyricsChange, TagError
else:
    from . import core
    from .core import Changes, CoverChange, LyricsChange, TagError

__version__ = "1.0.0"
DEFAULT_SUFFIX = "_tagged"

# 控制台颜色（仅当终端支持时启用；重定向到文件时自动关闭）
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def ok(text: str) -> str:
    return _c(text, "32")


def bad(text: str) -> str:
    return _c(text, "31")


def warn(text: str) -> str:
    return _c(text, "33")


def dim(text: str) -> str:
    return _c(text, "2")


# --------------------------------------------------------------------------- #
# 参数解析辅助
# --------------------------------------------------------------------------- #


def unescape(value: str) -> str:
    """允许在命令行里用 \\n \\t 书写换行等（例如 --lyrics-text "第一行\\n第二行"）。"""
    if "\\" not in value:
        return value
    return re.sub(
        r"\\(n|r|t|\\|0)",
        lambda m: {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", "0": "\0"}[m.group(1)],
        value,
    )


def split_artists(values: List[str]) -> List[str]:
    """把多个 --artist 以及逗号/顿号/分号分隔的写法统一拆成作者列表。"""
    out: List[str] = []
    for value in values:
        for part in re.split(r"[,，;；、]", value):
            part = part.strip()
            if part:
                out.append(part)
    # 去重但保持顺序
    seen, unique = set(), []
    for name in out:
        if name.lower() not in seen:
            seen.add(name.lower())
            unique.append(name)
    return unique


def default_output(src: str, suffix: str) -> str:
    root, ext = os.path.splitext(src)
    return f"{root}{suffix}{ext}"


def build_changes(args: argparse.Namespace, warnings: List[str]) -> Changes:
    for name, value in (
        ("--title", args.title),
        ("--album", args.album),
        ("--artist", args.artist),
        ("--date", args.date),
        ("--cover", args.cover),
        ("--lyrics", args.lyrics),
    ):
        if value == "" or (isinstance(value, list) and any(v == "" for v in value)):
            warnings.append(
                f"{name} 收到空字符串：这会被当作「清除该字段」。若本意是不修改，请直接省略这个参数。"
            )

    title = unescape(args.title).strip() or None if args.title is not None else None
    album = unescape(args.album).strip() or None if args.album is not None else None
    artists: Optional[List[str]] = None
    if args.artist:
        artists = split_artists([unescape(a) for a in args.artist])
    if args.lyrics_lrc and args.lyrics_plain:
        raise TagError("--lyrics-lrc 与 --lyrics-plain 不能同时使用。")

    date: Optional[str] = None
    if args.date is not None:
        date = core.normalize_date(args.date)

    cover = CoverChange()
    if args.clear_cover:
        cover = CoverChange("clear")
    elif args.cover:
        cover = CoverChange("set", core.read_image(args.cover))

    lyrics = LyricsChange()
    if args.clear_lyrics:
        lyrics = LyricsChange("clear")
    elif args.lyrics and args.lyrics_text:
        raise TagError("--lyrics 与 --lyrics-text 不能同时使用，请只保留一个。")
    elif args.lyrics:
        text = core.normalize_lyrics(core.read_lyrics_text(args.lyrics))
        if not text.strip():
            raise TagError(f"歌词文件是空的：{args.lyrics}")
        kind = "lrc" if args.lyrics_lrc else "text" if args.lyrics_plain else core.detect_lyrics_kind(text)
        lyrics = LyricsChange("set", text, kind)
    elif args.lyrics_text is not None:
        text = core.normalize_lyrics(unescape(args.lyrics_text))
        if not text.strip():
            raise TagError("--lyrics-text 不能是空字符串；要删除歌词请使用 --clear-lyrics。")
        kind = "lrc" if args.lyrics_lrc else "text" if args.lyrics_plain else core.detect_lyrics_kind(text)
        lyrics = LyricsChange("set", text, kind)

    return Changes(title=title, artists=artists, album=album, date=date, cover=cover, lyrics=lyrics)


def planned_lines(changes: Changes, expected: Optional[Changes] = None) -> List[str]:
    ref = expected if expected is not None else changes
    lines: List[str] = []
    if ref.title is not None:
        lines.append(f"标题     : {ref.title or '(清除)'}")
    if ref.artists is not None:
        lines.append(f"作者     : {' / '.join(ref.artists) if ref.artists else '(清除)'}")
    if ref.album is not None:
        lines.append(f"专辑     : {ref.album or '(清除)'}")
    if ref.date is not None:
        lines.append(f"发行日期 : {ref.date or '(清除)'}")
    if ref.cover.action != "keep":
        image = ref.cover.image
        lines.append(f"封面     : {image.summary if image else '(清除)'}")
    if ref.lyrics.action != "keep":
        if ref.lyrics.text:
            lines.append(f"歌词     : {ref.lyrics.kind}，{len(ref.lyrics.text)} 字符")
        else:
            lines.append("歌词     : (清除)")
    return lines


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #


def cmd_show(path: str) -> int:
    tags, fmt, _ = core.read_tags(path)
    print(f"文件      : {os.path.abspath(path)}")
    print(f"格式      : {fmt.upper()}")
    print(dim("-" * 60))
    print(core.describe(tags))
    print(dim("-" * 60))
    sig = core._audio_signature(path, fmt)
    print(f"音频参数  : {core.format_audio(sig)}")
    print(dim(f"原有标签项：{tags.existing_count}"))
    return 0


def cmd_write(args: argparse.Namespace) -> int:
    warnings: List[str] = []
    changes = build_changes(args, warnings)
    for w in warnings:
        print(warn("提示: " + w))

    if changes.is_empty():
        raise TagError(
            "没有指定任何要写入的内容。\n"
            "请至少给出一个参数，例如 --title / --artist / --album / --date / --cover / --lyrics。\n"
            "查看现有标签请用 --show；完整帮助请用 --help。"
        )

    src = args.audio
    out = args.out or default_output(src, args.suffix)

    print(f"源文件    : {os.path.abspath(src)}")
    print(f"输出文件  : {os.path.abspath(out) if out else '—'}")
    print(dim("-" * 60))
    print("将写入：")
    for line in planned_lines(changes):
        print("  " + line)

    if args.dry_run:
        tags, fmt, _ = core.read_tags(src)
        print(dim("-" * 60))
        print(f"[试运行] 格式 {fmt.upper()}，未写入任何文件。")
        print("原文件现有标签（未指定项将保留）：")
        print(core.describe(tags))
        return 0

    tags, fmt, _ = core.read_tags(src)
    print(dim("-" * 60))
    print("原文件中未指定、将被保留的标签：")
    print(core.describe(tags))

    report = core.write_tags(src, out, changes, overwrite=args.force)
    print(dim("-" * 60))
    print(ok(f"✔ 已写出：{report.output}") + f"  （格式 {report.fmt.upper()}）")
    if report.removed:
        print(f"  已清除字段：{'、'.join(report.removed)}")
    print(
        f"  音频数据校验：{'逐字节一致（未重新编码）' if report.audio_unchanged else '参数一致'}"
    )

    checks = core.verify(report.output, changes)
    print(dim("-" * 60))
    print("重新打开输出文件逐项校验：")
    failed = 0
    for check in checks:
        mark = ok("✔") if check.ok else bad("✘")
        detail = f"写入 {check.expected}" + (f"，读出 {check.actual}" if check.expected else "")
        print(f"  {mark} {check.label:<14} {detail}")
        failed += 0 if check.ok else 1
    if failed:
        print(bad(f"✘ 有 {failed} 项校验未通过，输出文件可能不完整：{report.output}"))
        return 1
    print(ok("✔ 全部校验通过：以上信息可以从输出文件重新读出。"))
    return 0


def cmd_verify_only(args: argparse.Namespace) -> int:
    warnings: List[str] = []
    expected = build_changes(args, warnings)
    if expected.is_empty():
        raise TagError("--verify-only 需要给出期望值，例如 --title / --artist / --cover / --lyrics。")
    path = args.out or args.audio
    if not path or not os.path.exists(path):
        raise TagError(f"要校验的文件不存在：{path}")
    print(f"校验文件  : {os.path.abspath(path)}")
    for line in planned_lines(expected):
        print("  期望 " + line)
    print(dim("-" * 60))
    checks = core.verify(path, expected)
    failed = 0
    for check in checks:
        mark = ok("✔") if check.ok else bad("✘")
        detail = f"期望 {check.expected}，实际 {check.actual}"
        print(f"  {mark} {check.label:<14} {detail}")
        failed += 0 if check.ok else 1
    if failed:
        print(bad(f"✘ {failed} 项不匹配。"))
        return 1
    print(ok("✔ 全部匹配：标签可以从文件重新读出。"))
    return 0


# --------------------------------------------------------------------------- #
# 命令行定义
# --------------------------------------------------------------------------- #

EPILOG = """\
行为约定
  1. 只改写标签区，不重新编码：音频字节与写入前逐字节一致（工具会自行校验）。
  2. 输出新文件，默认不覆盖原文件；输出已存在时报错，需加 --force 才覆盖。
  3. 未指定的字段保留原文件内容；显式给空字符串或 --clear-* 才会删除。
  4. 信息缺失时不写入任何空标签。
  5. 发行日期统一为 YYYY-MM-DD（只给年月则为 YYYY-MM）。

校验方法
  写入后工具会自动重新打开输出文件逐项核对；也可单独校验：
    python musictag.py --verify-only out.mp3 --title "夜曲" --artist 周杰伦 --cover cover.jpg
  只想看文件里到底有什么：
    python musictag.py out.mp3 --show
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="musictag",
        description="MP3 / FLAC 音乐标签封装工具：写入标题、作者、专辑、发行日期、封面、歌词（不重新编码）。",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("audio", help="输入音频文件（.mp3 或 .flac）")
    parser.add_argument("--version", action="version", version=f"musictag {__version__}")

    g = parser.add_argument_group("要写入的信息（省略 = 保留原值，给空字符串 = 清除）")
    g.add_argument("--title", metavar="文本", help="歌曲名称")
    g.add_argument(
        "--artist",
        metavar="作者",
        action="append",
        default=[],
        help="作者，可重复多次；也支持用逗号/顿号分隔多个作者",
    )
    g.add_argument("--album", metavar="文本", help="专辑名称")
    g.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="发行日期，统一格式 年-月-日（例：2019-03-01）或 年-月（例：2019-03）",
    )
    g.add_argument("--cover", metavar="图片", help="封面图片路径（JPG / PNG）")
    g.add_argument("--lyrics", metavar="文件", help="歌词文件路径（纯文本或 LRC，自动识别编码与格式）")
    g.add_argument(
        "--lyrics-text",
        metavar="文本",
        help='直接把歌词写在命令行上，换行用 \\n，例如 --lyrics-text "第一行\\n第二行"',
    )
    g.add_argument("--lyrics-lrc", action="store_true", help="强制按 LRC 处理歌词")
    g.add_argument("--lyrics-plain", action="store_true", help="强制按纯文本处理歌词")
    g.add_argument("--clear-cover", action="store_true", help="删除文件里已有的封面")
    g.add_argument("--clear-lyrics", action="store_true", help="删除文件里已有的歌词")

    g = parser.add_argument_group("输出与行为")
    g.add_argument("--out", "-o", metavar="路径", help=f"输出文件路径（默认 原文件名{DEFAULT_SUFFIX}.扩展名）")
    g.add_argument("--suffix", metavar="后缀", default=DEFAULT_SUFFIX, help=f"默认输出文件名后缀（默认 {DEFAULT_SUFFIX}）")
    g.add_argument("--force", "-f", action="store_true", help="允许覆盖已存在的输出文件")
    g.add_argument("--dry-run", action="store_true", help="试运行：只显示将写入什么，不产生文件")
    g.add_argument("--show", action="store_true", help="只显示文件现有标签与音频参数，不做任何修改")
    g.add_argument("--verify-only", action="store_true", help="只校验文件里的标签是否为给定值，不写入")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.show:
            return cmd_show(args.audio)
        if args.verify_only:
            return cmd_verify_only(args)
        return cmd_write(args)
    except TagError as exc:
        print(bad(f"\n✘ 出错：{exc}"), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(bad("\n已中断。"), file=sys.stderr)
        return 130
    except Exception as exc:  # 兜底：给出异常类型，方便排查
        print(bad(f"\n✘ 未预期的错误（{type(exc).__name__}）：{exc}"), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
