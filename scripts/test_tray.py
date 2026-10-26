# -*- coding: utf-8 -*-
"""托盘动作回归测试：直接调 Tray.on_command()，跑「停止 / 启动 / 重启 / 自启开关」。

**要点：动作走的是处理器本身，不是发窗口消息。**
托盘菜单用 TrackPopupMenu + TPM_RETURNCMD，命令 ID 从返回值拿、直接交给 on_command；
所以 wndproc 里的 WM_COMMAND 分支只是给自动化留的入口。
（第一版测试就是发 WM_COMMAND 去的 —— 消息被 DefWindowProc 吞掉，
再叠加「服务本来就在跑」造成的假阳性，看起来像 2/4 通过。）

直接在进程内调处理器还有个好处：不依赖托盘进程活着
（agent shell 里拉起来的进程会被作业对象回收，见用户级 MEMORY）。

不测 ID_OPEN_UI / ID_OPEN_PANEL（会开浏览器）和 ID_STATUS（模态框会阻塞）。

用法：python scripts\\test_tray.py      需要 Python 3.11（有 pywin32）
"""
import os
import socket
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

import tray  # noqa: E402  依赖自举在模块级，正常环境直接过
import win32con  # noqa: E402
import win32gui  # noqa: E402

ID_START, ID_STOP, ID_RESTART, ID_AUTOSTART = 1003, 1004, 1005, 1007


def up(port):
    s = socket.socket()
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def wait(pred, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.6)
    print(f"       !! 超时：{what}")
    return False


def autostart_value():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            return winreg.QueryValueEx(k, "LlamaCppLocalService")[0]
    except FileNotFoundError:
        return None


