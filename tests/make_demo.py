"""生成一组可以直接拿来试手的演示文件（放在 preview\\ 目录）。"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import fixtures  # noqa: E402

D = os.path.join(ROOT, "preview")
if os.path.isdir(D):
    shutil.rmtree(D)
os.makedirs(D, exist_ok=True)

fixtures.make_mp3(os.path.join(D, "示例歌曲.mp3"))
fixtures.make_flac(os.path.join(D, "示例歌曲.flac"))
fixtures.make_png(os.path.join(D, "封面.png"), 600, 600)
fixtures.make_jpeg(os.path.join(D, "封面.jpg"), 500, 500)
with open(os.path.join(D, "歌词.lrc"), "w", encoding="utf-8") as fh:
    fh.write(
        "[00:00.50]夜航\n"
        "[00:04.20]作词：甲\n"
        "[00:07.80]作曲：乙\n"
        "[00:12.00]第一句歌词\n"
        "[00:18.50]第二句歌词\n"
    )
print("演示文件已生成：", D)
for name in sorted(os.listdir(D)):
    print("   ", name, os.path.getsize(os.path.join(D, name)), "B")
