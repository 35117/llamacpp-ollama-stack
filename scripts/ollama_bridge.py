#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ollama 兼容桥 —— 让 llama.cpp 顶掉 Ollama 而不改任何调用方。

为什么需要这一层：
  Ollama 一个端口（11434）同时提供两种协议：
    a) OpenAI 协议   /v1/chat/completions  —— llama-swap 原生支持
    b) Ollama 原生   /api/chat /api/generate /api/tags ...  —— llama-swap 不支持
  而且 Ollama 的 `create` 能把 SYSTEM 人格烙进模型，llama-swap 是「一模型一进程」，
  没有这个概念。

  所以本桥接层监听 11434，同时：
    1) 把 Ollama 原生 /api/* 翻译成 OpenAI 协议转发给 llama-swap（11435）；
    2) 按模型名注入人格提示词（同一份权重可以对外表现为多个人格，切换零重载）；
    3) /v1/* 原样透传，但重写 model 字段、并补齐 /v1/models 里的人格列表。

依赖：仅标准库。不依赖 requests / PyYAML。
"""

import argparse
import hashlib
import http.client
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gguf_probe   # noqa: E402  （同目录，用来直接读本地 GGUF 头）
import bridge_admin  # noqa: E402  （Web 界面：人格管理 + 对话历史）
import residency  # noqa: E402      （模型驻留：闲置自动卸载 / 手动卸载）
try:
    import turtle_web  # noqa: E402  （海龟汤小游戏，挂在 /turtle 下）
except Exception:      # noqa: BLE001  —— 缺了也不能让桥起不来
    turtle_web = None
try:
    import humanizer_web  # noqa: E402  （去 AI 味重写，挂在 /humanize 下）
except Exception:         # noqa: BLE001  —— 同上，缺了只影响这一个功能
    humanizer_web = None

# ---------------------------------------------------------------- 配置

DEFAULT_BRIDGE_CFG = {
    "listen_host": "127.0.0.1",
    "listen_port": 11434,
    "upstream": "http://127.0.0.1:11435",
    "ollama_version": "0.30.10",
    "log_requests": True,
    # 空串 = 只放行同源与 localhost（推荐，见 BridgeHandler.end_headers 的说明）。
    # 只有确实需要跨源浏览器客户端时才显式指定，例如 "http://localhost:5173"。
    # "*" = 恢复到无限制放行（任意网页都能驱动本机 GPU、读写人格与会话）。
    "cors": {"allow_origin": ""},
}

# 海龟汤小游戏的挂载前缀（实现在 scripts/turtle_web.py）
TURTLE_PREFIX = "/turtle"

# 「去 AI 味」重写的挂载前缀（实现在 scripts/humanizer_web.py）
HUMANIZE_PREFIX = "/humanize"

# 请求里 Ollama 的 options -> OpenAI/llama.cpp 请求体字段
OPTION_MAP = {
    "temperature": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",
    "min_p": "min_p",
    "typical_p": "typical_p",
    "seed": "seed",
    "num_predict": "max_tokens",
    "stop": "stop",
    "repeat_penalty": "repeat_penalty",
    "repeat_last_n": "repeat_last_n",
    "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty",
}

# 这里只影响 /api/show 与 /api/tags 里上报的能力标记，
# 真正的开关在 config/llama-swap.yaml 的 capabilities 里。
THINKING_BASES = {"qwen3-r1-8b", "qwen3-8b", "ds-r1-heretic", "qwen35-2b", "qwen35-4b-heretic"}


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _partial_digest(path, size, window=8 << 20):
    """大小 + 首尾各 window 字节 的 sha256。

    不做整文件哈希：30GB 的模型库要跑一两分钟，启动时不可接受。
    结果稳定、能区分不同文件，客户端也只把它当不透明标识。
    """
    h = hashlib.sha256()
    h.update(str(size).encode())
    try:
        with open(path, "rb") as fh:
            h.update(fh.read(window))
            if size > 2 * window:
                fh.seek(-window, os.SEEK_END)
                h.update(fh.read(window))
    except OSError:
        pass
    return h.hexdigest()


class Registry:
    """把 bases + personas 合成一张「对外模型名 -> 运行参数」的表。"""

    def __init__(self, personas_json, model_map, list_opts=None):
        self.personas_json = personas_json
        self.model_map = {m["ollama_id"]: m for m in model_map.get("models", [])}
        self._size_cache = {}      # 探测开销大，reload 时保留
        # 「列表接口展示什么」的开关，来自 config/bridge.json 的 list 段。
        # 缺省**不列人格/别名** —— 外部客户端会把它们当成一堆独立模型，看着很乱。
        self.list_opts = dict(list_opts or {})
        # 重建期间不加锁的话，并发请求会读到被清空的 entries（随机「模型不存在」）。
        # 用 RLock 是因为 _build 内部与 read 路径可能在同一线程里嵌套取锁。
        self._lock = threading.RLock()
        self._build()

    def reload(self):
        """重新读 config，让界面上的编辑立刻生效（不用重启服务）。"""
        with self._lock:
            self._build()

    def _build(self):
        with self._lock:
            self.entries = {}
        self.order = []
        self.unsupported = {}      # 名称 -> 不适用的原因（对外不注册，但给明确报错）

        personas_json = self.personas_json
        mm = self.model_map
        bases = personas_json["bases"]

        # supported 是生成 personas.json 那一刻按「文件在不在」拍下的快照。模型后来补上了
        # （典型场景：先跑 init_config 初始化，再下载权重）快照就过期了，桥会一直报
        # 「权重文件不在 models/ 里」—— 与磁盘实际状态矛盾，很难查。
        # 这里按文件实际存在重判，但只做**恢复**不做撤销：真正不兼容的模型
        # （缺 per-layer 参数、文件被挪进 models/_unusable/）依然保持不支持。
        models_dir = os.path.join(ROOT, "models")
        for _bid, _b in bases.items():
            _gguf = _b.get("gguf")
            if _b.get("supported") is False and _gguf:
                if os.path.exists(os.path.join(models_dir, _gguf)):
                    _b["supported"] = True
                    _b["unsupported_reason"] = None

        def add(mid, base_id, system, params, label, kind, source=None):
            b = bases[base_id]
            merged = dict(b.get("params") or {})
            merged.update(params or {})
            self.entries[mid] = {
                "id": mid,
                "upstream_id": base_id,
                "base": b,
                "system": (system or "").strip() or None,
                "params": merged,
                "label": label,
                "kind": kind,          # base | persona
                "source": source,
            }
            # 人格 id 若与底座 id 重名会互相覆盖，这里去重防止列表出现重复项
            if mid not in self.order:
                self.order.append(mid)

        # 0) 与当前 llama.cpp 不兼容的底座：不注册，但记下原因给出可读报错
        for bid, b in bases.items():
            if b.get("supported") is False:
                reason = b.get("unsupported_reason") or "该模型与当前 llama.cpp 版本不兼容"
                self.unsupported[bid] = reason
                for alias in b.get("ollama_alias") or []:
                    self.unsupported[alias] = reason

        # 1) 底座模型本身也对外暴露（干净底座，无任何人格）
        for bid, b in bases.items():
            if b.get("supported") is False:
                continue
            add(bid, bid, None, {}, b["desc"], "base")

        # 2) 人格（底座可能被界面改过 → 用覆盖后的「有效底座」）
        ov = bridge_admin.overlay() or {}
        for p in personas_json["personas"]:
            pid = p["id"]
            o = ov.get(pid) or {}
            base_id = o.get("base") or p["base"]
            if bases.get(base_id, {}).get("supported") is False:
                # 底座没权重/不兼容：不注册，但留一条能看懂的报错，
                # 而不是让调用方拿到一个含糊的「模型不存在」。
                why = bases.get(base_id, {}).get("unsupported_reason") or "底座不可用"
                self.unsupported[pid] = (
                    f"人格 '{pid}' 的底座 '{base_id}' 不可用（{why}）；"
                    f"到「人格管理」里给它换个底座即可")
                continue
            system = o.get("system")
            if system is None:                 # 只改了底座，提示词仍是自动抽取的
                spath = os.path.join(ROOT, "config", p["system_file"])
                system = ""
                if os.path.exists(spath):
                    with open(spath, "r", encoding="utf-8") as fh:
                        system = fh.read()
            add(pid, base_id, system, p.get("params"), p.get("label"),
                "persona", p["source_modelfile"])
            if o:
                self.entries[pid]["origin"] = "override"   # 改过（提示词或底座）

        # 2.5) 叠加界面上**新建**的人格
        #      改动存在 config/personas-local/ 下，不会被 apply-personas.bat 覆盖。
        for pid, rec in ov.items():
            if rec["origin"] != "local":
                continue
            bid = rec.get("base")
            if bid not in bases or bases[bid].get("supported") is False:
                continue
            if pid in self.entries:
                continue
            add(pid, bid, rec["system"], None, rec.get("label") or pid,
                "persona", "界面新建")
            self.entries[pid]["origin"] = "local"

        # 3) Ollama 原生名 -> 人格/底座 的别名
        self.aliases = {}
        for alias, target in (personas_json.get("ollama_aliases") or {}).items():
            if target in self.unsupported:
                continue
            if target in self.entries:
                self.aliases[alias] = target
                self.entries[alias] = dict(self.entries[target], id=alias)
                if alias not in self.order:
                    self.order.append(alias)
        # 4) 底座对应的 GGUF 元信息在 gguf_meta() 里按需探测并缓存（_size_cache 由 __init__ 建）

    def resolve(self, model_id):
        # 入参类型兜底：{"model": 123} 会让 `"123" in ...` 之后的 `":" not in model_id`
        # 抛 TypeError，{"model": {...}} 会让 dict 当 key 抛 unhashable —— 两者都会一路
        # 穿到 socketserver，客户端只看到连接被重置。宁可当场返回 None 走 404 分支。
        if not isinstance(model_id, str) or not model_id.strip():
            return None
        with self._lock:
            return self._resolve_locked(model_id)

    def _resolve_locked(self, model_id):
        if model_id in self.entries:
            return self.entries[model_id]
        # Ollama 允许省略 tag，e.g. "qwen3" -> "qwen3:latest"
        if ":" not in model_id:
            for cand in (model_id + ":latest", model_id + ":8b"):
                if cand in self.entries:
                    return self.entries[cand]
        # 大小写不敏感兜底
        low = model_id.lower()
        for k, v in self.entries.items():
            if k.lower() == low:
                return v
        return None

    def list_ids(self, include_aliases=True):
        if include_aliases:
            return list(self.order)
        return [e["id"] for e in self.entries.values()
                if e["id"] in self.order and e["id"] not in self.aliases]

    def list_discoverable(self):
        """**列表接口**（/api/tags、/v1/models）该展示的名字。

        人格与 Ollama 旧别名默认不列：外部客户端（如 AstrBot）会把它们
        当成一堆独立模型显示，一个 7 模型的部署看着像 33 个模型。
        但它们**仍然可以直接按名字调用** —— 只是「不被发现」，不是「不可用」；
        想恢复旧行为，把 config/bridge.json 的 list.personas / list.aliases 改成 true。
        """
        show_personas = bool(self.list_opts.get("personas", False))
        show_aliases = bool(self.list_opts.get("aliases", False))
        out = []
        for mid in self.order:
            e = self.entries.get(mid)
            if e is None:
                continue
            if mid in self.aliases and not show_aliases:
                continue
            if e.get("kind") == "persona" and not show_personas:
                continue
            out.append(mid)
        return out

    def list_summary(self):
        """给启动横幅用：底座/人格/别名各多少，列出来的又有多少。"""
        n_base = sum(1 for m in self.order if (self.entries.get(m) or {}).get("kind") == "base"
                     and m not in self.aliases)
        n_persona = sum(1 for m in self.order if (self.entries.get(m) or {}).get("kind") == "persona"
                        and m not in self.aliases)
        return n_base, n_persona, len(self.aliases), len(self.list_discoverable())

    def gguf_meta(self, entry):
        """给出该底座对应 GGUF 的 size / digest / 架构信息。

        直接读本地文件（scripts/gguf_probe.py），不依赖 models/model_map.json ——
        那是从 Ollama 同步时期的产物，Ollama 卸载后就没意义了。
        单一数据源 = 实际的 .gguf 文件本身。

        digest 用「大小 + 首尾各 8MB」算，够稳定且足够快；
        不会等于 Ollama 原来的 blob sha256（那个要整文件哈希，30GB 太慢），
        但客户端只把它当不透明标识用。
        """
        base_id = entry["upstream_id"]
        cached = self._size_cache.get(base_id)
        if cached:
            return cached

        bpath = os.path.join(ROOT, "models", entry["base"]["gguf"])
        size, info = 0, {}
        digest = ""
        if os.path.exists(bpath):
            size = os.path.getsize(bpath)
            try:
                pr = gguf_probe.probe(bpath)
                if pr.get("ok"):
                    info = {
                        "family": pr.get("arch"),
                        "block_count": pr.get("block_count"),
                        "context_length": pr.get("context_length"),
                        "embedding_length": pr.get("embedding_length"),
                        "quant": pr.get("quant"),
                        "name": pr.get("name"),
                        "n_params": pr.get("n_params"),
                        "size": size,
                    }
            except Exception as exc:
                sys.stderr.write(f"[warn] probe {bpath} 失败: {exc}\n")
            digest = "sha256:" + _partial_digest(bpath, size)
        elif self.model_map:
            # 本地文件缺失时的兜底：翻历史记录
            target = os.path.basename(entry["base"]["gguf"]).replace(".gguf", "")
            for m in self.model_map.values():
                if m.get("gguf") and os.path.basename(m["gguf"]).replace(".gguf", "") == target:
                    gi = m.get("gguf_info") or {}
                    info = {"family": gi.get("arch"), "block_count": gi.get("block_count"),
                            "context_length": gi.get("context_length"),
                            "embedding_length": gi.get("embedding_length"),
                            "quant": gi.get("quant"), "size": gi.get("size") or size}
                    break
        if not digest:
            digest = "sha256:" + ("0" * 64)
        res = {"size": size, "digest": digest, "info": info}
        self._size_cache[base_id] = res
        return res

    def param_size(self, entry):
        """参数量描述，如 8.2B。优先用解析出的准确张量元素总数。"""
        gi = self.gguf_meta(entry)["info"]
        n = gi.get("n_params")
        if n:
            return f"{n / 1e9:.1f}B" if n >= 1e9 else f"{n / 1e6:.0f}M"
        blk, emb = gi.get("block_count"), gi.get("embedding_length")
        if not blk or not emb:
            return "unknown"
        return f"{(12 * blk * emb * emb) / 1e9:.1f}B（估算）"

    def capabilities(self, entry):
        base_id = entry["upstream_id"]
        b = entry["base"]
        caps = []
        if b.get("embedding"):
            return ["embedding"]
        caps.append("completion")
        if base_id in THINKING_BASES:
            caps.append("thinking")
        if b.get("vision"):
            caps.append("vision")
        caps.append("tools")
        return caps


# ---------------------------------------------------------------- 上游调用

class Upstream:
    def __init__(self, url, logger):
        p = urllib.parse.urlsplit(url)
        self.host = p.hostname or "127.0.0.1"
        self.port = p.port or 80
        self.base = url.rstrip("/")
        self.log = logger

    def open(self, method, path, body=None, headers=None, timeout=1800):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Length"] = str(len(payload))
        conn.request(method, path, body=payload, headers=hdrs)
        return conn, conn.getresponse()

    def json(self, method, path, body=None, timeout=1800):
        conn, resp = self.open(method, path, body, timeout=timeout)
        try:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8"))
            except Exception:
                return resp.status, {"_raw": raw.decode("utf-8", "replace")}
        finally:
            conn.close()

    def stream_raw(self, method, path, body=None, timeout=1800):
        conn, resp = self.open(method, path, body, timeout=timeout)
        return conn, resp


# ---------------------------------------------------------------- 工具函数

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sse_iter(resp):
    """逐行解析 OpenAI 的 SSE 流，yield 出每个 data: 后面的 JSON 对象。
    最后以 None 结束（对应 data: [DONE] 或流自然断掉）。"""
    while True:
        raw_line = resp.readline()
        if not raw_line:
            yield None
            break
        line = raw_line.decode("utf-8", "replace").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            yield None
            break
        try:
            yield json.loads(data)
        except Exception:
            continue


def ollama_images_to_openai(images):
    """Ollama 的 images 是裸 base64；OpenAI 需要 data URI。"""
    out = []
    for img in images or []:
        if not isinstance(img, str):
            continue
        if img.startswith("data:"):
            url = img
        else:
            url = "data:image/png;base64," + img
        out.append({"type": "image_url", "image_url": {"url": url}})
    return out


def convert_messages(msgs):
    """Ollama 消息 -> OpenAI 消息（主要处理 images 与 tool_calls 字段差异）。"""
    out = []
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = m.get("content", "")
        images = m.get("images") or []
        nm = {"role": role}
        if images and role == "user":
            parts = []
            if content:
                parts.append({"type": "text", "text": content})
            parts.extend(ollama_images_to_openai(images))
            nm["content"] = parts
        else:
            nm["content"] = content
        if m.get("tool_calls"):
            nm["tool_calls"] = m["tool_calls"]
        if m.get("tool_name") and not nm.get("name"):
            nm["name"] = m["tool_name"]
        if m.get("tool_call_id"):
            nm["tool_call_id"] = m["tool_call_id"]
        out.append(nm)
    return out


def inject_system(messages, system_text):
    """把人格提示词合并进消息列表（不产生两条 system，避免模板渲染异常）。"""
    if not system_text:
        return messages
    msgs = [dict(m) for m in messages]
    if msgs and msgs[0].get("role") == "system":
        old = msgs[0].get("content") or ""
        if isinstance(old, str) and old.strip() == system_text.strip():
            return msgs
        if isinstance(old, str):
            msgs[0]["content"] = system_text.strip() + "\n\n" + old
            return msgs
    return [{"role": "system", "content": system_text.strip()}] + msgs


def build_options(entry, options):
    """合并 底座默认采样值 + 请求 options + Ollama 默认值。"""
    body = {}
    merged = dict(entry.get("params") or {})
    merged.update(options or {})
    for k, v in merged.items():
        target = OPTION_MAP.get(k)
        if not target or v is None:
            continue
        if k == "num_predict" and (v == -1 or v == 0):
            continue  # -1 = 无限，交给上游默认
        body[target] = v
    return body


def merge_tool_call_deltas(acc, deltas):
    """把 OpenAI 的 tool_call **分片增量**合并成完整调用。

    坑：流式响应里同一个 `index` 的 arguments 是**逐字符追加**的 —— 首帧给
    id/name，后续帧每帧吐几个字符。直接 `acc.extend(delta)` 会把 N 个分片当成
    N 个独立调用，之后逐个 `json.loads` 出来的全是 `{"_raw": "{\\ti"}` 这种
    碎片，工具调用实际上完全不可用。

    所以按 index 归位，把 name/arguments 累加到同一个槽位上。
    """
    for d in deltas:
        idx = d.get("index")
        if idx is None:
            acc.append(d)
            continue
        while len(acc) <= idx:
            acc.append({"index": len(acc), "id": None, "type": "function",
                        "function": {"name": "", "arguments": ""}})
        slot = acc[idx]
        if d.get("id"):
            slot["id"] = d["id"]
        if d.get("type"):
            slot["type"] = d["type"]
        fn = d.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]
    return acc


def tool_calls_to_ollama(tcs):
    """OpenAI tool_calls -> Ollama tool_calls（arguments 从 JSON 串变对象）。"""
    out = []
    for tc in tcs or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"_raw": args}
        out.append({"function": {"name": fn.get("name"), "arguments": args or {}}})
    return out


def stats_from(usage, timings):
    """映射成 Ollama 的计数字段。"""
    out = {}
    if usage:
        if usage.get("prompt_tokens") is not None:
            out["prompt_eval_count"] = usage["prompt_tokens"]
        if usage.get("completion_tokens") is not None:
            out["eval_count"] = usage["completion_tokens"]
    if timings:
        if timings.get("prompt_ms") is not None:
            out["prompt_eval_duration"] = int(timings["prompt_ms"] * 1e6)
        if timings.get("predicted_ms") is not None:
            out["eval_duration"] = int(timings["predicted_ms"] * 1e6)
        if timings.get("prompt_ms") is not None and timings.get("predicted_ms") is not None:
            out["total_duration"] = int((timings["prompt_ms"] + timings["predicted_ms"]) * 1e6)
    return out


# ---------------------------------------------------------------- HTTP 处理

class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OllamaBridge/1.0"
    registry = None
    upstream = None
    bridge_cfg = None

    # -------------------------------------------------- 底层写入helper
    def log_message(self, fmt, *args):
        if self.bridge_cfg.get("log_requests", True):
            sys.stderr.write("[%s] %s\n" % (now_iso(), fmt % args))
            sys.stderr.flush()

    def _send_json(self, obj, status=200, extra_headers=None):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _send_text(self, text, status=200, ctype="text/plain; charset=utf-8"):
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_bytes(self, payload, ctype="application/octet-stream", status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ------------------------------------------- Web 界面 + 管理 API（/ui /admin）
    def _is_admin_route(self, path):
        return path in ("/ui", "/ui/") or path.startswith("/ui/") \
            or path.startswith("/admin/") \
            or path == TURTLE_PREFIX or path.startswith(TURTLE_PREFIX + "/") \
            or path == HUMANIZE_PREFIX or path.startswith(HUMANIZE_PREFIX + "/")

    def _handle_admin(self, method, path, query):
        # 先把请求体一次性读完 —— HTTP/1.1 keep-alive 下，没读完的 body 会残留下来
        # 污染同一连接上的下一个请求（会表现为「上一个请求的 JSON 跑到下一个」）。
        body = self._read_body() if method in ("POST", "PUT", "DELETE", "PATCH") else {}

        # ---------- 静态页面
        if path == "/ui" or path.startswith("/ui/"):
            rel = path[3:] or "/"
            data, ctype = bridge_admin.static_file(rel)
            if data is None:
                return self._err("界面文件不存在（应为 ui/index.html）", status=404)
            return self._send_bytes(data, ctype)

        # ---------- 海龟汤小游戏（挂在 /turtle，实现在 scripts/turtle_web.py）
        if path == TURTLE_PREFIX or path.startswith(TURTLE_PREFIX + "/"):
            return self._handle_turtle(method, path, body)

        # ---------- 去 AI 味（挂在 /humanize，实现在 scripts/humanizer_web.py）
        if path == HUMANIZE_PREFIX or path.startswith(HUMANIZE_PREFIX + "/"):
            return self._handle_humanize(method, path, body)

        # ---------- 模型驻留（闲置自动卸载 / 手动卸载）
        if path == "/admin/residency" or path.startswith("/admin/residency/"):
            return self._handle_residency(method, body)

        parts = [p for p in path.strip("/").split("/") if p]   # admin/xxx/yyy
        seg = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else None

        try:
            # ---------- 状态总览
            if seg == "state" and method == "GET":
                st = bridge_admin.snapshot(self.registry)
                st["service"] = {
                    "bridge_port": self.bridge_cfg.get("listen_port"),
                    "upstream": self.upstream.base if hasattr(self.upstream, "base") else None,
                    "models": len(self.registry.order),
                }
                return self._send_json(st)

            # ---------- 对话
            if seg == "chats":
                if method == "GET" and not arg:
                    return self._send_json({"chats": bridge_admin.list_chats()})
                if method == "GET" and arg:
                    d, err = bridge_admin.load_chat(arg)
                    return self._err(err, 404) if err else self._send_json(d)
                if method == "POST" and not arg:
                    return self._send_json({"id": bridge_admin.new_chat_id()}, 201)
                if method == "PUT" and arg:
                    rec, err = bridge_admin.save_chat(arg, body)
                    return self._err(err, 400) if err else self._send_json(rec)
                if method == "DELETE" and arg:
                    r, err = bridge_admin.delete_chat(arg)
                    return self._err(err, 400) if err else self._send_json(r)

            # ---------- 人格
            if seg == "personas":
                if method == "POST" and not arg:
                    r, err = bridge_admin.create_persona(body)
                    if err:
                        return self._err(err, 400)
                    self.registry.reload()
                    return self._send_json(r, 201)
                if method == "POST" and arg and parts[3:4] == ["reset"]:
                    r, err = bridge_admin.reset_persona(arg)
                    if err:
                        return self._err(err, 400)
                    self.registry.reload()
                    return self._send_json(r)
                if method == "PUT" and arg:
                    r, err = bridge_admin.update_persona(arg, body)
                    if err:
                        return self._err(err, 400)
                    self.registry.reload()
                    return self._send_json(r)
                if method == "DELETE" and arg:
                    r, err = bridge_admin.delete_persona(arg)
                    if err:
                        return self._err(err, 400)
                    self.registry.reload()
                    return self._send_json(r)
        except Exception as exc:
            import traceback
            sys.stderr.write(traceback.format_exc())
            return self._err(f"管理接口出错: {exc}", status=500)

        return self._err(f"未知管理路由: {method} {path}", status=404)

    # ------------------------------------------------------ 海龟汤小游戏
    def _handle_turtle(self, method, path, body):
        """把 /turtle 与 /turtle/api/* 交给 scripts/turtle_web.py。

        判题请求直接发往 **llama-swap 上游（11435）**，不走桥自己 ——
        否则桥会递归调用自己。
        """
        if turtle_web is None:
            return self._err(
                "海龟汤模块未加载（scripts/turtle_web.py 缺失或导入失败）",
                status=503)
        name = turtle_web.api_name(path)
        if name is None:
            rel = path[len(TURTLE_PREFIX):]
            data, ctype = turtle_web.static_file(rel)
            if data is None:
                return self._err("海龟汤页面不存在（应为 turtle/index.html）",
                                 status=404)
            return self._send_bytes(data, ctype)
        status, payload = turtle_web.handle_api(
            method, name, body,
            base=self.upstream.base if hasattr(self.upstream, "base") else None)
        return self._send_json(payload, status)

    # ------------------------------------------------------ 去 AI 味（humanizer）
    def _handle_residency(self, method, body):
        """模型驻留：查状态 / 改策略 / 手动卸载。

        GET  -> 驻留总览（谁在显存里、闲置多久、还有多久自动卸载）
        POST -> {"action":"unload","model":"名字"}  只卸这一个（省略 model = 全卸）
                {"action":"reap"}                  立刻跑一次闲置清理
                {"action":"set","policy":{...}}    改策略并落盘
        """
        try:
            if method == "GET":
                return self._send_json(residency.snapshot())
            if method != "POST":
                return self._err("只支持 GET / POST", status=405)

            body = body if isinstance(body, dict) else {}
            action = body.get("action") or "status"
            if action == "unload":
                ok, msg = residency.unload(body.get("model") or None,
                                           force=bool(body.get("force")))
                return self._send_json({"ok": ok, "message": msg},
                                       status=200 if ok else 400)
            if action == "reap":
                return self._send_json({"ok": True, "unloaded": residency.reap()})
            if action == "set":
                patch = body.get("policy")
                if not isinstance(patch, dict):
                    return self._err("policy 必须是对象", status=400)
                return self._send_json({"ok": True,
                                        "policy": residency.save_policy(patch)})
            return self._err("未知 action: %s" % action, status=400)
        except Exception as exc:      # noqa: BLE001
            return self._err(str(exc), status=500)

    def _handle_humanize(self, method, path, body):
        """把 /humanize 与 /humanize/api/* 交给 scripts/humanizer_web.py。

        模型请求由 humanizer_web 直接发往 **llama-swap 上游（11435）**，
        不走桥自己 —— 否则桥会递归调用自己。

        `/humanize/api/stream` 是流式（ndjson）：长文逐块回传进度，
        否则用户要对着空白屏幕等一两分钟。
        """
        if humanizer_web is None:
            return self._err(
                "「去 AI 味」模块未加载（scripts/humanizer_web.py 缺失或导入失败）",
                status=503)
        base = self.upstream.base if hasattr(self.upstream, "base") else None
        name = humanizer_web.api_name(path)
        if name is None:
            rel = path[len(HUMANIZE_PREFIX):]
            data, ctype = humanizer_web.static_file(rel)
            if data is None:
                return self._err("「去 AI 味」页面不存在（应为 humanizer/index.html）",
                                 status=404)
            return self._send_bytes(data, ctype)

        # 流式：逐块把进度写出去
        if name == "stream" and method == "POST":
            self._begin_ndjson()
            try:
                for ev in humanizer_web.stream_run(body or {}, base=base):
                    self._ndjson(ev)
            except Exception as exc:  # noqa: BLE001
                import traceback
                sys.stderr.write(traceback.format_exc())
                self._ndjson({"type": "error", "error": f"服务端异常: {exc}"})
            finally:
                self._end_chunked()
            return

        status, payload = humanizer_web.handle_api(method, name, body, base=base)
        return self._send_json(payload, status)

    def _begin_ndjson(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _write_chunk(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not data:
            return
        self.wfile.write(b"%x\r\n" % len(data))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _end_chunked(self):
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    def _ndjson(self, obj):
        self._write_chunk(json.dumps(obj, ensure_ascii=False) + "\n")

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _err(self, msg, status=400, code=None):
        self._send_json({"error": msg}, status=status)

    def _safe_err(self, msg, status=502):
        """在「响应头可能已经发出去」的前提下安全地报错。

        坑（本文件实测）：流式接口会先发 200 + Transfer-Encoding: chunked，
        进入转发循环后上游一旦断流/超时/报错，异常分支再调 _send_json 就会往
        chunked 体里写**第二组 HTTP 状态行**。那既不是合法的 chunk-size，也
        没有终止块 —— 客户端要么报 InvalidChunkLength，要么一直挂到超时，而
        真正的错误码永远送不到。

        所以这里分流：头没发 -> 正常发错误状态码；头已发 -> 补一个错误 chunk
        并正确终止 chunked 流，然后关掉这条连接（协议已经无法继续复用）。
        """
        if getattr(self, "_headers_sent", False):
            try:
                self._ndjson({"error": msg, "done": True,
                              "done_reason": "error"})
            except Exception:      # noqa: BLE001  客户端已经断开
                pass
            self._end_chunked()
            self.close_connection = True
            return
        self._send_json({"error": msg}, status=status)

    def _known_unsupported(self, name):
        """这个名字（或省掉 tag 的形式）是不是「已知但不支持」的模型。

        用于 /v1：解析不到时不要一律 404 —— 那会误伤「llama-swap 原生模型名」
        的透传用法；只在**确实有原因**时给出可读报错。
        """
        if not name:
            return False
        if name in self.registry.unsupported:
            return True
        base = str(name).split(":")[0]
        return any(k.split(":")[0] == base for k in self.registry.unsupported)

    def _model_not_found(self, name):
        """未知模型名。若它其实是「已知但不兼容/不可用」的模型，给出具体原因而不是笼统 404。"""
        # 入参可能不是字符串（{"model": {"a":1}} 之类）—— dict/list 不可哈希，
        # 拿去当 key 会抛 TypeError，异常一路穿到 socketserver，客户端只看到
        # 连接被重置、服务端只在 stderr 留个 traceback。这里先兜住。
        if not isinstance(name, str):
            name = str(name)
        reason = self.registry.unsupported.get(name)
        if not reason:
            # 名称里可能省了 tag，例如 qwen3.5
            for key, why in self.registry.unsupported.items():
                if key.split(":")[0] == str(name).split(":")[0]:
                    reason = why
                    break
        if reason:
            return self._err(f"模型 '{name}' 在本部署中不可用。\n原因：{reason}", status=404)
        return self._err(f"model '{name}' not found", status=404)

    # -------------------------------------------------- 路由
    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only=False):
        split = urllib.parse.urlsplit(self.path)
        path = split.path
        if self._is_admin_route(path):
            return self._handle_admin("GET", path, split.query)
        if path in ("/", ""):
            # Ollama 客户端会拿这句话判断「服务在不在」
            return self._send_text("Ollama is running")
        if path == "/api/version":
            return self._send_json({"version": self.bridge_cfg["ollama_version"]})
        if path == "/api/tags":
            return self._api_tags()
        if path == "/api/ps":
            return self._api_ps()
        if path == "/v1/models":
            return self._v1_models()
        return self._passthrough("GET", head_only=head_only)

    def do_POST(self):
        split = urllib.parse.urlsplit(self.path)
        path = split.path
        if self._is_admin_route(path):
            return self._handle_admin("POST", path, split.query)
        if path == "/api/chat":
            return self._api_chat()
        if path == "/api/generate":
            return self._api_generate()
        if path == "/api/embed":
            return self._api_embed(legacy=False)
        if path == "/api/embeddings":
            return self._api_embed(legacy=True)
        if path == "/api/show":
            return self._api_show()
        if path in ("/api/create", "/api/copy", "/api/push", "/api/delete", "/api/pull"):
            name = path.rsplit("/", 1)[-1]
            return self._err(
                f"'{name}' 由 llama.cpp 部署接管，不再支持在线增删改模型。\n"
                f"   模型权重: {os.path.join(ROOT, 'models')}\\\n"
                f"   人格配置: {os.path.join(ROOT, 'config', 'personas')}  "
                f"(改完运行 scripts\\apply-personas.bat)\n"
                f"   服务配置: {os.path.join(ROOT, 'config', 'llama-swap.yaml')}",
                status=400,
            )
        if path in ("/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/v1/responses"):
            return self._v1_proxy(path)
        return self._passthrough("POST")

    def do_DELETE(self):
        split = urllib.parse.urlsplit(self.path)
        if self._is_admin_route(split.path):
            return self._handle_admin("DELETE", split.path, split.query)
        return self._passthrough("DELETE")

    def do_PUT(self):
        split = urllib.parse.urlsplit(self.path)
        if self._is_admin_route(split.path):
            return self._handle_admin("PUT", split.path, split.query)
        return self._passthrough("PUT")

    def send_response(self, code, message=None):
        # 记录「响应头已经开始发」，供 _safe_err 判断还能不能改状态码。
        super().send_response(code, message)
        self._headers_sent = True

    def handle_one_request(self):
        # HTTP/1.1 keep-alive 下同一个 handler 实例会连续服务多个请求，
        # 每次都得把标志位清掉，否则第二个请求会被误判成「头已发出」。
        self._headers_sent = False
        super().handle_one_request()

    def end_headers(self):
        """统一补 CORS 头。

        坑：之前只在 do_OPTIONS 里加过 —— 预检（OPTIONS）能过，
        但真正的 GET/POST 响应里没有 Access-Control-Allow-Origin，
        浏览器照样拦下来。凡是浏览器端客户端都受影响，所以在收尾处统一注入。
        """
        if not getattr(self, "_cors_done", False):
            self._cors_done = True
            # 以前是无条件 `*` —— 意味着用户在浏览器里打开**任意网页**，那个页面
            # 就能静默读写本机人格、对话历史，并用本地 GPU 跑推理（桥默认只监听
            # 127.0.0.1，但浏览器发起的请求天然带本机身份）。
            # 现在改成「回显同源 / localhost 来源」；跨源的浏览器客户端按需在
            # bridge.json 里设 cors.allow_origin（如 "http://localhost:5173"）。
            origin = self.headers.get("Origin") or ""
            allowed = (getattr(self.server, "cors_allow_origin", "") or "").strip()
            if allowed == "*":
                allow = "*"
            elif allowed and origin == allowed:
                allow = origin
            elif origin.startswith(("http://127.0.0.1", "http://localhost",
                                    "https://127.0.0.1", "https://localhost")):
                allow = origin
            else:
                allow = "null"
            self.send_header("Access-Control-Allow-Origin", allow)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -------------------------------------------------- 透传
    def _passthrough(self, method, head_only=False):
        conn = None
        try:
            conn, resp = self.upstream.open(method, self.path, body=None,
                                            headers={"Accept": "*/*"})
            data = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not head_only:
                self.wfile.write(data)
        except Exception as exc:
            self._err(f"上游 llama-swap 不可达: {exc}", status=502)
        finally:
            if conn:
                conn.close()

    # -------------------------------------------------- /v1 重写
    def _v1_proxy(self, path):
        body = self._read_body()
        entry = None
        if isinstance(body, dict) and body.get("model"):
            entry = self.registry.resolve(body["model"])
            if entry:
                body["model"] = entry["upstream_id"]
            elif self._known_unsupported(body["model"]):
                # 例如人格的底座被删了 —— 给和 /api/* 一样的可读报错，
                # 别把 llama-swap 的 "no router for requested model" 漏给调用方。
                return self._model_not_found(body["model"])
        if entry and path == "/v1/chat/completions":
            body["messages"] = inject_system(
                body.get("messages") or [], entry.get("system"))

        # 先连上游、确认状态，**再**决定发什么响应头。
        # 以前是先 send_response() 再连，上游一报错就只能把错误塞进 body，
        # 客户端照着 200 当成功处理（详见 _safe_err 的说明）。
        conn = None
        try:
            conn, resp = self.upstream.open("POST", path, body=body,
                                            headers={"Accept": "*/*"})
        except Exception as exc:      # noqa: BLE001
            return self._safe_err(f"上游 llama-swap 不可达: {exc}", status=502)

        try:
            ctype = resp.getheader("Content-Type", "application/json")
            is_stream = "event-stream" in ctype
            if resp.status >= 400:
                # 上游明确报错：状态码与原文都如实透回去。
                text = resp.read().decode("utf-8", "replace")
                try:
                    return self._send_json(json.loads(text), status=resp.status)
                except ValueError:
                    return self._send_json({"error": text}, status=resp.status)
            self.send_response(resp.status)
            self.send_header("Content-Type", ctype)
            if is_stream:
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    self._write_chunk(line)
                self._end_chunked()
            else:
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except Exception as exc:      # noqa: BLE001
            self._safe_err(f"转发中断: {exc}", status=502)
        finally:
            if conn:
                conn.close()

    # -------------------------------------------------- /v1/models
    def _v1_models(self):
        models = []
        for mid in self.registry.list_discoverable():
            e = self.registry.entries[mid]
            gm = self.registry.gguf_meta(e)
            models.append({
                "id": mid,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "llama.cpp",
                "name": e["label"],
                "context_length": e["base"]["ctx"],
                "size": gm["size"],
                "capabilities": self.registry.capabilities(e),
            })
        self._send_json({"object": "list", "data": models})

    # -------------------------------------------------- /api/tags
    def _api_tags(self):
        out = []
        for mid in self.registry.list_discoverable():
            e = self.registry.entries[mid]
            gm = self.registry.gguf_meta(e)
            out.append({
                "name": mid,
                "model": mid,
                "modified_at": now_iso(),
                "size": gm["size"],
                "digest": gm["digest"],
                "details": {
                    "parent_model": "",
                    "format": "gguf",
                    "family": (gm["info"].get("family") or "llama"),
                    "families": [gm["info"].get("family") or "llama"],
                    "parameter_size": self.registry.param_size(e),
                    "quantization_level": "GGUF",
                },
            })
        self._send_json({"models": out})

    # -------------------------------------------------- /api/ps
    def _api_ps(self):
        models = []
        try:
            status, data = self.upstream.json("GET", "/running", timeout=5)
            running = []
            if isinstance(data, dict):
                running = list(data.get("running") or data.get("models") or [])
            elif isinstance(data, list):
                running = data
            for item in running:
                mid = item if isinstance(item, str) else (item.get("model") or item.get("id"))
                if not mid:
                    continue
                e = self.registry.resolve(mid)
                if not e:
                    continue
                gm = self.registry.gguf_meta(e)
                models.append({
                    "name": mid, "model": mid,
                    "size": gm["size"], "digest": gm["digest"],
                    "details": {"format": "gguf", "family": "llama",
                                "parameter_size": self.registry.param_size(e),
                                "quantization_level": "GGUF"},
                    "expires_at": "9999-12-31T23:59:59Z",
                    "size_vram": gm["size"],
                })
        except Exception as exc:      # noqa: BLE001
            # 以前是 except: pass —— swap 挂了 / /running 结构变了，界面上都只显示
            # 「没有模型在跑」，与真实状态相反，排障时被自己骗。至少留下痕迹，
            # 并给调用方一个能区分「真的空」和「查不到」的字段。
            sys.stderr.write(f"[bridge] /api/ps 查询失败: {exc}\n")
            sys.stderr.flush()
            return self._send_json({"models": models, "upstream_ok": False,
                                    "error": str(exc)})
        self._send_json({"models": models, "upstream_ok": True})

    # -------------------------------------------------- /api/show
    def _api_show(self):
        body = self._read_body()
        name = body.get("model") or body.get("name")
        e = self.registry.resolve(name)
        if not e:
            return self._model_not_found(name)
        gm = self.registry.gguf_meta(e)
        system = e.get("system") or ""
        params_txt = "\n".join(f'PARAMETER {k} {json.dumps(v, ensure_ascii=False)}'
                               for k, v in (e.get("params") or {}).items())
        modelfile = f"# 由 llama.cpp 部署提供 · 底座 {e['upstream_id']}\nFROM {e['base']['gguf']}\n"
        if system:
            modelfile += f'SYSTEM """{system}"""\n'
        if params_txt:
            modelfile += params_txt + "\n"
        self._send_json({
            "license": "",
            "modelfile": modelfile,
            "parameters": params_txt,
            "template": "{{ .Prompt }}",
            "system": system,
            "details": {
                "parent_model": "",
                "format": "gguf",
                "family": gm["info"].get("family") or "llama",
                "families": [gm["info"].get("family") or "llama"],
                "parameter_size": self.registry.param_size(e),
                "quantization_level": "GGUF",
            },
            "model_info": {
                "general.architecture": gm["info"].get("family"),
                "general.name": e["base"]["file_name"],
                f"{gm['info'].get('family') or 'llama'}.context_length":
                    gm["info"].get("context_length"),
                f"{gm['info'].get('family') or 'llama'}.block_count":
                    gm["info"].get("block_count"),
                f"{gm['info'].get('family') or 'llama'}.embedding_length":
                    gm["info"].get("embedding_length"),
            },
            "capabilities": self.registry.capabilities(e),
        })

    # -------------------------------------------------- /api/chat
    def _api_chat(self):
        body = self._read_body()
        name = body.get("model")
        e = self.registry.resolve(name)
        if not e:
            return self._model_not_found(name)
        # 记一次使用 —— 闲置自动卸载靠它计时（/running 里报的是上游模型名）
        residency.note_use(e["upstream_id"])

        stream = body.get("stream", True)
        req_options = body.get("options") or {}
        msgs = convert_messages(body.get("messages"))
        if body.get("system"):
            msgs = inject_system(msgs, body["system"])
        msgs = inject_system(msgs, e.get("system"))

        payload = {"model": e["upstream_id"], "messages": msgs, "stream": bool(stream)}
        payload.update(build_options(e, req_options))

        fmt = body.get("format")
        if fmt == "json":
            payload["response_format"] = {"type": "json_object"}
        elif isinstance(fmt, dict):
            payload["response_format"] = {"type": "json_schema", "json_schema": fmt}

        if body.get("tools"):
            payload["tools"] = body["tools"]
        if body.get("think") is False:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if body.get("keep_alive") is not None:
            pass  # llama-swap 用 ttl 管理生命周期，这里忽略
        if stream:
            payload["stream_options"] = {"include_usage": True}

        if not stream:
            status, data = self.upstream.json("POST", "/v1/chat/completions", payload)
            if status != 200:
                return self._send_json(data, status=status)
            return self._send_json(self._chat_response(e, data, stream=False))

        # 先连上游再 _begin_ndjson()：这样上游报错时我们还能回真正的错误状态码，
        # 而不是 200 + 一个藏在 body 里的 error 字段（详见 _safe_err）。
        conn = None
        try:
            conn, resp = self.upstream.open("POST", "/v1/chat/completions", payload)
        except Exception as exc:      # noqa: BLE001
            return self._safe_err(f"上游 llama-swap 不可达: {exc}", status=502)

        try:
            if resp.status != 200:
                text = resp.read().decode("utf-8", "replace")
                try:
                    return self._send_json(json.loads(text), status=resp.status)
                except ValueError:
                    return self._send_json({"error": text}, status=resp.status)
            self._begin_ndjson()
            acc = {"content": "", "thinking": "", "tool_calls": [], "usage": None,
                   "timings": None, "done_reason": None}
            for obj in sse_iter(resp):
                if obj is None:
                    break
                choice = (obj.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                finish = choice.get("finish_reason")
                if finish:
                    acc["done_reason"] = finish
                if obj.get("usage"):
                    acc["usage"] = obj["usage"]
                if obj.get("timings"):
                    acc["timings"] = obj["timings"]
                if not delta:
                    continue
                msg = {"role": "assistant"}
                if delta.get("content"):
                    msg["content"] = delta["content"]
                    acc["content"] += delta["content"]
                if delta.get("reasoning_content"):
                    msg["thinking"] = delta["reasoning_content"]
                    acc["thinking"] += delta["reasoning_content"]
                if delta.get("tool_calls"):
                    merge_tool_call_deltas(acc["tool_calls"],
                                           delta["tool_calls"])
                self._ndjson({
                    "model": name, "created_at": now_iso(),
                    "message": msg, "done": False,
                })
            final = {
                "model": name, "created_at": now_iso(),
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": acc["done_reason"] or "stop",
            }
            if acc["tool_calls"]:
                final["message"]["tool_calls"] = tool_calls_to_ollama(acc["tool_calls"])
            final.update(stats_from(acc["usage"], acc["timings"]))
            self._ndjson(final)
            self._end_chunked()
        except Exception as exc:      # noqa: BLE001
            self._safe_err(f"生成中断: {exc}", status=502)
        finally:
            if conn:
                conn.close()

    def _chat_response(self, entry, data, stream=False):
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        out_msg = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("reasoning_content"):
            out_msg["thinking"] = msg["reasoning_content"]
        if msg.get("tool_calls"):
            out_msg["tool_calls"] = tool_calls_to_ollama(msg["tool_calls"])
        res = {
            "model": entry["id"],
            "created_at": now_iso(),
            "message": out_msg,
            "done": True,
            "done_reason": choice.get("finish_reason") or "stop",
        }
        res.update(stats_from(data.get("usage"), data.get("timings")))
        return res

    # -------------------------------------------------- /api/generate
    def _api_generate(self):
        body = self._read_body()
        name = body.get("model")
        e = self.registry.resolve(name)
        if not e:
            return self._model_not_found(name)
        # 同 /api/chat：记一次使用
        residency.note_use(e["upstream_id"])

        stream = body.get("stream", True)
        req_options = body.get("options") or {}
        prompt = body.get("prompt", "")
        raw = bool(body.get("raw"))
        images = body.get("images") or []

        payload = {"model": e["upstream_id"], "stream": bool(stream)}
        payload.update(build_options(e, req_options))

        use_completions = raw and not e.get("system") and not body.get("system")
        if use_completions:
            payload["prompt"] = prompt
            target = "/v1/completions"
        else:
            user_msg = {"role": "user", "content": prompt}
            if images:
                user_msg["images"] = images
            msgs = inject_system(convert_messages([user_msg]), body.get("system"))
            msgs = inject_system(msgs, e.get("system"))
            payload["messages"] = msgs
            target = "/v1/chat/completions"
            fmt = body.get("format")
            if fmt == "json":
                payload["response_format"] = {"type": "json_object"}
            elif isinstance(fmt, dict):
                payload["response_format"] = {"type": "json_schema", "json_schema": fmt}
            if stream:
                payload["stream_options"] = {"include_usage": True}
            # 与 /api/chat 对称：显式 think=False 时关闭思考。
            # 注意只对 chat completions 生效（/v1/completions 没有聊天模板）。
            if body.get("think") is False:
                payload["chat_template_kwargs"] = {"enable_thinking": False}

        if not stream:
            status, data = self.upstream.json("POST", target, payload)
            if status != 200:
                return self._send_json(data, status=status)
            if use_completions:
                choice = (data.get("choices") or [{}])[0]
                text = choice.get("text") or ""
                finish = choice.get("finish_reason")
            else:
                choice = (data.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content") or ""
                finish = choice.get("finish_reason")
            res = {
                "model": name, "created_at": now_iso(), "response": text,
                "done": True, "done_reason": finish or "stop",
                "context": [],
            }
            res.update(stats_from(data.get("usage"), data.get("timings")))
            return self._send_json(res)

        # 同 /api/chat：先连上游再 _begin_ndjson()，好在出错时回真正的状态码。
        conn = None
        try:
            conn, resp = self.upstream.open("POST", target, payload)
        except Exception as exc:      # noqa: BLE001
            return self._safe_err(f"上游 llama-swap 不可达: {exc}", status=502)

        try:
            if resp.status != 200:
                text = resp.read().decode("utf-8", "replace")
                try:
                    return self._send_json(json.loads(text), status=resp.status)
                except ValueError:
                    return self._send_json({"error": text}, status=resp.status)
            self._begin_ndjson()
            usage, timings, finish_reason = None, None, None
            for obj in sse_iter(resp):
                if obj is None:
                    break
                choice = (obj.get("choices") or [{}])[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                if obj.get("usage"):
                    usage = obj["usage"]
                if obj.get("timings"):
                    timings = obj["timings"]
                if use_completions:
                    piece = choice.get("text") or ""
                else:
                    piece = (choice.get("delta") or {}).get("content") or ""
                if piece:
                    self._ndjson({"model": name, "created_at": now_iso(),
                                  "response": piece, "done": False})
            final = {"model": name, "created_at": now_iso(), "response": "",
                     "done": True, "done_reason": finish_reason or "stop", "context": []}
            final.update(stats_from(usage, timings))
            self._ndjson(final)
            self._end_chunked()
        except Exception as exc:      # noqa: BLE001
            self._safe_err(f"生成中断: {exc}", status=502)
        finally:
            if conn:
                conn.close()

    # -------------------------------------------------- /api/embed
    def _api_embed(self, legacy=False):
        body = self._read_body()
        name = body.get("model")
        e = self.registry.resolve(name)
        if not e:
            return self._model_not_found(name)
        # 同 /api/chat：记一次使用
        residency.note_use(e["upstream_id"])

        if legacy:
            inputs = [body.get("prompt") or ""]
        else:
            inp = body.get("input")
            inputs = inp if isinstance(inp, list) else [inp or ""]

        status, data = self.upstream.json("POST", "/v1/embeddings", {
            "model": e["upstream_id"],
            "input": inputs,
            "encoding_format": "float",
        })
        if status != 200:
            return self._send_json(data, status=status)

        vectors = [d.get("embedding") or [] for d in (data.get("data") or [])]
        usage = data.get("usage") or {}
        if legacy:
            return self._send_json({"embedding": vectors[0] if vectors else []})
        self._send_json({
            "model": name,
            "embeddings": vectors,
            "total_duration": 0,
            "load_duration": 0,
            "prompt_eval_count": usage.get("prompt_tokens", 0),
        })


# ---------------------------------------------------------------- 启动

def main():
    ap = argparse.ArgumentParser(description="Ollama 兼容桥（llama.cpp 后端）")
    ap.add_argument("-config", "--config", default=os.path.join(ROOT, "config", "bridge.json"))
    args = ap.parse_args()

    cfg = dict(DEFAULT_BRIDGE_CFG)
    cfg.update(load_json(args.config, {}) or {})

    personas_json = load_json(os.path.join(ROOT, "config", "personas.json"))
    model_map = load_json(os.path.join(ROOT, "models", "model_map.json"), {"models": []})
    if not personas_json:
        print("缺少 config/personas.json —— 请先运行 scripts/extract_personas.py", file=sys.stderr)
        return 2

    registry = Registry(personas_json, model_map, list_opts=(cfg.get("list") or {}))
    logger = lambda m: (sys.stderr.write(m + "\n"), sys.stderr.flush())  # noqa: E731

    BridgeHandler.registry = registry
    BridgeHandler.upstream = Upstream(cfg["upstream"], logger)
    BridgeHandler.bridge_cfg = cfg

    host, port = cfg["listen_host"], cfg["listen_port"]
    ThreadingHTTPServer.allow_reuse_address = True

    # 端口占用时给出明确提示，而不是丢一个 traceback。
    #
    # ⚠️ 坑：探测**不能**用 SO_REUSEADDR。allow_reuse_address 默认就是 True，
    # 而 Windows 的 SO_REUSEADDR 允许第二个 socket 绑上已被监听的端口 —— 于是
    # 探测永远成功，桥也「起得来」，结果和仍在运行的 Ollama 一起抢 11434，
    # 请求随机落到其中一个，现象是「有时能用有时不能」，极难定位。
    # 用 SO_EXCLUSIVEADDRUSE 才能真正探到「已经有别人在监听」。
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
    except OSError as exc:
        print(f"!! 端口 {host}:{port} 无法监听：{exc}\n"
              f"   多半是 Ollama 还在跑。先退出 Ollama（托盘右键 Quit）再启动。", file=sys.stderr)
        return 3
    finally:
        probe.close()

    srv = ThreadingHTTPServer((host, port), BridgeHandler)
    srv.cors_allow_origin = str((cfg.get("cors") or {}).get("allow_origin") or "").strip()
    # 启动闲置自动卸载：策略在 config/residency.json，改完下次 reload 生效
    residency.start_reaper(log=lambda m: (sys.stderr.write(m + "\n"),
                                          sys.stderr.flush()))
    n_base, n_persona, n_alias, n_shown = registry.list_summary()
    hidden = (n_base + n_persona + n_alias) - n_shown
    sys.stderr.write(
        f"Ollama 兼容桥已启动  http://{host}:{port}\n"
        f"  上游 llama-swap      {cfg['upstream']}\n"
        f"  底座/人格/别名        {n_base} / {n_persona} / {n_alias}"
        f"（列表接口只列 {n_shown} 个"
        + (f"，藏起 {hidden} 个；藏起来的仍可按名字调用" if hidden else "") + "）\n"
        f"  想让人格/别名也出现在列表里：config/bridge.json 的 list.personas / list.aliases\n"
        f"  Ollama 协议          /api/chat /api/generate /api/tags /api/embed /api/show /api/ps\n"
        f"  OpenAI 协议          /v1/chat/completions /v1/models /v1/embeddings\n")
    if turtle_web is not None:
        sys.stderr.write(f"  小游戏（海龟汤）     http://{host}:{port}/turtle\n")
    if humanizer_web is not None:
        sys.stderr.write(f"  去 AI 味（重写）     http://{host}:{port}/humanize\n")
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
