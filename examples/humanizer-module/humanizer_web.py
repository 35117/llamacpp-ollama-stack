#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「去 AI 味」挂在桥上的后端（走 /humanize 前缀）。

和海龟汤同一套路子：**零额外进程** —— 服务起来后直接访问
    http://127.0.0.1:11434/humanize
就能用，不必单独跑一个服务。

职责只有两件：解析 `humanizer/` 下的静态文件、把 `/humanize/api/*` 分发给
`humanizer/humanizer_core.py`。业务逻辑一律不在这里。

注意：模型请求由调用方直接发往 **llama-swap 上游（11435）**，不走桥自己 ——
否则桥会递归调用自己。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HZ_DIR = os.path.join(ROOT, "humanizer")
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _d in (HZ_DIR, SCRIPTS_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import humanizer_core as hz  # noqa: E402
import swap_ctl  # noqa: E402  （模型驻留查询/卸载）

PREFIX = "/humanize"
API_PREFIX = PREFIX + "/api/"

CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
}


def _resolve(rel):
    """把 /humanize/xxx 映射到 humanizer/xxx；越界（路径穿越）返回 None。"""
    rel = (rel or "").lstrip("/")
    if not rel:
        rel = "index.html"
    full = os.path.normpath(os.path.join(HZ_DIR, rel))
    if full != HZ_DIR and not full.startswith(HZ_DIR + os.sep):
        return None
    return full


def static_file(rel):
    """返回 (bytes, ctype)；找不到或越界返回 (None, None)。"""
    full = _resolve(rel)
    if not full or not os.path.isfile(full):
        return None, None
    ext = os.path.splitext(full)[1].lower()
    ctype = CTYPES.get(ext, "application/octet-stream")
    try:
        with open(full, "rb") as fh:
            return fh.read(), ctype
    except OSError:
        return None, None


def api_name(path):
    """从 /humanize/api/run 取出 run；不是 API 路径则返回 None。"""
    if not path.startswith(API_PREFIX):
        return None
    return path[len(API_PREFIX):].strip("/")


# ---------------------------------------------------------------- 各接口

def _model_info(base):
    """报告模型是否就绪 + 走哪种量化文件（只看文件在不在，不探测显存）。"""
    try:
        pf = hz.load_prompt_format()
        fp_ok = hz.check_prompt_format(pf)
    except Exception as exc:  # noqa: BLE001
        return {"ready": False, "error": str(exc), "base": base, "model": hz.DEFAULT_MODEL}
    models_dir = os.path.join(ROOT, "models")
    variants = []
    for name, note in (
        ("humanizer-12b-Q8_0.gguf", "32GB+ 内存，质量最好"),
        ("humanizer-12b-Q6_K.gguf", "16GB 内存"),
        ("humanizer-12b-Q4_K_M.gguf", "约 14GB 内存"),
        ("humanizer-12b-Q3-QAT.gguf", "12GB 内存（本机档位）"),
        ("humanizer-12b-IQ2_XS-QAT.gguf", "8GB 内存，最小"),
    ):
        p = os.path.join(models_dir, name)
        if os.path.exists(p):
            variants.append({"file": name, "gb": round(os.path.getsize(p) / 2**30, 2),
                             "note": note})
    return {
        "ready": True,
        "base": base,
        "model": hz.DEFAULT_MODEL,
        "prompt_fingerprint_ok": fp_ok,
        # 是否正驻留显存 —— 页面据此显示「已加载 / 未加载」和释放按钮
        "loaded": hz.DEFAULT_MODEL in swap_ctl.running_names(base=base, timeout=4),
        "variants": variants,
        "sampling": hz.SAMPLING,
        "max_try": hz.MAX_TRY,
    }


def api_status(body, base=None):
    return 200, _model_info(base or hz.DEFAULT_BASE)


