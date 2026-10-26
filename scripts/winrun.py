#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静默运行控制台辅助程序 —— **所有** netstat / taskkill / tasklist / nvidia-smi
之类的调用都必须走这里。

为什么要单独一个模块：从 `pythonw.exe`（无控制台的托盘）里启动一个控制台程序时，
如果不加 `CREATE_NO_WINDOW`，Windows 会**给每个子进程新建一个控制台窗口** ——
用户看到的就是「启动/停止时弹出一串黑框」。

一次「停止服务」原来要调：
    netstat ×2（探两个端口）+ taskkill /PID ×2 + taskkill /IM ×2  = 6 个窗口
再加上之后托盘刷新状态（netstat ×2 + tasklist ×1）= 3 个。
所以用户看到的「很多弹窗」就是这么来的，不是服务本身弹的。
"""
import subprocess

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# 下面这个**只作为「别用」的反面教材**保留，不要拿它去启动控制台程序：
# MSDN：CREATE_NO_WINDOW 在与 DETACHED_PROCESS（或 CREATE_NEW_CONSOLE）同时使用时
# **会被忽略**，于是控制台程序照样弹出一个可见窗口（2026-09-28 llama-swap 就是这样
# 留下了一个空的常驻黑框）。需要「不弹窗」就用 CREATE_NO_WINDOW 单独一个。
DETACHED_PROCESS_MUST_NOT_BE_COMBINED_WITH_CREATE_NO_WINDOW = 0x00000008


def run(cmd, timeout=20, **kw):
    """跑一个控制台辅助程序并**不弹窗口**。返回 CompletedProcess（stdout 为 bytes）。

    默认 `capture_output=True` + `stdin=DEVNULL`：
    中文 Windows 下这些命令输出是 GBK，交给 text=True 会在读取线程里抛
    UnicodeDecodeError，所以调用方自己拿 bytes 容错解码。
    """
    kw.setdefault("capture_output", True)
    kw.setdefault("stdin", subprocess.DEVNULL)
    extra = kw.pop("creationflags", 0)
    return subprocess.run(cmd, creationflags=extra | CREATE_NO_WINDOW, timeout=timeout, **kw)


def decode(raw):
    """bytes → str，容错解码（GBK 输出走 utf-8 会出替换字符，但足够我们抓数字）。"""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", "replace")
