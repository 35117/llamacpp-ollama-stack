#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""海龟汤小游戏挂在桥上的后端（走 /turtle 前缀）。

好处：**零额外进程** —— 底座起来后直接访问
    http://127.0.0.1:11434/turtle
就能玩，不必再单独启动 `turtle/server.py`。

职责只有两件：解析 `turtle/` 下的静态文件、把 `/turtle/api/*` 分发给
`turtle/game.py`。业务逻辑一律不在这里。

注意：判题请求由调用方直接发往 **llama-swap 上游（11435）**，
不走桥自己 —— 否则桥会递归调用自己。
"""
from __future__ import annotations

import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TURTLE_DIR = os.path.join(ROOT, "turtle")
if TURTLE_DIR not in sys.path:
    sys.path.insert(0, TURTLE_DIR)

from game import Game  # noqa: E402

PREFIX = "/turtle"
API_PREFIX = PREFIX + "/api/"

CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
}

_game = None
_game_lock = threading.Lock()
_game_base = None
_game_puzzle_mtime = None

PUZZLE_FILE = os.path.join(TURTLE_DIR, "puzzles", "classic.json")


def _puzzle_mtime():
    """题库文件的 mtime；读不到时返回 None（当作不参与判断）。"""
    try:
        return os.path.getmtime(PUZZLE_FILE)
    except OSError:
        return None


def get_game(base=None, **kwargs):
    """进程内单例。

    第一次调用时用传入的参数构造；之后复用。以下两种情况会重建：
      - `base`（上游地址）变了 —— 免得改了配置还在用旧地址
      - 题库文件 mtime 变了 —— **加完题不必重启桥**

    重建会顺带重置提示次数（`_hint_counts` 挂在 Game 上），换来的是
    改题库不用中断整个模型服务。
    """
    global _game, _game_base, _game_puzzle_mtime
    with _game_lock:
        mtime = _puzzle_mtime()
        if (
            _game is None
            or (base and base != _game_base)
            or mtime != _game_puzzle_mtime
        ):
            _game = Game(base=base, **kwargs)
            _game_base = base
            _game_puzzle_mtime = mtime
        return _game


def reset_game(**kwargs):
    """丢弃单例并重建（测试或改配置后用）。"""
    global _game
    with _game_lock:
        _game = None
    return get_game(**kwargs)


def _resolve(rel):
    """把 /turtle/xxx 映射到 turtle/xxx；越界（路径穿越）返回 None。"""
    rel = (rel or "").lstrip("/")
    if not rel:
        rel = "index.html"
    full = os.path.normpath(os.path.join(TURTLE_DIR, rel))
    if full != TURTLE_DIR and not full.startswith(TURTLE_DIR + os.sep):
        return None
    return full


def static_file(rel):
    """返回 (bytes, ctype)；找不到或越界返回 (None, None)。"""
    full = _resolve(rel)
    if not full or not os.path.isfile(full):
        return None, None
    ext = os.path.splitext(full)[1].lower()
    ctype = CTYPES.get(ext, "application/octet-stream")
    try:
        with open(full, "rb") as fh:
            return fh.read(), ctype
    except OSError:
        return None, None


def api_name(path):
    """从 /turtle/api/ask 取出 ask；不是 API 路径则返回 None。"""
    if not path.startswith(API_PREFIX):
        return None
    return path[len(API_PREFIX):].strip("/")


def handle_api(method, name, body, base=None):
    """返回 (status, payload dict)。"""
    try:
        game = get_game(base=base)
    except Exception as exc:  # noqa: BLE001
        return 500, {"error": f"海龟汤初始化失败: {exc}"}
    return game.dispatch(method, name, body or {})
