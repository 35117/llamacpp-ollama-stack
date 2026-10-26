#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""状态总览：服务端口、GPU、已加载模型、对外可调用清单。

原来这些逻辑写在 status.bat 里，但 cmd 的 `find`/括号 echo 在 PATH 里有 Git 时
会被劫持、报语法错，所以整体挪到 Python。
"""
import http.client
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import winrun  # noqa: E402  所有控制台辅助程序都要走它，否则会闪黑框

BRIDGE_PORT, SWAP_PORT = 11434, 11435


def run_bytes(cmd, timeout=20):
    """拿 bytes 再容错解码 —— 中文 Windows 的命令输出是 GBK，不能交给 text=True。"""
    try:
        return winrun.decode(winrun.run(cmd, timeout=timeout).stdout)
    except Exception:
        return ""


def port_up(port):
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def count_proc(name):
    out = run_bytes(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"])
    return sum(1 for line in out.splitlines() if name.lower() in line.lower())


def api(port, path, timeout=6):
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", path)
        r = c.getresponse()
        data = json.loads(r.read().decode("utf-8"))
        c.close()
        return data
    except Exception:
        return None


def tray_running():
    """托盘进程是否在跑。查窗口类即可，不需要 pywin32。"""
    try:
        import ctypes
        return bool(ctypes.windll.user32.FindWindowW("LlamaCppTrayWnd", "本地模型服务"))
    except Exception:
        return False


def main():
    # IDLE 等环境会把 sys.stdout 换成没有 reconfigure() 的对象，
    # 直接调用会 AttributeError 把整个脚本打断（用户从 IDLE 跑时踩到过）。
    # 这里兜底：设不了就跳过，中文输出交给宿主环境自己的编码处理。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ok = lambda b: "RUNNING" if b else "STOPPED"  # noqa: E731

    print("=== 服务 ===============================================================")
    print(f"  Ollama 兼容桥   11434 : {ok(port_up(BRIDGE_PORT))}")
    print(f"  llama-swap      11435 : {ok(port_up(SWAP_PORT))}")
    n = count_proc("llama-server.exe")
    print(f"  llama-server 子进程   : {n} 个{'（已加载模型）' if n else '（当前无模型驻留）'}")
    tr = tray_running()
    print(f"  系统托盘              : {'运行中' if tr else '未运行'}"
          f"{'' if tr else '   —— 双击 scripts\\tray.vbs 启动'}")

    print("\n=== GPU ================================================================")
    gpu = run_bytes(["nvidia-smi",
                     "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                     "--format=csv"])
    for line in gpu.strip().splitlines():
        print("  " + line)
    if not gpu.strip():
        print("  （拿不到 nvidia-smi 输出）")

    print("\n=== 对外可调用名称 =====================================================")
    tags = api(BRIDGE_PORT, "/api/tags")
    if tags:
        ms = tags.get("models") or []
        print(f"  共 {len(ms)} 个（底座 + 人格 + Ollama 旧名别名）")
        for m in sorted(ms, key=lambda x: x.get("model", "")):
            print(f"    {m.get('model',''):<48}{(m.get('size') or 0) / 2**20:>8.0f}MB  "
                  f"{(m.get('details') or {}).get('family','')}")
    else:
        print("  桥未运行，读配置文件：")
        reg = os.path.join(ROOT, "config", "personas.json")
        if os.path.exists(reg):
            with open(reg, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            for bid, b in d["bases"].items():
                flag = "  [不可用]" if b.get("supported") is False else ""
                print(f"    底座 {bid:<20} {b['desc']}{flag}")
            for p in d["personas"]:
                print(f"    人格 {p['id']:<20} {p['label']}  (底座 {p['base']})")
        else:
            print("    缺少 config/personas.json，请先运行 scripts\\apply-personas.bat")

    print("\n=== 端点速查 ===========================================================")
    print(f"  Ollama 原生  http://127.0.0.1:{BRIDGE_PORT}/api/chat   /api/generate  /api/tags")
    print(f"  OpenAI 兼容  http://127.0.0.1:{BRIDGE_PORT}/v1/chat/completions  /v1/models")
    print(f"  llama-swap   http://127.0.0.1:{SWAP_PORT}  （内部使用，勿对外）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
