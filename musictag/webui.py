#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
musictag 图形界面（本地 Web UI，**只用 Python 标准库**）。

启动后在本机 127.0.0.1 上起一个小服务并自动打开浏览器。界面文件在
`musictag/ui/` 下；标签的读取、写入、校验**全部调用 `musictag.core`**，
本模块只负责三件事：

  1. 把「界面表单」搬成 `core.Changes`（差异比对在 `musictag.ui_logic` 里）；
  2. 通过独立进程弹原生文件对话框，拿到拖拽拿不到的真实磁盘路径；
  3. 把 core 的结果与异常转成 JSON 交给页面。

不重新编码、原子替换、音频指纹校验等保证，全部来自 core，这里不重复实现。

启动：
    python musictag\\webui.py                 # 默认 127.0.0.1:8770 并自动开浏览器
    python musictag\\webui.py --port 9000 --no-browser
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

if __package__ in (None, ""):  # 支持 `python webui.py` 直接运行
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from musictag import batch as batch_mod
    from musictag import core, ui_logic
    from musictag.core import ImageError, TagError
else:
    from . import batch as batch_mod
    from . import core, ui_logic
    from .core import ImageError, TagError

__all__ = ["main", "serve"]

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
DIALOG_PY = os.path.join(HERE, "_dialog.py")
DEFAULT_PORT = 8770
MAX_UPLOAD = 600 * 1024 * 1024  # 单个文件上限 600 MB，防止误传巨大文件把内存吃光

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


# --------------------------------------------------------------------------- #
# 本地目录与配置（只记住「上次用的输出目录」，不做别的）
# --------------------------------------------------------------------------- #


def data_root() -> str:
    override = os.environ.get("MUSICTAG_UI_HOME")
    if override:
        return os.path.abspath(override)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "musictag-ui")


def import_dir() -> str:
    return os.path.join(data_root(), "import")


def config_path() -> str:
    return os.path.join(data_root(), "config.json")


