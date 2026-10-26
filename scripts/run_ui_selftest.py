#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用无头浏览器跑 ui/_selftest.html，并把结果收回来。

为什么要这么绕（而不是 --dump-dom 一把梭）：
无头模式的 `--dump-dom` / `--screenshot` **只在页面 load 那一刻触发一次**，
而自检要等异步对话结束；`--virtual-time-budget` 又会把 setTimeout 快进
（实测把 15 秒轮询压成几十毫秒，直接误报超时）。
所以自检页每完成一项就 `fetch('/ui/__report?r=<json>')`，
桥把请求行写进 `logs/bridge.log` —— 这里从日志里读结果。

另外：**只杀自己那棵进程树**（`taskkill /F /T /PID`），绝不 `taskkill /IM msedge.exe`，
那会把用户自己开着的浏览器一起做掉。

用法：python scripts\\run_ui_selftest.py [最长等待秒数]
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)

import winrun  # noqa: E402

LOG = os.path.join(ROOT, "logs", "bridge.log")
URL = "http://127.0.0.1:11434/ui/_selftest.html"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
WAIT = int(sys.argv[1]) if len(sys.argv) > 1 else 420


def start_service():
    """**restart 而不是 start**：这个驱动是在「刚改完代码」时用的，
    而 start 遇到已在运行的服务会直接跳过 —— 于是浏览器跑的是新界面、
    后端还是旧代码，出现「界面发了请求、后端 200 但什么都没变」的假象（踩过）。
    """
    py = sys.executable
    winrun.run([py, os.path.join(ROOT, "scripts", "service_ctl.py"), "restart"], timeout=240)


def new_log_lines(offset):
    """读到 offset 之后的新日志，同时返回新的 offset。"""
    if not os.path.exists(LOG):
        return "", 0
    size = os.path.getsize(LOG)
    if size < offset:            # 日志被截断/轮转过，从头读
        offset = 0
    with open(LOG, "rb") as fh:
        fh.seek(offset)
        raw = fh.read()
    return raw.decode("utf-8", "replace"), max(size, offset)


def parse_reports(text):
    """从日志行里把 __report?r=<urlencoded json> 抠出来。"""
    out = []
    for line in text.splitlines():
        if "__report?r=" not in line:
            continue
        seg = line.split("__report?r=", 1)[1]
        seg = seg.split(" HTTP/")[0].strip()
        try:
            out.append(json.loads(urllib.parse.unquote(seg)))
        except Exception:
            out.append({"t": "?", "m": seg[:120]})
    return out


def main():
    print("  1) 确保服务在跑…")
    start_service()
    offset = os.path.getsize(LOG) if os.path.exists(LOG) else 0

    profile = tempfile.mkdtemp(prefix="ui-selftest-")
    print("  2) 启动无头浏览器（独立 profile，跑完只杀自己这棵树）…")
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         "--disable-extensions", "--mute-audio", "--window-size=1400,1000",
         "--user-data-dir=" + profile, URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=winrun.CREATE_NO_WINDOW)

    print(f"  3) 等自检跑完（最多 {WAIT}s，看到 DONE/FAILED 就提前结束）…")
    reports, done, t0 = [], None, time.time()
    while time.time() - t0 < WAIT:
        time.sleep(3)
        text, offset = new_log_lines(offset)
        reports += parse_reports(text)
        if any(r.get("t") in ("DONE", "FAILED") for r in reports):
            done = time.time() - t0
            break
    elapsed = time.time() - t0

    print("  4) 收尾：只结束这个浏览器实例（含子进程）…")
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                   capture_output=True, timeout=60)

    # 等日志落完（最后一次 fetch 可能还在路上）
    time.sleep(1.5)
    text, offset = new_log_lines(offset)
    reports += parse_reports(text)

    print()
    if not reports:
        print("  !! 一条结果都没收到 —— 自检页可能根本没跑起来")
        print("     （先手工打开 http://127.0.0.1:11434/ui/_selftest.html 看看）")
        return 1

    for r in reports:
        tag, msg = r.get("t"), r.get("m", "")
        mark = {"pass": "  [通过]", "fail": "  [失败]"}.get(tag, "  ·")
        if tag in ("pass", "fail", "start", "DONE", "FAILED", "chat-sent"):
            print(f"{mark} {msg[:150]}")

    fails = [r for r in reports if r.get("t") == "fail"]
    print()
    print(f"  用时 {elapsed:.0f}s，收到 {len(reports)} 条记录，失败 {len(fails)} 条")
    if not done:
        print("  !! 没看到结束标记 DONE/FAILED —— 可能是超时，也可能自检中途断了")
    return 1 if fails or not done else 0


if __name__ == "__main__":
    sys.exit(main())
