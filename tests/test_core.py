#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心功能测试（进程内直接调用 musictag.core）。

运行：
    python tests/test_core.py
退出码 0 表示全部通过。测试会在 tests/_work/ 下生成临时文件，可随时删除。
"""

from __future__ import annotations

import os
import shutil
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import fixtures  # noqa: E402
from musictag import core  # noqa: E402
from musictag.core import Changes, CoverChange, LyricsChange, ImageError, TagError  # noqa: E402

WORK = os.path.join(HERE, "_work")
FAILED: list = []
PASSED = 0

LRC = "[00:01.00]第一行歌词\n[00:05.50]第二行歌词\n[00:10.00]副歌\n"
PLAIN = "第一行歌词\n第二行歌词\n第三行\n"


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def expect_error(label: str, fn, exc=TagError, needle: str = "") -> None:
    try:
        fn()
    except exc as err:
        if needle and needle not in str(err):
            check(label, False, f"错误信息里没有 {needle!r}：{err}")
        else:
            check(label, True)
    except Exception as err:  # noqa: BLE001
        check(label, False, f"抛出了 {type(err).__name__}: {err}")
    else:
        check(label, False, "本应报错但没有报错")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------- #

def test_detection() -> None:
    section("格式识别与基础错误")
    check("识别 flac", core.detect_format(os.path.join(WORK, "sample.flac")) == "flac")
    check("识别 mp3", core.detect_format(os.path.join(WORK, "sample.mp3")) == "mp3")
    expect_error("不存在的文件报错", lambda: core.detect_format(os.path.join(WORK, "nope.mp3")), needle="文件不存在")
    expect_error("目录当文件报错", lambda: core.detect_format(WORK), needle="目录")
    bogus = os.path.join(WORK, "fake.wav")
    with open(bogus, "wb") as fh:
        fh.write(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    expect_error("不支持的格式报错", lambda: core.detect_format(bogus), exc=core.FormatError, needle="只处理 MP3 与 FLAC")
    empty = os.path.join(WORK, "empty.mp3")
    open(empty, "wb").close()
    expect_error("空文件报错", lambda: core.detect_format(empty), exc=core.FormatError, needle="空")


def test_dates() -> None:
    section("发行日期统一格式")
    cases = {
        "2005-11-01": "2005-11-01",
        "2005/11/1": "2005-11-01",
        "2005.11.01": "2005-11-01",
        "2005年11月1日": "2005-11-01",
        "20051101": "2005-11-01",
        "2019-03": "2019-03",
        "2019": "2019",
        "2019-03-01 12:30:00": "2019-03-01 12:30:00",
    }
    for raw, want in cases.items():
        got = core.normalize_date(raw)
        check(f"日期 {raw!r} -> {want}", got == want, f"实际 {got!r}")
    for bad in ("三月一日", "2019-13-45", ""):
        expect_error(f"非法日期 {bad!r} 报错", lambda b=bad: core.normalize_date(b))


def test_images() -> None:
    section("封面图片校验")
    png = core.read_image(os.path.join(WORK, "cover.png"))
    check("PNG 尺寸解析 300x200", (png.mime, png.width, png.height) == ("image/png", 300, 200), str(png))
    jpg = core.read_image(os.path.join(WORK, "cover.jpg"))
    check("JPEG 尺寸解析 240x160", (jpg.mime, jpg.width, jpg.height) == ("image/jpeg", 240, 160), str(jpg))
    expect_error("损坏 PNG 报错", lambda: core.read_image(os.path.join(WORK, "broken.png")), exc=ImageError, needle="PNG")
    expect_error("损坏 JPEG 报错", lambda: core.read_image(os.path.join(WORK, "broken.jpg")), exc=ImageError, needle="JPEG")
    expect_error("封面不存在报错", lambda: core.read_image(os.path.join(WORK, "nope.png")), exc=ImageError, needle="不存在")
    txt = os.path.join(WORK, "notimage.png")
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write("这不是图片")
    expect_error("非图片文件报错", lambda: core.read_image(txt), exc=ImageError, needle="不是可识别的图片")


def test_lyrics_io() -> None:
    section("歌词文件读取与格式识别")
    lrc_path = os.path.join(WORK, "lyrics.lrc")
    with open(lrc_path, "w", encoding="utf-8") as fh:
        fh.write(LRC)
    txt_path = os.path.join(WORK, "lyrics.txt")
    with open(txt_path, "w", encoding="gb18030") as fh:
        fh.write(PLAIN)
    lrc = core.read_lyrics_text(lrc_path)
    plain = core.read_lyrics_text(txt_path)
    check("LRC 识别为 lrc", core.detect_lyrics_kind(lrc) == "lrc")
    check("纯文本识别为 text", core.detect_lyrics_kind(plain) == "text")
    check("GB18030 歌词可读", "第一行歌词" in plain, repr(plain[:20]))
    expect_error("歌词文件不存在报错", lambda: core.read_lyrics_text(os.path.join(WORK, "nope.lrc")), needle="不存在")
    other = os.path.join(WORK, "lyrics-utf8.txt")
    with open(other, "w", encoding="utf-8") as fh:
        fh.write(PLAIN)
    check("UTF-8 歌词可读", "第二行歌词" in core.read_lyrics_text(other))


def full_changes(png: core.Image) -> Changes:
    return Changes(
        title="夜曲",
        artists=["周杰伦", "方文山"],
        album="十一月的萧邦",
        date="2005-11-01",
        cover=CoverChange("set", png),
        lyrics=LyricsChange("set", LRC, "lrc"),
    )


def test_full_write(fmt: str, name: str) -> None:
    section(f"完整写入 + 校验（{fmt.upper()}）")
    src = os.path.join(WORK, f"{name}-input.{fmt}")
    dst = os.path.join(WORK, f"{name}-full.{fmt}")
    png = core.read_image(os.path.join(WORK, "cover.png"))
    changes = full_changes(png)

    before = core._audio_signature(src, fmt)
    report = core.write_tags(src, dst, changes)
    check("输出文件已生成", os.path.exists(dst))
    check("音频参数写入后一致", report.audio_unchanged, f"{report.audio_before} vs {report.audio_after}")
    check("报告格式正确", report.fmt == fmt)

    checks = core.verify(dst, changes)
    for c in checks:
        check(f"校验项 {c.label}", c.ok, f"期望 {c.expected} 实际 {c.actual}")

    tags, _, _ = core.read_tags(dst)
    check("标题读回一致", tags.title == "夜曲", str(tags.title))
    check("多位作者读回一致", tags.artists == ["周杰伦", "方文山"], str(tags.artists))
    check("专辑读回一致", tags.album == "十一月的萧邦", str(tags.album))
    check("日期读回一致", tags.date == "2005-11-01", str(tags.date))
    check("歌词读回一致", core.normalize_lyrics(tags.lyrics or "").strip() == LRC.strip(), repr((tags.lyrics or "")[:40]))
    check("封面存在且尺寸正确", bool(tags.cover) and (tags.cover.width, tags.cover.height) == (300, 200), str(tags.cover))
    check("封面数据一致", bool(tags.cover) and tags.cover.data == png.data)
    check("封面 MIME 正确", bool(tags.cover) and tags.cover.mime == "image/png", str(tags.cover.mime if tags.cover else None))

    after = core._audio_signature(dst, fmt)
    for key, value in before.items():
        check(f"音频参数 {key} 未变", after.get(key) == value, f"{value} -> {after.get(key)}")

    raw_src = open(src, "rb").read()
    raw_dst = open(dst, "rb").read()
    if fmt == "flac":
        check("FLAC 音频段逐字节一致", raw_src[-4000:] == raw_dst[-4000:])
        check("FLAC 仍以 fLaC 开头", raw_dst[:4] == b"fLaC")
    else:
        check("MP3 音频段逐字节一致", raw_src == raw_dst[-len(raw_src):])
        check("MP3 ID3v2 头存在", raw_dst[:3] == b"ID3")
        check("MP3 帧同步字保留", b"\xff\xfb" in raw_dst)
    return dst


def test_preserve_and_update(fmt: str, name: str) -> None:
    section(f"保留其它标签 + 局部更新（{fmt.upper()}）")
    src = os.path.join(WORK, f"{name}-rich.{fmt}")
    step1 = os.path.join(WORK, f"{name}-rich-1.{fmt}")
    step2 = os.path.join(WORK, f"{name}-rich-2.{fmt}")
    shutil.copyfile(os.path.join(WORK, f"{name}-input.{fmt}"), src)

    # 先写入一批「原有标签」
    core.write_tags(
        src,
        step1,
        Changes(
            title="原标题",
            artists=["原歌手"],
            album="原专辑",
            date="1999-01-01",
            cover=CoverChange("set", core.read_image(os.path.join(WORK, "cover.jpg"))),
            lyrics=LyricsChange("set", PLAIN, "text"),
        ),
    )
    # 追加一个本工具不认识的字段，验证迁移时保留
    if fmt == "flac":
        from mutagen.flac import FLAC

        audio = FLAC(step1)
        audio["comment"] = ["自定义备注"]
        audio["genre"] = ["Rock"]
        audio.save()
    else:
        from mutagen.id3 import COMM

        from mutagen.mp3 import MP3

        audio = MP3(step1)
        audio.tags.add(COMM(encoding=3, lang="XXX", desc="", text=["自定义备注"]))
        audio.tags.add(core.id3.TCON(encoding=3, text=["Rock"]))
        audio.save(v2_version=4)

    # 只改标题，其余应保留
    core.write_tags(step1, step2, Changes(title="新标题"))
    tags, _, _ = core.read_tags(step2)
    check("标题已更新", tags.title == "新标题", str(tags.title))
    check("作者被保留", tags.artists == ["原歌手"], str(tags.artists))
    check("专辑被保留", tags.album == "原专辑", str(tags.album))
    check("日期被保留", tags.date == "1999-01-01", str(tags.date))
    check("歌词被保留", PLAIN.strip() in (tags.lyrics or ""), repr((tags.lyrics or "")[:20]))
    check("封面被保留", bool(tags.cover) and tags.cover.mime == "image/jpeg", str(tags.cover))

    if fmt == "flac":
        from mutagen.flac import FLAC

        check("自定义字段 comment 保留", FLAC(step2).get("comment") == ["自定义备注"])
        check("其它字段 genre 保留", FLAC(step2).get("genre") == ["Rock"])
    else:
        from mutagen.mp3 import MP3

        audio = MP3(step2)
        comms = [str(f) for f in audio.tags.getall("COMM")]
        check("自定义字段 COMM 保留", any("自定义备注" in c for c in comms), str(comms))
        check("其它字段 TCON 保留", bool(audio.tags.getall("TCON")))

    # 清空封面与歌词，其它仍在
    step3 = os.path.join(WORK, f"{name}-rich-3.{fmt}")
    core.write_tags(step2, step3, Changes(cover=CoverChange("clear"), lyrics=LyricsChange("clear")))
    tags3, _, _ = core.read_tags(step3)
    check("封面已清除", tags3.cover is None, str(tags3.cover))
    check("歌词已清除", not tags3.lyrics, repr(tags3.lyrics))
    check("清空封面后标题仍在", tags3.title == "新标题", str(tags3.title))
    check("清空封面后作者仍在", tags3.artists == ["原歌手"], str(tags3.artists))


def test_no_empty_tags(fmt: str, name: str) -> None:
    section(f"缺省不写空标签（{fmt.upper()}）")
    src = os.path.join(WORK, f"{name}-input.{fmt}")
    dst = os.path.join(WORK, f"{name}-minimal.{fmt}")
    core.write_tags(src, dst, Changes(title="只有标题"))
    tags, _, _ = core.read_tags(dst)
    check("标题写入", tags.title == "只有标题", str(tags.title))
    check("作者为空", tags.artists == [], str(tags.artists))
    check("专辑为空", not tags.album, str(tags.album))
    check("日期为空", not tags.date, str(tags.date))
    check("封面为空", tags.cover is None, str(tags.cover))
    check("歌词为空", not tags.lyrics, str(tags.lyrics))

    # 多作者：两位
    dst2 = os.path.join(WORK, f"{name}-two-artists.{fmt}")
    core.write_tags(src, dst2, Changes(artists=["甲", "乙"]))
    tags2, _, _ = core.read_tags(dst2)
    check("两位作者读回", tags2.artists == ["甲", "乙"], str(tags2.artists))

    # 单作者
    dst3 = os.path.join(WORK, f"{name}-one-artist.{fmt}")
    core.write_tags(src, dst3, Changes(artists=["只有一位"]))
    tags3, _, _ = core.read_tags(dst3)
    check("单作者读回", tags3.artists == ["只有一位"], str(tags3.artists))


def test_errors_and_overwrite(fmt: str, name: str) -> None:
    section(f"错误提示与覆盖保护（{fmt.upper()}）")
    src = os.path.join(WORK, f"{name}-input.{fmt}")
    out = os.path.join(WORK, f"{name}-exists.{fmt}")
    core.write_tags(src, out, Changes(title="第一次"))
    expect_error(
        "输出已存在时报错",
        lambda: core.write_tags(src, out, Changes(title="第二次")),
        needle="输出文件已存在",
    )
    report = core.write_tags(src, out, Changes(title="第二次"), overwrite=True)
    tags, _, _ = core.read_tags(out)
    check("--force 后可覆盖", tags.title == "第二次", str(tags.title))
    check("覆盖后音频仍一致", report.audio_unchanged)
    expect_error(
        "输出路径与原文件相同时报错",
        lambda: core.write_tags(src, src, Changes(title="x")),
        needle="输出路径与原文件相同",
    )
    expect_error(
        "输出目录不存在时报错",
        lambda: core.write_tags(src, os.path.join(WORK, "nodir", "out." + fmt), Changes(title="x")),
        needle="输出目录不存在",
    )
    expect_error(
        "没有任何要写入的内容时报错",
        lambda: core.write_tags(src, os.path.join(WORK, "x." + fmt), Changes()),
        needle="没有需要写入的内容",
    )
    expect_error(
        "封面损坏时拒绝写入",
        lambda: core.write_tags(
            src, os.path.join(WORK, "y." + fmt), Changes(cover=CoverChange("set", core.read_image(os.path.join(WORK, "broken.png"))))
        ),
        exc=ImageError,
    )
    # 拒绝写入时不应留下半成品文件
    check("失败后未留下输出文件", not os.path.exists(os.path.join(WORK, "y." + fmt)))


def test_verify_detects_mismatch(fmt: str, name: str) -> None:
    section(f"校验能发现不一致（{fmt.upper()}）")
    src = os.path.join(WORK, f"{name}-input.{fmt}")
    out = os.path.join(WORK, f"{name}-check.{fmt}")
    core.write_tags(src, out, Changes(title="正确标题", album="正确专辑"))
    good = core.verify(out, Changes(title="正确标题", album="正确专辑"))
    check("一致时全部通过", all(c.ok for c in good), str([(c.label, c.actual) for c in good if not c.ok]))
    wrong = core.verify(out, Changes(title="错误标题"))
    check("不一致时被检出", any(not c.ok for c in wrong), str([(c.label, c.ok) for c in wrong]))


def test_corrupt_cover_read(fmt: str, name: str) -> None:
    section(f"已有封面损坏时仍能读取标签（{fmt.upper()}）")
    src = os.path.join(WORK, f"{name}-input.{fmt}")
    out = os.path.join(WORK, f"{name}-corrupt.{fmt}")
    # 写入正常标签后再把封面数据破坏掉
    core.write_tags(src, out, Changes(title="标题", cover=CoverChange("set", core.read_image(os.path.join(WORK, "cover.png")))))
    data = bytearray(open(out, "rb").read())
    if fmt == "flac":
        marker = data.find(b"image/png")
        check("找到封面 MIME 标记", marker > 0)
        if marker > 0:
            data[marker : marker + 9] = b"image/xxx"
    else:
        marker = data.find(b"image/png")
        check("找到封面 MIME 标记", marker > 0)
        if marker > 0:
            data[marker : marker + 9] = b"image/xxx"
    open(out, "wb").write(bytes(data))
    try:
        tags, _, _ = core.read_tags(out)
        check("损坏封面不影响读取其它标签", tags.title == "标题", str(tags.title))
    except Exception as err:  # noqa: BLE001
        check("损坏封面不影响读取其它标签", False, f"{type(err).__name__}: {err}")


def test_output_extension_mismatch() -> None:
    """输出路径的扩展名与源文件格式不一致时也要能用（按文件头判断格式）。"""
    section("输出扩展名与源格式不一致")
    src = os.path.join(WORK, "b-input.mp3")
    # MP3 内容 + .flac 扩展名
    dst = os.path.join(WORK, "wrong-ext.flac")
    try:
        report = core.write_tags(src, dst, Changes(title="扩展名不符", album="专辑"))
        check("写出成功且格式按文件头判定为 mp3", report.fmt == "mp3", report.fmt)
        tags, fmt, _ = core.read_tags(dst)
        check("重新读回格式为 mp3", fmt == "mp3", fmt)
        check("标题读回正确", tags.title == "扩展名不符", str(tags.title))
        check("专辑读回正确", tags.album == "专辑", str(tags.album))
        check("音频指纹未变",
              core._audio_bytes_fingerprint(src, "mp3") == core._audio_bytes_fingerprint(dst, "mp3"))
        check("校验全部通过", all(c.ok for c in core.verify(dst, Changes(title="扩展名不符"))))
    except Exception as err:  # noqa: BLE001
        check("扩展名不一致时仍可写入", False, f"{type(err).__name__}: {err}")

    # 反过来：FLAC 内容 + .mp3 扩展名
    dst2 = os.path.join(WORK, "wrong-ext.mp3")
    try:
        report = core.write_tags(os.path.join(WORK, "a-input.flac"), dst2, Changes(title="反向"))
        check("FLAC 内容写成 .mp3 也能成功", report.fmt == "flac", report.fmt)
        check("反向读回格式为 flac", core.read_tags(dst2)[1] == "flac")
        check("反向标题读回正确", core.read_tags(dst2)[0].title == "反向")
    except Exception as err:  # noqa: BLE001
        check("FLAC 内容 + .mp3 扩展名仍可写入", False, f"{type(err).__name__}: {err}")


def main() -> int:
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    print(f"临时目录: {WORK}")

    sections = os.path.join(WORK, "sample")
    fixtures.make_flac(sections + ".flac")
    fixtures.make_mp3(sections + ".mp3", frames=40)
    fixtures.make_png(os.path.join(WORK, "cover.png"))
    fixtures.make_jpeg(os.path.join(WORK, "cover.jpg"))
    fixtures.make_broken_png(os.path.join(WORK, "broken.png"))
    fixtures.make_broken_jpeg(os.path.join(WORK, "broken.jpg"))
    # 每个用例用独立副本，避免互相影响
    for fmt, name in (("flac", "a"), ("mp3", "b")):
        shutil.copyfile(os.path.join(WORK, f"sample.{fmt}"), os.path.join(WORK, f"{name}-input.{fmt}"))

    tests = [
        test_detection,
        test_dates,
        test_images,
        test_lyrics_io,
        test_output_extension_mismatch,
    ]
    for fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            FAILED.append(f"{fn.__name__} 崩溃")
            traceback.print_exc()

    for fmt, name in (("flac", "a"), ("mp3", "b")):
        for fn in (
            test_full_write,
            test_preserve_and_update,
            test_no_empty_tags,
            test_errors_and_overwrite,
            test_verify_detects_mismatch,
            test_corrupt_cover_read,
        ):
            try:
                fn(fmt, name)
            except Exception:  # noqa: BLE001
                FAILED.append(f"{fmt}/{name} {fn.__name__} 崩溃")
                traceback.print_exc()

    print("\n" + "=" * 60)
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 通过 {PASSED} 项")
        for item in FAILED:
            print("  - " + item)
        return 1
    print(f"全部通过：{PASSED} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