def _load_config() -> dict:
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_config(cfg: dict) -> None:
    try:
        os.makedirs(data_root(), exist_ok=True)
        with open(config_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass  # 记不住偏好不影响主要功能


def _default_out_dir() -> str:
    cfg = _load_config()
    saved = cfg.get("output_dir")
    if saved and os.path.isdir(saved):
        return saved
    music = os.path.join(os.path.expanduser("~"), "Music")
    base = music if os.path.isdir(music) else os.path.expanduser("~")
    return os.path.join(base, "musictag 输出")


def _is_imported(path: str) -> bool:
    """判断这个源文件是不是「拖拽上传」进来的（那种情况下原始目录无从得知）。"""
    try:
        return os.path.commonpath([os.path.abspath(path), import_dir()]) == os.path.abspath(import_dir())
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# 封面图片缓存：页面只传一个 token，避免每次请求都把图片重新塞进 JSON
# --------------------------------------------------------------------------- #

_image_cache: dict[str, core.Image] = {}
_cache_lock = threading.Lock()


def _cache_image(image: core.Image) -> str:
    token = secrets.token_urlsafe(12)
    with _cache_lock:
        if len(_image_cache) > 32:  # 简单上限，避免长时间运行越攒越多
            _image_cache.clear()
        _image_cache[token] = image
    return token


def _get_image(token: str | None) -> core.Image | None:
    if not token:
        return None
    with _cache_lock:
        return _image_cache.get(token)


# --------------------------------------------------------------------------- #
# 表单组装
# --------------------------------------------------------------------------- #


def form_for(path: str) -> dict:
    """读取一个音频文件，返回填满的表单（供页面直接渲染）。"""
    tags, fmt, _audio = core.read_tags(path)
    sig = core._audio_signature(path, fmt)
    audio_text = core.format_audio(sig)

    if _is_imported(path):
        # 拖拽进来的文件原目录未知，输出默认放到用户可找到的目录
        name = os.path.basename(path)
        root, ext = os.path.splitext(name)
        out = os.path.join(_default_out_dir(), f"{root}{ui_logic.DEFAULT_SUFFIX}{ext}")
    else:
        out = ui_logic.default_output(path)

    form = ui_logic.initial_form(path, tags, audio_text=audio_text, output=out)
    form["fmt"] = fmt.upper()
    form["imported"] = _is_imported(path)
    form["size"] = os.path.getsize(path)
    form["cover_preview"] = _image_payload(tags.cover) if tags.cover is not None else None
    return form


def _image_payload(image: core.Image) -> dict:
    """把一个 core.Image 变成页面可以直接显示的对象（内嵌 data URL，并登记 token）。"""
    return {
        "token": _cache_image(image),
        "summary": image.summary,
        "mime": image.mime,
        "width": image.width,
        "height": image.height,
        "depth": image.depth,
        "size": len(image.data),
        "source": image.source or "",
        "data_url": f"data:{image.mime};base64,{base64.b64encode(image.data).decode('ascii')}",
    }


def _prepare_form(form: dict) -> dict:
    """把 token 换成真正的 Image 对象，再交给 ui_logic 做差异比对。"""
    form = dict(form or {})
    cover = dict(form.get("cover") or {})
    if (cover.get("action") or "keep").lower() == "set":
        image = _get_image(cover.get("token"))
        if image is None:
            raise TagError("封面图片已失效（可能是服务重启过），请重新选择一次封面。")
        cover["image"] = image
    form["cover"] = cover
    return form


def _changes_from(form: dict):
    """返回 (src, original_tags, fmt, changes)。每次都重新读源文件，不用缓存的状态。"""
    src = (form.get("src") or "").strip()
    if not src:
        raise TagError("请先选择一个 MP3 / FLAC 文件。")
    original, fmt, _audio = core.read_tags(src)
    changes = ui_logic.build_changes(_prepare_form(form), original)
    return src, original, fmt, changes


def _check_output(src: str, out: str, overwrite: bool) -> None:
    if not out:
        raise TagError("请填写输出文件路径。")
    if os.path.isdir(out):
        raise TagError(f"输出路径是一个目录，请给出文件名：{out}")
    # 先判「输出就是源文件」：这比「文件已存在」更贴近用户真正想干的事，
    # 而且无论有没有勾选覆盖都不允许 —— 界面承诺过源文件始终不动。
    if os.path.abspath(out) == os.path.abspath(src):
        raise TagError("输出路径不能和源文件相同。请换一个输出路径，源文件始终保持不变。")
    parent = os.path.dirname(os.path.abspath(out))
    if not os.path.isdir(parent):
        raise TagError(f"输出目录不存在：{parent}")
    if os.path.exists(out) and not overwrite:
        raise TagError(f"输出文件已存在：{out}\n默认不覆盖，请勾选「覆盖已存在的输出文件」，或换一个输出路径。")


# --------------------------------------------------------------------------- #
# 原生对话框（独立进程）
# --------------------------------------------------------------------------- #


def _item_payload(item) -> dict:
    """把一个 batch.Item 变成页面能直接显示的小字典。"""
    if item is None:
        return {}
    return {
        "name": item.name,
        "kind": item.kind,
        "fmt": (item.fmt or "").upper(),
        "lrc": os.path.basename(item.lrc) if item.lrc else "",
        "match": item.match,
        "status": item.status,
        "message": item.message,
        "filled": list(item.filled),
        "output": item.output or "",
        "checks_failed": sum(0 if c.ok else 1 for c in item.checks),
    }


def _batch_payload(plan) -> dict:
    return {
        "src_dir": plan.src_dir,
        "out_dir": plan.out_dir,
        "summary": plan.summary(),
        "items": [_item_payload(i) for i in plan.items],
        "orphans": list(plan.orphan_lrc),
        "notes": list(plan.notes),
    }


def ask_native(kind: str, initial: str = "") -> str:
    """调用独立进程弹原生对话框，返回真实路径；取消时返回空字符串。"""
    cmd = [sys.executable, DIALOG_PY, kind]
    if initial:
        cmd.append(initial)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env, timeout=600)
    except subprocess.TimeoutExpired:
        raise TagError("文件对话框超时未响应，请重试。")
    except OSError as exc:
        raise TagError(f"无法打开系统文件对话框：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or f"退出码 {proc.returncode}"
        raise TagError(f"文件对话框打开失败：{detail}")
    return (proc.stdout or "").strip()


def reveal_in_explorer(path: str) -> None:
    """在资源管理器里定位到这个文件（没有文件时退化为打开所在目录）。"""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise TagError(f"找不到要打开的路径：{path}")
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except OSError as exc:
        raise TagError(f"无法打开文件夹：{exc}") from exc


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "musictag-ui"
    token = ""  # 由 serve() 注入

    # ---- 基础工具 ----

    def log_message(self, fmt, *args):  # 安静一点，不往控制台刷访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_UPLOAD:
            raise TagError(f"文件太大（{length / 1048576:.0f} MB），上限 {MAX_UPLOAD // 1048576} MB。")
        chunks, remaining = [], length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _json(self) -> dict:
        raw = self._body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TagError(f"请求内容不是合法的 JSON：{exc}") from exc
        return data if isinstance(data, dict) else {}

    # ---- 分块输出（用于真实进度：写入是分阶段的，逐步推给页面） ----

    def _begin_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _stream(self, obj: dict) -> None:
        payload = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        self.wfile.write(b"%X\r\n" % len(payload) + payload + b"\r\n")
        self.wfile.flush()

    def _end_stream(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _check_token(self) -> None:
        if self.headers.get("X-Token") != self.token:
            raise TagError("会话令牌无效，请刷新页面重试。")

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        static = {"/": "index.html", "/index.html": "index.html", "/style.css": "style.css", "/app.js": "app.js"}
        name = static.get(path)
        if not name:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        full = os.path.join(UI_DIR, name)
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send(500, f"界面文件缺失：{full}".encode("utf-8"), "text/plain; charset=utf-8")
            return
        if name == "index.html":
            # 把令牌注入页面，之后所有 /api 请求都要带上它
            body = body.replace(b"__TOKEN__", self.token.encode("ascii"))
        ctype = _CONTENT_TYPES.get(os.path.splitext(name)[1], "application/octet-stream")
        self._send(200, body, ctype)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/write":
            self._handle_write()
            return
        if route == "/api/batch/run":
            self._handle_batch_run()
            return
        handler = {
            "/api/ping": lambda: {"version": core.__dict__.get("__version__", "")},
            "/api/open": self._api_open,
            "/api/upload": self._api_upload,
            "/api/pick": self._api_pick,
            "/api/image": self._api_image,
            "/api/lyrics": self._api_lyrics,
            "/api/preview": self._api_preview,
            "/api/normalize-date": self._api_normalize_date,
            "/api/reveal": self._api_reveal,
            "/api/batch/scan": self._api_batch_scan,
        }.get(route)
        if handler is None:
            self._send_json({"ok": False, "error": f"未知接口：{route}"}, 404)
            return
        try:
            self._check_token()
            result = handler()
            self._send_json({"ok": True, **(result or {})})
        except (TagError, ImageError) as exc:
            self._send_json({"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001  任何意外都要变成页面上的可读提示
            self._send_json({"ok": False, "error": f"未预期的错误（{type(exc).__name__}）：{exc}"})

    # ---- 各接口实现 ----

    def _api_open(self) -> dict:
        path = (self._json().get("path") or "").strip().strip('"')
        if not path:
            raise TagError("请先选择音频文件。")
        return {"form": form_for(path)}

    def _api_upload(self) -> dict:
        """拖拽上传：浏览器只给文件名，内容通过请求体发过来，服务端落盘后再处理。"""
        raw_name = self.headers.get("X-Filename") or ""
        name = os.path.basename(unquote(raw_name)).strip() or "dropped"
        name = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or "dropped"
        target_dir = import_dir()
        os.makedirs(target_dir, exist_ok=True)

        dest = os.path.join(target_dir, name)
        stem, ext = os.path.splitext(name)
        counter = 1
        while os.path.exists(dest):
            dest = os.path.join(target_dir, f"{stem}({counter}){ext}")
            counter += 1

        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_UPLOAD:
                raise TagError(f"文件太大（{length / 1048576:.0f} MB），上限 {MAX_UPLOAD // 1048576} MB。")
            remaining = length
            with open(dest, "wb") as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)
        except TagError:
            raise
        except OSError as exc:
            raise TagError(f"保存拖入的文件失败：{exc}") from exc

        if os.path.getsize(dest) == 0:
            core._safe_unlink(dest)
            raise TagError(f"收到的文件是空的：{name}")

        try:
            return {"form": form_for(dest)}
        except TagError:
            core._safe_unlink(dest)  # 不是能处理的音频，别把垃圾留在导入目录
            raise

    def _api_pick(self) -> dict:
        data = self._json()
        kind = (data.get("kind") or "").strip()
        if kind not in ("audio", "image", "lyrics", "folder"):
            raise TagError(f"不支持的选择类型：{kind}")
        return {"path": ask_native(kind, (data.get("initial") or "").strip())}

    def _api_image(self) -> dict:
        path = (self._json().get("path") or "").strip()
        if not path:
            raise TagError("请先选择封面图片。")
        image = core.read_image(path)  # 顺带校验是否损坏 / 是否是 JPG、PNG
        payload = _image_payload(image)
        payload["path"] = path
        return payload

    def _api_lyrics(self) -> dict:
        path = (self._json().get("path") or "").strip()
        if not path:
            raise TagError("请先选择歌词文件。")
        text = core.read_lyrics_text(path)
        kind = core.detect_lyrics_kind(text)
        return {"text": text, "kind": kind, "source": path}

    def _api_normalize_date(self) -> dict:
        text = (self._json().get("text") or "").strip()
        if not text:
            return {"date": ""}
        return {"date": core.normalize_date(text)}  # 认不出来会抛 TagError，由上层转成提示

    def _api_preview(self) -> dict:
        data = self._json()
        form = data.get("form") or {}
        src, original, fmt, changes = _changes_from(form)
        out = (form.get("output") or "").strip()
        errors = []
        try:
            _check_output(src, out, bool(form.get("overwrite")))
        except TagError as exc:
            errors.append(str(exc))
        return {
            "rows": ui_logic.describe_changes(changes, original),
            "nothing": changes.is_empty(),
            "errors": errors,
            "output": out,
            "fmt": fmt.upper(),
            "existing_count": original.existing_count,
        }

    def _api_reveal(self) -> dict:
        path = (self._json().get("path") or "").strip()
        if not path:
            raise TagError("没有可打开的路径。")
        reveal_in_explorer(path)
        return {"opened": os.path.abspath(path)}

    # ---- 批量打包：扫描只读，写入走流式 ----

    def _batch_args(self, data: dict):
        src = (data.get("src_dir") or "").strip().strip('"')
        out = (data.get("out_dir") or "").strip().strip('"')
        if not src:
            raise TagError("请先选择要打包的目录（源目录）。")
        plan = batch_mod.scan(src, out or None)
        return plan, out or None

    def _api_batch_scan(self) -> dict:
        plan, _out = self._batch_args(self._json())
        return _batch_payload(plan)

    def _handle_batch_run(self) -> None:
        try:
            self._check_token()
            data = self._json()
        except TagError as exc:
            self._send_json({"ok": False, "error": str(exc)})
            return

        only = bool(data.get("only_with_lyrics"))
        overwrite = bool(data.get("overwrite"))
        lyrics_mode = (data.get("lyrics_mode") or "lrc").strip()

        # 扫描放在起流之前：出错就用普通 JSON 回一个清楚的提示
        try:
            plan, out_dir = self._batch_args(data)
            if not plan.items:
                raise TagError("这个目录里没有找到 mp3 / flac / ncm 文件。")
            todo = plan.with_lyrics if only else list(plan.items)
            if not todo:
                raise TagError(
                    "这个目录里没有配到歌词的文件。\n"
                    "请检查歌词文件（.lrc）是不是和音频放在一起、文件名是不是对应。"
                )
        except (TagError, ImageError) as exc:
            self._send_json({"ok": False, "error": str(exc)})
            return

        try:
            self._begin_stream()
            self._stream({"type": "start", "total": len(todo), "plan": _batch_payload(plan)})

            def on_event(ev):
                if ev.get("type") != "item":
                    return
                self._stream({
                    "type": "item",
                    "index": ev.get("index"),
                    "total": ev.get("total"),
                    "name": ev.get("name"),
                    "status": ev.get("status"),
                    "text": ev.get("text") or "",
                    "item": _item_payload(ev.get("item")),
                })

            stats = batch_mod.run(
                plan,
                overwrite=overwrite,
                lyrics_mode=lyrics_mode,
                on_event=on_event,
                only_with_lyrics=only,
            )

            if out_dir:
                cfg = _load_config()
                cfg["output_dir"] = plan.out_dir
                _save_config(cfg)

            self._stream({
                "type": "done",
                "result": {
                    "stats": stats,
                    "src_dir": plan.src_dir,
                    "out_dir": plan.out_dir,
                    "items": [_item_payload(i) for i in plan.items],
                    "orphans": list(plan.orphan_lrc),
                    "notes": list(plan.notes),
                },
            })
        except (TagError, ImageError) as exc:
            self._stream({"type": "error", "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._stream({"type": "error", "error": f"未预期的错误（{type(exc).__name__}）：{exc}"})
        finally:
            try:
                self._end_stream()
            except OSError:
                pass

    # ---- 写入：分阶段推进，页面能看到真实进度 ----

    def _handle_write(self) -> None:
        try:
            self._check_token()
            data = self._json()
        except TagError as exc:
            self._send_json({"ok": False, "error": str(exc)})
            return

        form = data.get("form") or {}
        out = (form.get("output") or "").strip()
        overwrite = bool(form.get("overwrite"))

        stages = [
            "正在读取源文件的标签…",
            "正在比对改动…",
            "正在写入标签并核对音频指纹…",
            "正在重新打开输出文件逐项校验…",
        ]
        try:
            self._begin_stream()
            self._stream({"type": "start", "total": len(stages)})

            self._stream({"type": "stage", "index": 0, "total": len(stages), "text": stages[0]})
            src, original, fmt, changes = _changes_from(form)

            if changes.is_empty():
                raise TagError("没有检测到任何改动。请修改标题、作者、专辑、日期、封面或歌词后再试。")

            self._stream({"type": "stage", "index": 1, "total": len(stages), "text": stages[1]})
            rows = ui_logic.describe_changes(changes, original)
            _check_output(src, out, overwrite)

            self._stream({"type": "stage", "index": 2, "total": len(stages), "text": stages[2]})
            report = core.write_tags(src, out, changes, overwrite=overwrite)

            self._stream({"type": "stage", "index": 3, "total": len(stages), "text": stages[3]})
            checks = core.verify(report.output, changes)

            if not _is_imported(out):
                cfg = _load_config()
                cfg["output_dir"] = os.path.dirname(os.path.abspath(out))
                _save_config(cfg)

            self._stream(
                {
                    "type": "done",
                    "result": {
                        "output": report.output,
                        "fmt": report.fmt.upper(),
                        "removed": report.removed,
                        "preserved": report.preserved,
                        "audio_unchanged": report.audio_unchanged,
                        "audio_before": core.format_audio(report.audio_before),
                        "audio_after": core.format_audio(report.audio_after),
                        "rows": rows,
                        "checks": [
                            {"label": c.label, "ok": c.ok, "expected": c.expected, "actual": c.actual}
                            for c in checks
                        ],
                        "failed": sum(0 if c.ok else 1 for c in checks),
                        "total": len(checks),
                    },
                }
            )
        except (TagError, ImageError) as exc:
            self._stream({"type": "error", "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._stream({"type": "error", "error": f"未预期的错误（{type(exc).__name__}）：{exc}"})
        finally:
            try:
                self._end_stream()
            except OSError:
                pass


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """建好服务但不启动（便于测试）。令牌每次启动重新生成。"""
    Handler.token = secrets.token_urlsafe(18)
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError:
        # 端口被占用就换一个临时端口，不因为这个原因打不开界面
        httpd = ThreadingHTTPServer((host, 0), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="musictag-webui",
        description="musictag 图形界面（本地网页版）。标签写入全部复用 musictag.core，不重新编码。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认只监听本机 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}，被占用时自动换端口）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args(argv)

    if not os.path.isdir(UI_DIR):
        sys.stderr.write(f"✘ 界面文件不存在：{UI_DIR}\n")
        return 2

    httpd = serve(args.host, args.port)
    host, port = httpd.server_address[0], httpd.server_address[1]
    url = f"http://{host}:{port}/"

    print("=" * 64)
    print("  musictag 图形界面已启动")
    print(f"  地址：{url}")
    print(f"  只监听本机（{host}），不会对外网开放。按 Ctrl+C 退出。")
    print("=" * 64)

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
