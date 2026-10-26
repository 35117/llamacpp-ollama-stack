# -*- coding: utf-8 -*-
"""托盘右键菜单的**真实弹出**测试。

为什么单独一个文件：`test_tray.py` 是在进程内调 `on_command()`，
碰不到 `menu()`、也碰不到 `wndproc` 里处理右键的那一段。
而这个「真实路径」曾经藏着两个致命 bug（都是靠这个测法才发现的）：

1. `AppendMenu(m, MF_SEPARATOR, 0, None)` —— pywin32 拒绝 None，
   抛 `TypeError: None is not a valid string in this context`，整个菜单建不出来。
2. `win32gui.SetForegroundWindow(hwnd)` 抛 `pywintypes.error`
   （后台进程不许抢前台），异常**中断整个窗口过程** → `TrackPopupMenu` 根本没执行。

两个都表现为「右键毫无反应」。修好后本测试应报 ✓。

流程：启动服务 → 拉起托盘 → 发 WM_RBUTTONUP → 找类名 `#32768` 的弹出菜单窗口
→ 断言存在且尺寸合理 → 截图 → 结束托盘（TrackPopupMenu 会阻塞它，必须杀掉）。

用法：python scripts\\test_tray_menu.py            需要 Python 3.11（有 pywin32）
      python scripts\\test_tray_menu.py --no-shot  不截图
"""
import os
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)

import win32gui  # noqa: E402
import winrun  # noqa: E402  控制台辅助程序一律走它，免得多闪一个黑框

WM_TRAY_NOTIFY = 0x0400 + 20 + 1        # 与 tray.py 里的 WM_TRAY_NOTIFY 一致
WM_RBUTTONUP = 0x0205
CREATE_NO_WINDOW = 0x08000000
DETACHED, NEW_PROCESS_GROUP = 0x8, 0x200

PY311W = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe")
if not os.path.exists(PY311W):
    PY311W = sys.executable.replace("python.exe", "pythonw.exe")

CTL = os.path.join(ROOT, "scripts", "service_ctl.py")


def env_clean():
    e = dict(os.environ)
    e.pop("PYTHONPATH", None)
    e["PYTHONUTF8"] = "1"
    return e


def find_menu_window():
    """弹出菜单是一个独立顶层窗口，类名固定为 #32768。"""
    hits = []

    def cb(h, _):
        if win32gui.GetClassName(h) == "#32768":
            r = win32gui.GetWindowRect(h)
            if r[2] - r[0] > 60 and r[3] - r[1] > 60:
                hits.append((h, r))

    win32gui.EnumWindows(cb, None)
    return hits[0] if hits else (None, None)


def count_dialogs():
    """可见的模态对话框数量（#32770 是 MessageBox 的窗口类）。"""
    n = 0

    def cb(h, _):
        nonlocal n
        try:
            if win32gui.GetClassName(h) == "#32770" and win32gui.IsWindowVisible(h):
                n += 1
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    return n


