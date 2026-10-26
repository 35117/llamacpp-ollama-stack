#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端口小工具（给 .bat 启动脚本用，纯标准库）

  port_tool.py wait  <port> [timeout_s]   等到端口可连接，成功 exit 0
  port_tool.py check <port>               正在监听则 exit 0
  port_tool.py who   <port>               打印占用该端口的 PID
  port_tool.py free  <port>               结束占用该端口的进程
"""
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import winrun  # noqa: E402  统一走「不弹控制台窗口」的调用


def is_listening(port, host="127.0.0.1", timeout=0.6):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, int(port))) == 0
    finally:
        s.close()


def pids_on_ports(ports):
    """一次 netstat 拿到多个端口的监听 PID，返回 {int(port): [pid, ...]}。

    为什么要「一次拿多个」：每次调 netstat 都会弹一个控制台窗口
    （从 pythonw 启动时），查两个端口就是两个黑框。
    """
    want = {int(p) for p in ports}
    res = {p: [] for p in want}
    try:
        raw = winrun.run(["netstat", "-ano", "-p", "TCP"]).stdout or b""
    except Exception:
        return {p: [] for p in want}
    for line in winrun.decode(raw).splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local, state, pid = parts[1], parts[3], parts[4]
        if state.upper() != "LISTENING":
            continue
        # 形如 127.0.0.1:11434 或 [::]:11434
        try:
            port = int(local.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        if port in want:
            res[port].append(pid)
    return {p: sorted(set(v)) for p, v in res.items()}


def pids_on_port(port):
    """用 netstat 找出 LISTENING 状态下的 PID（不依赖 psutil）。

    坑：中文 Windows 的 netstat 输出是 GBK 编码，用 text=True 让 Python 按 UTF-8
    解码会在读取线程里抛 UnicodeDecodeError，stdout 变成 None（踩过）。
    所以这里拿 bytes，自己按容错方式解码。
    """
    return pids_on_ports([port]).get(int(port), [])


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    cmd, port = sys.argv[1], sys.argv[2]

    if cmd == "check":
        return 0 if is_listening(port) else 1

    if cmd == "wait":
        timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
        deadline = time.time() + timeout
        while time.time() < deadline:
            if is_listening(port):
                return 0
            time.sleep(0.3)
        return 1

    if cmd == "who":
        pids = pids_on_port(port)
        if not pids:
            return 1
        print(",".join(pids))
        return 0

    if cmd == "free":
        pids = pids_on_port(port)
        if not pids:
            print(f"port {port} is free")
            return 0
        for pid in pids:
            # 同样不能用 text=True：taskkill 的中文提示是 GBK，会让读取线程抛异常
            winrun.run(["taskkill", "/F", "/PID", pid])
            print(f"killed pid {pid} (port {port})")
        time.sleep(0.5)
        return 0 if not is_listening(port) else 1

    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
