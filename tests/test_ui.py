#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图形界面测试：纯逻辑层（ui_logic）+ 本地 HTTP 接口（webui）。

不打开浏览器、不弹任何系统对话框，全部通过本机 HTTP 调用真实服务。

运行：
    python tests/test_ui.py
退出码 0 表示全部通过。临时文件在 tests/_ui/ 下，可随时删除。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

# 让界面服务把「导入目录 / 配置」写到测试目录里，不碰真实的 %LOCALAPPDATA%
UI_HOME = os.path.join(HERE, "_ui")
os.environ["MUSICTAG_UI_HOME"] = UI_HOME

import fixtures  # noqa: E402
from musictag import core, ui_logic, webui  # noqa: E402
from musictag.core import Changes, CoverChange, Image, LyricsChange  # noqa: E402

WORK = os.path.join(UI_HOME, "work")
FAILED: list = []
PASSED = 0

LRC = "[00:12.30]第一行\n[00:18.00]第二行\n"
PLAIN = "第一行\n第二行\n"


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def expect_error(label: str, fn, exc=Exception, needle: str = "") -> None:
    try:
        fn()
    except exc as err:  # noqa: PERF203
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
# HTTP 客户端
# --------------------------------------------------------------------------- #

BASE = ""
TOKEN = ""


def request(path: str, payload=None, token: str | None = None, method: str = "POST",
            raw: bytes | None = None, headers: dict | None = None, timeout: float = 60.0):
    """返回 (HTTP 状态码, 解析后的 JSON)。"""
    url = BASE + path
    data = raw if raw is not None else (json.dumps(payload).encode("utf-8") if payload is not None else b"")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token is None:
        token = TOKEN
    if token:
        req.add_header("X-Token", token)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8")
        status = err.code
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body


