#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务控制：启动 / 停止 / 重启 / 状态 —— **单一真相源**。

为什么单独抽出来：启动逻辑原本写在 start-all.bat 里，托盘程序、计划任务、
自动化脚本如果各写一份，迟早会跑偏（端口、参数、日志路径对不上）。
现在 bat 脚本和托盘都调这里。

启动方式默认是**隐藏后台**（无控制台窗口）：
  - llama-swap 用 CREATE_NO_WINDOW（**不要**再叠 DETACHED_PROCESS，会让它失效）
  - 桥用 pythonw.exe（根本不创建控制台），输出重定向到 logs/bridge.log
`--console` 则让桥留在前台，方便调试。

用法：
    python service_ctl.py start              隐藏后台启动
    python service_ctl.py start --console    桥留在前台（调试用）
    python service_ctl.py stop               停止全部
    python service_ctl.py restart
    python service_ctl.py status             人看的状态
    python service_ctl.py status --json      机器读的状态
    python service_ctl.py autostart on|off|status   开机自启（HKCU Run）
    python service_ctl.py unload [模型名]            卸载驻留模型，释放显存（省略名字=全部）
    python service_ctl.py residency status         驻留概览（谁占着显存、闲置多久）
    python service_ctl.py residency get            打印驻留策略
    python service_ctl.py residency set idle_ttl=900 [auto_unload=false]
    python service_ctl.py residency reap           立刻跑一次闲置清理
    python service_ctl.py stop --force             额外按镜像名清理（会误伤自己起的进程）

驻留策略在 config/residency.json，改完热生效（reaper 每轮重读）。
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import port_tool  # noqa: E402  （复用端口探测/释放，避免两套实现）
import swap_ctl  # noqa: E402  （模型驻留查询/卸载，别再自己写 HTTP）
import residency  # noqa: E402 （驻留策略：闲置自动卸载 / 手动卸载）
import winrun  # noqa: E402    （所有控制台辅助程序都必须不弹窗口）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
LOGS = os.path.join(ROOT, "logs")
BRIDGE_PORT = 11434
SWAP_PORT = 11435

# python 定位：优先用户自己装的稳定版本，最后才用 PATH
PY_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python313\python.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python312\python.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python311\python.exe"),
]

# Windows 进程创建标志
# 注意：**故意不定义 DETACHED_PROCESS** —— 它会让 CREATE_NO_WINDOW 失效，
# 于是控制台程序（llama-swap.exe）会弹出空窗口。见 _spawn_flags() 的说明。
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _spawn_flags():
    """启动长期进程用的标志。

    **绝对不要加 DETACHED_PROCESS**：MSDN 明确写着 `CREATE_NO_WINDOW` 在与
    `DETACHED_PROCESS`（或 `CREATE_NEW_CONSOLE`）同用时**被忽略**。
    llama-swap.exe 是控制台程序，于是会**弹出一个空的、常驻的控制台窗口** ——
    用户报的「最后一个窗口一直不退、里面啥也没有」就是它（2026-09-28）。
    实测对照（无控制台的父进程下，靶子 `cmd /c ping -n 6`）：
        裸调用                              -> 2 个可见窗口
        CREATE_NO_WINDOW                    -> 0 个 ✓
        DETACHED_PROCESS | CREATE_NO_WINDOW -> 2 个可见窗口 ✗ ← 曾经的写法
    只留 CREATE_NO_WINDOW 也够「脱离」：子进程拿到的是**自己的隐藏控制台**，
    不继承父进程的，父窗口关掉也不影响它。
    """
    return CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


def _spawn(cmd, log_name, env_extra=None):
    """把子进程彻底放出去：无窗口、脱离父进程。

    会先试 CREATE_BREAKAWAY_FROM_JOB —— 如果父进程处在作业对象里
    （比如被 IDE / 沙箱托管），不加这个标志子进程会随作业一起被回收。
    作业不允许 breakaway 时 CreateProcess 会直接失败，那就退回普通标志。
    """
    os.makedirs(LOGS, exist_ok=True)
    logpath = os.path.join(LOGS, log_name)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)          # 别把 agent shell 的 shim 传进去
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)

    # 日志轮转：桥开了 log_requests 时客户端会 30s 一次心跳，长期跑不切割会一直涨
    _rotate_log(logpath)

    for extra in (CREATE_BREAKAWAY_FROM_JOB, 0):
        logfh = open(logpath, "ab", buffering=0)
        try:
            p = subprocess.Popen(cmd, cwd=ROOT, stdout=logfh,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 env=env, creationflags=_spawn_flags() | extra,
                                 close_fds=True)
            # 记下 PID：桥是 pythonw.exe，卡在启动中时端口还没监听，
            # 只按端口清理会漏掉它（见 stop() 的说明）
            _write_pid(os.path.join(LOGS, log_name + ".pid"), p.pid)
            return p.pid
        except OSError:
            logfh.close()
            if extra == 0:
                raise
    raise RuntimeError("无法启动子进程")


AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "LlamaCppLocalService"


# ------------------------------------------------- 作业对象 / 托盘中继
# 与 tray.py 的菜单 ID 保持一致
WM_COMMAND = 0x0111
TRAY_CMD_IDS = {"start": 1003, "stop": 1004, "restart": 1005}
TRAY_WND_CLASS = "LlamaCppTrayWnd"


def _in_job():
    """当前进程是否处在作业对象（job）里。

    处在作业里时，子进程会被作业连坐回收 —— 命令一结束，llama-swap 和桥
    一起消失。用户报的「你重启似乎就是关闭，并没有启动」就是这个：
    agent 的 shell 把命令放进作业，作业**不允许** breakaway 时
    `_spawn` 里的 CREATE_BREAKAWAY_FROM_JOB 会让 CreateProcess 直接失败，
    于是退回普通标志，服务留在作业里等着被回收。
    """
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        r = wintypes.BOOL()
        if not k32.IsProcessInJob(k32.GetCurrentProcess(), None, ctypes.byref(r)):
            return False
        return bool(r.value)
    except Exception:  # noqa: BLE001
        return False


def _tray_hwnd():
    """正在运行的托盘窗口句柄（类名与 tray.py 一致）；没有则 0。"""
    try:
        import ctypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.FindWindowW.restype = ctypes.c_void_p
        hwnd = u32.FindWindowW(TRAY_WND_CLASS, None)
        return int(hwnd) if hwnd else 0
    except Exception:  # noqa: BLE001
        return 0


def _tray_pid():
    """托盘窗口所属进程的 pid；没托盘则 0。"""
    hwnd = _tray_hwnd()
    if not hwnd:
        return 0
    try:
        import ctypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        pid = ctypes.c_ulong(0)
        u32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:  # noqa: BLE001
        return 0


def _parent_is_tray():
    """调用方是不是托盘自己。

    **不能只靠环境变量**：已经在运行的老托盘不会设它（代码是旧的），
    那样就会把动作转交回它自己 —— 托盘 → service_ctl → 托盘，无限循环。
    直接比对父进程 pid 才稳（tray.py 的 ctl() 是 pythonw 直接 exec service_ctl）。
    """
    tp = _tray_pid()
    if not tp:
        return False
    try:
        return os.getppid() == tp
    except Exception:  # noqa: BLE001
        return False


