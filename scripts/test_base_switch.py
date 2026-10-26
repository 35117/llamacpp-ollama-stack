#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""换底座之后，是否**真的**跑在新底座的权重上（端到端）。

界面能改底座只是第一步。要证明的是：改完发消息，llama-swap 起来的是**新底座的
权重**，而且**还是同一个人格**（提示词没丢、没被邻居的提示词覆盖）。

判据（四条）：
  1. PUT /admin/personas/<id> 带上新 base → 200，/admin/state 里 base 变了；
  2. 提示词长度不变（只换了底座，没动提示词）；
  3. 用这个人格名发一句 → 有回复，且 llama-swap 日志里出现**新底座**的模型名
     （旧底座不会起）；
  4. 还原后 base 回到原值。

注意：**一切都在最后还原**，不留改动。
用法：python scripts\\test_base_switch.py [人格id] [新底座id]
      （默认挑一个「底座已被删除」的人格，换到 qwen35-9b-heretic）
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)

import winrun  # noqa: E402

BASE = "http://127.0.0.1:11434"
SWAP_LOG = os.path.join(ROOT, "logs", "llama-swap.log")
NEW_BASE = "qwen35-9b-heretic"
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method, path, body=None, timeout=900):
    data = None if body is None else json.dumps(body).encode("utf-8")
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with op.open(r, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def state():
    return req("GET", "/admin/state")


def log_size():
    return os.path.getsize(SWAP_LOG) if os.path.exists(SWAP_LOG) else 0


def log_tail(offset):
    if not os.path.exists(SWAP_LOG):
        return ""
    with open(SWAP_LOG, "rb") as fh:
        fh.seek(offset)
        return fh.read().decode("utf-8", "replace")


def main():
    pid = sys.argv[1] if len(sys.argv) > 1 else None
    newbase = sys.argv[2] if len(sys.argv) > 2 else NEW_BASE
    ok = total = 0

    print("  0) 重启服务（确保后端是当前代码）…")
    winrun.run([sys.executable, os.path.join(ROOT, "scripts", "service_ctl.py"),
                "restart"], timeout=240)
    st = state()
    bases = {b["id"]: b for b in st["bases"]}

    if newbase not in bases or not bases[newbase]["supported"]:
        print(f"  !! 新底座 {newbase} 不可用，先换个可用的")
        return 1

    if not pid:
        dead = [p for p in st["personas"]
                if not bases.get(p["base"], {}).get("supported")]
        if not dead:
            print("  · 没有「底座不可用」的人格，挑第一个凑合测")
            pid = st["personas"][0]["id"]
        else:
            pid = dead[0]["id"]
    p0 = next((p for p in st["personas"] if p["id"] == pid), None)
    if not p0:
        print(f"  !! 没有这个人格: {pid}")
        return 1
    oldbase, oldchars = p0["base"], p0["chars"]

    print(f"  1) 人格 {pid}：底座 {oldbase} → {newbase}")
    print(f"     提示词 {oldchars} 字（改完应保持不变）")
    offset = log_size()
    req("PUT", "/admin/personas/" + urllib.parse.quote(pid), {"base": newbase})
    st = state()
    p1 = next(p for p in st["personas"] if p["id"] == pid)

    total += 1
    if p1["base"] == newbase:
        print(f"     ✓ /admin/state 里 base 已变成 {newbase}")
        ok += 1
    else:
        print(f"     ✗ base 还是 {p1['base']}")

    total += 1
    if p1["chars"] == oldchars:
        print(f"     ✓ 提示词没被动过（还是 {p1['chars']} 字）")
        ok += 1
    else:
        print(f"     ✗ 提示词变了：{oldchars} → {p1['chars']}")

    print(f"  2) 用这个人格发一句（首次要加载 {newbase} 的权重，可能要 1 分钟）…")
    t0 = time.time()
    try:
        r = req("POST", "/api/chat", {
            "model": pid, "stream": False, "think": False,
            "messages": [{"role": "user", "content": "只回复两个字：收到"}],
            "options": {"num_predict": 20, "temperature": 0.2},
        })
        reply = (r.get("message") or {}).get("content", "")
    except Exception as exc:
        print(f"     !! 请求失败：{exc}")
        reply = ""
    print(f"     用时 {time.time() - t0:.1f}s   回复={reply.strip()[:40]!r}")

    total += 1
    if reply.strip():
        print("     ✓ 人格还能正常对话（提示词也在作用域里）")
        ok += 1
    else:
        print("     ✗ 没回复")

    tail = log_tail(offset)
    total += 1
    started_new = f"<{newbase}>" in tail
    started_old = f"<{oldbase}>" in tail
    if started_new and not started_old:
        print(f"     ✓ llama-swap 起的是新底座 <{newbase}>（旧底座 <{oldbase}> 没起）")
        ok += 1
    else:
        print(f"     ✗ 换载不对：新={started_new} 旧={started_old}")
        for line in tail.splitlines()[-6:]:
            print("       " + line[:110])

    total += 1
    try:
        ps = req("GET", "/api/ps", timeout=30)
        names = [m.get("name") for m in ps.get("models", [])]
        print(f"     /api/ps 当前加载：{names}")
        if newbase in names:
            print("     ✓ 显存里驻留的正是新底座")
            ok += 1
        else:
            print("     ✗ /api/ps 里没看到新底座")
    except Exception as exc:
        print(f"     !! /api/ps 失败：{exc}")

    print("  3) 还原…")
    req("POST", f"/admin/personas/{urllib.parse.quote(pid)}/reset")
    st = state()
    p2 = next(p for p in st["personas"] if p["id"] == pid)
    total += 1
    if p2["base"] == oldbase:
        print(f"     ✓ 底座已回到 {oldbase}")
        ok += 1
    else:
        print(f"     ✗ 还原失败，现在是 {p2['base']}")

    print(f"\n  换底座端到端：{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