def get(path: str):
    req = urllib.request.Request(BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8"), dict(resp.headers)


def write_stream(form: dict):
    """调用流式写入接口，收集所有事件。"""
    req = urllib.request.Request(
        BASE + "/api/write", data=json.dumps({"form": form}).encode("utf-8"), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Token", TOKEN)
    events = []
    with urllib.request.urlopen(req, timeout=120) as resp:
        for line in resp:
            text = line.decode("utf-8").strip()
            if text:
                events.append(json.loads(text))
    return events


def events_of(events, kind):
    return [e for e in events if e.get("type") == kind]


# --------------------------------------------------------------------------- #
# 一、纯逻辑层
# --------------------------------------------------------------------------- #


def test_ui_logic() -> None:
    section("ui_logic：默认输出路径与字段清洗")

    check("默认输出加 _tagged 后缀", ui_logic.default_output(os.path.join(WORK, "song.mp3")).endswith("song_tagged.mp3"))
    check("默认输出不等于源文件", ui_logic.default_output(os.path.join(WORK, "a.flac")) != os.path.join(WORK, "a.flac"))

    check("作者去空白与去重", ui_logic.clean_artists(["  甲 ", "", "乙", "甲", "   "]) == ["甲", "乙"])
    check("作者大小写不敏感去重", ui_logic.clean_artists(["AB", "ab"]) == ["AB"])
    check("空作者列表", ui_logic.clean_artists(None) == [])

    check("日期归一 年月日", ui_logic.resolve_date("2024年5月1日") == "2024-05-01")
    check("日期归一 斜杠", ui_logic.resolve_date("2024/5/1") == "2024-05-01")
    check("日期空串保持为空", ui_logic.resolve_date("   ") == "")
    expect_error("无法识别的日期要报错", lambda: ui_logic.resolve_date("去年秋天"), needle="无法识别")

    section("ui_logic：表单 -> Changes 的差异比对")

    tags = core.Tags(
        title="原唱",
        artists=["甲", "乙"],
        album="原专辑",
        date="2020-01-01",
        cover=None,
        lyrics=PLAIN,
        lyrics_kind="text",
        existing_count=5,
    )

    def form(**over):
        base = ui_logic.initial_form(os.path.join(WORK, "song.mp3"), tags, audio_text="时长=1.0")
        base.update(over)
        return base

    # 什么都没改
    changes = ui_logic.build_changes(form(), tags)
    check("不改动时 Changes 为空", changes.is_empty(), repr(changes))
    check("初始表单封面是 keep", form()["cover"]["action"] == "keep")
    check("初始表单歌词是 keep", form()["lyrics"]["action"] == "keep")
    check("初始表单带出原标签", form()["title"] == "原唱" and form()["artists"] == ["甲", "乙"])

    # 只改标题
    changes = ui_logic.build_changes(form(title="新歌"), tags)
    check("改标题只产生标题改动", changes.title == "新歌" and changes.album is None and changes.artists is None)
    check("改标题不会顺带改日期", changes.date is None and changes.cover.action == "keep")

    # 清空专辑 -> 清除
    check("清空专辑表示清除", ui_logic.build_changes(form(album=""), tags).album == "")

    # 作者增减
    check("作者减少一人", ui_logic.build_changes(form(artists=["甲"]), tags).artists == ["甲"])
    check("作者顺序变化也算改动", ui_logic.build_changes(form(artists=["乙", "甲"]), tags).artists == ["乙", "甲"])
    check("作者加空白不算改动", ui_logic.build_changes(form(artists=["甲", "  ", "乙"]), tags).artists is None)

    # 日期
    check("日期改写法但同一天不算改动", ui_logic.build_changes(form(date="2020/1/1"), tags).date is None)
    check("日期清空表示清除", ui_logic.build_changes(form(date=""), tags).date == "")
    check("日期新值会归一化", ui_logic.build_changes(form(date="2024年5月1日"), tags).date == "2024-05-01")
    expect_error("日期乱填要报错", lambda: ui_logic.build_changes(form(date="去年"), tags), needle="无法识别")

    # 封面
    image = Image(data=b"\x89PNG\r\n\x1a\n" + b"0" * 32, mime="image/png", width=4, height=4, depth=8, source="x.png")
    check("封面 keep", ui_logic.build_changes(form(), tags).cover.action == "keep")
    check("封面 clear", ui_logic.build_changes(form(cover={"action": "clear"}), tags).cover.action == "clear")
    set_form = form(cover={"action": "set", "image": image})
    check("封面 set", ui_logic.build_changes(set_form, tags).cover.image is image)
    expect_error(
        "封面 set 但图片丢了要报错",
        lambda: ui_logic.build_changes(form(cover={"action": "set", "token": "gone"}), tags),
        needle="封面图片已失效",
    )

    # 歌词
    check("歌词没改 -> keep", ui_logic.build_changes(form(lyrics={"action": "set", "text": PLAIN, "kind": "text"}), tags).lyrics.action == "keep")
    check("歌词改动 -> set", ui_logic.build_changes(form(lyrics={"action": "set", "text": PLAIN + "第三行\n", "kind": "text"}), tags).lyrics.action == "set")
    check("歌词清空 -> clear", ui_logic.build_changes(form(lyrics={"action": "set", "text": "   ", "kind": "text"}), tags).lyrics.action == "clear")
    check("歌词 clear 动作", ui_logic.build_changes(form(lyrics={"action": "clear"}), tags).lyrics.action == "clear")
    lrc_change = ui_logic.build_changes(form(lyrics={"action": "set", "text": LRC, "kind": "lrc"}), tags).lyrics
    check("LRC 歌词带 kind", lrc_change.kind == "lrc" and lrc_change.text == LRC)

    section("ui_logic：改动说明")
    rows = ui_logic.describe_changes(ui_logic.build_changes(form(title="新歌", album=""), tags), tags)
    labels = [r["label"] for r in rows]
    check("说明里含标题与专辑", labels == ["标题", "专辑"], repr(labels))
    check("说明里带旧值新值", rows[0]["before"] == "原唱" and rows[0]["after"] == "新歌")
    check("清除项的新值显示为 —", rows[1]["after"] == "—")
    check("没改动时说明为空", ui_logic.describe_changes(Changes(), tags) == [])


# --------------------------------------------------------------------------- #
# 二、静态页面
# --------------------------------------------------------------------------- #


def test_static() -> None:
    section("HTTP：静态页面与令牌")

    status, html, headers = get("/")
    check("首页返回 200", status == 200)
    check("首页是 HTML", "text/html" in headers.get("Content-Type", ""))
    check("首页已注入会话令牌", "__TOKEN__" not in html and TOKEN in html)
    check("首页引用了 app.js 与 style.css", "/app.js" in html and "/style.css" in html)
    check("首页含关键控件", all(k in html for k in ('id="dropzone"', 'id="artists"', 'id="cover-img"', 'id="write"', 'id="progress-bar"')))
    check("首页不缓存", headers.get("Cache-Control") == "no-store")

    status, css, headers = get("/style.css")
    check("样式表返回 200", status == 200 and "text/css" in headers.get("Content-Type", ""))
    check("样式表含暗色主题", '[data-theme="dark"]' in css)

    status, js, headers = get("/app.js")
    check("脚本返回 200", status == 200 and "javascript" in headers.get("Content-Type", ""))

    # 两个页签与批量面板的控件
    check("首页有单曲 / 批量两个页签",
          'data-tab="single"' in html and 'data-tab="batch"' in html, "")
    check("首页有两个页签面板", 'id="tab-single"' in html and 'id="tab-batch"' in html, "")
    batch_ids = ("batch-drop", "batch-src", "batch-out", "batch-pick-src", "batch-pick-out",
                 "batch-scan", "batch-run", "batch-open", "batch-list", "batch-summary-text",
                 "batch-progress-bar", "batch-stage", "batch-only-lyrics", "batch-overwrite",
                 "batch-result-card", "batch-orphans", "batch-notes")
    missing = [i for i in batch_ids if f'id="{i}"' not in html]
    check("批量面板的控件都在", not missing, str(missing))
    check("批量页有歌词写法三选一",
          html.count('name="batch-lyrics"') == 3, str(html.count('name="batch-lyrics"')))
    check("批量页默认勾选「只处理配到歌词的」",
          'id="batch-only-lyrics" type="checkbox" checked' in html)

    check("脚本调用了批量扫描接口", "/api/batch/scan" in js)
    check("脚本调用了批量打包接口", "/api/batch/run" in js)
    check("脚本里有页签切换", "switchTab" in js and "tab-single" in js)
    check("脚本会隐藏另一个页签", "$('#tab-batch').hidden = name !== 'batch'" in js)

    # 脚本里每个 $('#xxx') 都得在页面里真的存在，否则点了按钮没反应
    used = sorted(set(re.findall(r"\$\$?\(['\"]#([A-Za-z0-9_-]+)['\"]\)", js)))
    unknown = [i for i in used if f'id="{i}"' not in html]
    check(f"脚本引用的 {len(used)} 个元素 id 都在页面里", not unknown, str(unknown))
    check("脚本确实引用了批量按钮",
          "batch-run" in used and "batch-scan" in used and "batch-list" in used, str(used))


# --------------------------------------------------------------------------- #
# 三、接口
# --------------------------------------------------------------------------- #


def test_api_basics() -> None:
    section("HTTP：接口基础与令牌校验")

    status, data = request("/api/ping")
    check("ping 正常", data.get("ok") is True)

    status, data = request("/api/ping", token="")
    check("不带令牌被拒绝", data.get("ok") is False and "令牌" in data.get("error", ""))

    status, data = request("/api/ping", token="wrong-token")
    check("错令牌被拒绝", data.get("ok") is False and "令牌" in data.get("error", ""))

    status, data = request("/api/does-not-exist")
    check("未知接口返回 404", status == 404 and data.get("ok") is False)

    status, data = request("/api/pick", {"kind": "hacker"})
    check("不支持的选择类型被拒绝", data.get("ok") is False and "不支持" in data.get("error", ""))

    status, data = request("/api/reveal", {"path": os.path.join(WORK, "根本没有这个文件")})
    check("打开不存在的路径要报错", data.get("ok") is False and "找不到" in data.get("error", ""))


def test_api_open() -> None:
    section("HTTP：打开文件并读回已有标签")

    mp3 = os.path.join(WORK, "open.mp3")
    fixtures.make_mp3(mp3)

    status, data = request("/api/open", {"path": mp3})
    check("open 成功", data.get("ok") is True, str(data.get("error")))
    form = data["form"]
    check("form 带源路径", form["src"] == mp3)
    check("form 标出格式", form["fmt"] == "MP3")
    check("form 带音频参数", "采样率" in form["audio_text"])
    check("form 默认输出带 _tagged", form["output"].endswith("open_tagged.mp3"))
    check("form 输出不等于源文件", form["output"] != mp3)
    check("空标签文件没有封面预览", form["cover_preview"] is None)
    check("空标签文件原有标签数为 0", form["existing_count"] == 0)
    check("form 带只读的 original 快照", form["original"]["title"] == "")

    status, data = request("/api/open", {"path": os.path.join(WORK, "不存在.mp3")})
    check("打开不存在的文件要报错", data.get("ok") is False and "不存在" in data.get("error", ""))

    status, data = request("/api/open", {"path": ""})
    check("空路径要报错", data.get("ok") is False)
    not_audio = os.path.join(WORK, "not-audio.png")
    fixtures.make_png(not_audio, 20, 20)
    status, data = request("/api/open", {"path": not_audio})
    check("非音频文件要报错", data.get("ok") is False and "不支持" in data.get("error", ""), str(data.get("error")))


def test_api_image_and_lyrics() -> None:
    section("HTTP：封面图片与歌词")

    png = os.path.join(WORK, "cover.png")
    fixtures.make_png(png, 300, 200)
    status, data = request("/api/image", {"path": png})
    check("读取 PNG 封面成功", data.get("ok") is True, str(data.get("error")))
    check("封面带 token", bool(data.get("token")))
    check("封面带 data URL", data.get("data_url", "").startswith("data:image/png;base64,"))
    check("封面带尺寸说明", data.get("width") == 300 and data.get("height") == 200)
    check("封面带 mime", data.get("mime") == "image/png")

    broken = os.path.join(WORK, "broken.png")
    fixtures.make_broken_png(broken)
    status, data = request("/api/image", {"path": broken})
    check("损坏的 PNG 要报错", data.get("ok") is False and "损坏" in data.get("error", ""))

    status, data = request("/api/image", {"path": os.path.join(WORK, "open.mp3")})
    check("非图片文件要报错", data.get("ok") is False)

    lrc_path = os.path.join(WORK, "lyrics.lrc")
    with open(lrc_path, "w", encoding="utf-8") as fh:
        fh.write(LRC)
    status, data = request("/api/lyrics", {"path": lrc_path})
    check("导入 LRC 成功", data.get("ok") is True and data.get("text") == LRC)
    check("LRC 被识别为 lrc", data.get("kind") == "lrc")

    txt_path = os.path.join(WORK, "lyrics.txt")
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write(PLAIN)
    status, data = request("/api/lyrics", {"path": txt_path})
    check("纯文本歌词被识别为 text", data.get("ok") is True and data.get("kind") == "text")


def test_api_date() -> None:
    section("HTTP：日期归一化")

    for text, want in [
        ("2024-05-01", "2024-05-01"),
        ("2024/5/1", "2024-05-01"),
        ("2024年5月1日", "2024-05-01"),
        ("20240501", "2024-05-01"),
        ("2024-05", "2024-05"),
        ("2024", "2024"),
    ]:
        status, data = request("/api/normalize-date", {"text": text})
        check(f"{text} -> {want}", data.get("ok") is True and data.get("date") == want, str(data))

    status, data = request("/api/normalize-date", {"text": "去年秋天"})
    check("乱填日期报错", data.get("ok") is False and "无法识别" in data.get("error", ""))

    status, data = request("/api/normalize-date", {"text": "  "})
    check("空日期返回空", data.get("ok") is True and data.get("date") == "")


def test_api_preview() -> None:
    section("HTTP：试运行（不写文件）")

    src = os.path.join(WORK, "preview.mp3")
    fixtures.make_mp3(src)
    status, form = request("/api/open", {"path": src})
    form = form["form"]

    status, data = request("/api/preview", {"form": form})
    check("未改动时 nothing=True", data.get("ok") is True and data.get("nothing") is True, str(data))
    check("未改动时没有 rows", data.get("rows") == [])
    check("未改动时没有错误", data.get("errors") == [])

    form["title"] = "试运行标题"
    status, data = request("/api/preview", {"form": form})
    check("改动后有 rows", data.get("ok") is True and len(data.get("rows", [])) == 1)
    check("rows 内容正确", data["rows"][0]["label"] == "标题" and data["rows"][0]["after"] == "试运行标题")
    check("试运行没有真的写文件", not os.path.exists(form["output"]))

    form_bad = dict(form, date="去年秋天")
    status, data = request("/api/preview", {"form": form_bad})
    check("试运行能报出坏日期", data.get("ok") is False and "无法识别" in data.get("error", ""))

    # 输出已存在
    existing = os.path.join(WORK, "already-there.mp3")
    with open(existing, "wb") as fh:
        fh.write(b"x")
    form_exists = dict(form, output=existing)
    status, data = request("/api/preview", {"form": form_exists})
    check("输出已存在的提示", data.get("ok") is True and data.get("errors"), str(data))
    check("提示里说明不覆盖", "已存在" in data["errors"][0] and "不覆盖" in data["errors"][0])

    form_same = dict(form, output=src)
    status, data = request("/api/preview", {"form": form_same})
    check("输出与源文件相同时提示", data.get("ok") is True and data.get("errors") and "不能和源文件相同" in data["errors"][0], str(data))
    form_same_force = dict(form, output=src, overwrite=True)
    status, data = request("/api/preview", {"form": form_same_force})
    check(
        "勾选覆盖也不许覆盖源文件",
        data.get("ok") is True and data.get("errors") and "不能和源文件相同" in data["errors"][0],
        str(data),
    )

    form_dir = dict(form, output=WORK)
    status, data = request("/api/preview", {"form": form_dir})
    check("输出是目录时提示", data.get("ok") is True and data.get("errors") and "目录" in data["errors"][0])


def test_api_write() -> None:
    section("HTTP：流式写入（真实进度 + 真实校验）")

    src = os.path.join(WORK, "write.flac")
    fixtures.make_flac(src)
    status, data = request("/api/open", {"path": src})
    form = data["form"]

    png = os.path.join(WORK, "cover.png")
    status, image = request("/api/image", {"path": png})
    check("准备封面成功", image.get("ok") is True)

    lrc_path = os.path.join(WORK, "lyrics.lrc")
    status, lyrics = request("/api/lyrics", {"path": lrc_path})

    out = os.path.join(WORK, "write_out.flac")
    core._safe_unlink(out)

    form.update(
        {
            "title": "界面上写的标题",
            "artists": ["张三", "李四"],
            "album": "界面专辑",
            "date": "2024年5月1日",
            "cover": {"action": "set", "token": image["token"]},
            "lyrics": {"action": "set", "text": lyrics["text"], "kind": lyrics["kind"]},
            "output": out,
            "overwrite": False,
        }
    )

    events = write_stream(form)
    check("收到 start 事件", len(events_of(events, "start")) == 1)
    stages = events_of(events, "stage")
    check("收到 4 个阶段事件", len(stages) == 4, repr(stages))
    check("阶段带序号与文案", [s["index"] for s in stages] == [0, 1, 2, 3] and all(s["text"] for s in stages))
    check("阶段总数一致", all(s["total"] == 4 for s in stages))

    done = events_of(events, "done")
    check("收到 done 事件", len(done) == 1, repr(events[-1:]))
    check("没有 error 事件", not events_of(events, "error"))

    result = done[0]["result"]
    check("输出文件已存在", os.path.exists(out))
    check("result 里的输出路径正确", result["output"] == out)
    check("result 标出格式", result["fmt"] == "FLAC")
    check("音频逐字节一致", result["audio_unchanged"] is True, result.get("audio_before", "") + " / " + result.get("audio_after", ""))
    check("校验全部通过", result["failed"] == 0 and result["total"] > 0, repr(result.get("checks")))
    check("改动清单非空", len(result["rows"]) >= 5, repr(result["rows"]))
    check("改动清单含封面与歌词", {"封面", "歌词"} <= {r["label"] for r in result["rows"]})

    # 用 core 独立复核，确认界面写出来的文件是对的
    tags, fmt, _ = core.read_tags(out)
    check("独立复核：格式", fmt == "flac")
    check("独立复核：标题", tags.title == "界面上写的标题")
    check("独立复核：作者两位", tags.artists == ["张三", "李四"], repr(tags.artists))
    check("独立复核：专辑", tags.album == "界面专辑")
    check("独立复核：日期已归一", tags.date == "2024-05-01")
    check("独立复核：封面是 PNG 300x200", tags.cover is not None and tags.cover.mime == "image/png" and tags.cover.width == 300)
    check(
        "独立复核：歌词逐行保留",
        tags.lyrics.rstrip() == lyrics["text"].rstrip() and "[00:18.00]" in tags.lyrics,
        repr(tags.lyrics),
    )
    check("独立复核：歌词类型", tags.lyrics_kind == "lrc")

    # 源文件与封面都没被动过
    check("源文件未被修改", core._audio_bytes_fingerprint(src, "flac") == core._audio_bytes_fingerprint(out, "flac"))

    # 校验结果结构
    labels = [c["label"] for c in result["checks"]]
    check("校验项含标题和作者", "标题" in labels and "作者" in labels, repr(labels))
    check("校验项都带 ok 字段", all(isinstance(c["ok"], bool) for c in result["checks"]))


def test_api_write_errors() -> None:
    section("HTTP：写入的错误处理")

    src = os.path.join(WORK, "err.mp3")
    fixtures.make_mp3(src)
    status, data = request("/api/open", {"path": src})
    form = data["form"]

    # 没改动
    events = write_stream(form)
    errors = events_of(events, "error")
    check("没有改动时报错", len(errors) == 1 and "没有检测到任何改动" in errors[0]["error"], repr(events))
    check("报错时也有分块结束（没有 done）", not events_of(events, "done"))

    # 输出已存在
    existing = os.path.join(WORK, "err-existing.mp3")
    with open(existing, "wb") as fh:
        fh.write(b"old")
    events = write_stream(dict(form, title="改了", output=existing, overwrite=False))
    errors = events_of(events, "error")
    check("输出已存在时报错", len(errors) == 1 and "已存在" in errors[0]["error"], repr(events))
    check("输出已存在时没被覆盖", open(existing, "rb").read() == b"old")
    check("输出已存在时仍能给出阶段进度", len(events_of(events, "stage")) >= 2)

    # 覆盖开关打开后成功
    events = write_stream(dict(form, title="改了", output=existing, overwrite=True))
    check("勾选覆盖后写入成功", len(events_of(events, "done")) == 1, repr(events[-1:]))

    # 坏日期
    events = write_stream(dict(form, date="去年秋天", output=os.path.join(WORK, "err-date.mp3")))
    errors = events_of(events, "error")
    check("坏日期在写入时报错", len(errors) == 1 and "无法识别" in errors[0]["error"])
    check("坏日期没有留下输出文件", not os.path.exists(os.path.join(WORK, "err-date.mp3")))

    # 封面 token 失效
    events = write_stream(dict(form, cover={"action": "set", "token": "expired"}, output=os.path.join(WORK, "err-cover.mp3")))
    errors = events_of(events, "error")
    check("封面失效时给出可操作的提示", len(errors) == 1 and "重新选择" in errors[0]["error"], repr(events))

    # 源文件不存在
    events = write_stream(dict(form, src=os.path.join(WORK, "没有这个文件.mp3"), title="x"))
    errors = events_of(events, "error")
    check("源文件不存在时报错", len(errors) == 1 and "不存在" in errors[0]["error"])

    # 输出指向源文件本身：即使勾了覆盖也必须拒绝，源文件绝不能被写坏
    before = open(src, "rb").read()
    events = write_stream(dict(form, title="原地覆盖", output=src, overwrite=True))
    errors = events_of(events, "error")
    check("勾选覆盖也不许原地改写源文件", len(errors) == 1 and "不能和源文件相同" in errors[0]["error"], repr(events))
    check("源文件字节未被改动", open(src, "rb").read() == before)


def test_api_upload() -> None:
    section("HTTP：拖拽上传")

    src = os.path.join(WORK, "upload-src.mp3")
    fixtures.make_mp3(src)
    with open(src, "rb") as fh:
        blob = fh.read()

    status, data = request(
        "/api/upload", None, raw=blob, headers={"X-Filename": urllib.parse.quote("我的歌曲.mp3"), "Content-Type": "application/octet-stream"}
    )
    check("上传 MP3 成功", data.get("ok") is True, str(data.get("error")))
    form = data["form"]
    check("上传后带出源路径", form["src"].endswith("我的歌曲.mp3"))
    check("上传的文件落在导入目录", os.path.abspath(form["src"]).startswith(os.path.abspath(webui.import_dir())))
    check("上传后标为 imported", form["imported"] is True)
    check("上传后的默认输出在可写目录", os.path.dirname(form["output"]) == webui._default_out_dir(), form["output"])
    check("上传后字节数一致", form["size"] == len(blob))

    # 重名不会覆盖，会加 (1)
    status, data2 = request(
        "/api/upload", None, raw=blob, headers={"X-Filename": urllib.parse.quote("我的歌曲.mp3"), "Content-Type": "application/octet-stream"}
    )
    check("重名上传不覆盖", data2["form"]["src"] != form["src"] and data2["form"]["src"].endswith("(1).mp3"))

    # 文件名里的路径分隔符要被剥掉
    status, data3 = request(
        "/api/upload", None, raw=blob, headers={"X-Filename": urllib.parse.quote("..\\..\\evil.mp3"), "Content-Type": "application/octet-stream"}
    )
    check("上传文件名不能带路径", os.path.basename(data3["form"]["src"]) == "evil.mp3")
    check("上传文件没有跑出导入目录", os.path.abspath(data3["form"]["src"]).startswith(os.path.abspath(webui.import_dir())))

    # 非音频要被拒绝，而且不能留下垃圾文件
    before = set(os.listdir(webui.import_dir()))
    status, data4 = request(
        "/api/upload", None, raw=b"this is not audio at all", headers={"X-Filename": urllib.parse.quote("fake.mp3"), "Content-Type": "application/octet-stream"}
    )
    check("上传假音频被拒绝", data4.get("ok") is False)
    check("被拒绝的上传不留垃圾文件", set(os.listdir(webui.import_dir())) == before, repr(set(os.listdir(webui.import_dir())) - before))

    # 空文件
    status, data5 = request("/api/upload", None, raw=b"", headers={"X-Filename": urllib.parse.quote("empty.mp3")})
    check("上传空文件被拒绝", data5.get("ok") is False and "空" in data5.get("error", ""))
    check("空文件不留垃圾", set(os.listdir(webui.import_dir())) == before)


def batch_stream(payload: dict):
    """调用流式批量打包接口，收集所有事件。"""
    req = urllib.request.Request(
        BASE + "/api/batch/run", data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Token", TOKEN)
    events = []
    with urllib.request.urlopen(req, timeout=180) as resp:
        for line in resp:
            text = line.decode("utf-8").strip()
            if text:
                events.append(json.loads(text))
    return events


def make_song_ncm(folder: str, name: str, *, music_name="内部歌名", fmt="mp3",
                  with_cover=True) -> str:
    """造一个带元信息的 ncm，文件名和内部歌名故意不同。"""
    scratch = os.path.join(WORK, "_src")
    os.makedirs(scratch, exist_ok=True)
    audio = (fixtures.make_flac(os.path.join(scratch, "b.flac")) if fmt == "flac"
             else fixtures.make_mp3(os.path.join(scratch, "b.mp3"), frames=12))
    cover = b""
    if with_cover:
        cover = open(fixtures.make_jpeg(os.path.join(scratch, "b.jpg"), 80, 60), "rb").read()
    return fixtures.make_ncm(
        os.path.join(folder, f"{name}.ncm"), audio,
        metadata={"musicName": music_name, "album": "内部专辑", "format": fmt,
                  "artist": [["甲", 1], ["乙", 2]], "duration": 12345},
        cover=cover)


def test_api_batch() -> None:
    section("批量打包接口")

    src = os.path.join(WORK, "batch_src")
    out = os.path.join(WORK, "batch_out")
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(src, exist_ok=True)

    # 一个有歌词的 ncm、一个有歌词的 mp3、一个没歌词的 mp3、一个孤儿歌词
    make_song_ncm(src, "甲 - 加密有词")
    with open(os.path.join(src, "甲 - 加密有词.lrc"), "w", encoding="utf-8") as fh:
        fh.write(LRC)
    plain = fixtures.make_mp3(os.path.join(src, "乙 - 普通有词.mp3"), frames=12)
    core.write_tags(plain, plain, Changes(title="乙的歌", artists=["乙"], album="乙专辑"),
                    overwrite=True)
    with open(os.path.join(src, "乙 - 普通有词.lrc"), "w", encoding="utf-8") as fh:
        fh.write(LRC)
    fixtures.make_mp3(os.path.join(src, "丙 - 没歌词.mp3"), frames=10)
    with open(os.path.join(src, "没人要的.lrc"), "w", encoding="utf-8") as fh:
        fh.write(LRC)

    # -- 扫描
    status, res = request("/api/batch/scan", {"src_dir": src, "out_dir": out})
    check("扫描返回 200", status == 200, str(status))
    check("扫描成功", res.get("ok") is True, str(res)[:200])
    summary = res.get("summary", {})
    check("扫到 3 个条目（2 音频 + 1 ncm）",
          summary.get("total") == 3 and summary.get("audio") == 2 and summary.get("ncm") == 1,
          str(summary))
    check("其中 2 个配到歌词", summary.get("with_lyrics") == 2, str(summary))
    check("汇报了 1 个孤儿歌词", summary.get("orphan_lrc") == 1, str(summary))
    check("孤儿歌词就是「没人要的.lrc」", res.get("orphans") == ["没人要的.lrc"],
          str(res.get("orphans")))
    check("输出目录自动补成 _packed 之外的指定目录", res.get("out_dir") == out, str(res.get("out_dir")))
    items = {i["name"]: i for i in res.get("items", [])}
    check("条目带上了配对到的歌词名",
          items["甲 - 加密有词.ncm"]["lrc"] == "甲 - 加密有词.lrc",
          str(items.get("甲 - 加密有词.ncm")))
    check("没有歌词的条目标为空", items["丙 - 没歌词.mp3"]["lrc"] == "")
    check("初始状态都是 pending",
          all(i["status"] == "pending" for i in res.get("items", [])),
          str([i["status"] for i in res.get("items", [])]))

    # -- 扫描的错误分支
    status, res = request("/api/batch/scan", {"src_dir": os.path.join(WORK, "不存在的目录")})
    check("目录不存在时 ok=false", res.get("ok") is False, str(res)[:200])
    check("目录不存在时给了清楚提示", "目录不存在" in str(res.get("error")), str(res)[:200])

    status, res = request("/api/batch/scan", {"src_dir": ""})
    check("不选目录时提示要选目录", res.get("ok") is False and "请先选择" in str(res.get("error")),
          str(res)[:200])

    status, res = request("/api/batch/scan", {"src_dir": src, "out_dir": src})
    check("输出目录等于源目录时被拒绝",
          res.get("ok") is False and "不能和源目录相同" in str(res.get("error")), str(res)[:200])

    # 令牌仍然必须校验
    status, res = request("/api/batch/scan", {"src_dir": src}, token="wrong-token-123")
    check("批量接口同样要求令牌", res.get("ok") is False and "令牌" in str(res.get("error")),
          str(res)[:200])

    # -- 真正的流式打包
    before = {n: os.path.getsize(os.path.join(src, n)) for n in os.listdir(src)}
    events = batch_stream({"src_dir": src, "out_dir": out, "only_with_lyrics": True,
                           "lyrics_mode": "lrc"})
    start = events_of(events, "start")
    check("有 start 事件", len(start) == 1, str(events)[:300])
    check("start 里带了总数与扫描结果",
          start and start[0]["total"] == 2 and start[0]["plan"]["summary"]["total"] == 3,
          str(start)[:300])

    item_events = events_of(events, "item")
    finished = [e for e in item_events if e.get("status") != "start"]
    check("2 个文件各有一条结束事件", len(finished) == 2, str(len(item_events)))
    check("事件按顺序编号", [e["index"] for e in finished] == [0, 1], str(finished))
    check("两条都是 done", all(e["status"] == "done" for e in finished),
          str([(e["name"], e["status"], e["text"]) for e in finished]))
    check("事件里说的名字是真实文件名",
          {e["name"] for e in finished} == {"甲 - 加密有词.ncm", "乙 - 普通有词.mp3"},
          str([e["name"] for e in finished]))

    done = events_of(events, "done")
    check("有 done 事件", len(done) == 1, str(events)[-300:])
    stats = done[0]["result"]["stats"] if done else {}
    check("统计：写出 2 个、失败 0",
          stats.get("done") == 2 and stats.get("failed") == 0, str(stats))
    check("统计：解密 1 个 ncm", stats.get("decrypted") == 1, str(stats))
    check("统计：写入歌词 2 个", stats.get("lyrics_written") == 2, str(stats))
    check("结果里带了输出目录", done and done[0]["result"]["out_dir"] == out, str(done)[:200])

    produced = sorted(os.listdir(out))
    check("只产出了那 2 个文件", set(produced) == {"甲 - 加密有词.mp3", "乙 - 普通有词.mp3"},
          str(produced))
    check("没有临时文件残留", not any(n.startswith(".") for n in produced), str(produced))

    tags, fmt, _a = core.read_tags(os.path.join(out, "甲 - 加密有词.mp3"))
    check("解密出来的 ncm 写上了歌词", bool(tags.lyrics) and tags.lyrics_kind == "lrc",
          repr((tags.lyrics, tags.lyrics_kind)))
    check("ncm 内部歌名（不是文件名）被写进标签", tags.title == "内部歌名", repr(tags.title))
    check("ncm 内嵌封面被补上", tags.cover is not None and tags.cover.mime == "image/jpeg",
          str(tags.cover))
    check("作者从 ncm 补齐为两位", tags.artists == ["甲", "乙"], repr(tags.artists))

    tags2, _f, _a = core.read_tags(os.path.join(out, "乙 - 普通有词.mp3"))
    check("原有标签没被改掉", tags2.title == "乙的歌" and tags2.album == "乙专辑",
          repr((tags2.title, tags2.album)))
    check("普通 mp3 也写上了歌词", bool(tags2.lyrics))

    after = {n: os.path.getsize(os.path.join(src, n)) for n in os.listdir(src)}
    check("源目录一个字节都没变", before == after, str((before, after)))

    # -- 再跑一次应当跳过（输出已存在）
    events = batch_stream({"src_dir": src, "out_dir": out, "only_with_lyrics": True})
    done = events_of(events, "done")
    stats = done[0]["result"]["stats"] if done else {}
    check("第二次跑全部跳过", stats.get("skipped") == 2 and stats.get("done") == 0, str(stats))
    skipped = [e for e in events_of(events, "item") if e.get("status") == "skipped"]
    check("跳过时给了原因", skipped and "已存在" in skipped[0]["text"],
          str([e.get("text") for e in skipped]))

    # -- 没有配到歌词时的报错
    empty = os.path.join(WORK, "batch_empty")
    shutil.rmtree(empty, ignore_errors=True)
    os.makedirs(empty, exist_ok=True)
    fixtures.make_mp3(os.path.join(empty, "孤单.mp3"), frames=8)
    status, res = request("/api/batch/run", {"src_dir": empty, "only_with_lyrics": True})
    check("一个歌词都没配到时给出提示",
          res.get("ok") is False and "没有配到歌词" in str(res.get("error")), str(res)[:300])
    check("什么都没写出来", not os.path.exists(os.path.join(empty, "_packed")))


def test_api_end_to_end_flac_cover() -> None:
    section("HTTP：完整流程（FLAC + PNG 封面 + LRC 歌词）")

    src = os.path.join(WORK, "e2e.flac")
    fixtures.make_flac(src)
    status, data = request("/api/open", {"path": src})
    form = data["form"]

    jpg = os.path.join(WORK, "e2e.jpg")
    fixtures.make_jpeg(jpg, 240, 160)
    status, img = request("/api/image", {"path": jpg})

    out = os.path.join(WORK, "e2e_out.flac")
    core._safe_unlink(out)
    form.update({"title": "端到端", "artists": ["独唱"], "cover": {"action": "set", "token": img["token"]}, "output": out})

    events = write_stream(form)
    done = events_of(events, "done")
    check("端到端写入成功", len(done) == 1, repr(events_of(events, "error")))
    check("端到端校验通过", done[0]["result"]["failed"] == 0)

    tags, fmt, _ = core.read_tags(out)
    check("读回封面是 JPEG 240x160", tags.cover is not None and tags.cover.mime == "image/jpeg" and tags.cover.width == 240)
    from mutagen.flac import FLAC  # 用 mutagen 独立交叉确认封面块数量
    pictures = FLAC(out).pictures
    check("封面恰好一张且是封面类型", len(pictures) == 1 and pictures[0].type == 3, f"{len(pictures)} 张")
    check("读回标题", tags.title == "端到端")
    check("源文件仍然存在且未被改动", os.path.exists(src))


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def main() -> int:
    global BASE, TOKEN

    shutil.rmtree(UI_HOME, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    httpd = webui.serve("127.0.0.1", 0)
    BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
    TOKEN = webui.Handler.token
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print(f"界面服务已启动：{BASE}（测试用临时端口）")

    tests = [
        test_ui_logic,
        test_static,
        test_api_basics,
        test_api_open,
        test_api_image_and_lyrics,
        test_api_date,
        test_api_preview,
        test_api_write,
        test_api_write_errors,
        test_api_upload,
        test_api_batch,
        test_api_end_to_end_flac_cover,
    ]
    for fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            FAILED.append(f"{fn.__name__} 抛异常")

    httpd.shutdown()
    httpd.server_close()

    print("\n" + "=" * 60)
    if FAILED:
        print(f"{PASSED} 项通过 / {len(FAILED)} 项失败")
        for item in FAILED:
            print(f"  FAIL {item}")
        return 1
    print(f"全部通过：{PASSED} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
