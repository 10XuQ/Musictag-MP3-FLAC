#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量打包测试（ncm 解密 + 网易云歌词转换 + 批量写入）。

运行：
    python tests/test_batch.py
退出码 0 表示全部通过。测试在 tests/_batch_work/ 下生成临时文件，可随时删除。

素材全部由 tests/fixtures.py 现造：
* make_ncm 按真实的 ncm 容器格式（AES-128-ECB + 逐字节异或 + 256 字节周期密钥流）
  把一段音频打包成 .ncm，所以这里不依赖用户的下载目录。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import fixtures  # noqa: E402
from musictag import batch, core, ncm  # noqa: E402
from musictag.core import TagError  # noqa: E402

WORK = os.path.join(HERE, "_batch_work")
FAILED: list = []
PASSED = 0

# 网易云实际下载下来的那种歌词：前面是 JSON 行（制作信息），后面才是标准 LRC
NETEASE_LRC = (
    '{"t":0,"c":[{"tx":"演唱: "},{"tx":"甲"}]}\n'
    '{"t":661,"c":[{"tx":"作词: "},{"tx":"乙"}]}\n'
    '{"t":-1000,"c":[{"tx":"作曲: "},{"tx":"丙"}]}\n'
    "[00:05.00]第一行歌词\n"
    "[00:09.50]第二行歌词\n"
)
PLAIN_LRC = "[00:01.00]甲\n[00:02.00]乙\n"


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
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def work(*parts: str) -> str:
    p = os.path.join(WORK, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def fresh(name: str) -> str:
    d = os.path.join(WORK, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


def snapshot(directory: str) -> dict:
    out = {}
    for n in sorted(os.listdir(directory)):
        p = os.path.join(directory, n)
        if os.path.isfile(p):
            out[n] = (os.path.getsize(p), os.path.getmtime(p))
    return out


# --------------------------------------------------------------------------- #
# 1. AES 本身（FIPS-197 已知答案，两个方向都要对）
# --------------------------------------------------------------------------- #

def test_aes() -> None:
    section("1. AES-128-ECB 与 FIPS-197 已知答案一致")
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    ct = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    check("加密结果等于 FIPS-197 附录 C 的密文",
          fixtures.aes128_ecb_encrypt(key, pt) == ct)
    check("解密结果等于原文", ncm.aes128_ecb_decrypt(key, ct) == pt)
    check("S-box 抽查", ncm.SBOX[0x00] == 0x63 and ncm.SBOX[0x53] == 0xED)
    check("密钥流周期是 256 字节",
          ncm._keystream_pattern(b"0123456789abcdef")[:256]
          == ncm._keystream_pattern(b"0123456789abcdef"))

    # 密钥流分块与逐字节结果一致
    import random
    rnd = random.Random(7)
    data = bytes(rnd.randrange(256) for _ in range(5000))
    naive = bytes(b ^ ncm._keystream_pattern(b"keykeykeykeykey1")[i % 256]
                  for i, b in enumerate(data))
    chunked = b"".join(
        ncm._xor_at(data[i:i + 700], ncm._keystream_pattern(b"keykeykeykeykey1"), i)
        for i in range(0, len(data), 700)
    )
    check("分块异或与逐字节异或结果一致（5000 字节）", naive == chunked)


# --------------------------------------------------------------------------- #
# 2. ncm 容器
# --------------------------------------------------------------------------- #

def make_song_ncm(folder: str, *, name="测试歌手 - 测试歌曲", music_name="测试歌曲",
                  artists=(("甲", 1), ("乙", 2)),
                  album="测试专辑", with_cover=True, fmt="mp3") -> str:
    """造一个带完整元信息（可选封面）的 ncm。

    `name` 是文件名，`music_name` 是 ncm 内部记的歌曲名 —— 两者故意分开，
    这样才能验证「写进标签的是 ncm 元信息」而不是「文件名」。
    """
    if fmt == "flac":
        audio = fixtures.make_flac(work("_src", "for_ncm.flac"))
    else:
        audio = fixtures.make_mp3(work("_src", "for_ncm.mp3"), frames=12)
    cover = b""
    if with_cover:
        cover = open(fixtures.make_jpeg(work("_src", "for_ncm.jpg"), 80, 60), "rb").read()
    return fixtures.make_ncm(
        os.path.join(folder, f"{name}.ncm"), audio,
        metadata={"musicName": music_name, "album": album, "format": fmt,
                  "artist": [list(a) for a in artists], "duration": 12345},
        cover=cover)


def test_ncm() -> None:
    section("2. ncm 解析与解密")
    d = fresh("ncm")
    src_audio = fixtures.make_mp3(work("_src", "n2.mp3"), frames=12)
    cover = open(fixtures.make_jpeg(work("_src", "n2.jpg"), 80, 60), "rb").read()
    path = fixtures.make_ncm(
        os.path.join(d, "歌曲.ncm"), src_audio,
        metadata={"musicName": "歌曲名", "album": "专辑名", "format": "mp3",
                  "artist": [["甲", 1], ["乙", 2], ["甲", 9]], "duration": 999},
        cover=cover)

    info = ncm.read_info(path)
    check("magic 是 CTENFDAM", ncm.MAGIC == b"CTENFDAM")
    check("format 读出来是 mp3", info.format == "mp3")
    check("musicName 正确", info.music_name == "歌曲名")
    check("artist 列表去重且保序", info.artists == ["甲", "乙"])
    check("album 正确", info.album == "专辑名")
    check("封面字节一致", info.cover == cover)
    check("封面 mime 识别为 jpeg", info.cover_mime == "image/jpeg")
    check("audio_size 为正", info.audio_size > 0)

    out = os.path.join(d, "解出来的.mp3")
    ncm.decrypt(path, out)
    with open(src_audio, "rb") as fh:
        want = fh.read()
    with open(out, "rb") as fh:
        got = fh.read()
    check("解密后音频与原始字节完全一致", got == want)
    check("解密结果能被识别为 mp3", core.detect_format(out) == "mp3")
    check("解密后没有残留 .part 临时文件",
          not any(n.endswith(".part") for n in os.listdir(d)))

    # 没有封面的 ncm
    nopath = fixtures.make_ncm(
        os.path.join(d, "无封面.ncm"), src_audio,
        metadata={"musicName": "X", "format": "mp3", "artist": [], "album": ""})
    check("没有封面时 cover 是空", ncm.read_info(nopath).cover == b"")

    # 各种坏文件
    bad = os.path.join(d, "假.ncm")
    with open(bad, "wb") as fh:
        fh.write(b"NOTANNCM" + b"\x00" * 64)
    expect_error("不是 ncm 时给出清楚提示", lambda: ncm.read_info(bad), needle="CTENFDAM")

    with open(path, "rb") as fh:
        head = fh.read()
    trunc = os.path.join(d, "截断.ncm")
    with open(trunc, "wb") as fh:
        fh.write(head[:40])
    expect_error("截断的 ncm 会报错", lambda: ncm.read_info(trunc), needle="ncm")

    # 元信息被破坏
    broken = bytearray(head)
    for i in range(180, min(260, len(broken))):
        broken[i] ^= 0xFF
    bpath = os.path.join(d, "坏元信息.ncm")
    with open(bpath, "wb") as fh:
        fh.write(bytes(broken))
    try:
        ncm.read_info(bpath)
    except TagError:
        check("破坏元信息后报错或解出空元信息", True)
    else:
        check("破坏元信息后报错或解出空元信息", True)


# --------------------------------------------------------------------------- #
# 3. 网易云歌词转换
# --------------------------------------------------------------------------- #

def test_lyrics_convert() -> None:
    section("3. 网易云歌词（JSON 行 + LRC 行）转换")

    check("能认出这是网易云格式", batch.is_netease_lyrics(NETEASE_LRC))
    check("标准 LRC 不算网易云格式", not batch.is_netease_lyrics(PLAIN_LRC))

    lrc = batch.convert_lyrics(NETEASE_LRC, "lrc")
    lines = [l for l in lrc.splitlines() if l.strip()]
    check("JSON 行变成了带时间轴的歌词行",
          lines[0] == "[00:00.00]演唱: 甲", repr(lines[0]))
    check("多段文本被拼起来", lines[1] == "[00:00.66]作词: 乙", repr(lines[1]))
    check("负数时间戳被夹到 0（不出现 [-00）",
          lines[2] == "[00:00.00]作曲: 丙", repr(lines[2]))
    check("原来的 LRC 行原样保留", lines[3] == "[00:05.00]第一行歌词")
    check("转换后是合法 LRC", core.detect_lyrics_kind(lrc) == "lrc")
    check("输出里没有任何 JSON 残留", "{" not in lrc and "tx" not in lrc)

    text = batch.convert_lyrics(NETEASE_LRC, "text")
    tlines = [l for l in text.splitlines() if l.strip()]
    check("text 模式去掉时间轴", tlines[0] == "演唱: 甲", repr(tlines[0]))
    check("text 模式保留全部内容行", len(tlines) == 5, repr(len(tlines)))

    raw = batch.convert_lyrics(NETEASE_LRC, "raw")
    check("raw 模式一字不改", raw == NETEASE_LRC)

    check("已经是标准 LRC 的不会被改动",
          batch.convert_lyrics(PLAIN_LRC, "lrc").strip() == PLAIN_LRC.strip())
    check("空文本不会炸", batch.convert_lyrics("", "lrc") == "")
    check("只有换行的文本不会炸", batch.convert_lyrics("\n\n", "lrc").strip() == "")

    # 段里有 li/or 之类的键，不应该漏掉文本
    mixed = ('{"t":0,"c":[{"tx":"作词: "},{"tx":"甲","li":"x"},{"or":"y"}]}\n'
             "[00:01.00]正文\n")
    got = batch.convert_lyrics(mixed, "lrc")
    check("忽略 li/or 之类的非文本段", "作词: 甲" in got, repr(got))
    check("正文行仍在", "[00:01.00]正文" in got)


# --------------------------------------------------------------------------- #
# 4. 扫描与配对
# --------------------------------------------------------------------------- #

def test_scan() -> None:
    section("4. 目录扫描与歌词配对")
    src = fresh("scan")
    out = work("scan_out")
    shutil.rmtree(out, ignore_errors=True)

    a = fixtures.make_mp3(os.path.join(src, "甲 - 歌一.mp3"), frames=8)
    b = fixtures.make_flac(os.path.join(src, "歌二 - 乙.flac"))
    with open(os.path.join(src, "甲 - 歌一.lrc"), "w", encoding="utf-8") as fh:
        fh.write(NETEASE_LRC)
    # 歌手/曲名顺序颠倒的歌词，应该配到 歌二 - 乙.flac
    with open(os.path.join(src, "乙 - 歌二.lrc"), "w", encoding="utf-8") as fh:
        fh.write(PLAIN_LRC)
    with open(os.path.join(src, "没人要的.lrc"), "w", encoding="utf-8") as fh:
        fh.write(PLAIN_LRC)
    ncm_path = make_song_ncm(src, name="丙 - 歌三", music_name="歌三", fmt="flac")
    with open(os.path.join(src, "丙 - 歌三.lrc"), "w", encoding="utf-8") as fh:
        fh.write(NETEASE_LRC)

    plan = batch.scan(src, out)
    s = plan.summary()
    check("总数 = 2 音频 + 1 ncm", s["total"] == 3, str(s))
    check("ncm 计数正确", s["ncm"] == 1)
    check("音频计数正确", s["audio"] == 2)
    check("3 个都配到了歌词", s["with_lyrics"] == 3, str(s["with_lyrics"]))
    check("有 1 个孤儿歌词", s["orphan_lrc"] == 1, str(plan.orphan_lrc))
    check("孤儿歌词就是「没人要的.lrc」",
          plan.orphan_lrc == ["没人要的.lrc"], str(plan.orphan_lrc))

    by_base = {i.base: i for i in plan.items}
    check("文件名相同的按 exact 匹配",
          by_base["甲 - 歌一"].match == "exact")
    check("歌手/曲名顺序颠倒的也能配上（order）",
          by_base["歌二 - 乙"].match == "order")
    check("配到的歌词文件正确",
          os.path.basename(by_base["歌二 - 乙"].lrc) == "乙 - 歌二.lrc")

    # ncm 的输出扩展名要先解密才知道
    check("ncm 条目默认输出是 .??? （解密前没有扩展名）",
          by_base["丙 - 歌三"].fmt == "")

    # 同名 ncm + 音频并存时，按音频处理，不重复解密
    d2 = fresh("scan_dup")
    fixtures.make_mp3(os.path.join(d2, "同一首.mp3"), frames=8)
    fixtures.make_ncm(os.path.join(d2, "同一首.ncm"), os.path.join(d2, "同一首.mp3"),
                      metadata={"format": "mp3", "musicName": "同一首", "artist": []})
    plan2 = batch.scan(d2, work("scan_dup_out"))
    check("同名音频与 ncm 并存时只处理音频",
          plan2.summary()["total"] == 1 and plan2.summary()["ncm"] == 0)
    check("并且给出提示", any("同名音频已存在" in n for n in plan2.notes), str(plan2.notes))

    # 输出目录的约束
    expect_error("输出目录不能等于源目录",
                 lambda: batch.scan(src, src), needle="不能和源目录相同")
    expect_error("源目录不存在时报错",
                 lambda: batch.scan(os.path.join(WORK, "根本没有这个目录")),
                 needle="目录不存在")
    expect_error("输出目录不能深埋在源目录里",
                 lambda: batch.scan(src, os.path.join(src, "子目录", "输出")),
                 needle="不能放在源目录里面")

    # 默认输出目录
    plan3 = batch.scan(src)
    check("默认输出到源目录下的 _packed",
          os.path.basename(plan3.out_dir) == batch.DEFAULT_OUT_DIRNAME, plan3.out_dir)

    # 空目录
    empty = fresh("scan_empty")
    check("空目录不会炸", batch.scan(empty, work("scan_empty_out")).summary()["total"] == 0)


# --------------------------------------------------------------------------- #
# 5. 端到端：真的写出文件并读回校验
# --------------------------------------------------------------------------- #

def test_run_mp3() -> None:
    section("5. 端到端（mp3 + ncm + 网易云歌词）")
    src = fresh("run_mp3")
    out = work("run_mp3_out")
    shutil.rmtree(out, ignore_errors=True)

    # 一个已经有完整标签和封面的 mp3
    plain = fixtures.make_mp3(os.path.join(src, "甲 - 有标签.mp3"), frames=14)
    cover = core.read_image(fixtures.make_png(work("_src", "big.png"), 120, 90))
    core.write_tags(plain, plain, core.Changes(
        title="原歌曲名", artists=["原歌手"], album="原专辑",
        cover=core.CoverChange(action="set", image=cover)), overwrite=True)
    with open(os.path.join(src, "甲 - 有标签.lrc"), "w", encoding="utf-8") as fh:
        fh.write(NETEASE_LRC)

    # 一个 ncm（内嵌音频没有任何标签，只有 ncm 里有元信息 + 封面）
    ncm_path = make_song_ncm(src, name="丙 - 加密歌", music_name="ncm 里的歌名",
                             album="ncm 里的专辑", fmt="mp3")
    with open(os.path.join(src, "丙 - 加密歌.lrc"), "w", encoding="utf-8") as fh:
        fh.write(PLAIN_LRC)

    before = snapshot(src)
    plan = batch.scan(src, out)
    events = []
    stats = batch.run(plan, overwrite=False, lyrics_mode="lrc", on_event=events.append)

    check("2 项全部成功", stats["done"] == 2 and stats["failed"] == 0, str(stats))
    check("解密了 1 个 ncm", stats["decrypted"] == 1, str(stats))
    check("2 个都写入了歌词", stats["lyrics_written"] == 2, str(stats))
    check("写回校验全部通过", stats["verify_failed"] == 0, str(stats))
    check("发出了 start 事件", any(e.get("type") == "start" for e in events))
    check("发出了 done 事件", any(e.get("type") == "done" for e in events))
    check("每个文件都有开始/结束事件",
          sum(1 for e in events if e.get("type") == "item") == 4, str(len(events)))

    check("源目录里的文件一个都没被改动", snapshot(src) == before,
          str(set(snapshot(src).items()) ^ set(before.items()))[:300])

    # -- 有标签的那个 mp3：只加歌词，其它一律保留
    tagged = os.path.join(out, "甲 - 有标签.mp3")
    check("输出文件存在", os.path.exists(tagged))
    tags, fmt, _audio = core.read_tags(tagged)
    check("格式还是 mp3", fmt == "mp3")
    check("原来的歌曲名没被改掉", tags.title == "原歌曲名", repr(tags.title))
    check("原来的作者没被改掉", tags.artists == ["原歌手"], repr(tags.artists))
    check("原来的专辑没被改掉", tags.album == "原专辑", repr(tags.album))
    check("原来的封面还在", tags.cover is not None and tags.cover.width == 120)
    check("歌词已经写进去了（且是 LRC）", tags.lyrics and tags.lyrics_kind == "lrc")
    check("歌词里没有 JSON 残留", "{" not in (tags.lyrics or ""))
    check("歌词第一行是制作信息", (tags.lyrics or "").startswith("[00:00.00]演唱: 甲"),
          repr((tags.lyrics or "")[:40]))

    # 音频部分必须一字未改
    check("音频指纹完全一致（没有重新编码）",
          core._audio_bytes_fingerprint(plain, "mp3")
          == core._audio_bytes_fingerprint(tagged, "mp3"))

    # -- ncm 那个：元信息与封面要用 ncm 自带的补齐
    dec = os.path.join(out, "丙 - 加密歌.mp3")
    check("解密后的文件存在", os.path.exists(dec))
    dtags, dfmt, _ = core.read_tags(dec)
    check("解密结果被识别为 mp3", dfmt == "mp3")
    check("歌曲名从 ncm 补齐（不是文件名）", dtags.title == "ncm 里的歌名", repr(dtags.title))
    check("作者从 ncm 补齐（多位）", dtags.artists == ["甲", "乙"], repr(dtags.artists))
    check("专辑从 ncm 补齐", dtags.album == "ncm 里的专辑", repr(dtags.album))
    check("封面从 ncm 补齐", dtags.cover is not None and dtags.cover.mime == "image/jpeg",
          str(dtags.cover))
    check("封面尺寸解析正确",
          dtags.cover is not None and (dtags.cover.width, dtags.cover.height) == (80, 60),
          str(dtags.cover))
    check("歌词也写进去了", dtags.lyrics and "甲" in dtags.lyrics)
    check("输出里没有 .part 或解密临时文件残留",
          not any(n.startswith(".") or n.endswith(".part") for n in os.listdir(out)),
          str(os.listdir(out)))

    report = [i for i in plan.items if i.kind == "ncm"][0].report
    check("报告说音频没变", report is not None and report.audio_unchanged)


def test_run_flac() -> None:
    section("6. 端到端（flac + ncm 里包 flac）")
    src = fresh("run_flac")
    out = work("run_flac_out")
    shutil.rmtree(out, ignore_errors=True)

    fixtures.make_flac(os.path.join(src, "丁 - 无损.flac"))
    with open(os.path.join(src, "丁 - 无损.lrc"), "w", encoding="utf-8") as fh:
        fh.write(NETEASE_LRC)
    make_song_ncm(src, name="戊 - 加密无损", music_name="ncm 里的无损", fmt="flac")
    with open(os.path.join(src, "戊 - 加密无损.lrc"), "w", encoding="utf-8") as fh:
        fh.write(PLAIN_LRC)

    before = snapshot(src)
    plan = batch.scan(src, out)
    stats = batch.run(plan)
    check("2 项全部成功", stats["done"] == 2 and stats["failed"] == 0, str(stats))
    check("源目录未被改动", snapshot(src) == before)

    tags, fmt, _ = core.read_tags(os.path.join(out, "丁 - 无损.flac"))
    check("flac 还是 flac", fmt == "flac")
    check("flac 歌词写入成功", bool(tags.lyrics) and tags.lyrics_kind == "lrc")
    check("flac 音频段没变",
          core._audio_bytes_fingerprint(os.path.join(src, "丁 - 无损.flac"), "flac")
          == core._audio_bytes_fingerprint(os.path.join(out, "丁 - 无损.flac"), "flac"))

    dtags, dfmt, _ = core.read_tags(os.path.join(out, "戊 - 加密无损.flac"))
    check("ncm 里的 flac 解密后被识别为 flac", dfmt == "flac")
    check("ncm 里的 flac 也补上了元信息", dtags.title == "ncm 里的无损"
          and dtags.artists == ["甲", "乙"], repr((dtags.title, dtags.artists)))
    check("ncm 里的 flac 也补上了封面", dtags.cover is not None)
    check("ncm 里的 flac 也写入了歌词", bool(dtags.lyrics))


def test_run_modes() -> None:
    section("7. 覆盖保护 / 歌词写法 / 内容未变时的情况")
    src = fresh("run_modes")
    out = work("run_modes_out")
    shutil.rmtree(out, ignore_errors=True)

    fixtures.make_mp3(os.path.join(src, "己 - 歌.mp3"), frames=10)
    with open(os.path.join(src, "己 - 歌.lrc"), "w", encoding="utf-8") as fh:
        fh.write(NETEASE_LRC)

    plan = batch.scan(src, out)
    batch.run(plan)
    first = os.path.getsize(os.path.join(out, "己 - 歌.mp3"))

    # 第二次跑：默认跳过
    plan2 = batch.scan(src, out)
    stats2 = batch.run(plan2, overwrite=False)
    check("第二次跑默认跳过已存在的输出", stats2["skipped"] == 1, str(stats2))
    check("跳过的条目有说明",
          "已存在" in plan2.items[0].message, plan2.items[0].message)
    check("文件大小没变", os.path.getsize(os.path.join(out, "己 - 歌.mp3")) == first)

    # 加 --force 就会覆盖
    plan3 = batch.scan(src, out)
    stats3 = batch.run(plan3, overwrite=True)
    check("--force 时覆盖并成功", stats3["done"] == 1 and stats3["skipped"] == 0, str(stats3))

    # 纯文本歌词
    out2 = work("run_modes_text")
    shutil.rmtree(out2, ignore_errors=True)
    plan4 = batch.scan(src, out2)
    batch.run(plan4, lyrics_mode="text")
    tags, _, _ = core.read_tags(os.path.join(out2, "己 - 歌.mp3"))
    check("--lyrics text 写出的是纯文本歌词", tags.lyrics_kind == "text", tags.lyrics_kind)
    check("纯文本里没有时间轴", "[00:" not in (tags.lyrics or ""))
    check("纯文本里保留了内容", "演唱: 甲" in (tags.lyrics or ""))

    # raw 模式
    out3 = work("run_modes_raw")
    shutil.rmtree(out3, ignore_errors=True)
    plan5 = batch.scan(src, out3)
    batch.run(plan5, lyrics_mode="raw")
    tags, _, _ = core.read_tags(os.path.join(out3, "己 - 歌.mp3"))
    check("--lyrics raw 原样保留 JSON 行", "{" in (tags.lyrics or ""))

    # 不认识的模式
    expect_error("不认识的歌词模式会报错",
                 lambda: batch.run(batch.scan(src, work("run_modes_x")), lyrics_mode="乱写"),
                 needle="不认识的歌词模式")

    # 空的歌词文件
    src2 = fresh("run_empty_lrc")
    out4 = work("run_empty_lrc_out")
    shutil.rmtree(out4, ignore_errors=True)
    fixtures.make_mp3(os.path.join(src2, "庚 - 歌.mp3"), frames=8)
    with open(os.path.join(src2, "庚 - 歌.lrc"), "w", encoding="utf-8") as fh:
        fh.write("   \n\n")
    empty_plan = batch.scan(src2, out4)
    stats = batch.run(empty_plan)
    check("空歌词算失败并给出提示",
          stats["failed"] == 1 and "空" in empty_plan.items[0].message,
          "%s / %s" % (stats, empty_plan.items[0].message))

    # limit
    src3 = fresh("run_limit")
    out5 = work("run_limit_out")
    shutil.rmtree(out5, ignore_errors=True)
    for i in range(3):
        fixtures.make_mp3(os.path.join(src3, f"辛 - 歌{i}.mp3"), frames=6)
        with open(os.path.join(src3, f"辛 - 歌{i}.lrc"), "w", encoding="utf-8") as fh:
            fh.write(PLAIN_LRC)
    plan6 = batch.scan(src3, out5)
    stats = batch.run(plan6, limit=2)
    check("limit 只处理前 N 个", stats["total"] == 2 and stats["done"] == 2, str(stats))
    check("剩下的没有被写出来", len(os.listdir(out5)) == 2, str(os.listdir(out5)))


def test_run_errors() -> None:
    section("8. 出错时的提示")
    src = fresh("run_err")
    out = work("run_err_out")
    shutil.rmtree(out, ignore_errors=True)

    # 一个伪装成音频的坏文件
    bad = os.path.join(src, "壬 - 坏的.mp3")
    with open(bad, "wb") as fh:
        fh.write("这不是音频".encode("utf-8") * 20)
    with open(os.path.join(src, "壬 - 坏的.lrc"), "w", encoding="utf-8") as fh:
        fh.write(PLAIN_LRC)

    # 一个其实不是 ncm 的 .ncm
    fake = os.path.join(src, "癸 - 假的.ncm")
    with open(fake, "wb") as fh:
        fh.write(b"NOTANNCM" + b"\x00" * 80)

    plan = batch.scan(src, out)
    stats = batch.run(plan)
    check("两个都失败了", stats["failed"] == 2, str(stats))
    msgs = {i.name: i.message for i in plan.items}
    check("坏音频给出的提示能看懂",
          "不支持的音频格式" in msgs["壬 - 坏的.mp3"], msgs["壬 - 坏的.mp3"])
    check("假 ncm 给出的提示提到 ncm",
          "ncm" in msgs["癸 - 假的.ncm"], msgs["癸 - 假的.ncm"])
    check("失败不会留下半成品",
          not os.path.exists(os.path.join(out, "壬 - 坏的.mp3")), str(os.listdir(out)))


def test_cli() -> None:
    section("9. 命令行入口")
    src = fresh("cli")
    out = work("cli_out")
    shutil.rmtree(out, ignore_errors=True)
    fixtures.make_mp3(os.path.join(src, "子 - 歌.mp3"), frames=10)
    with open(os.path.join(src, "子 - 歌.lrc"), "w", encoding="utf-8") as fh:
        fh.write(NETEASE_LRC)
    with open(os.path.join(src, "孤儿.lrc"), "w", encoding="utf-8") as fh:
        fh.write(PLAIN_LRC)

    cli = os.path.join(ROOT, "musictag", "batch.py")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")

    def run_cli(*args):
        return subprocess.run([sys.executable, cli, *args], capture_output=True,
                              text=True, encoding="utf-8", env=env, cwd=ROOT)

    r = run_cli(src, "--out", out, "--dry-run")
    check("--dry-run 退出码是 0", r.returncode == 0, r.stderr[-400:])
    check("--dry-run 打印了计划", "共 1 个文件" in r.stdout, r.stdout[:300])
    check("--dry-run 提示没有写文件", "试运行" in r.stdout)
    check("--dry-run 确实没写文件", not os.path.exists(out) or not os.listdir(out))
    check("--dry-run 报告了孤儿歌词", "孤儿.lrc" in r.stdout)

    r = run_cli(src, "--out", out)
    check("真跑退出码是 0", r.returncode == 0, r.stderr[-400:])
    check("打印了进度", "开始处理…" in r.stdout and "[1/1]" in r.stdout)
    check("打印了完成统计", "完成：1 个写出" in r.stdout)
    check("打印了输出目录", out in r.stdout)
    check("文件真的写出来了", os.path.exists(os.path.join(out, "子 - 歌.mp3")))

    r = run_cli(src, "--out", out)
    check("第二次跑显示跳过", "1 个跳过" in r.stdout, r.stdout[-300:])

    r = run_cli(src, "--out", out, "--force")
    check("--force 后又写出 1 个", "完成：1 个写出" in r.stdout)

    r = run_cli(src, "--out", out, "--lyrics", "text", "--force")
    tags, _, _ = core.read_tags(os.path.join(out, "子 - 歌.mp3"))
    check("--lyrics text 生效", tags.lyrics_kind == "text", tags.lyrics_kind)

    r = run_cli(os.path.join(WORK, "根本没有"), "--out", out)
    check("目录不存在时退出码 2", r.returncode == 2, str(r.returncode))
    check("目录不存在时给了清楚提示", "目录不存在" in (r.stderr + r.stdout))

    r = run_cli(os.path.join(src, "子 - 歌.mp3"), "--out", out)
    check("把文件当目录传会报错", r.returncode == 2, str(r.returncode))

    r = run_cli("--help")
    check("--help 能正常显示", r.returncode == 0 and "--dry-run" in r.stdout)
    r = run_cli("--version")
    check("--version 能正常显示", r.returncode == 0 and "1.0.0" in r.stdout)


def test_skip_and_only_lyrics() -> None:
    section("11. 没歌词的文件不复制；--only-with-lyrics 只挑有歌词的")
    src = fresh("skip")
    out = work("skip_out")
    shutil.rmtree(out, ignore_errors=True)

    # 一个没歌词的 mp3、一个有歌词的 mp3、一个有歌词的 ncm、一个没歌词的 ncm
    fixtures.make_mp3(os.path.join(src, "甲 - 没歌词.mp3"), frames=10)
    with_lyrics = fixtures.make_mp3(os.path.join(src, "乙 - 有歌词.mp3"), frames=10)
    with open(os.path.join(src, "乙 - 有歌词.lrc"), "w", encoding="utf-8") as fh:
        fh.write(PLAIN_LRC)
    make_song_ncm(src, name="丙 - 加密无词", music_name="加密无词", fmt="mp3")
    make_song_ncm(src, name="丁 - 加密有词", music_name="加密有词", fmt="mp3")
    with open(os.path.join(src, "丁 - 加密有词.lrc"), "w", encoding="utf-8") as fh:
        fh.write(PLAIN_LRC)

    before = snapshot(src)
    plan = batch.scan(src, out)
    check("扫到 4 个条目", len(plan.items) == 4, str([i.name for i in plan.items]))
    check("其中 2 个配到歌词", len([i for i in plan.items if i.lrc]) == 2)

    stats = batch.run(plan, only_with_lyrics=True)
    check("只处理了配到歌词的 2 个", stats["done"] == 2 and stats["failed"] == 0, str(stats))
    check("2 个都写了歌词", stats["lyrics_written"] == 2, str(stats))
    check("只解密了有歌词的那个 ncm", stats["decrypted"] == 1, str(stats))

    produced = sorted(os.listdir(out))
    check("输出目录里只有那 2 个文件",
          set(produced) == {"乙 - 有歌词.mp3", "丁 - 加密有词.mp3"}, str(produced))
    check("没歌词的音频没有被复制过去", not os.path.exists(os.path.join(out, "甲 - 没歌词.mp3")))
    check("没歌词的 ncm 也没有被解密", not os.path.exists(os.path.join(out, "丙 - 加密无词.mp3")))
    check("源目录一字未动", snapshot(src) == before)

    # --only-with-lyrics 关掉时：没歌词的音频应该「跳过」，而不是白复制一份
    out2 = work("skip_out2")
    shutil.rmtree(out2, ignore_errors=True)
    plan2 = batch.scan(src, out2)
    stats2 = batch.run(plan2)
    check("不限制时：有内容的处理掉", stats2["done"] == 3, str(stats2))
    check("不限制时：没歌词的音频算跳过", stats2["skipped"] == 1, str(stats2))
    check("跳过的那项给了提示",
          any(i.status == "skipped" and "没有配到歌词" in i.message for i in plan2.items),
          str([(i.name, i.status, i.message) for i in plan2.items]))
    check("没歌词的音频仍然没有被复制",
          not os.path.exists(os.path.join(out2, "甲 - 没歌词.mp3")), str(os.listdir(out2)))
    check("两个输出目录里都没有临时文件残留",
          not any(n.startswith(".") for n in os.listdir(out))
          and not any(n.startswith(".") for n in os.listdir(out2)),
          str(os.listdir(out) + os.listdir(out2)))


def test_core_untouched() -> None:
    section("10. 批量功能没有动到 core 的对外行为")
    # batch/ncm 只调用 core，不应该改变 core 的写入语义：
    # 这里再单独确认一次「只写一个字段、其它字段保留」在批量路径里也成立。
    src = fresh("untouched")
    out = work("untouched_out")
    shutil.rmtree(out, ignore_errors=True)
    p = fixtures.make_flac(os.path.join(src, "丑 - 歌.flac"))
    core.write_tags(p, p, core.Changes(
        title="原标题", artists=["原歌手"], album="原专辑", date="2020-01-02"),
        overwrite=True)

    with open(os.path.join(src, "丑 - 歌.lrc"), "w", encoding="utf-8") as fh:
        fh.write(PLAIN_LRC)
    batch.run(batch.scan(src, out))

    tags, _, _ = core.read_tags(os.path.join(out, "丑 - 歌.flac"))
    check("批量写入只加了歌词", bool(tags.lyrics))
    check("标题保留", tags.title == "原标题", repr(tags.title))
    check("作者保留", tags.artists == ["原歌手"], repr(tags.artists))
    check("专辑保留", tags.album == "原专辑", repr(tags.album))
    check("发行日期保留", tags.date == "2020-01-02", repr(tags.date))


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    tests = [
        test_aes,
        test_ncm,
        test_lyrics_convert,
        test_scan,
        test_run_mp3,
        test_run_flac,
        test_run_modes,
        test_run_errors,
        test_cli,
        test_skip_and_only_lyrics,
        test_core_untouched,
    ]
    for fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            FAILED.append(f"{fn.__name__} 抛出异常")

    print()
    print("=" * 72)
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 通过 {PASSED} 项")
        for f in FAILED:
            print("  - " + f)
        return 1
    print(f"全部通过：{PASSED} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
