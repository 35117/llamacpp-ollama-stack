#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自检 —— 真实调用 11434 上的桥，逐项验证 Ollama / OpenAI 两套协议。

用法：  python scripts/selftest.py [--port 11434] [--skip-heavy]

只依赖标准库。每一项都会打印 PASS / FAIL 与实测数据。
"""
import argparse
import http.client
import json
import sys
import time

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results = []


def req(port, method, path, body=None, timeout=600, raw=False):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if payload:
        headers["Content-Length"] = str(len(payload))
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    if raw:
        return resp.status, data.decode("utf-8", "replace")
    try:
        return resp.status, json.loads(data.decode("utf-8"))
    except Exception:
        return resp.status, {"_raw": data.decode("utf-8", "replace")[:400]}


def ndjson_stream(port, path, body, timeout=600):
    """读 Ollama 的 NDJSON 流，返回 (分块列表, 拼接内容, 拼接思考, 末行)"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    conn.request("POST", path, body=payload,
                 headers={"Content-Type": "application/json", "Content-Length": str(len(payload))})
    resp = conn.getresponse()
    chunks, content, thinking, last = [], "", "", None
    if resp.status != 200:
        conn.close()
        return [], "", "", {"error": resp.read().decode("utf-8", "replace")[:300]}
    while True:
        line = resp.readline()
        if not line:
            break
        s = line.decode("utf-8", "replace").strip()
        if not s:
            continue
        try:
            d = json.loads(s)
        except Exception:
            continue
        chunks.append(d)
        m = d.get("message") or {}
        content += m.get("content") or ""
        thinking += m.get("thinking") or ""
        if "response" in d:
            content += d["response"] or ""
        last = d
    conn.close()
    return chunks, content, thinking, last


