"""CLI 端到端 + 字节级交付验收。

自包含：自己生成测试素材、自己调用命令行入口、自己检查结果。
覆盖：完整写入、默认输出路径、覆盖保护、各类错误提示、试运行、查看、
只校验、局部更新、清除字段；以及「音频逐字节不变」「原文件未改动」
「ID3 / FLAC 结构符合播放器预期」。

运行： python tests/test_delivery.py
"""
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from mutagen.flac import FLAC  # noqa: E402
from mutagen.id3 import ID3  # noqa: E402

import fixtures  # noqa: E402
from musictag import core  # noqa: E402

PY = sys.executable
CLI = os.path.join(ROOT, "musictag", "musictag.py")
WORK = os.path.join(HERE, "_delivery")

oks = 0
fails = []


def check(label, cond, extra=""):
    global oks
    if cond:
        oks += 1
        print(f"  ok   {label}" + (f" — {extra}" if extra else ""))
    else:
        fails.append(label)
        print(f"  FAIL {label}" + (f" — {extra}" if extra else ""))


def run(args):
    """跑一次命令行工具，返回 (退出码, 输出文本)。"""
    proc = subprocess.run(
        [PY, CLI] + args, capture_output=True, cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"}
    )
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    return proc.returncode, out


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def phase(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# --------------------------------------------------------------------------- #
phase("0. 准备测试素材")
if os.path.isdir(WORK):
    shutil.rmtree(WORK)
os.makedirs(WORK, exist_ok=True)
D = WORK
src_mp3 = fixtures.make_mp3(os.path.join(D, "song.mp3"))
src_flac = fixtures.make_flac(os.path.join(D, "song.flac"))
png = fixtures.make_png(os.path.join(D, "cover.png"), 300, 200)
jpg = fixtures.make_jpeg(os.path.join(D, "cover.jpg"), 240, 160)
broken = fixtures.make_broken_png(os.path.join(D, "cover_broken.png"))
lrc = os.path.join(D, "lyrics.lrc")
plain = os.path.join(D, "lyrics.txt")
with open(lrc, "w", encoding="utf-8") as fh:
    fh.write("[00:01.00]第一行歌词\n[00:05.50]第二行歌词\n")
with open(plain, "w", encoding="utf-8") as fh:
    fh.write("普通歌词第一行\n普通歌词第二行\n")
before = {n: sha(os.path.join(D, n)) for n in
          ("song.mp3", "song.flac", "cover.png", "cover.jpg", "lyrics.lrc")}
check("素材生成完毕", len(before) == 5, f"{len(before)} 个输入文件")

# --------------------------------------------------------------------------- #
phase("1. 完整写入 MP3（JPG 封面 + 纯文本歌词 + 逗号分隔作者）")
out_mp3 = os.path.join(D, "out.mp3")
code, text = run([src_mp3, "--title", "夜航", "--artist", "甲,乙", "--album", "合集",
                  "--date", "2024年5月1日", "--cover", jpg, "--lyrics", plain, "--out", out_mp3])
check("退出码为 0", code == 0, f"exit={code}")
check("提示音频逐字节一致", "逐字节一致" in text)
check("逐项校验全部通过", "全部校验通过" in text and "✘" not in text)

phase("2. 完整写入 FLAC（PNG 封面 + LRC 歌词 + 年月日期）")
out_flac = os.path.join(D, "full.flac")
code, text = run([src_flac, "--title", "夜航", "--artist", "甲", "--artist", "乙",
                  "--album", "合集", "--date", "2024-05", "--cover", png,
                  "--lyrics", lrc, "--out", out_flac])
check("退出码为 0", code == 0, f"exit={code}")
check("逐项校验全部通过", "全部校验通过" in text and "✘" not in text)

phase("3. 默认输出路径（不加 --out）")
code, text = run([src_flac, "--title", "默认输出"])
default_out = os.path.join(D, "song_tagged.flac")
check("退出码为 0", code == 0, f"exit={code}")
check("生成 song_tagged.flac", os.path.exists(default_out))
check("标题已写入", core.read_tags(default_out)[0].title == "默认输出")

phase("4. 覆盖保护")
code, text = run([src_flac, "--title", "重复", "--out", default_out])
check("已存在时报错，退出码 2", code == 2, f"exit={code}")
check("提示里说明用 --force", "输出文件已存在" in text and "--force" in text)
check("未改动原文件", sha(src_flac) == before["song.flac"])
code, text = run([src_flac, "--title", "用 force 覆盖", "--out", default_out, "--force"])
check("加 --force 后成功", code == 0, f"exit={code}")

phase("5. 各类错误提示（都应退出码 2，且原文件不变）")
cases = [
    ("文件不存在", [os.path.join(D, "不存在.mp3"), "--title", "x"], "文件不存在"),
    ("格式不支持", [png, "--title", "x"], "只处理 MP3 与 FLAC"),
    ("图片损坏", [src_mp3, "--cover", broken, "--out", os.path.join(D, "bad.mp3")], "损坏"),
    ("日期无法识别", [src_mp3, "--date", "去年秋天", "--out", os.path.join(D, "bad2.mp3")], "无法识别"),
    ("没有给任何参数", [src_flac], "没有指定任何要写入的内容"),
]
for label, args, expect in cases:
    code, text = run(args)
    check(f"{label}：退出码 2", code == 2, f"exit={code}")
    check(f"{label}：提示含「{expect}」", expect in text)
check("错误场景未留下半成品",
      not os.path.exists(os.path.join(D, "bad.mp3")) and not os.path.exists(os.path.join(D, "bad2.mp3")))

phase("6. 试运行 / 查看 / 只校验")
code, text = run([out_mp3, "--title", "改标题", "--dry-run"])
check("--dry-run 退出码 0", code == 0, f"exit={code}")
check("--dry-run 不产生文件", "[试运行]" in text and not os.path.exists(os.path.join(D, "out_tagged.mp3")))
code, text = run([out_mp3, "--show"])
check("--show 退出码 0 并列出标签", code == 0 and "标题" in text and "夜航" in text)
code, text = run([out_mp3, "--verify-only", "--title", "夜航", "--artist", "甲",
                  "--artist", "乙", "--cover", jpg, "--lyrics", plain])
check("--verify-only 一致时退出码 0", code == 0, f"exit={code}")
code, text = run([out_mp3, "--verify-only", "--title", "错的标题"])
check("--verify-only 不一致时退出码 1", code == 1, f"exit={code}")

phase("7. 局部更新与清除（保留未指定的字段）")
out2 = os.path.join(D, "out2.mp3")
code, text = run([out_mp3, "--album", "新专辑", "--out", out2])
t2 = core.read_tags(out2)[0]
check("只改专辑，其余保留", code == 0 and t2.album == "新专辑" and t2.title == "夜航"
      and t2.artists == ["甲", "乙"] and t2.date == "2024-05-01" and t2.cover is not None
      and t2.lyrics is not None)
out3 = os.path.join(D, "out3.mp3")
code, text = run([out2, "--clear-cover", "--clear-lyrics", "--out", out3])
t3 = core.read_tags(out3)[0]
check("清除封面与歌词成功", code == 0 and t3.cover is None and t3.lyrics is None)
check("清除后其它字段仍在", t3.title == "夜航" and t3.album == "新专辑" and t3.artists == ["甲", "乙"])

# --------------------------------------------------------------------------- #
phase("8. 不重新编码：音频数据逐字节一致")
check("MP3 音频段 SHA256 写入前后一致",
      core._audio_bytes_fingerprint(src_mp3, "mp3") == core._audio_bytes_fingerprint(out_mp3, "mp3"))
check("FLAC 音频段 SHA256 写入前后一致",
      core._audio_bytes_fingerprint(src_flac, "flac") == core._audio_bytes_fingerprint(out_flac, "flac"))
a_b, a_a = core._audio_signature(src_mp3, "mp3"), core._audio_signature(out_mp3, "mp3")
check("MP3 音频参数不变", a_b == a_a and a_b, f"{a_a}")
f_b, f_a = core._audio_signature(src_flac, "flac"), core._audio_signature(out_flac, "flac")
check("FLAC 音频参数不变（含 STREAMINFO MD5）", f_b == f_a and f_b,
      f"MD5={f_a.get('STREAMINFO MD5')}")
check("MP3 只多了标签字节", os.path.getsize(out_mp3) > os.path.getsize(src_mp3),
      f"{os.path.getsize(src_mp3)} -> {os.path.getsize(out_mp3)}")

phase("9. 原文件未被改动")
for name in ("song.mp3", "song.flac", "cover.png", "cover.jpg", "lyrics.lrc"):
    check(f"{name} SHA256 不变", sha(os.path.join(D, name)) == before[name])

phase("10. MP3 / ID3v2 结构（播放器兼容性）")
head = open(out_mp3, "rb").read(10)
check("以 'ID3' 开头", head[:3] == b"ID3", head[:3].decode("latin-1"))
check("ID3v2 版本为 2.3 或 2.4", head[3] in (3, 4), f"v2.{head[3]}")
check("标签头 flags 无实验位", not (head[5] & 0x1F), f"flags=0x{head[5]:02x}")
tags = ID3(out_mp3)
fids = {f.FrameID for f in tags.values()}
check("含 TIT2 / TPE1 / TALB", {"TIT2", "TPE1", "TALB"} <= fids, str(sorted(fids)))
check("含时间帧 TDRC 或 TYER", bool({"TDRC", "TYER"} & fids))
check("含 APIC 与 USLT", {"APIC", "USLT"} <= fids)
apic = tags.getall("APIC")[0]
check("APIC 类型为 3（封面）", int(apic.type) == 3, f"type={int(apic.type)}")
check("APIC 图片字节与源 JPG 一致", hashlib.sha256(apic.data).hexdigest() == before["cover.jpg"],
      f"{len(apic.data)}B")
check("APIC mime 为 image/jpeg", apic.mime == "image/jpeg", apic.mime)
uslt = tags.getall("USLT")[0]
check("USLT 语言码为 3 个字符", len(uslt.lang) == 3, uslt.lang)
check("USLT 歌词两行齐全", len(uslt.text.strip().splitlines()) == 2)
tpe1 = tags.getall("TPE1")[0]
check("TPE1 使用 UTF 编码", int(tpe1.encoding) in (1, 3), f"encoding={int(tpe1.encoding)}")
check("TPE1 两位作者", len(tpe1.text) == 2, str(tpe1.text))
check("读回的日期为统一格式", core.read_tags(out_mp3)[0].date == "2024-05-01",
      str(core.read_tags(out_mp3)[0].date))

phase("11. FLAC / Vorbis Comment 结构（播放器兼容性）")
check("以 'fLaC' 开头", open(out_flac, "rb").read(4) == b"fLaC")
fa = FLAC(out_flac)
check("采样率保持 44100", fa.info.sample_rate == 44100, str(fa.info.sample_rate))
check("总采样数保持 44100", fa.info.total_samples == 44100, str(fa.info.total_samples))
check("位深保持 16", fa.info.bits_per_sample == 16, str(fa.info.bits_per_sample))
raw = open(out_flac, "rb").read()
check("注释键按大写写出", all(k in raw for k in (b"TITLE=", b"ARTIST=", b"ALBUM=", b"DATE=", b"LYRICS=")))
check("ARTIST 两位作者", fa.tags.get("artist") == ["甲", "乙"], str(fa.tags.get("artist")))
check("恰好一个 type=3 图片块", len([p for p in fa.pictures if p.type == 3]) == 1,
      str([(p.type, p.mime) for p in fa.pictures]))
check("图片字节与源 PNG 一致",
      hashlib.sha256(fa.pictures[0].data).hexdigest() == before["cover.png"])
check("图片块声明宽高 300x200", (fa.pictures[0].width, fa.pictures[0].height) == (300, 200))
check("LRC 歌词原样写入", fa.tags.get("lyrics", [""])[0].startswith("[00:01.00]"),
      str(fa.tags.get("lyrics"))[:28])
check("读回日期为 YYYY-MM", core.read_tags(out_flac)[0].date == "2024-05",
      str(core.read_tags(out_flac)[0].date))

# --------------------------------------------------------------------------- #
print(f"\n{'=' * 70}")
print(f"结果：{oks} 项通过 / {len(fails)} 项失败")
for f in fails:
    print("  - " + f)
print(f"（测试产物目录：{WORK}）")
sys.exit(1 if fails else 0)