def main():
    t = tray.Tray()
    t.state = tray.probe()
    print(f"  起始状态: {t.state}   autostart_on={t.autostart_on}")
    ok = total = 0

    # ---- 1) 停止
    total += 1
    print("  [1] on_command(ID_STOP) → 等端口关闭…")
    t.on_command(ID_STOP)
    wait(lambda: not up(11434) and not up(11435), 5, "")  # 后台线程已启动，先给它一点时间
    if wait(lambda: not up(11434) and not up(11435), 90, "停止"):
        print("       ✓ 两个端口都已关闭")
        ok += 1
    else:
        print(f"       现状 11434={up(11434)} 11435={up(11435)}")

    # ---- 2) 启动
    total += 1
    print("  [2] on_command(ID_START) → 等端口打开…")
    t.on_command(ID_START)
    if wait(lambda: up(11434) and up(11435), 120, "启动"):
        print("       ✓ 两个端口都已打开")
        ok += 1

    # ---- 3) 重启
    total += 1
    print("  [3] on_command(ID_RESTART) → 等重新就绪…")
    t.on_command(ID_RESTART)
    time.sleep(6)
    if wait(lambda: up(11434) and up(11435), 120, "重启"):
        print("       ✓ 重启后两个端口都在")
        ok += 1

    # ---- 4) 开机自启（读当前态 → 点一次 → 值应翻转）
    total += 1
    before = autostart_value()
    print(f"  [4] 当前自启={bool(before)}，点一次 ID_AUTOSTART…")
    t.on_command(ID_AUTOSTART)
    time.sleep(3)
    after = autostart_value()
    print(f"       点后={after}")
    if bool(before) != bool(after):
        print("       ✓ 自启开关翻转成功")
        ok += 1
        t.on_command(ID_AUTOSTART)          # 还原
        time.sleep(3)
        print(f"       已还原为 {'启用' if autostart_value() else '关闭'}")
    else:
        print("       !! 没有翻转")

    # ---- 5) 右键菜单能否构建、条目是否齐全
    # 这一项覆盖的是「菜单本身」——上面 4 步只走命令处理器，碰不到 menu()。
    # 曾经因为 `AppendMenu(m, MF_SEPARATOR, 0, None)`（None 被 pywin32 拒绝）
    # 让整个菜单建不出来、右键毫无反应，就是漏了这一步。
    total += 1
    print("  [5] 构建右键菜单并核对条目…")
    try:
        import ctypes
        u32 = ctypes.windll.user32
        u32.GetMenuStringW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p,
                                       ctypes.c_int, ctypes.c_uint]
        buf = ctypes.create_unicode_buffer(128)

        def item_text(hmenu, idx):
            # 这个 pywin32 版本没有 win32gui.GetMenuString，直接调 user32
            n = u32.GetMenuStringW(hmenu, idx, buf, 128, win32con.MF_BYPOSITION)
            return buf.value if n else ""

        m = t.menu()
        n = win32gui.GetMenuItemCount(m)
        texts = [item_text(m, i) for i in range(n)]
        win32gui.DestroyMenu(m)
        expected = ["打开对话界面", "打开模型面板", "", "启动服务", "停止服务",
                    "重启服务", "查看状态…", "", "开机自启", "", "退出托盘"]
        if texts == expected:
            print(f"       ✓ {n} 项，内容与预期一致")
            ok += 1
        else:
            print("       !! 条目不符")
            print(f"          实际 {texts}")
            print(f"          预期 {expected}")
    except Exception as exc:
        print(f"       !! 构建菜单抛异常: {type(exc).__name__}: {exc}")

    # ---- 6) 空闲状态下退出不该弹确认框
    # 用守护线程 + 超时：万一哪天又加了确认框，quit() 会阻塞在 MessageBox 上，
    # 直接调用会把测试挂死；这里最多等 2 秒就判失败。
    total += 1
    print("  [6] 空闲状态下 quit() 应直接返回、不弹确认框…")
    import threading
    src = open(os.path.join(ROOT, "scripts", "tray.py"), encoding="utf-8").read()
    t.state = "off"
    done = []
    th = threading.Thread(target=lambda: (t.quit(), done.append(True)), daemon=True)
    t0 = time.time()
    th.start()
    th.join(2.0)
    dt = time.time() - t0
    if done:
        print(f"       ✓ 立即返回（{dt:.2f}s，无对话框阻塞）")
        ok += 1
    else:
        print("       !! 卡住了 —— 八成是又弹了确认框（quit() 里不该有 MB_* 阻塞）")
    if "确定退出托盘" in src:
        print("       !! 源码里还留着旧的确认框文案「确定退出托盘」")

    # ---- 7) 重复启动应当安静退出、不弹框
    # 只在「本机已经有托盘在运行」时可测；否则新实例会变成那个托盘、一直不退。
    print("  [7] 再启动一个托盘实例：应当安静退出、不弹任何框…")
    existing = win32gui.FindWindow("LlamaCppTrayWnd", tray.APP_NAME)
    if not existing:
        print("       · 本机此刻没有托盘在运行，跳过此项（跑了就变成「启动托盘」了）")
    else:
        total += 1

        def dialog_count():
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

        pyw = sys.executable.replace("python.exe", "pythonw.exe")
        before_dlg = dialog_count()
        before_hwnd = existing
        proc = subprocess.Popen(
            [pyw, os.path.join(ROOT, "scripts", "tray.py")], cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=tray.CREATE_NO_WINDOW)
        exited, rc = False, None
        try:
            rc = proc.wait(timeout=40)
            exited = True
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(1.5)
        after_dlg = dialog_count()
        after_hwnd = win32gui.FindWindow("LlamaCppTrayWnd", tray.APP_NAME)
        if exited and rc == 0 and after_dlg == before_dlg and after_hwnd == before_hwnd:
            print("       ✓ 安静退出（返回码 0），没弹框，原托盘未受影响")
            ok += 1
        else:
            print(f"       !! exited={exited} rc={rc} 对话框 {before_dlg}->{after_dlg} "
                  f"托盘窗口 {before_hwnd}->{after_hwnd}")

    print(f"\n  托盘动作：{ok}/{total} 通过")
    print("  （右键菜单的真实弹出另见 scripts\\test_tray_menu.py）")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
