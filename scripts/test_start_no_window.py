# -*- coding: utf-8 -*-
"""端到端：跑一次真实的服务启动，确认**一个可见窗口都不会冒出来**。

为什么单独一个测试：`check_no_window.py` 验的是「探针子进程有没有控制台」，
而用户实际看到的是「屏幕上有没有窗口」。两者不等价（`cmd.exe` 会自己分配控制台、
python 不会），所以要用**产品真实路径**测一遍：
    停止服务 → 记录当前可见控制台窗口 → 用 pythonw 跑 service_ctl.py start
    → 再记录一次 → 断言没有新增可见窗口。

这条测试能抓住的典型回归：给启动加回 DETACHED_PROCESS / CREATE_NEW_CONSOLE
（它们会让 CREATE_NO_WINDOW 失效，llama-swap 就会留下一个空的常驻黑框）。

用法：python scripts\\test_start_no_window.py      需要 Python 3.11（有 pywin32）
"""
import json
import os
import subprocess
import sys
import time
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)

import win32gui  # noqa: E402
import winrun  # noqa: E402

CTL = os.path.join(ROOT, "scripts", "service_ctl.py")
CONSOLE_CLASSES = ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS",
                   "PseudoConsoleWindow")


def visible_consoles():
    found = []

    def cb(h, _):
        try:
            cls = win32gui.GetClassName(h)
        except Exception:
            return
        if (cls in CONSOLE_CLASSES or "Terminal" in cls) and win32gui.IsWindowVisible(h):
            # 标题会变（Windows Terminal 复用窗口），所以键里带上 hwnd
            found.append((cls, win32gui.GetWindowText(h)[:70]))

    win32gui.EnumWindows(cb, None)
    return Counter(found)


def run_ctl(*args):
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return winrun.run([pyw, CTL, *args], timeout=180)


def main():
    ok = total = 0

    print("  1) 先停服务，拿到干净的基线…")
    run_ctl("stop")
    time.sleep(2)
    base = visible_consoles()
    print(f"     基线可见控制台窗口: {sum(base.values())} 个")

    print("  2) 用 pythonw 跑 service_ctl.py start（与托盘、双击完全同一条路径）…")
    r = run_ctl("start")
    time.sleep(4)                     # 给窗口机会出现（如果有的话）
    after = visible_consoles()
    new = after - base

    total += 1
    if not new:
        print("     ✓ 没有新增可见窗口")
        ok += 1
    else:
        print(f"     ✗ 多出 {sum(new.values())} 个可见窗口：")
        for (cls, title), n in new.items():
            print(f"         class={cls} title={title!r} ×{n}")

    print("  3) 服务是否真的起来了…")
    total += 1
    st = json.loads(winrun.decode(run_ctl("status", "--json").stdout) or "{}")
    if st.get("running"):
        print(f"     ✓ 桥与 llama-swap 都在跑（可调用名称 {st.get('models')} 个）")
        ok += 1
    else:
        print(f"     ✗ 服务没起来: {st}")

    print(f"\n  结果：{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