def api_run(body, base=None):
    """一次性返回（非流式）。适合脚本调用。"""
    text = (body or {}).get("text") or (body or {}).get("draft") or ""
    if not text.strip():
        return 400, {"error": "输入为空"}
    try:
        res = hz.humanize(text, base=base or hz.DEFAULT_BASE,
                          model=hz.DEFAULT_MODEL,
                          timeout=float((body or {}).get("timeout") or 900))
    except hz.HumanizeError as exc:
        return 502, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return 500, {"error": f"服务端异常: {exc}"}
    return 200, res


def api_inspect(body, base=None):
    """只做本地检查（不调模型）：给前端预览分块与数字抽取结果。"""
    text = (body or {}).get("text") or ""
    plan = hz.plan_pieces(text) if text.strip() else []
    return 200, {
        "blocks": len(plan),
        "rewrite_pieces": sum(1 for p in plan if p["kind"] == "prose"),
        "kept": sum(1 for p in plan if p["kind"] == "keep"),
        "lang": hz.detect_lang(text) if text.strip() else "en",
        "units": hz.length_units(text),
        "numbers": sorted(hz.normalize_numbers(text)),
        "plan": [{"kind": p["kind"],
                  "chars": len(p["text"]),
                  "head": p["text"][:60]} for p in plan],
    }


def api_models(body, base=None):
    """当前驻留在显存里的模型 —— 页面据此显示「已加载 / 未加载」。"""
    base = base or hz.DEFAULT_BASE
    names = swap_ctl.running_names(base=base, timeout=6)
    v = swap_ctl.version(base=base, timeout=4) or {}
    return 200, {"running": names, "loaded": hz.DEFAULT_MODEL in names,
                 "version": v.get("version")}


def api_unload(body, base=None):
    """卸载模型释放显存。默认卸本功能自己的模型；body.all=true 则全卸。

    小模型（12B）加载要 ~10 秒，所以**不自动卸**：由用户点按钮或让
    llama-swap 的空闲 TTL 兜底（见 config/llama-swap.yaml 的 ttl）。
    """
    base = base or hz.DEFAULT_BASE
    body = body or {}
    model = None if body.get("all") else (body.get("model") or hz.DEFAULT_MODEL)
    ok, msg = swap_ctl.unload(model, base=base)
    return (200 if ok else 502), {
        "ok": ok, "message": msg,
        "running": swap_ctl.running_names(base=base, timeout=5),
    }


ROUTES = {
    "status": api_status,
    "run": api_run,
    "inspect": api_inspect,
    "models": api_models,
    "unload": api_unload,
}


def handle_api(method, name, body, base=None):
    """返回 (status, payload dict)。"""
    fn = ROUTES.get(name)
    if not fn:
        return 404, {"error": f"no such route: {method} {name}"}
    if method not in ("POST", "GET"):
        return 405, {"error": f"method not allowed: {method}"}
    try:
        return fn(body or {}, base=base)
    except hz.HumanizeError as exc:
        return 502, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return 500, {"error": f"服务端异常: {exc}"}


# ---------------------------------------------------------------- 流式接口

def stream_run(body, base=None):
    """流式（ndjson）改写：逐块回传进度，长文不至于「一片空白等一分钟」。

    产出 dict，由桥写成一行行 JSON。
    """
    text = (body or {}).get("text") or (body or {}).get("draft") or ""
    if not text.strip():
        yield {"type": "error", "error": "输入为空"}
        return
    base = base or hz.DEFAULT_BASE
    events: list[dict] = []

    def on_event(ev):
        events.append(ev)

    # humanize 是同步的，这里在生成器里跑，边跑边吐。
    # 用一个简单的协作方式：把 on_event 的事件塞进队列，主循环 yield。
    import queue
    import threading

    q: "queue.Queue" = queue.Queue()

    def emit(ev):
        q.put(ev)

    def worker():
        try:
            hz.humanize(text, base=base, model=hz.DEFAULT_MODEL, on_event=emit,
                        timeout=float((body or {}).get("timeout") or 900))
        except Exception as exc:  # noqa: BLE001
            q.put({"type": "error", "error": str(exc)})
        finally:
            q.put(None)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    while True:
        ev = q.get()
        if ev is None:
            break
        yield ev
