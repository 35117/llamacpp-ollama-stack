#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""驱动**正在运行的托盘**去启停服务 —— 自动化入口。

为什么需要这个文件
------------------
从 agent 的 shell 里直接跑 `service_ctl.py start` 会「启动了、但几秒后就没了」：
shell 把命令放在一个作业对象（job）里，作业不允许 breakaway 时
`CREATE_BREAKAWAY_FROM_JOB` 会失败，子进程就留在作业里 —— 命令一结束，
作业被回收，llama-swap 和桥跟着一起死。用户看到的现象正是
「你重启似乎就是关闭，并没有启动」。

托盘不一样：它是**用户自己双击起来的**进程，不在任何 agent 作业里。
所以让它去调 `service_ctl.py`（托盘内部就是这么做的），服务的父进程就是托盘，
agent 的 shell 死多少回都波及不到 —— 与手动点右键菜单完全等价。

托盘在 `tray.py` 里留了自动化入口：
    PostMessage(hwnd, WM_COMMAND, ID_xxx, 0)
窗口类名固定为 `LlamaCppTrayWnd`。

用法
----
    python scripts\\tray_cmd.py find        # 只查托盘在不在（不弹任何窗口）
    python scripts\\tray_cmd.py start
    python scripts\\tray_cmd.py stop
    python scripts\\tray_cmd.py restart
    python scripts\\tray_cmd.py autostart
    python scripts\\tray_cmd.py exit

退出码：0 成功 / 1 查询类未命中 / 3 托盘没在跑 / 4·5 结果不符合预期 / 2 用法错。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLASS = "LlamaCppTrayWnd"
BRIDGE_PORT = 11434

ID_OPEN_UI = 1001
ID_OPEN_PANEL = 1002
ID_START = 1003
ID_STOP = 1004
ID_RESTART = 1005
ID_STATUS = 1006
ID_AUTOSTART = 1007
ID_EXIT = 1008

CMDS = {
    "ui": ID_OPEN_UI,
    "panel": ID_OPEN_PANEL,
    "start": ID_START,
    "stop": ID_STOP,
    "restart": ID_RESTART,
    "autostart": ID_AUTOSTART,
    "exit": ID_EXIT,
}

# 带 pywin32 的解释器（与 tray.py 的自举列表一致；本机是 Python311 那个有）
PY_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python313\python.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python312\python.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python311\python.exe"),
]

CREATE_NO_WINDOW = 0x08000000


def _relaunch_with_pywin32():
    """没有 pywin32 时换一个有的解释器重跑自己；返回子进程退出码。"""
    for cand in PY_CANDIDATES:
        if not os.path.exists(cand):
            continue
        try:
            r = subprocess.run([cand, "-c", "import win32gui, win32con"],
                               capture_output=True, timeout=25,
                               creationflags=CREATE_NO_WINDOW)
        except Exception:
            continue
        if r.returncode == 0:
            env = dict(os.environ, TRAY_CMD_RELAUNCHED="1")
            return subprocess.call([cand, os.path.abspath(__file__)] + sys.argv[1:],
                                   env=env, cwd=ROOT)
    sys.stderr.write("找不到带 pywin32 的 Python —— tray_cmd 用不了。\n")
    return 2


def _port_open(port, timeout=0.4):
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_port(port, want_open, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if _port_open(port) == want_open:
            return True
        time.sleep(0.3)
    return False


def main():
    args = sys.argv[1:]
    what = (args[0] if args else "find").lower()

    try:
        import win32con
        import win32gui
        import win32process
    except ImportError:
        if os.environ.get("TRAY_CMD_RELAUNCHED") == "1":
            raise
        return _relaunch_with_pywin32()

    hwnd = win32gui.FindWindow(CLASS, None)

    if what in ("find", "status"):
        if not hwnd:
            print("tray NOT running")
            return 1
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        print(f"tray running: hwnd={hwnd} pid={pid}")
        return 0

    if what not in CMDS:
        sys.stderr.write(
            "用法: tray_cmd.py [find|start|stop|restart|autostart|exit|ui|panel]\n")
        return 2

    if not hwnd:
        sys.stderr.write(
            "托盘没有在运行 —— 先双击 scripts\\tray.vbs 把它拉起来。\n"
            "没有「托盘子进程」这个身份，服务还是会随 agent 的命令被回收。\n")
        return 3

    win32gui.PostMessage(hwnd, win32con.WM_COMMAND, CMDS[what], 0)
    print(f"已把 {what} 发给托盘（hwnd={hwnd}）")

    # 给一个确定的结论，而不是「发了就算」
    if what in ("start", "restart"):
        ok = _wait_port(BRIDGE_PORT, True, 90)
        print("桥 11434:", "已就绪" if ok else "90 秒内没起来（看 logs\\bridge.log）")
        return 0 if ok else 4
    if what == "stop":
        ok = _wait_port(BRIDGE_PORT, False, 60)
        print("桥 11434:", "已停止" if ok else "60 秒内仍在监听")
        return 0 if ok else 5
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
