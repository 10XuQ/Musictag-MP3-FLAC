"""批量打包：把一个下载目录里的歌词（和加密的 ncm）一次性处理成带标签的音频。

针对网易云音乐的下载习惯：

* 每个 `某某 - 某某.mp3` 旁边会有一个同名的 `.lrc`；
* 会员歌曲下载下来是 `.ncm`（加密），旁边同样有 `.lrc`；
* 那些 `.lrc` **不是标准 LRC** —— 前面若干行是 JSON（演唱/作词/作曲…等制作信息），
  后面才是标准 `[mm:ss.xx]` 歌词行。直接塞进标签会让播放器显示出一堆 JSON。

所以本模块做三件事：

1. 把 `.ncm` 解密回 mp3 / flac（见 `ncm` 模块）；
2. 把网易云那种混合格式的歌词**转成合法 LRC**（或纯文本）；
3. 按文件名把歌词配到音频上，用 `core.write_tags` 写进去，并逐项校验。

**源文件一律不动**：所有产物都写到另一个目录。
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import unicodedata

if __package__ in (None, ""):  # 支持 `python batch.py ...` 直接运行
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from musictag import core
    from musictag import ncm as ncm_mod
else:
    from . import core
    from . import ncm as ncm_mod

AUDIO_EXTS = (".mp3", ".flac")
NCM_EXT = ".ncm"
LRC_EXT = ".lrc"

LYRICS_MODES = ("lrc", "text", "raw")
DEFAULT_OUT_DIRNAME = "_packed"


class BatchError(core.TagError):
    """批量打包过程中的错误。"""


# --------------------------------------------------------------------------
# 歌词转换：网易云的「JSON 行 + LRC 行」混合格式 -> 标准 LRC / 纯文本
# --------------------------------------------------------------------------

_LRC_LINE = re.compile(r"^\s*\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")


def _netease_json_line(line):
    """把 `{"t":661,"c":[{"tx":"作词: "},{"tx":"Ranzer"}]}` 解成 (时间戳, 文本)。"""
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or "c" not in obj:
        return None
    parts = []
    for seg in obj.get("c") or []:
        if isinstance(seg, dict):
            tx = seg.get("tx")
            if tx:
                parts.append(str(tx))
        elif isinstance(seg, str):
            parts.append(seg)
    content = "".join(parts).strip()
    try:
        ms = int(obj.get("t", 0))
    except (TypeError, ValueError):
        ms = 0
    if ms < 0:                      # 见过 t = -1000 的
        ms = 0
    cs = ms // 10
    stamp = "%02d:%02d.%02d" % (cs // 6000, (cs % 6000) // 100, cs % 100)
    return stamp, content


def convert_lyrics(text, mode="lrc"):
    """把网易云的混合歌词转成可以直接放进标签的形式。

    mode: ``lrc`` 转成合法 LRC（默认，播放器能滚动）；``text`` 只要文字；
    ``raw`` 原样不动。
    """
    if mode == "raw":
        return text
    if mode not in LYRICS_MODES:
        raise BatchError("不认识的歌词模式：%s（可选 %s）" % (mode, "/".join(LYRICS_MODES)))
    out = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parsed = _netease_json_line(line)
        if parsed is not None:
            stamp, content = parsed
            if mode == "lrc":
                out.append("[%s]%s" % (stamp, content))
            elif content:
                out.append(content)
            continue
        m = _LRC_LINE.match(line)
        if m:
            out.append(line if mode == "lrc" else line[m.end():].strip())
            continue
        out.append(line)
    return "\n".join(out)


def is_netease_lyrics(text):
    """歌词里有没有网易云那种 JSON 行。"""
    for raw_line in text.splitlines():
        if raw_line.strip().startswith("{"):
            if _netease_json_line(raw_line.strip()) is not None:
                return True
    return False


# --------------------------------------------------------------------------
# 文件名匹配
# --------------------------------------------------------------------------

_DASH_SPLIT = re.compile(r"\s+-\s+")


def _key(name):
    s = unicodedata.normalize("NFC", name).casefold()
    s = re.sub(r"[\s_]+", " ", s)
    return s.strip()


def _token_key(name):
    """把 `A - B` 拆开排序，用来容忍「歌手 - 曲名」和「曲名 - 歌手」两种写法。"""
    parts = [p.strip() for p in _DASH_SPLIT.split(name) if p.strip()]
    if len(parts) < 2:
        return None
    return tuple(sorted(_key(p) for p in parts))


class Item(object):
    """一个待处理的音频。"""

    def __init__(self, src, base, kind, fmt=""):
        self.src = src
        self.base = base
        self.kind = kind          # 'ncm' 或 'audio'
        self.fmt = fmt            # 'mp3' / 'flac'（ncm 的要解密后才知道）
        self.lrc = None
        self.match = ""           # 'exact' / 'order'（曲名歌手顺序不同）
        self.output = None
        self.status = "pending"   # pending / done / skipped / failed
        self.message = ""
        self.report = None
        self.checks = []
        self.filled = []          # 用 ncm 自带信息补齐的字段名

    @property
    def name(self):
        return os.path.basename(self.src)

    def __repr__(self):
        return "<Item %s %s>" % (self.kind, self.name)


class Plan(object):
    def __init__(self, src_dir, out_dir):
        self.src_dir = src_dir
        self.out_dir = out_dir
        self.items = []
        self.orphan_lrc = []      # 配不到音频的歌词
        self.notes = []           # 其它需要告诉用户的发现

    @property
    def ncm_items(self):
        return [i for i in self.items if i.kind == "ncm"]

    @property
    def audio_items(self):
        return [i for i in self.items if i.kind == "audio"]

    @property
    def with_lyrics(self):
        return [i for i in self.items if i.lrc]

    def summary(self):
        return {
            "src_dir": self.src_dir,
            "out_dir": self.out_dir,
            "total": len(self.items),
            "ncm": len(self.ncm_items),
            "audio": len(self.audio_items),
            "with_lyrics": len(self.with_lyrics),
            "orphan_lrc": len(self.orphan_lrc),
            "notes": list(self.notes),
        }


def scan(src_dir, out_dir=None):
    """扫描目录，决定要处理哪些文件、每个文件配哪首歌词。"""
    if not os.path.isdir(src_dir):
        raise core.TagError("目录不存在：%s" % src_dir)
    src_dir = os.path.abspath(src_dir)
    out_dir = os.path.abspath(out_dir or os.path.join(src_dir, DEFAULT_OUT_DIRNAME))
    src_n = os.path.normcase(src_dir)
    out_n = os.path.normcase(out_dir)
    if out_n == src_n:
        raise core.TagError(
            "输出目录不能和源目录相同（源文件会被改写）。请换一个输出目录。"
        )
    # 允许直接在源目录下建子目录（例如默认的 _packed），但不允许更深一层：
    # 那样下次扫描会把上次的产物当成新文件重复处理。
    if out_n.startswith(src_n + os.sep) and os.path.dirname(out_n) != src_n:
        raise core.TagError("输出目录不能放在源目录里面，否则下次会重复处理：%s" % out_dir)

    plan = Plan(src_dir, out_dir)
    names = [n for n in sorted(os.listdir(src_dir))
             if os.path.isfile(os.path.join(src_dir, n))]

    ncms = [n for n in names if n.lower().endswith(NCM_EXT)]
    audios = [n for n in names if n.lower().endswith(AUDIO_EXTS)]
    lrcs = [n for n in names if n.lower().endswith(LRC_EXT)]

    # 同名的 ncm 和音频同时存在时，按音频处理，不重复解密
    audio_bases = set()
    for n in audios:
        audio_bases.add(_key(os.path.splitext(n)[0]))
    items = []
    for n in audios:
        items.append(Item(os.path.join(src_dir, n), os.path.splitext(n)[0], "audio",
                          os.path.splitext(n)[1].lower().lstrip(".")))
    for n in ncms:
        base = os.path.splitext(n)[0]
        if _key(base) in audio_bases:
            plan.notes.append("已跳过 %s：同名音频已存在，不需要再解密。" % n)
            continue
        items.append(Item(os.path.join(src_dir, n), base, "ncm"))

    # 建索引用于配歌词
    by_base = {}
    by_token = {}
    for it in items:
        by_base.setdefault(_key(it.base), []).append(it)
        tk = _token_key(it.base)
        if tk:
            by_token.setdefault(tk, []).append(it)

    for n in lrcs:
        base = os.path.splitext(n)[0]
        path = os.path.join(src_dir, n)
        hit, how = None, ""
        cand = by_base.get(_key(base))
        if cand and len(cand) == 1:
            hit, how = cand[0], "exact"
        else:
            tk = _token_key(base)
            cand = by_token.get(tk) if tk else None
            if cand and len(cand) == 1:
                hit, how = cand[0], "order"
            elif cand and len(cand) > 1:
                plan.notes.append(
                    "歌词 %s 匹配到多个音频（%s），已跳过。"
                    % (n, "、".join(c.name for c in cand))
                )
        if hit is None:
            if hit is None and not cand:
                plan.orphan_lrc.append(n)
            continue
        if hit.lrc:
            plan.notes.append(
                "歌词 %s 和 %s 都想配给 %s，已保留前者。"
                % (os.path.basename(hit.lrc), n, hit.name)
            )
            continue
        hit.lrc = path
        hit.match = how

    plan.items = items
    return plan


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

def _emit(on_event, **kw):
    if on_event:
        on_event(kw)


def run(plan, overwrite=False, lyrics_mode="lrc", on_event=None, limit=None,
        only_with_lyrics=False):
    """按 plan 跑一遍。返回统计字典。

    `only_with_lyrics` 只处理配到歌词的条目（ncm 也一并跳过），
    适合「我只关心有歌词的那几首」的场景。
    """
    if lyrics_mode not in LYRICS_MODES:
        raise BatchError("不认识的歌词模式：%s" % lyrics_mode)
    os.makedirs(plan.out_dir, exist_ok=True)

    todo = [i for i in plan.items if i.lrc] if only_with_lyrics else list(plan.items)
    if limit:
        todo = todo[:limit]
    total = len(todo)
    stats = {"total": total, "done": 0, "failed": 0, "skipped": 0,
             "lyrics_written": 0, "decrypted": 0, "verify_failed": 0}
    _emit(on_event, type="start", total=total, out_dir=plan.out_dir)

    for index, item in enumerate(todo):
        _emit(on_event, type="item", index=index, total=total,
              name=item.name, status="start", text="正在处理 %s" % item.name)
        try:
            _run_one(item, plan, overwrite, lyrics_mode)
            stats[item.status] = stats.get(item.status, 0) + 1
            if item.status == "done":
                if item.kind == "ncm":
                    stats["decrypted"] += 1
                if item.lrc:
                    stats["lyrics_written"] += 1
                if any(not c.ok for c in item.checks):
                    stats["verify_failed"] += 1
        except core.TagError as exc:
            item.status = "failed"
            item.message = str(exc)
            stats["failed"] += 1
        except Exception as exc:                                # noqa: BLE001
            item.status = "failed"
            item.message = "未预期的错误（%s）：%s" % (type(exc).__name__, exc)
            stats["failed"] += 1
        _emit(on_event, type="item", index=index, total=total,
              name=item.name, status=item.status, text=item.message, item=item)

    _emit(on_event, type="done", total=total, stats=stats, plan=plan)
    return stats


def _retry(fn, attempts=8, delay=0.25):
    """Windows 上刚写完的大文件常被杀毒/索引器短暂占用，重试几次就好。"""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except OSError as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(delay)
    raise last


def _robust_unlink(path, attempts=5, delay=0.2):
    """删掉临时文件。删不掉也不能把整个批量任务搞崩，但要如实返回结果。"""
    if not path or not os.path.exists(path):
        return True
    try:
        _retry(lambda: os.unlink(path), attempts=attempts, delay=delay)
        return True
    except OSError:
        return False


def _run_one(item, plan, overwrite, lyrics_mode):
    """跑一个条目。解密出来的临时文件无论成败都不会留在输出目录里。"""
    holder = {"staged": None}
    try:
        _run_one_inner(item, plan, overwrite, lyrics_mode, holder)
    finally:
        staged = holder["staged"]
        if staged and not _robust_unlink(staged):
            note = "临时文件没能删掉，请手动删除：%s" % staged
            item.message = (item.message + "；" + note) if item.message else note


def _run_one_inner(item, plan, overwrite, lyrics_mode, holder):
    out_dir = plan.out_dir
    staged = None          # 解密出来的临时音频
    info = None            # ncm 自带的元信息（只有 ncm 才有）

    if item.kind == "ncm":
        info = ncm_mod.read_info(item.src)
        fmt = info.format
        if not fmt:
            # 元信息里没写格式，就先解一小段看文件头
            probe = b""
            for blk in info.iter_audio(64 * 1024):
                probe = blk
                break
            fmt = "flac" if probe[:4] == b"fLaC" else "mp3"
        item.fmt = fmt
        staged = os.path.join(out_dir, ".%s.decrypting.%s" % (item.base, fmt))
        holder["staged"] = staged
        ncm_mod.decrypt(item.src, staged)
        real = core.detect_format(staged)
        if real != fmt:
            fmt = real
            item.fmt = real
    else:
        item.fmt = core.detect_format(item.src)

    target = os.path.join(out_dir, "%s.%s" % (item.base, item.fmt))
    item.output = target

    if os.path.exists(target) and not overwrite:
        item.status = "skipped"
        item.message = "输出已存在，未覆盖（加 --force 可覆盖）：%s" % target
        return

    changes = core.Changes()
    need_write = False

    if item.lrc:
        text = core.read_lyrics_text(item.lrc)
        converted = convert_lyrics(text, lyrics_mode)
        if not converted.strip():
            raise core.TagError("歌词文件是空的：%s" % item.lrc)
        kind = "lrc" if lyrics_mode == "lrc" else core.detect_lyrics_kind(converted)
        changes.lyrics = core.LyricsChange(action="set", text=converted, kind=kind)
        need_write = True

    if info is not None:
        # 解密出来的音频常常缺封面（标签里没嵌图），而 ncm 自己有图，补上。
        filled = _fill_from_ncm(info, staged, changes)
        if filled:
            item.filled = filled
            need_write = True

    if item.kind == "ncm" and not need_write:
        # 没什么要改的，把解密结果直接搬过去
        _retry(lambda: os.replace(staged, target))
        holder["staged"] = None      # 已经搬走了，不需要再清理
        item.message = "已解密（无歌词可写）"
        item.status = "done"
        item.checks = _verify_output(item, core.Changes())
        return

    if not need_write:
        # 普通音频、又没有歌词可写 —— 复制一份过去没有任何意义，直接跳过。
        item.status = "skipped"
        item.message = "没有配到歌词，跳过（不需要改动）"
        return

    src = staged if staged else item.src
    # 刚解密出来的大文件在 Windows 上可能被杀毒/索引器短暂占用，重试几次。
    report = _retry(lambda: core.write_tags(src, target, changes, overwrite=True))
    item.report = report

    item.checks = core.verify(target, changes)
    failed = [c for c in item.checks if not c.ok]
    if failed:
        item.message = "已写出，但校验没通过：" + "；".join(
            "%s 期望 %s 实际 %s" % (c.label, c.expected, c.actual) for c in failed
        )
    else:
        bits = ["已写出 %s" % os.path.basename(target)]
        if item.lrc:
            bits.append("歌词 %d 字符" % len(changes.lyrics.text))
        if item.filled:
            bits.append("按 ncm 补齐 " + "、".join(item.filled))
        if report and not report.audio_unchanged:
            bits.append("注意：音频指纹变了")
        item.message = "，".join(bits)
    item.status = "done"


def _fill_from_ncm(info, staged, changes):
    """解密结果里缺什么，就用 ncm 自带的信息补什么。已有的标签一律不动。

    返回补齐的字段名列表（中文，用于结果文案）。
    """
    try:
        tags, _fmt, _audio = core.read_tags(staged)
    except core.TagError:
        return []

    filled = []

    if not (tags.title or "").strip() and (info.music_name or "").strip():
        changes.title = info.music_name.strip()
        filled.append("歌曲名")
    if not tags.artists and info.artists:
        changes.artists = list(info.artists)
        filled.append("作者")
    if not (tags.album or "").strip() and (info.album or "").strip():
        changes.album = info.album.strip()
        filled.append("专辑")
    if tags.cover is None and info.cover:
        try:
            mime, width, height, depth = core.sniff_image(info.cover, "ncm 内嵌封面")
        except core.ImageError:
            pass
        else:
            changes.cover = core.CoverChange(
                action="set",
                image=core.Image(data=info.cover, mime=mime, width=width,
                                 height=height, depth=depth, source="ncm 内嵌封面"),
            )
            filled.append("封面")

    return filled


def _verify_output(item, changes):
    try:
        return core.verify(item.output, changes)
    except core.TagError:
        return []


def describe_plan(plan, limit=None):
    """把计划变成适合打印的行。"""
    rows = []
    for item in plan.items[:limit] if limit else plan.items:
        if item.lrc:
            how = "文件名相同" if item.match == "exact" else "歌手/曲名顺序不同"
            rows.append("%-9s %s\n            + 歌词 %s（%s）"
                        % ("[解密]" if item.kind == "ncm" else "[音频]",
                           item.name, os.path.basename(item.lrc), how))
        else:
            note = "（没有配到歌词）" if item.kind == "audio" else "（没有配到歌词）"
            rows.append("%-9s %s  %s"
                        % ("[解密]" if item.kind == "ncm" else "[音频]", item.name, note))
    return rows


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text, code):
    return "\033[%sm%s\033[0m" % (code, text) if _COLOR else text


def ok(text):
    return _c(text, "32")


def bad(text):
    return _c(text, "31")


def warn(text):
    return _c(text, "33")


def dim(text):
    return _c(text, "2")


def build_parser():
    p = argparse.ArgumentParser(
        prog="batch.py",
        description="一键打包：把下载目录里的歌词（含加密的 .ncm）批量写进音频标签。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例\n"
            "  先看看会处理哪些文件（不写任何东西）：\n"
            "    python batch.py \"D:\\CloudMusic\\VipSongsDownload\" --dry-run\n\n"
            "  直接跑（输出到 源目录\\_packed）：\n"
            "    python batch.py \"D:\\CloudMusic\\VipSongsDownload\"\n\n"
            "  输出到别处，并覆盖上次的结果：\n"
            "    python batch.py \"D:\\CloudMusic\\VipSongsDownload\" --out \"D:\\打包结果\" --force\n\n"
            "  只处理前 3 个（先试试水）：\n"
            "    python batch.py \"D:\\CloudMusic\\VipSongsDownload\" --limit 3\n\n"
            "  歌词写成纯文本（去掉时间轴）：\n"
            "    python batch.py \"D:\\CloudMusic\\VipSongsDownload\" --lyrics text\n"
        ),
    )
    p.add_argument("src_dir", help="下载目录（里面有音频/.ncm 和同名 .lrc）")
    p.add_argument("--out", "-o", default=None,
                   help="输出目录；默认在源目录下建 %s" % DEFAULT_OUT_DIRNAME)
    p.add_argument("--lyrics", "-l", default="lrc", choices=LYRICS_MODES,
                   help="歌词写法：lrc=保留时间轴（默认）、text=纯文本、raw=原样不改")
    p.add_argument("--force", "-f", action="store_true",
                   help="输出已存在时覆盖（默认跳过）")
    p.add_argument("--dry-run", "-n", action="store_true",
                   help="只列出计划，不写任何文件")
    p.add_argument("--limit", type=int, default=None,
                   help="最多处理多少个文件（先小范围试跑用）")
    p.add_argument("--only-with-lyrics", "--lyrics-only", dest="only_with_lyrics",
                   action="store_true",
                   help="只处理配到歌词的文件（其余原样留在源目录，不复制）")
    p.add_argument("--version", action="version", version="musictag batch 1.0.0")
    return p


def _print_plan(plan, limit=None):
    s = plan.summary()
    print("源目录 : %s" % s["src_dir"])
    print("输出到 : %s" % s["out_dir"])
    print("共 %d 个文件：%d 个音频 + %d 个要解密的 ncm，其中 %d 个配到了歌词"
          % (s["total"], s["audio"], s["ncm"], s["with_lyrics"]))
    if plan.notes:
        print()
        for n in plan.notes:
            print("  " + warn("注意 ") + n)
    print()
    for row in describe_plan(plan, limit):
        print("  " + row)
    if plan.orphan_lrc:
        print()
        print("  " + warn("有 %d 个歌词找不到对应的音频（这些歌词不会被写入）："
                          % len(plan.orphan_lrc)))
        for n in plan.orphan_lrc:
            print("     " + n)


def main(argv=None):
    args = build_parser().parse_args(argv)
    limit = args.limit if (args.limit and args.limit > 0) else None
    try:
        plan = scan(args.src_dir, args.out)
    except core.TagError as exc:
        print(bad("✘ 出错：") + str(exc), file=sys.stderr)
        return 2

    if not plan.items:
        print(warn("这个目录里没有找到 mp3 / flac / ncm 文件。"))
        return 0

    _print_plan(plan, limit)

    if args.dry_run:
        print()
        print(dim("（这是试运行，没有写任何文件。去掉 --dry-run 就会真的开始处理。）"))
        return 0

    print()
    print("开始处理…")
    try:
        def on_event(ev):
            if ev.get("type") == "item" and ev.get("status") != "start":
                mark = {"done": ok("✔"), "skipped": warn("•"), "failed": bad("✘")}
                print("  [%d/%d] %s %s" % (ev["index"] + 1, ev["total"],
                                           mark.get(ev["status"], "?"), ev["name"]))
                if ev.get("text"):
                    print("        " + dim(ev["text"]))

        stats = run(plan, overwrite=args.force, lyrics_mode=args.lyrics,
                    on_event=on_event, limit=limit,
                    only_with_lyrics=args.only_with_lyrics)
    except KeyboardInterrupt:
        print()
        print(warn("已中断。已经写好的文件都留在输出目录里，源文件没有被改动。"))
        return 130

    print()
    print("完成：%d 个写出，%d 个跳过，%d 个失败"
          % (stats["done"], stats["skipped"], stats["failed"]))
    print("     其中解密 ncm %d 个，写入歌词 %d 个" % (stats["decrypted"], stats["lyrics_written"]))
    if stats["verify_failed"]:
        print(bad("     有 %d 个文件的写回校验没通过，请检查上面的明细。" % stats["verify_failed"]))
    if plan.orphan_lrc:
        print(warn("     有 %d 个歌词没配到音频，没有被写入。" % len(plan.orphan_lrc)))
    print()
    print("输出目录：%s" % plan.out_dir)
    print(dim("源目录里的文件一个都没动。"))

    return 2 if (stats["failed"] or stats["verify_failed"]) else 0


if __name__ == "__main__":
    sys.exit(main())
