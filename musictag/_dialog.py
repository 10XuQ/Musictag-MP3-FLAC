# -*- coding: utf-8 -*-
"""
musictag UI 的原生文件对话框（独立进程运行）。

为什么单独一个进程：Tk 不是线程安全的，而 HTTP 服务是「每个请求一个线程」。
在独立进程里创建 Tk 解释器，既能拿到**真实磁盘路径**（浏览器出于安全不会给出
拖拽文件的完整路径），又不会把 Tk 的不稳定因素带进服务进程。

用法：
    python _dialog.py audio|image|lyrics|folder [起始目录]
选中路径写到 stdout；用户取消时写空字符串。退出码 0 正常，2 参数错误。
"""

from __future__ import annotations

import sys

_FILTERS = {
    "audio": [
        ("音频文件", "*.mp3 *.flac"),
        ("MP3", "*.mp3"),
        ("FLAC", "*.flac"),
        ("全部文件", "*.*"),
    ],
    "image": [
        ("图片", "*.jpg *.jpeg *.png"),
        ("JPEG", "*.jpg *.jpeg"),
        ("PNG", "*.png"),
        ("全部文件", "*.*"),
    ],
    "lyrics": [
        ("歌词", "*.lrc *.txt"),
        ("LRC", "*.lrc"),
        ("文本", "*.txt"),
        ("全部文件", "*.*"),
    ],
}

_TITLES = {
    "audio": "选择 MP3 / FLAC 音频文件",
    "image": "选择封面图片",
    "lyrics": "选择歌词文件",
    "folder": "选择输出文件夹",
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in _TITLES:
        sys.stderr.write("用法: _dialog.py audio|image|lyrics|folder [起始目录]\n")
        return 2

    kind = argv[1]
    initial = argv[2] if len(argv) > 2 and argv[2] else ""
    import os

    if initial and not os.path.isdir(initial):
        initial = os.path.dirname(initial) if os.path.dirname(initial) else ""

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:  # noqa: BLE001  某些窗口管理器不支持
        pass
    root.update()

    kwargs: dict = {"title": _TITLES[kind]}
    if initial:
        kwargs["initialdir"] = initial

    try:
        if kind == "folder":
            path = filedialog.askdirectory(mustexist=True, **kwargs)
        else:
            path = filedialog.askopenfilename(filetypes=_FILTERS[kind], **kwargs)
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass

    sys.stdout.write(path or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