def sse_stream(port, path, body, timeout=600):
    """读 OpenAI 的 SSE 流（data: {...} / data: [DONE]），返回 (原始行数, 拼接文本, 末个对象)"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    conn.request("POST", path, body=payload,
                 headers={"Content-Type": "application/json", "Content-Length": str(len(payload))})
    resp = conn.getresponse()
    if resp.status != 200:
        err = resp.read().decode("utf-8", "replace")[:300]
        conn.close()
        return 0, "", {"error": err}
    nlines, text, last = 0, "", None
    while True:
        line = resp.readline()
        if not line:
            break
        s = line.decode("utf-8", "replace").strip()
        if not s or s.startswith(":"):
            continue
        if not s.startswith("data:"):
            continue
        data = s[5:].strip()
        if data == "[DONE]":
            break
        nlines += 1
        try:
            d = json.loads(data)
        except Exception:
            continue
        last = d
        delta = ((d.get("choices") or [{}])[0].get("delta") or {})
        text += delta.get("content") or delta.get("reasoning_content") or ""
    conn.close()
    return nlines, text, last or {}


def check(name, fn):
    t0 = time.time()
    try:
        status, detail = fn()
    except Exception as exc:
        status, detail = FAIL, f"异常: {type(exc).__name__}: {exc}"
    dt = (time.time() - t0) * 1000
    results.append((name, status, detail, dt))
    mark = {PASS: "[PASS]", FAIL: "[FAIL]", SKIP: "[SKIP]"}[status]
    print(f"{mark} {name}   ({dt:.0f} ms)")
    if detail:
        for line in str(detail).splitlines():
            print(f"        {line}")
    print()


def main():
    # IDLE 等环境会把 sys.stdout 换成没有 reconfigure() 的对象，
    # 直接调用会 AttributeError 把整个脚本打断（用户从 IDLE 跑时踩到过）。
    # 这里兜底：设不了就跳过，中文输出交给宿主环境自己的编码处理。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11434)
    ap.add_argument("--skip-heavy", action="store_true",
                    help="跳过换载大模型（会清空已加载模型）的用例")
    args = ap.parse_args()
    port = args.port

    # ---------------------------------------------------------------- 用例
    # **不要写死模型名**：用户会换模型（删旧的、加新的），写死会让整套测试开始误报，
    # 把真正的问题埋在噪音里（2026-10-01 就变成 9/15）。这里动态挑，挑不到就 SKIP。
    MARK = "喵呜口令XYZ"
    TMP_ID = "selftest-probe"

    def admin_get(path):
        st, d = req(port, "GET", path)
        return d if isinstance(d, dict) else {}

    _st = admin_get("/admin/state")
    _bases = _st.get("bases", [])
    LIVE_BASES = [b["id"] for b in _bases if b.get("supported")]
    # 向量模型（embedding）不能当对话底座 —— 挑进来会得到
    # "the current context does not logits computation. skipping"（踩过）
    CHAT_BASES = [b["id"] for b in _bases
                  if b.get("supported") and not b.get("embedding")]
    _live = set(LIVE_BASES)
    USABLE_PERSONAS = [p["id"] for p in _st.get("personas", []) if p.get("base") in _live]
    # 有长提示词的自动抽取人格（验「人格元数据」用；没有就退回临时探针）
    REAL_PERSONA = next((p["id"] for p in _st.get("personas", [])
                         if p.get("base") in _live and p.get("origin") != "local"
                         and (p.get("chars") or 0) >= 100), None)

    # 临时探针人格：行为确定（只回标记），不依赖用户的人格是否还在
    FIX = None
    if CHAT_BASES:
        req(port, "DELETE", "/admin/personas/" + TMP_ID)
        _s, _d = req(port, "POST", "/admin/personas", {
            "id": TMP_ID, "base": CHAT_BASES[0], "label": "自检探针",
            "system": "你只会回复「" + MARK + "」，不要有任何其他内容。"})
        if _s == 201:
            FIX = TMP_ID
    print(f"  可用底座: {LIVE_BASES}")
    print(f"  可对话底座: {CHAT_BASES}")
    print(f"  可用人格: {USABLE_PERSONAS or '（无 —— 底座都被删了？）'}")
    print(f"  探针人格: {FIX or '（建不出来，相关项会 SKIP）'}\n")

    # ---------------------------------------------------------------- 探活
    def t_root():
        st, body = req(port, "GET", "/", raw=True)
        if st == 200 and "Ollama is running" in body:
            return PASS, "GET / 返回 'Ollama is running'（客户端靠它判断服务在线）"
        return FAIL, f"status={st} body={body[:120]}"
    check("探活 GET /", t_root)

    def t_version():
        st, d = req(port, "GET", "/api/version")
        return (PASS, f"version={d.get('version')}") if st == 200 and d.get("version") else (FAIL, str(d)[:200])
    check("GET /api/version", t_version)

    tags_models = []

    def t_tags():
        nonlocal tags_models
        st, d = req(port, "GET", "/api/tags")
        ms = d.get("models") or []
        tags_models = [m["model"] for m in ms]
        if st != 200 or not ms:
            return FAIL, str(d)[:200]
        missing = [k for k in ("name", "model", "size", "digest", "details") if k not in ms[0]]
        s = f"{len(ms)} 个可调用名称；样例 {ms[0]['model']} size={ms[0]['size']/2**20:.0f}MB"
        if missing:
            return FAIL, s + f"；缺少字段 {missing}"
        return PASS, s
    check("GET /api/tags", t_tags)

    def t_v1models():
        st, d = req(port, "GET", "/v1/models")
        n = len(d.get("data") or [])
        return (PASS, f"/v1/models 返回 {n} 项") if st == 200 and n else (FAIL, str(d)[:200])
    check("GET /v1/models (OpenAI)", t_v1models)

    def t_show():
        who = REAL_PERSONA or FIX
        if not who:
            return SKIP, "没有可用的人格（底座权重都被删了？）—— 跳过"
        st, d = req(port, "POST", "/api/show", {"model": who})
        if st != 200:
            return FAIL, str(d)[:200]
        system = d.get("system") or ""
        syschars = len(system)
        caps = d.get("capabilities")
        if who == TMP_ID:
            ok = MARK in system
            return ((PASS if ok else FAIL),
                    f"{who}（临时探针）system 含标记={ok}；capabilities={caps}")
        if syschars < 100:
            return FAIL, f"system 提示词过短({syschars})，人格可能没挂上"
        return PASS, f"{who} 系统提示词 {syschars} 字；capabilities={caps}"
    check("POST /api/show (人格元数据)", t_show)

    # ------------------------------------------------- 人格注入 + 流式 NDJSON
    def t_persona_stream():
        if not FIX:
            return SKIP, "没有探针人格（没有可用底座）—— 跳过"
        chunks, content, thinking, last = ndjson_stream(port, "/api/chat", {
            "model": FIX, "stream": True,
            "messages": [{"role": "user", "content": "随便说点什么"}],
            "options": {"num_predict": 120, "temperature": 0.3},
        })
        if last and last.get("error"):
            return FAIL, str(last["error"])[:300]
        if not chunks:
            return FAIL, "没有收到任何 NDJSON 分块"
        need = {"prompt_eval_count", "eval_count", "done"}
        miss = need - set(last or {})
        body = content or thinking
        # 用人格自己的一句话当判据（比"输出里有分类字段"更确定，也不挑人格）。
        # **比对时去掉空白**：模型常把 "喵呜口令XYZ" 写成 "喵呜口令 XYZ"。
        if MARK.replace(" ", "") not in body.replace(" ", "").replace("\n", ""):
            return FAIL, f"输出里没有人格注入的标记，提示词可能没生效。前 200 字: {body[:200]}"
        if miss:
            return FAIL, f"末行缺少统计字段 {miss}"
        return PASS, (f"{len(chunks)} 个流式分块；末行统计 {last.get('prompt_eval_count')} in / "
                      f"{last.get('eval_count')} out；人格注入生效（输出含标记）")
    check("POST /api/chat 流式 + 人格注入", t_persona_stream)

    def t_persona_nostream():
        who = FIX or (CHAT_BASES[0] if CHAT_BASES else None)
        if not who:
            return SKIP, "没有可用模型 —— 跳过"
        st, d = req(port, "POST", "/api/chat", {
            "model": who, "stream": False, "think": False,
            "messages": [{"role": "user", "content": "用一句话打招呼"}],
            "options": {"num_predict": 60},
        })
        if st != 200 or d.get("error"):
            return FAIL, str(d)[:250]
        m = d.get("message") or {}
        if m.get("role") != "assistant":
            return FAIL, f"message.role 应为 assistant，实际 {m.get('role')}"
        if not (m.get("content") or "").strip():
            return FAIL, "content 为空"
        return PASS, f"{who}（think:false）生效，content={m['content'][:60]!r}"
    check("POST /api/chat 非流式 + think:false", t_persona_nostream)

    def t_generate():
        # 用非思维模型（混元翻译），这样 response 一定是正文而不是被思维链吃光的空串
        st, d = req(port, "POST", "/api/generate", {
            "model": "hunyuan-mt-7b", "stream": False, "think": False,
            "prompt": "把下面这句翻成英文：今天天气不错。", "options": {"num_predict": 80},
        })
        if st != 200 or d.get("error"):
            return FAIL, str(d)[:250]
        if "response" not in d:
            return FAIL, f"缺少 response 字段，keys={list(d.keys())}"
        if not (d["response"] or "").strip():
            return FAIL, "response 为空"
        return PASS, f"response={d['response'][:80]!r}"
    check("POST /api/generate", t_generate)

    def t_openai_stream():
        if not CHAT_BASES:
            return SKIP, "没有可对话的模型 —— 跳过"
        nlines, joined, last = sse_stream(port, "/v1/chat/completions", {
            "model": (FIX or CHAT_BASES[0]) if CHAT_BASES else "", "stream": True,
            "messages": [{"role": "user", "content": "老师，用一句话介绍你自己"}],
            "max_tokens": 120,
        })
        if last.get("error"):
            return FAIL, str(last["error"])[:250]
        if not joined:
            return FAIL, f"OpenAI 流式没有收到文本（收到 {nlines} 个 SSE 事件）"
        # llama-swap 的加载横幅会污染输出（它塞进 delta.reasoning_content，
        # 桥再映射成 message.thinking）。config 里已设 sendLoadingState:false，
        # 这里做回归检查，避免以后被误改回来。
        if "llama-swap loading model" in joined or "━━━━━" in joined:
            return FAIL, "输出里混进了 llama-swap 的加载横幅（检查 sendLoadingState 是否为 false）"
        return PASS, f"OpenAI 流式 {nlines} 个 SSE 事件，{joined[:70]!r}"
    check("POST /v1/chat/completions 流式 + 人格", t_openai_stream)

    def t_no_loading_banner():
        """换载一个没加载的模型，确认流里没有横幅污染（走 Ollama 协议这条路径）。"""
        who = (CHAT_BASES[-1] if len(CHAT_BASES) > 1
               else (FIX or (CHAT_BASES[0] if CHAT_BASES else None)))
        if not who:
            return SKIP, "没有可用模型 —— 跳过"
        chunks, content, thinking, last = ndjson_stream(port, "/api/chat", {
            "model": who, "stream": True, "think": False,
            "messages": [{"role": "user", "content": "用一句话问好"}],
            "options": {"num_predict": 40},
        })
        if last and last.get("error"):
            return FAIL, str(last["error"])[:250]
        if not chunks:
            return FAIL, "换载时没有收到任何 NDJSON 分块"
        joined = content + thinking
        if "llama-swap loading model" in joined or "━━━━━" in joined:
            return FAIL, f"换载时的流里混进了加载横幅: {joined[:120]!r}"
        return PASS, f"换载到 {who}，{len(chunks)} 个分块，输出干净（{joined[:50]!r}）"
    check("换载时无加载横幅污染", t_no_loading_banner)

    def t_openai_alias():
        # 别写死某个旧名 —— 它映射的底座可能已经被删了。挨个试，全不行才 SKIP。
        tried = []
        for alias in ("deepseek-r1:8b", "qwen3:latest", "qwen3.5-9b-heretic:latest",
                      "qwen-3.5-4b-heretic:latest", "huihui-qwen3.5-9b:latest"):
            st, d = req(port, "POST", "/api/show", {"model": alias})
            if st == 200:
                return PASS, f"Ollama 旧名 '{alias}' 能解析（映射到已有底座）"
            tried.append(alias)
        return SKIP, f"候选旧名都不可用（{', '.join(tried)}）—— 底座被删了？跳过"
    check("Ollama 旧名别名解析", t_openai_alias)

    # ---------------------------------------------------------------- 向量
    def t_embed():
        st, d = req(port, "POST", "/api/embed", {
            "model": "nomic-embed", "input": ["你好", "hello world"],
        })
        embs = d.get("embeddings") or []
        if st != 200 or len(embs) != 2:
            return FAIL, str(d)[:250]
        if not embs[0] or len(embs[0]) < 64:
            return FAIL, f"向量维度异常: {len(embs[0]) if embs[0] else 0}"
        return PASS, f"2 条输入 -> 维度 {len(embs[0])} 的向量；prompt_eval_count={d.get('prompt_eval_count')}"
    check("POST /api/embed (nomic-embed)", t_embed)

    def t_ps():
        st, d = req(port, "GET", "/api/ps")
        if st != 200 or "models" not in d:
            return FAIL, str(d)[:200]
        return PASS, f"/api/ps 返回 {len(d['models'])} 个已加载模型"
    check("GET /api/ps", t_ps)

    def t_bad_model():
        st, d = req(port, "POST", "/api/chat", {
            "model": "no-such-model-xyz", "messages": [], "stream": False,
        })
        if st == 404 and "error" in d:
            return PASS, "不存在的模型名返回 404 + error 字段"
        return FAIL, f"期望 404，实际 {st} {str(d)[:120]}"
    check("错误处理（未知模型）", t_bad_model)

    # ------------------------------------------------------- 换载（最重的一步）
    if args.skip_heavy:
        results.append(("跨底座换载", SKIP, "--skip-heavy", 0))
        print("[SKIP] 跨底座换载   (--skip-heavy)\n")
    else:
        def t_swap():
            t0 = time.time()
            st, d1 = req(port, "POST", "/api/chat", {
                "model": "galtransl-7b", "stream": False, "think": False,
                "messages": [{"role": "user", "content": "こんにちは、元気ですか？"}],
                "options": {"num_predict": 60},
            })
            t_gal = time.time() - t0
            if st != 200 or d1.get("error"):
                return FAIL, f"galtransl-7b（6.0GB，显存最紧张）加载或推理失败: {str(d1)[:250]}"
            got = (d1.get("message") or {}).get("content", "")
            t0 = time.time()
            st, d2 = req(port, "POST", "/api/chat", {
                "model": "qwen35-4b-heretic", "stream": False, "think": False,
                "messages": [{"role": "user", "content": "hi"}],
                "options": {"num_predict": 20},
            })
            t_swap2 = time.time() - t0
            if st != 200 or d2.get("error"):
                return FAIL, f"切到 qwen35-4b-heretic 失败: {str(d2)[:250]}"
            return PASS, (f"galtransl-7b 加载+推理 {t_gal:.1f}s（输出 {got[:40]!r}）；"
                          f"再切 qwen35-4b-heretic {t_swap2:.1f}s")
        check("跨底座换载 + 最大显存模型", t_swap)

    # ---------------------------------------------------------------- 汇总
    if FIX:                     # 收尾：删掉测试期间建的探针人格
        try:
            req(port, "DELETE", "/admin/personas/" + FIX)
        except Exception:
            pass
    n_pass = sum(1 for r in results if r[1] == PASS)
    n_fail = sum(1 for r in results if r[1] == FAIL)
    n_skip = sum(1 for r in results if r[1] == SKIP)
    print("=" * 70)
    print(f"合计 {len(results)} 项：{n_pass} 通过 / {n_fail} 失败 / {n_skip} 跳过")
    if n_fail:
        print("失败项：")
        for name, st, detail, _ in results:
            if st == FAIL:
                print(f"  - {name}: {detail}")
    print("=" * 70)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
