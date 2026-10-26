#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型冒烟测试：真加载一次，看能不能跑、跑多快、有没有 thinking 泄漏、显存还剩多少。

加了新模型后跑一下，比看「服务起来了」有用得多：
  - 只验证「能加载 + 能出字」很容易，**thinking 泄漏**这种问题只有真跑才看得见；
  - 顺带给出 tok/s 与显存余量，用来判断这个量化档在这台机器上是否合适。

用法：
  python scripts\\smoke_model.py                     用默认提示词测所有可用对话底座
  python scripts\\smoke_model.py qwen35-9b-rp        只测指定模型
  python scripts\\smoke_model.py <模型> "你的提示词"    自定义提示词
"""
import json
import os
import subprocess
import sys
import time
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
PROMPT = "用一句话自我介绍，说明你擅长什么。"
# 判断「思考泄漏」用的特征：这些出现说明模板/参数没配对
LEAK_MARKS = ("<think>", "</think>", "Thinking Process", "思考过程", "We need to think")


def req(method, path, body=None, timeout=900):
    data = None if body is None else json.dumps(body).encode("utf-8")
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def vram():
    r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free",
                        "--format=csv,noheader,nounits"], capture_output=True, timeout=30)
    try:
        tot, used, free = (int(x) for x in r.stdout.decode().split(","))
        return tot, used, free
    except Exception:
        return None, None, None


def main():
    args = [a for a in sys.argv[1:]]
    prompt = PROMPT
    if args and not args[0].startswith("qwen") and not args[0].startswith("gal") \
            and not args[0].startswith("hunyuan") and not args[0].startswith("nomic"):
        prompt = args.pop(0)

    print("  0) 确保服务在跑…")
    winrun.run([sys.executable, os.path.join(ROOT, "scripts", "service_ctl.py"), "start"],
               timeout=240)
    st = req("GET", "/admin/state")
    bases = {b["id"]: b for b in st.get("bases", [])}
    chat_bases = [b["id"] for b in st.get("bases", [])
                  if b.get("supported") and not b.get("embedding")]
    targets = args or chat_bases
    if not targets:
        print("  !! 没有可用底座")
        return 1

    tot0, used0, _ = vram()
    print(f"  起始显存：已用 {used0} MiB / {tot0} MiB\n")
    for mid in targets:
        info = bases.get(mid, {})
        print(f"  ── {mid}" + (f"（{info.get('label')}）" if info.get("label") else ""))
        t0 = time.time()
        try:
            r = req("POST", "/api/chat", {
                "model": mid, "stream": False, "think": False,
                "messages": [{"role": "user", "content": prompt}],
                "options": {"num_predict": 120, "temperature": 0.8},
            })
        except Exception as exc:
            print(f"     ✗ 调用失败：{exc}\n")
            continue
        dt = time.time() - t0
        m = r.get("message") or {}
        content = (m.get("content") or "").strip()
        thinking = (m.get("thinking") or "").strip()
        ec = r.get("eval_count") or 0
        ed = r.get("eval_duration") or 0
        tps = ec / (ed / 1e9) if ec and ed else 0
        _, used1, free1 = vram()

        print(f"     加载+首答 {dt:.1f}s   {ec} tok   {tps:.1f} tok/s   "
              f"显存剩 {free1} MiB（用了 {used1} MiB）")
        print(f"     content  ：{content[:100]!r}")
        if thinking:
            print(f"     thinking ：{len(thinking)} 字 ← 有思考内容")
        leaked = [k for k in LEAK_MARKS if k in content]
        if leaked:
            print(f"     ✗ 正文里出现疑似思考泄漏标记：{leaked}")
        elif not content:
            print("     ✗ content 为空")
        else:
            print("     ✓ 正文干净、无 thinking 泄漏")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