def main():
    shot = "--no-shot" not in sys.argv
    ok = total = 0

    # 已经有托盘在跑就跳过：新实例会撞互斥体（旧代码那里会弹模态框），
    # 而且 FindWindow 找到的是**别人**的窗口，右键菜单会弹到用户正在用的实例上。
    existing = win32gui.FindWindow("LlamaCppTrayWnd", "本地模型服务")
    if existing:
        print(f"  已有托盘在运行（hwnd={existing}），跳过。")
        print("  本项要在「没有托盘」的环境下跑，否则会互相干扰。请先退出托盘再试。")
        return 2

    print("  1) 启动服务（菜单状态才有意义）…")
    subprocess.run([sys.executable, CTL, "start"], cwd=ROOT, timeout=180,
                   env=env_clean(), creationflags=CREATE_NO_WINDOW)

    print("  2) 拉起托盘…")
    tray_proc = subprocess.Popen(
        [PY311W, os.path.join(ROOT, "scripts", "tray.py")], cwd=ROOT, env=env_clean(),
        stdout=open(os.path.join(ROOT, "logs", "tray.log"), "ab", buffering=0),
        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=DETACHED | NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
    time.sleep(8)

    hwnd = win32gui.FindWindow("LlamaCppTrayWnd", "本地模型服务")
    total += 1
    if hwnd:
        print(f"       ✓ 托盘窗口 hwnd={hwnd}")
        ok += 1
    else:
        print("       !! 托盘窗口没建起来")
        print(f"\n  托盘菜单：{ok}/{total} 通过")
        return 1

    print("  3) 发右键消息 WM_RBUTTONUP…")
    win32gui.PostMessage(hwnd, WM_TRAY_NOTIFY, 0, WM_RBUTTONUP)
    time.sleep(3)

    mh, rect = find_menu_window()
    total += 1
    if mh:
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        print(f"       ✓ 菜单已弹出 hwnd={mh} @ {rect}（{w}×{h}）")
        ok += 1
        if shot:
            try:
                from PIL import ImageGrab
                img = ImageGrab.grab()
                pad = 30
                box = (max(0, rect[0] - pad), max(0, rect[1] - pad),
                       min(img.width, rect[2] + pad), min(img.height, rect[3] + pad))
                crop = img.crop(box)
                crop = crop.resize((crop.width * 2, crop.height * 2), 1)
                path = os.path.join(ROOT, "logs", "tray-menu.png")
                crop.save(path)
                print(f"       截图 {path}")
            except Exception as exc:
                print(f"       截图失败（不影响结论）: {exc}")
    else:
        print("       !! 没找到菜单窗口 —— 右键应该毫无反应，看 logs/tray.log")

    print("  3b) 再启动一个托盘实例：应当安静退出、不弹任何框…")
    dlg_before = count_dialogs()
    hwnd_before = win32gui.FindWindow("LlamaCppTrayWnd", "本地模型服务")
    second = subprocess.Popen(
        [PY311W, os.path.join(ROOT, "scripts", "tray.py")], cwd=ROOT, env=env_clean(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW)
    exited, rc = False, None
    try:
        rc = second.wait(timeout=40)
        exited = True
    except subprocess.TimeoutExpired:
        second.kill()
    time.sleep(1.5)
    dlg_after = count_dialogs()
    hwnd_after = win32gui.FindWindow("LlamaCppTrayWnd", "本地模型服务")
    total += 1
    if exited and rc == 0 and dlg_after == dlg_before and hwnd_after == hwnd_before:
        print("       ✓ 第二个实例安静退出（返回码 0），没有弹框，原托盘窗口未受影响")
        ok += 1
    else:
        print(f"       !! exited={exited} rc={rc} 对话框 {dlg_before}->{dlg_after} "
              f"托盘窗口 {hwnd_before}->{hwnd_after}")

    print("  4) 收尾：结束托盘（TrackPopupMenu 阻塞着，必须杀掉）")
    winrun.run(["taskkill", "/F", "/PID", str(tray_proc.pid)], timeout=30)
    time.sleep(1)
    # 按 pid 判断「我们起的那个」没了没有 —— 不能只看窗口存在与否
    out = winrun.decode(winrun.run(["tasklist", "/FI", f"PID eq {tray_proc.pid}",
                                    "/FO", "CSV", "/NH"], timeout=30).stdout)
    if str(tray_proc.pid) in out:
        print(f"       !! 进程 {tray_proc.pid} 还在")
    else:
        print("       ✓ 已清理")

    log = os.path.join(ROOT, "logs", "tray.log")
    if os.path.exists(log) and os.path.getsize(log):
        with open(log, encoding="utf-8", errors="replace") as fh:
            content = fh.read().strip()
        # 已知的无害输出：PIL/libpng 的 iCCP 噪声、以及「重复启动直接退出」的提示
        benign = ("libpng", "iCCP", "已有托盘实例在运行")
        errors = [l for l in content.splitlines()
                  if l.strip() and not any(b in l for b in benign)]
        noise = len(content.splitlines()) - len(errors)
        if noise:
            print(f"\n  （logs/tray.log 有 {noise} 行已知无害输出，已忽略）")
        if errors:
            print("\n  !! logs/tray.log 有内容（窗口过程里出过异常）：")
            print("    " + "\n    ".join(errors))
        else:
            print("\n  ✓ logs/tray.log 无真实异常")

    print(f"\n  托盘菜单：{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
