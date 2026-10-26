#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统托盘：本地模型服务的启动 / 停止 / 状态。

依赖：pywin32 + Pillow（用户机器上已有；缺了会自己去找带 pywin32 的 python 重启自己）。
服务控制一律走 `service_ctl.py`，不在托盘里另写一套启动逻辑。

用法：
    pythonw scripts\\tray.py        正常启动（无控制台）
    python  scripts\\tray.py        带控制台，方便看报错

托盘操作：
    双击 / 左键  打开对话界面
    右键          菜单：启动 / 停止 / 重启 / 状态 / 界面 / 开机自启 / 退出
    图标颜色      绿=正常，黄=只有一个在跑（半开），灰=已停止，蓝=正在启动
"""
import os
import subprocess
import sys
import ctypes
import tempfile
import threading
import time
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTL = os.path.join(ROOT, "scripts", "service_ctl.py")
BRIDGE_PORT = 11434
SWAP_PORT = 11435
UI_URL = f"http://127.0.0.1:{BRIDGE_PORT}/ui"
PANEL_URL = f"http://127.0.0.1:{SWAP_PORT}/ui"
APP_NAME = "本地模型服务"

PY_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python313\python.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python312\python.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python311\python.exe"),
]

# 进程创建标志。CREATE_NO_WINDOW 尤其重要：从 pythonw（无控制台）启动的任何
# 控制台子进程，不加它就会各自新建一个控制台窗口 —— 用户会看到一串黑框一闪。
# **不要**再叠 DETACHED_PROCESS / CREATE_NEW_CONSOLE：与它们同用时
# CREATE_NO_WINDOW 会被 Windows 忽略，照样弹窗口。
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _can_import_win32(py):
    try:
        # 这个探测是从 pythonw 里发起的，不加 CREATE_NO_WINDOW 会闪一个黑框
        return subprocess.run([py, "-c", "import win32gui, win32con, PIL.Image"],
                              capture_output=True, timeout=25,
                              creationflags=CREATE_NO_WINDOW).returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------- 依赖自举
try:
    import win32api
    import win32con
    import win32event
    import win32gui
    from PIL import Image, ImageDraw
except ImportError:
    # 换一个带 pywin32 的 python 重新跑自己（用 pythonw 免得弹控制台）
    if os.environ.get("LLAMA_TRAY_RELAUNCHED") != "1":
        for cand in PY_CANDIDATES + [sys.executable]:
            if not cand or not os.path.exists(cand):
                continue
            if not _can_import_win32(cand):
                continue
            gui = cand.replace("python.exe", "pythonw.exe")
            exe = gui if os.path.exists(gui) else cand
            env = dict(os.environ, LLAMA_TRAY_RELAUNCHED="1")
            # 同样不要叠 DETACHED_PROCESS：它会让 CREATE_NO_WINDOW 失效
            subprocess.Popen([exe, os.path.abspath(__file__)], env=env, cwd=ROOT,
                             creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
            sys.exit(0)
    sys.stderr.write("托盘需要 pywin32 和 Pillow，找不到可用的 Python。\n"
                     "装一下： pip install pywin32 pillow\n")
    sys.exit(2)


WM_TRAY = win32con.WM_USER + 20
WM_TRAY_NOTIFY = WM_TRAY + 1

ID_OPEN_UI = 1001
ID_OPEN_PANEL = 1002
ID_START = 1003
ID_STOP = 1004
ID_RESTART = 1005
ID_STATUS = 1006
ID_AUTOSTART = 1007
ID_EXIT = 1008

COLOR_OK = (46, 158, 91)        # 绿
COLOR_HALF = (196, 127, 23)     # 黄
COLOR_OFF = (140, 148, 158)     # 灰
COLOR_BUSY = (47, 111, 237)     # 蓝

# win32gui 没有 SetTimer/KillTimer，直接调 user32
_user32 = ctypes.windll.user32

TRAY_LOG = os.path.join(ROOT, "logs", "tray.log")


def log(msg):
    """托盘是后台程序、没有控制台 —— 出错必须落盘，否则无从查起。"""
    try:
        os.makedirs(os.path.dirname(TRAY_LOG), exist_ok=True)
        with open(TRAY_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def port_up(port, timeout=0.35):
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def probe():
    """轻量状态：只探端口，别在 UI 线程里跑 tasklist / HTTP。"""
    b, s = port_up(BRIDGE_PORT), port_up(SWAP_PORT)
    if b and s:
        return "ok"
    if b or s:
        return "half"
    return "off"


class Tray:
    def __init__(self):
        self.hwnd = None
        self.hicons = {}
        self.state = "off"
        self.busy = False
        self.busy_note = ""
        self.autostart_on = False
        self._ico_dir = os.path.join(tempfile.gettempdir(), "llama-cpp-tray")
        os.makedirs(self._ico_dir, exist_ok=True)

    # ------------------------------------------------------------ 图标
    def hicon(self, color):
        key = color
        if key in self.hicons:
            return self.hicons[key]
        path = os.path.join(self._ico_dir, "dot-%02x%02x%02x.ico" % color)
        if not os.path.exists(path):
            # 生成过就复用：省一次 PIL 往返，也少写一次盘
            img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((2, 2, 29, 29), fill=color + (255,), outline=(255, 255, 255, 210), width=2)
            img.save(path, format="ICO", sizes=[(16, 16), (32, 32)])
        h = win32gui.LoadImage(0, path, win32con.IMAGE_ICON, 0, 0,
                               win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
        self.hicons[key] = h
        return h

    def color_for(self, state):
        return {"ok": COLOR_OK, "half": COLOR_HALF, "off": COLOR_OFF}.get(state, COLOR_OFF)

    def tip(self):
        if self.busy:
            return f"{APP_NAME} · {self.busy_note}"
        label = {"ok": "运行中", "half": "半开（只有一个在跑）", "off": "已停止"}[self.state]
        return (f"{APP_NAME} · {label}\n"
                f"双击打开对话界面\n"
                f"右键查看菜单")

    # ------------------------------------------------------------ 托盘图标
    def add_icon(self):
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        nid = (self.hwnd, 0, flags, WM_TRAY_NOTIFY, self.hicon(self.color_for(self.state)), self.tip())
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
        self._added = True

    def update_icon(self):
        # 必须先 ADD 过才能 MODIFY —— 顺序反了会得到
        # pywintypes.error «Shell_NotifyIcon 未指定的错误»（踩过）。
        if not getattr(self, "_added", False):
            return
        flags = win32gui.NIF_ICON | win32gui.NIF_TIP
        nid = (self.hwnd, 0, flags, WM_TRAY_NOTIFY, self.hicon(self.color_for(self.state)), self.tip())
        win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)

    def remove_icon(self):
        if not self.hwnd:
            return                      # 没有窗口时无从删起，也别污染日志
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
        except Exception as exc:
            log(f"移除托盘图标失败: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ 动作
    def ctl(self, *args, background=True, note=""):
        """调用 service_ctl.py。默认放到线程里，避免卡住托盘。"""
        exe = sys.executable
        gui = exe.replace("python.exe", "pythonw.exe")
        if os.path.exists(gui):
            exe = gui
        cmd = [exe, CTL, *args]

        def work():
            self.busy = True
            self.busy_note = note or "处理中…"
            self.update_icon()
            try:
                env = dict(os.environ)
                env.pop("PYTHONPATH", None)
                env["PYTHONUTF8"] = "1"
                # 告诉 service_ctl「这次是托盘自己调的」——
                # 否则它会因为「托盘在跑」把动作再转交回托盘，形成死循环。
                env["LLAMA_SVC_OWNED_BY_TRAY"] = "1"
                subprocess.run(cmd, cwd=ROOT, env=env, timeout=120,
                               creationflags=CREATE_NO_WINDOW)
            except Exception as exc:
                self.notify("操作出错", str(exc))
            finally:
                self.busy = False
                self.busy_note = ""
                self.refresh()

        if background:
            threading.Thread(target=work, daemon=True).start()
        else:
            work()

    def refresh(self, force=False):
        st = probe()
        if st != self.state:
            self.state = st
        # 自启状态要起子进程（几十毫秒），别每 5 秒都查一次，60 秒一次够了。
        # 但 force=True 必须绕过节流 —— 刚点完「开机自启」得立刻回读，
        # 否则菜单勾选是旧的，下一次点击还会基于旧值再点同一个方向（连点两次 = 没反应）。
        self._ticks = getattr(self, "_ticks", 0) + 1
        if force or self._ticks % 12 == 1:
            try:
                self.autostart_on = "已启用" in subprocess.run(
                    [sys.executable, CTL, "autostart", "status"],
                    capture_output=True, timeout=20,
                    env={**os.environ, "PYTHONUTF8": "1"},
                    creationflags=CREATE_NO_WINDOW).stdout.decode("utf-8", "replace")
            except Exception as exc:
                log(f"查询自启状态失败: {type(exc).__name__}: {exc}")
        self.update_icon()

    def notify(self, title, text):
        if not self.hwnd:
            # 没有窗口（例如被当库调用做测试）时气泡无从谈起。
            # 必须早退：否则会往 logs/tray.log 写一堆假的「气泡通知失败」，
            # 让真正需要排查时这条日志失去可信度（踩过）。
            return
        try:
            flags = win32gui.NIF_INFO
            nid = (self.hwnd, 0, flags, WM_TRAY_NOTIFY, self.hicon(self.color_for(self.state)),
                   self.tip(), text, 5000, title, win32gui.NIIF_INFO)
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)
        except Exception as exc:
            # Win11 常把气泡折叠进通知中心，失败也别静默掉
            log(f"气泡通知失败: {type(exc).__name__}: {exc}")

    def show_status(self):
        st = probe()
        label = {"ok": "运行中", "half": "半开（只有一个在跑）", "off": "已停止"}[st]
        b = "运行中" if port_up(BRIDGE_PORT) else "已停止"
        s = "运行中" if port_up(SWAP_PORT) else "已停止"
        text = (f"整体状态：{label}\n\n"
                f"对话界面（{BRIDGE_PORT}）：{b}\n"
                f"模型交换器（{SWAP_PORT}）：{s}\n\n"
                f"界面地址：{UI_URL}\n"
                f"模型面板：{PANEL_URL}")
        win32gui.MessageBox(self.hwnd, text, APP_NAME + " · 状态",
                            win32con.MB_OK | win32con.MB_ICONINFORMATION)

    def menu(self):
        m = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(m, win32con.MF_STRING, ID_OPEN_UI, "打开对话界面")
        win32gui.AppendMenu(m, win32con.MF_STRING, ID_OPEN_PANEL, "打开模型面板")
        # 分隔符的文本必须是空串：传 None 会被 pywin32 拒绝
        # （TypeError: None is not a valid string in this context），
        # 结果是整个菜单建不出来、右键毫无反应 —— 踩过。
        win32gui.AppendMenu(m, win32con.MF_SEPARATOR, 0, "")
        running = self.state == "ok"
        win32gui.AppendMenu(m, win32con.MF_STRING | (win32con.MF_GRAYED if running else 0),
                            ID_START, "启动服务")
        win32gui.AppendMenu(m, win32con.MF_STRING | (win32con.MF_GRAYED if not running else 0),
                            ID_STOP, "停止服务")
        win32gui.AppendMenu(m, win32con.MF_STRING, ID_RESTART, "重启服务")
        win32gui.AppendMenu(m, win32con.MF_STRING, ID_STATUS, "查看状态…")
        # 分隔符的文本必须是空串：传 None 会被 pywin32 拒绝
        # （TypeError: None is not a valid string in this context），
        # 结果是整个菜单建不出来、右键毫无反应 —— 踩过。
        win32gui.AppendMenu(m, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(m, win32con.MF_STRING | (win32con.MF_CHECKED if self.autostart_on else 0),
                            ID_AUTOSTART, "开机自启")
        # 分隔符的文本必须是空串：传 None 会被 pywin32 拒绝
        # （TypeError: None is not a valid string in this context），
        # 结果是整个菜单建不出来、右键毫无反应 —— 踩过。
        win32gui.AppendMenu(m, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(m, win32con.MF_STRING, ID_EXIT, "退出托盘")
        return m

    def on_command(self, cmd):
        if cmd == ID_OPEN_UI:
            if self.state == "off":
                self.notify("服务没在跑", "先「启动服务」，再打开界面。")
            else:
                webbrowser.open(UI_URL)
        elif cmd == ID_OPEN_PANEL:
            webbrowser.open(PANEL_URL)
        elif cmd == ID_START:
            self.ctl("start", background=True, note="正在启动…")
        elif cmd == ID_STOP:
            self.ctl("stop", background=True, note="正在停止…")
        elif cmd == ID_RESTART:
            self.ctl("restart", background=True, note="正在重启…")
        elif cmd == ID_STATUS:
            self.show_status()
        elif cmd == ID_AUTOSTART:
            self.ctl("autostart", "off" if self.autostart_on else "on",
                     background=False)
            self.refresh(force=True)
            # 回读之后 autostart_on 就是**当前**状态：True 表示刚开启
            self.notify("开机自启", "已开启" if self.autostart_on else "已关闭")
        elif cmd == ID_EXIT:
            self.quit()

    def quit(self):
        running = self.state != "off"
        # 什么都不在跑就直接退，不弹确认框 —— 用户明确要求「尽量一个都别弹」。
        # 只有「退出顺带要停掉还在跑的服务」这种有副作用的动作才值得问一句。
        if running:
            r = win32gui.MessageBox(self.hwnd, "服务还在运行，要一起停止吗？", APP_NAME,
                                    win32con.MB_YESNO | win32con.MB_ICONQUESTION)
            if r != win32con.IDYES:
                return
        self.remove_icon()
        if running:
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env["PYTHONUTF8"] = "1"
            subprocess.run([sys.executable, CTL, "stop"], cwd=ROOT, env=env,
                           timeout=60, creationflags=CREATE_NO_WINDOW)
        win32gui.PostQuitMessage(0)

    # ------------------------------------------------------------ 消息循环
    def wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY_NOTIFY:
            if lparam in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
                m = self.menu()
                # SetForegroundWindow 会失败（后台进程不许抢前台，Windows 正常限制，
                # 真实使用中也会失败）。它抛的异常会**中断整个窗口过程**，导致下面的
                # TrackPopupMenu 根本不执行 → 右键毫无反应。所以必须包住并继续。
                # 这是预期内的情况，**不要记日志**，否则每点一次右键就污染一条 ——
                # 那条日志要留给真正的异常。
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
                pos = win32gui.GetCursorPos()
                cmd = win32gui.TrackPopupMenu(
                    m, win32con.TPM_LEFTALIGN | win32con.TPM_RETURNCMD | win32con.TPM_NONOTIFY,
                    pos[0], pos[1], 0, hwnd, None)
                win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
                win32gui.DestroyMenu(m)
                if cmd:
                    self.on_command(cmd)
            elif lparam in (win32con.WM_LBUTTONDBLCLK, win32con.WM_LBUTTONUP):
                self.on_command(ID_OPEN_UI)
            return 0
        if msg == win32con.WM_COMMAND:
            # 菜单本身走 TPM_RETURNCMD（返回值直接给 on_command），不经过这里。
            # 这个分支是给自动化留的入口：PostMessage(hwnd, WM_COMMAND, ID_xxx, 0)
            # 就能驱动**正在运行的那个托盘**，比另起一个实例测更真实。
            self.on_command(wparam & 0xFFFF)
            return 0
        if msg == win32con.WM_TIMER:
            self.refresh()
            return 0
        if msg == win32con.WM_DESTROY:
            _user32.KillTimer(hwnd, 1)
            self.remove_icon()
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def run(self):
        hinst = win32api.GetModuleHandle(None)
        cls = "LlamaCppTrayWnd"
        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = cls
        wc.lpfnWndProc = self.wndproc
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wc.hbrBackground = win32con.COLOR_WINDOW

        # 抢互斥体要放在最前面：已经有实例时**直接安静退出**。
        #  - 不弹框：用户明确要求「一个都不弹」，而且通知区域里本来就有图标，
        #    重复双击什么都没发生才是不打扰的做法（改动前这里弹的是模态框，
        #    一旦弹出来还会把新实例的窗口过程卡死）。
        #  - 也不先建窗口：否则会短暂出现一个「有窗口但没有托盘」的空壳，
        #    外部用 FindWindow 找托盘就会找到它（踩过）。
        mutex = win32event.CreateMutex(None, False, "LlamaCppTrayMutex")
        if win32api.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            log("已有托盘实例在运行，本实例直接退出（不弹框）")
            return 0

        try:
            win32gui.RegisterClass(wc)
        except win32gui.error:
            pass                      # 已注册过
        self.hwnd = win32gui.CreateWindow(cls, APP_NAME, win32con.WS_OVERLAPPED,
                                          0, 0, 0, 0, 0, 0, hinst, None)

        # 顺序要紧：先 ADD 图标再 refresh（refresh 里会 MODIFY）
        self.state = probe()
        self.add_icon()
        self.refresh()
        _user32.SetTimer(self.hwnd, 1, 5000, None)

        # 双击托盘就是「把服务管起来」，所以默认顺手拉起服务；
        # 只想看状态就加 --no-start。
        if "--no-start" not in sys.argv and self.state == "off":
            self.ctl("start", background=True, note="正在启动…")

        win32gui.PumpMessages()
        return 0


if __name__ == "__main__":
    sys.exit(Tray().run())