def _relay_to_tray(action, want_open, timeout=90):
    """托盘在跑时，把 start/restart 转交给它。

    为什么默认转交：从 agent / 自动化的 shell 里 `Popen` 出来的服务进程，会在
    **那条命令结束时被按进程树一起回收** —— 实测 `_in_job()` 是 False，
    所以 `CREATE_BREAKAWAY_FROM_JOB` 那条路救不了（它对「按树清理」无效）。
    现象正是用户报的「你重启似乎就是关闭，并没有启动」。

    托盘是用户自己起的进程，不在任何命令树里，让它去起服务最稳，
    而且与用户手点托盘右键菜单完全等价。

    例外：托盘自己调过来时**不能再转交**（否则托盘 → service_ctl → 托盘 死循环）。
    靠两个判据拦住：`LLAMA_SVC_OWNED_BY_TRAY`（tray.py 的 ctl() 会设）
    以及 `_parent_is_tray()`（对付还没更新的老托盘）。

    返回 (ok, steps)；不适用时返回 None，调用方照常自己起。
    """
    if os.environ.get("LLAMA_SVC_OWNED_BY_TRAY") == "1" or _parent_is_tray():
        return None
    hwnd = _tray_hwnd()
    if not hwnd:
        return None
    try:
        import ctypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.PostMessageW(ctypes.c_void_p(hwnd), WM_COMMAND, TRAY_CMD_IDS[action], 0)
    except Exception:  # noqa: BLE001
        return None

    why = "本进程在作业对象里" if _in_job() else "直接起会被本命令的进程树回收"
    steps = [f"托盘在运行（hwnd={hwnd}），已把 {action} 转交给它 —— "
             f"{why}，服务挂在托盘下、不受本命令结束影响"]

    if want_open:
        # ⚠️ restart 的起点通常就是「桥正在跑」，此时直接 wait_port 第一次轮询就会
        # 命中 → ok=True、"已就绪" 立刻返回 —— 而托盘可能还没开始 stop。
        # 失败被自己的成功信号盖住，这正是「你重启似乎就是关闭，并没有启动」
        # 能长期潜伏不被发现的原因。所以先等端口关闭（确认 stop 执行了），
        # 再等它重新打开。
        if port_tool.is_listening(BRIDGE_PORT):
            steps.append("等待旧服务释放端口…")
            if not wait_port_closed(BRIDGE_PORT, timeout):
                steps.append("!! 旧服务 %d 秒内没有释放端口 %d" % (timeout, BRIDGE_PORT))
                return False, steps
            steps.append("旧服务已停止")
        ok = wait_port(BRIDGE_PORT, timeout)
        steps.append(f"桥（{BRIDGE_PORT}）" + ("已就绪" if ok else f"{timeout} 秒内没起来，看 logs\\bridge.log"))
        return ok, steps

    deadline = time.time() + timeout
    while time.time() < deadline and port_tool.is_listening(BRIDGE_PORT):
        time.sleep(0.3)
    ok = not port_tool.is_listening(BRIDGE_PORT)
    steps.append(f"桥（{BRIDGE_PORT}）" + ("已停止" if ok else f"{timeout} 秒内仍在监听"))
    return ok, steps


def find_python(gui=False):
    """gui=True 时返回 pythonw.exe（不创建控制台）。"""
    for p in PY_CANDIDATES:
        cand = p.replace("python.exe", "pythonw.exe") if gui else p
        if os.path.exists(cand):
            return cand
    import shutil
    exe = shutil.which("pythonw.exe" if gui else "python.exe")
    return exe


