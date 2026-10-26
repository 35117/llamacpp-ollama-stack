# -*- coding: utf-8 -*-
"""真实服务端到端：起服务 -> 加载小模型 -> 查驻留 -> 定向卸载 -> 复查。

只跑一次，脚本自己负责收尾（stop）。用 nomic-embed（0.26GB）避免长时间等加载。
用法： python _test_live.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
FAILED = []
BRIDGE_PORT = 11434


def api(path, method="GET", payload=None, timeout=20):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (BRIDGE_PORT, path),
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method, headers={"Content-Type": "application/json"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def ctl(*args):
    return subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                        "service_ctl.py")] + list(args),
                          cwd=ROOT, capture_output=True, timeout=180)


def main():
    print("  启动服务…")
    r = ctl("start")
    print("    " + (r.stdout or b"").decode("utf-8", "replace").strip().replace("\n", "\n    "))

    # 等服务就绪
    ready = False
    for _ in range(40):
        try:
            s, _b = api("/admin/residency")
            if s == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    check("服务已就绪", ready)
    if not ready:
        print("  起不来，放弃")
        return 1

    s, b = api("/admin/residency")
    snap = json.loads(b.decode("utf-8"))
    check("/admin/residency 可解析且 swap_up=True", snap.get("swap_up") is True)
    before = [r["model"] for r in snap.get("resident") or []]
    print(f"    当前驻留: {before or '无'}")

    # 加载一个小模型（embedding）
    print("  调用 nomic-embed 触发加载…")
    s, b = api("/api/embed", "POST",
               {"model": "nomic-embed", "input": ["hello world"]})
    print(f"    /api/embed -> {s}")
    loaded_ok = s == 200
    check("嵌入模型可调用", loaded_ok, b.decode("utf-8", "replace")[:160])

    time.sleep(1.0)
    s, b = api("/admin/residency")
    snap = json.loads(b.decode("utf-8"))
    now = [r["model"] for r in snap.get("resident") or []]
    print(f"    加载后驻留: {now}")
    check("驻留列表反映新加载的模型",
          (not loaded_ok) or ("nomic-embed" in now), str(now))

    if loaded_ok and "nomic-embed" in now:
        entry = [r for r in snap["resident"] if r["model"] == "nomic-embed"][0]
        print(f"    条目: {json.dumps(entry, ensure_ascii=False)}")
        check("idle_sec 是数字", isinstance(entry.get("idle_sec"), int))

        # 定向卸载
        s, b = api("/admin/residency", "POST",
                   {"action": "unload", "model": "nomic-embed", "force": True})
        j = json.loads(b.decode("utf-8"))
        print(f"    卸载 -> {s} {j.get('message')}")
        check("定向卸载返回 ok", bool(j.get("ok")))

        time.sleep(1.0)
        s, b = api("/admin/residency")
        snap = json.loads(b.decode("utf-8"))
        after = [r["model"] for r in snap.get("resident") or []]
        print(f"    卸载后驻留: {after or '无'}")
        check("卸载后 nomic-embed 不再驻留", "nomic-embed" not in after, str(after))

    print("  停止服务…")
    r = ctl("stop")
    print("    " + (r.stdout or b"").decode("utf-8", "replace").strip().replace("\n", "\n    "))

    print()
    if FAILED:
        print("  失败项: " + ", ".join(FAILED))
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
