#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""列出本部署对外暴露的模型与人格。

优先问正在运行的桥（11434），问不到就退回读配置文件，保证离线也能看。
"""
import http.client
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def from_api(port=11434):
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        conn.request("GET", "/api/tags")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return data.get("models") or []
    except Exception:
        return None


def from_config():
    with open(os.path.join(ROOT, "config", "personas.json"), "r", encoding="utf-8") as fh:
        reg = json.load(fh)
    out = []
    for b in reg["bases"]:
        out.append({"model": b, "size": 0, "kind": "base"})
    for p in reg["personas"]:
        out.append({"model": p["id"], "size": 0, "kind": "persona",
                    "base": p["base"], "label": p["label"]})
    return out


def main():
    # IDLE 等环境会把 sys.stdout 换成没有 reconfigure() 的对象，
    # 直接调用会 AttributeError 把整个脚本打断（用户从 IDLE 跑时踩到过）。
    # 这里兜底：设不了就跳过，中文输出交给宿主环境自己的编码处理。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    live = from_api()
    if live is not None:
        print(f"  {'model':<42}{'size':>10}  说明")
        print("  " + "-" * 72)
        for m in sorted(live, key=lambda x: x.get("model", "")):
            size = m.get("size") or 0
            fam = (m.get("details") or {}).get("family", "")
            print(f"  {m.get('model',''):<42}{size / 2**20:>8.0f}MB  {fam}")
        print(f"\n  共 {len(live)} 个可调用名称（含 Ollama 旧名别名）")
        return 0

    print("  (桥未运行，以下读自 config/personas.json)")
    reg_path = os.path.join(ROOT, "config", "personas.json")
    if not os.path.exists(reg_path):
        print("  缺少 config/personas.json，请先运行 scripts\\apply-personas.bat")
        return 1
    with open(reg_path, "r", encoding="utf-8") as fh:
        reg = json.load(fh)

    # 已停用的底座（supported=False）不对外注册，也不该计入总数 ——
    # 否则离线看到的数量会比桥实际提供的多（曾出现 34 vs 32）。
    def usable(b):
        return b.get("supported") is not False

    ok_bases = {k: v for k, v in reg["bases"].items() if usable(v)}
    ok_personas = [p for p in reg["personas"] if usable(reg["bases"].get(p["base"], {}))]
    ok_aliases = {k: v for k, v in reg.get("ollama_aliases", {}).items()
                  if v not in reg["bases"] or usable(reg["bases"][v])}

    print(f"\n  底座模型（{len(ok_bases)} 个，直接调用就是干净底座）")
    print(f"  {'id':<20}{'上下文':>8}  {'GGUF':<52}")
    print("  " + "-" * 84)
    for bid, b in reg["bases"].items():
        note = "   [已停用]" if not usable(b) else ""
        print(f"  {bid:<20}{b['ctx']:>8}  {b['gguf']}{note}")
    if len(ok_bases) != len(reg["bases"]):
        print(f"\n  [已停用] 的说明：")
        for bid, b in reg["bases"].items():
            if not usable(b):
                print(f"    {bid}: {b.get('unsupported_reason', '与当前 llama.cpp 不兼容')}")

    print(f"\n  人格（{len(ok_personas)} 个，共享底座权重，切换零重载）")
    print(f"  {'id':<20}{'底座':<18}{'提示词':>7}  来源 Modelfile / 说明")
    print("  " + "-" * 84)
    for p in ok_personas:
        print(f"  {p['id']:<20}{p['base']:<18}{p['system_chars']:>6}字  "
              f"{p['source_modelfile']}  ({p['label']})")

    print(f"\n  Ollama 旧模型名 → 本部署对应项（其他程序可继续用老名字）")
    print("  " + "-" * 84)
    for k, v in ok_aliases.items():
        print(f"  {k:<52} -> {v}")
    print(f"\n  合计 {len(ok_bases) + len(ok_personas) + len(ok_aliases)} 个可调用名称")
    return 0


if __name__ == "__main__":
    sys.exit(main())