def wait_port(port, timeout=40.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_tool.is_listening(port):
            return True
        time.sleep(0.3)
    return False


def wait_port_closed(port, timeout=40.0):
    """等端口**停止**监听 —— 用来确认 stop 真的执行完了。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_tool.is_listening(port):
            return True
        time.sleep(0.3)
    return not port_tool.is_listening(port)


def _pid_alive(pid):
    try:
        rc = winrun.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                        timeout=10)
    except Exception:      # noqa: BLE001  timeout / 命令缺失都算「查不到」
        return False
    return str(pid) in winrun.decode(rc.stdout or b"")


def _write_pid(path, pid):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(pid))
    except OSError:
        pass


def _read_pid(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except Exception:      # noqa: BLE001
        return None


def _clear_pid(name):
    try:
        os.remove(os.path.join(LOGS, name))
    except OSError:
        pass


def _rotate_log(logpath, max_bytes=4 * 1024 * 1024, keep=3):
    """超过 max_bytes 就轮转：x.log -> x.log.1 -> x.log.2 -> x.log.3（丢弃）。

    桥开了 log_requests 时客户端会 30s 一次心跳，长期跑不切割会一直涨，
    而且文件越大打开/写入越慢，排障时也被历史噪音淹没。
    """
    try:
        if not os.path.exists(logpath) or os.path.getsize(logpath) < max_bytes:
            return
    except OSError:
        return
    old = "%s.%d" % (logpath, keep)
    if os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            pass
    for i in range(keep - 1, -1, -1):
        src = logpath if i == 0 else "%s.%d" % (logpath, i)
        if os.path.exists(src):
            try:
                os.replace(src, "%s.%d" % (logpath, i + 1))
            except OSError:
                pass





def start(console_bridge=False):
    """返回 (是否成功, 说明列表)"""
    steps = []

    # 托盘在跑就交给它起 —— 自己起会被本命令的进程树回收（见 _relay_to_tray）
    if not console_bridge:
        relayed = _relay_to_tray("start", want_open=True)
        if relayed is not None:
            return relayed

    if port_tool.is_listening(BRIDGE_PORT):
        return False, [f"端口 {BRIDGE_PORT} 已被占用 —— Ollama 可能还在跑，"
                       f"或者服务本来就起着。先跑 service_ctl.py stop 或退出 Ollama。"]

    swap = os.path.join(BIN, "llama-swap.exe")
    server = os.path.join(BIN, "llama-server.exe")
    for f in (swap, server):
        if not os.path.exists(f):
            return False, [f"缺少 {f}"]

    # ---------- llama-swap（内部，11435）
    if port_tool.is_listening(SWAP_PORT):
        steps.append(f"llama-swap 已在 {SWAP_PORT} 监听，跳过")
    else:
        pid = _spawn([swap, "-config", os.path.join(ROOT, "config", "llama-swap.yaml"),
                      "-listen", f"127.0.0.1:{SWAP_PORT}"], "llama-swap.log")
        steps.append(f"llama-swap 已启动（pid {pid}，隐藏窗口）")
        if not wait_port(SWAP_PORT, 30):
            return False, steps + [f"llama-swap 30 秒内没起来，看 logs\\llama-swap.log"]

    # ---------- 桥（对外，11434）
    py = find_python(gui=not console_bridge)
    if not py:
        return False, steps + ["找不到 python/pythonw"]
    cmd = [py, os.path.join(ROOT, "scripts", "ollama_bridge.py"),
           "-config", os.path.join(ROOT, "config", "bridge.json")]

    if console_bridge:
        steps.append("桥在前台运行，Ctrl+C 结束它")
        print("\n".join("  " + s for s in steps))
        os.execv(py, cmd)          # 交给它，本进程就地替换
        return True, steps

    pid = _spawn(cmd, "bridge.log")
    steps.append(f"桥已启动（pid {pid}，无窗口）")
    if not wait_port(BRIDGE_PORT, 30):
        return False, steps + [f"桥 30 秒内没起来，看 logs\\bridge.log"]
    return True, steps


def stop(force=False):
    """停掉全部。

    三路定位要杀的进程：PID 文件 ∪ 端口占用 ∪（可选）镜像名兜底。

    为什么必须记 PID 文件：桥是 pythonw.exe，**卡在启动中时端口还没监听**，
    netstat 查不到它，只按端口清就永远剩一个僵尸。
    为什么镜像名兜底默认关：`taskkill /F /IM llama-server.exe` 是**全局无差别
    扫射**，会把用户自己另起的实验进程一起带走。要就加 --force。

    成功文案一律放在「确认进程真的没了」之后 —— 以前是先拼成功文案再 kill，
    taskkill 失败（权限/占用）时用户看到的仍然是「已停止、显存已释放」。
    """
    steps = []

    # 1) 上次自己写下的 PID 文件
    pids = set()
    for key in ("bridge.log.pid", "swap.log.pid"):
        pid = _read_pid(os.path.join(LOGS, key))
        if pid:
            pids.add(pid)

    # 2) 端口占用（一次 netstat 查两个端口，少弹窗口）
    by_port = port_tool.pids_on_ports([BRIDGE_PORT, SWAP_PORT])
    for port in (BRIDGE_PORT, SWAP_PORT):
        for p in (by_port.get(port) or []):
            if str(p).isdigit():
                pids.add(int(p))

    kill_rc = 0
    if pids:
        args = ["taskkill", "/F"]
        for pid in sorted(pids):
            args += ["/PID", str(pid)]     # subprocess 只吃 str
        try:
            kill_rc = getattr(winrun.run(args, timeout=20), "returncode", 0)
        except Exception as exc:      # noqa: BLE001
            steps.append("!! taskkill 调用失败: %s" % exc)

    # 3) 镜像名兜底 —— 只在本工作区自己起的进程范围内兜，不搞全局扫射
    if force:
        for img in ("llama-server.exe", "llama-swap.exe"):
            try:
                winrun.run(["taskkill", "/F", "/IM", img], timeout=20)
            except Exception:      # noqa: BLE001
                pass
        steps.append("已按镜像名强制清理（--force）")

    # 等进程真的退出：taskkill /F 只是发终止信号，一个占着 6GB 显存的
    # llama-server 卸载 CUDA 上下文远不止 1 秒。以前用固定 sleep(0.6)，
    # 结果就是「stop 报成功但显存没还」，紧接着拉新进程直接 CUDA OOM。
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if not any(_pid_alive(p) for p in pids):
            break
        time.sleep(0.3)

    left_port = [p for p in (BRIDGE_PORT, SWAP_PORT) if port_tool.is_listening(p)]
    still = sorted(p for p in pids if _pid_alive(p))
    if left_port or still:
        steps.append("!! 未完全停止：端口仍监听 %s，进程仍存活 %s（taskkill rc=%s）"
                     % (left_port or "无", still or "无", kill_rc))
        return False, steps

    steps.append("全部进程已退出；驻留模型随 llama-swap 一起释放，显存已归还")
    _clear_pid("bridge.log.pid")
    _clear_pid("swap.log.pid")
    return True, steps


def status():
    by_port = port_tool.pids_on_ports([BRIDGE_PORT, SWAP_PORT])   # 一次 netstat 够用
    st = {"bridge": {"port": BRIDGE_PORT, "up": port_tool.is_listening(BRIDGE_PORT),
                     "pids": by_port.get(BRIDGE_PORT) or []},
          "swap": {"port": SWAP_PORT, "up": port_tool.is_listening(SWAP_PORT),
                   "pids": by_port.get(SWAP_PORT) or []},
          "llama_server": 0, "models": None, "ui": f"http://127.0.0.1:{BRIDGE_PORT}/ui"}
    try:
        raw = winrun.run(["tasklist", "/FI", "IMAGENAME eq llama-server.exe",
                          "/FO", "CSV", "/NH"], timeout=15).stdout or b""
        txt = winrun.decode(raw)
        st["llama_server"] = sum(1 for l in txt.splitlines() if "llama-server" in l.lower())
    except Exception:
        pass
    if st["bridge"]["up"]:
        try:
            import urllib.request
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(f"http://127.0.0.1:{BRIDGE_PORT}/api/tags", timeout=6) as r:
                st["models"] = len(json.loads(r.read().decode("utf-8")).get("models", []))
        except Exception:
            pass
    # 当前**驻留（占着显存）**的模型 —— 由 swap 的 /running 回答，不是"可调用"
    st["loaded"] = swap_ctl.running_names() if st["swap"]["up"] else []
    st["running"] = st["bridge"]["up"] and st["swap"]["up"]
    return st


def print_status(st):
    dot = lambda b: "● 运行中" if b else "○ 已停止"   # noqa: E731
    print("  本地模型服务")
    print(f"    对话界面（11434）: {dot(st['bridge']['up'])}")
    print(f"    模型交换器（11435）: {dot(st['swap']['up'])}")
    print(f"    已加载模型进程: {st['llama_server']} 个")
    if st["models"]:
        print(f"    可调用名称: {st['models']} 个")
    loaded = st.get("loaded") or []
    if loaded:
        print(f"    驻留显存中: {', '.join(loaded)}   （卸载: service_ctl.py unload [名字]）")
    else:
        print("    驻留显存中: 无（显存已释放）")
    print(f"    界面地址: {st['ui']}")
    if st["running"]:
        print("    状态: 正常")
    elif st["bridge"]["up"] or st["swap"]["up"]:
        print("    状态: 半开（只有一个在跑）—— 建议 restart")
    else:
        print("    状态: 全部停止")


def print_residency(snap):
    """人看的驻留状态，含「还有多久自动卸载」。"""
    pol = snap.get("policy") or {}
    print("  模型驻留")
    print(f"    闲置自动卸载: {'开' if pol.get('auto_unload') else '关'}"
          f"    默认上限: {pol.get('idle_ttl')}s"
          f"    检查间隔: {pol.get('check_interval')}s")
    if snap.get("error"):
        print(f"    !! {snap['error']}")
        return
    if not snap.get("resident"):
        print("    驻留: 无（显存已释放）")
        return
    for r in snap["resident"]:
        if r.get("protected"):
            tail = "protected，不会自动卸载"
        elif not r.get("ttl"):
            tail = "未设上限"
        else:
            tail = "%ds 后自动卸载" % r["unload_in_sec"]
        print(f"    ● {r['model']}  闲置 {r['idle_sec']}s  → {tail}")
    print("    改策略: service_ctl.py residency --set idle_ttl=900")
    print("    手动卸: service_ctl.py unload [名字]")


# ------------------------------------------------------------------ 开机自启
def autostart(action):
    import winreg
    vbs = os.path.join(ROOT, "scripts", "tray.vbs")
    cmdline = f'wscript.exe "{vbs}"'
    if action == "status":
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
                val, _ = winreg.QueryValueEx(k, AUTOSTART_NAME)
            return f"已启用：{val}"
        except FileNotFoundError:
            return "未启用"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
        if action == "on":
            winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, cmdline)
            return (f"已加入开机自启（HKCU\\...\\Run\\{AUTOSTART_NAME}）\n"
                    f"  {cmdline}\n"
                    f"  开机后会启动托盘并自动拉起服务；不想自启就在托盘菜单里再点一次。")
        try:
            winreg.DeleteValue(k, AUTOSTART_NAME)
        except FileNotFoundError:
            return "本来就是关的"
        return "已取消开机自启"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_start = sub.add_parser("start")
    p_start.add_argument("--console", action="store_true", help="桥留在前台（调试）")
    p_stop = sub.add_parser("stop")
    p_stop.add_argument("--force", action="store_true",
                        help="额外按镜像名清理 llama-server/llama-swap（会误伤自己起的进程）")
    sub.add_parser("restart")
    p_st = sub.add_parser("status")
    p_st.add_argument("--json", action="store_true")
    p_au = sub.add_parser("autostart")
    p_au.add_argument("action", choices=["on", "off", "status"])
    p_ul = sub.add_parser("unload", help="卸载驻留模型（释放显存）；省略名字 = 全部")
    p_ul.add_argument("model", nargs="?", default=None,
                      help="模型名，例如 humanizer-12b；省略则卸载全部")
    p_ul.add_argument("--force", action="store_true",
                      help="忽略 protected 名单，强行卸载")
    p_rs = sub.add_parser("residency",
                          help="模型驻留：状态 / 改策略 / 立刻清理")
    p_rs.add_argument("action", nargs="?", default="status",
                      choices=["status", "get", "reap"])
    p_rs.add_argument("--json", action="store_true")
    p_rs.add_argument("--set", dest="assign", nargs="+", metavar="K=V",
                      help="改策略，例如 --set idle_ttl=900 auto_unload=false")
    args = ap.parse_args()

    if args.cmd == "start":
        ok, steps = start(console_bridge=args.console)
    elif args.cmd == "stop":
        ok, steps = stop(force=args.force)
    elif args.cmd == "restart":
        # 同样：在作业对象里就把整个重启交给托盘（托盘自己会 stop + start）
        relayed = _relay_to_tray("restart", want_open=True)
        if relayed is not None:
            ok, steps = relayed
        else:
            ok1, s1 = stop()
            # stop() 内部已等到进程真的退出，不需要再用固定 sleep 猜时间
            ok, s2 = start()
            steps = s1 + s2
            ok = ok and ok1
    elif args.cmd == "autostart":
        print("  " + autostart(args.action))
        return 0
    elif args.cmd == "unload":
        # 卸载不涉及起进程，不受沙箱/作业对象回收影响，直接发 HTTP 即可。
        # 走 residency 而不是裸 swap_ctl：它会先检查 protected 名单，
        # 并且只在「确认真的不驻留了」之后才返回 ok。
        ok, msg = residency.unload(args.model, force=args.force)
        print("  " + msg)
        return 0 if ok else 1
    elif args.cmd == "residency":
        if args.assign:
            patch = {}
            for item in args.assign:
                if "=" not in item:
                    print("  !! 需要 K=V 形式，收到: %s" % item)
                    return 2
                k, v = item.split("=", 1)
                k, v = k.strip(), v.strip()
                low = v.lower()
                if low in ("true", "false"):
                    v = (low == "true")
                else:
                    try:
                        v = int(v)
                    except ValueError:
                        pass
                patch[k] = v
            pol = residency.save_policy(patch)
            print("  已更新 config\\residency.json")
            print(json.dumps(pol, ensure_ascii=False, indent=2))
            return 0
        if args.action == "get":
            print(json.dumps(residency.load_policy(), ensure_ascii=False, indent=2))
            return 0
        if args.action == "reap":
            done = residency.reap()
            print("  已卸载: " + (", ".join(done) if done else "无（没有闲置超时的）"))
            return 0
        snap = residency.snapshot()
        if args.json:
            print(json.dumps(snap, ensure_ascii=False, indent=2))
        else:
            print_residency(snap)
        return 0
    else:
        st = status()
        if args.json:
            print(json.dumps(st, ensure_ascii=False))
        else:
            print_status(st)
        return 0 if st["running"] or not st["bridge"]["up"] else 1

    for s in steps:
        print("  " + s)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
