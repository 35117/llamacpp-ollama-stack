#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「去 AI 味」重写的核心逻辑（纯标准库）。

给 `humanizer/index.html` 与 `scripts/humanizer_web.py` 共用。业务规则全部
来自模型作者随权重一起发布的 `models/prompt_format.json` 与 AGENTS.md，
**不要凭手感改** —— prompt 与采样参数写错，输出会明显跑偏。

两条铁律（踩过就知道）：

1. **必须走 `/completion`，不能用 chat 端点。**
   humanizer-12B 是纯 completion 模型（gemma-4-12B 微调），没有对话模板。
   走 chat 会被套上通用模板，输出直接变成「Sure, here is...」。

2. **采样参数必须显式发全。**
   temperature 1.0 / top_p 0.95 / top_k 0 / min_p 0 / repeat_penalty 1.0，
   且**不发 stop**（只靠 EOS 收尾）。llama-server 默认 top_k 40 / min_p 0.05，
   与评测时不一致，会掉质量。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_FILE = os.path.join(ROOT, "models", "prompt_format.json")

# 模型在 llama-swap 里的名字（见 config/llama-swap.yaml）
DEFAULT_MODEL = "humanizer-12b"
DEFAULT_BASE = "http://127.0.0.1:11435"

# 评测时的采样参数，一个都不能少
SAMPLING = {
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 0,
    "min_p": 0.0,
    "repeat_penalty": 1.0,
}

FINGERPRINT = "cc51d66b4c593fbe"   # sha256(build_prompt("X"))[:16]
MAX_TRY = 3                        # 检查不过时重采样几次（只有不过才重试，正常采样不付这个成本）
LANG_MAX_TRY = 3                   # 英文稿跑出中文时，最多重采样几次

# 分块目标：约 350 词 / 600 汉字（作者建议值）
PIECE_WORDS = 350
PIECE_CJK = 600


class HumanizeError(RuntimeError):
    """调用上游失败或配置有误。"""


# ------------------------------------------------------------------ prompt

def load_prompt_format(path: str | None = None) -> dict:
    p = path or PROMPT_FILE
    if not os.path.exists(p):
        raise HumanizeError(f"缺少 prompt_format.json：{p}")
    with open(p, "r", encoding="utf-8") as fh:
        pf = json.load(fh)
    for k in ("instr", "sep"):
        if not pf.get(k):
            raise HumanizeError(f"prompt_format.json 缺少 '{k}'")
    return pf


def build_prompt(draft: str, pf: dict | None = None) -> str:
    """字节级精确的 prompt —— 多余的空白都会让效果变差。"""
    pf = pf or load_prompt_format()
    return pf["instr"] + "\n\n" + draft.strip() + pf["sep"]


def check_prompt_format(pf: dict | None = None) -> bool:
    """自校验：指纹对不对（错了说明 prompt 被改坏了）。"""
    return hashlib.sha256(build_prompt("X", pf).encode("utf-8")).hexdigest()[:16] == FINGERPRINT


# ------------------------------------------------------------------ 文本工具

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ASCII_WORD = re.compile(r"[A-Za-z0-9']+")
_ASCII_LETTER_WORD = re.compile(r"[A-Za-z][A-Za-z0-9']*")   # 至少含一个字母
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def cjk_ratio(text: str) -> float:
    """汉字占「实义字符」的比例 —— 分母只算汉字 + 含字母的西文词。

    **不要**把分母写成「非空白字符总数」（旧实现）：那样代码块、表格竖线、
    标点、纯数字都会稀释比例。实测一份「短中文正文 + 代码块 + 表格」的文档
    比例只有 0.212，被误判成英文 —— 后果是白烧一次额外重试，并且把
    `language`（语言漂移）误报成问题。改成只统计实义词后同一文档为 0.58。
    """
    cjk = len(_CJK.findall(text))
    words = len(_ASCII_LETTER_WORD.findall(text))
    denom = cjk + words
    if not denom:
        return 0.0
    return cjk / denom


def length_units(text: str) -> int:
    """中英混排的「长度」：汉字按 1，西文按 1 词（≈ 1.7 汉字）。"""
    cjk = len(_CJK.findall(text))
    words = len(_ASCII_WORD.findall(text))
    return cjk + int(words * 1.7)


def normalize_numbers(text: str) -> set[str]:
    """抽出文本里的数字并归一化，用于比对事实是否丢失。

    `1,250` → `1250`；`480k` → `480000`；`31.7万` → `317000`。
    只做保守归一（不确定的形态原样保留），宁可漏判也不误报。
    """
    out: set[str] = set()
    for m in _NUMBER.finditer(text):
        raw = m.group(0).rstrip(".,")
        val = raw.replace(",", "")
        tail = text[m.end():m.end() + 1]
        if tail in ("万", "万亿", "亿"):
            continue          # 交给下面的中文计数分支处理，避免一个数记两遍
        try:
            n = float(val)
        except ValueError:
            continue
        if tail in ("k", "K"):
            n *= 1_000
        elif tail in ("w", "W"):               # 少见但无害
            n *= 10_000
        if n.is_integer():
            out.add(str(int(n)))
        else:
            out.add(("%f" % n).rstrip("0").rstrip("."))
    # 中文的「万 / 亿」计数
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*([万亿])", text):
        try:
            n = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        n *= 10000 if m.group(2) == "万" else 100000000
        out.add(str(int(n)) if n.is_integer() else ("%f" % n).rstrip("0").rstrip("."))
    return out


def ngram_copy_rate(draft: str, out: str) -> float:
    """抄袭率：改写结果里有多少 n-gram 是直接从原稿搬的。

    英文按 5 词、中文按 6 字（与作者 `hz` 的做法一致）。
    抄得越多说明模型没真改，需要重采样。
    """
    d = draft + "\n"
    o = out + "\n"
    # 分别算中英，取较大的那个，避免混排时互相稀释
    rates = []
    for is_cjk in (False, True):
        n = 6 if is_cjk else 5
        if is_cjk:
            gd = {d[i:i + n] for i in range(len(d) - n + 1) if not d[i:i + n].isspace()}
            go = {o[i:i + n] for i in range(len(o) - n + 1) if not o[i:i + n].isspace()}
        else:
            wd, wo = d.lower().split(), o.lower().split()
            gd = {tuple(wd[i:i + n]) for i in range(len(wd) - n + 1)}
            go = {tuple(wo[i:i + n]) for i in range(len(wo) - n + 1)}
        if go:
            rates.append(len(gd & go) / len(go))
    return max(rates) if rates else 0.0


def inspect(draft: str, out: str, lang: str = "auto") -> dict:
    """对一次改写结果做检查，返回 dict（原样可发给前端）。

    `lang` 只作保留参数（旧调用点仍在传），现在的判据一律从原稿本身取证据，
    不再依赖语料级的语言检测 —— 那种检测对混排文档不可靠。
    """
    out = (out or "").strip()
    dnum, onum = normalize_numbers(draft), normalize_numbers(out)
    problems = []
    if not out:
        problems.append("empty")
    if "### Rewritten" in out or out.lstrip().startswith("Rewrite the text below"):
        problems.append("prompt_echo")
    if re.search(r"<start_of_turn>|<end_of_turn>|<\|turn|<\|im_start\|>", out):
        problems.append("chat_markers")
    copy = ngram_copy_rate(draft, out)
    if copy > 0.5:
        problems.append("copy")
    missing = sorted(dnum - onum)
    if missing:
        problems.append("missing_numbers")
    added = sorted(onum - dnum)
    if added:
        problems.append("added_numbers")     # 只报告，不重试（可能是模型自己算的）
    # 语言漂移：英文稿跑出成片中文（作者说这是已知的偶发问题）。
    # 判据只看**原稿有没有汉字**，不看上面那个检测出来的 lang ——
    # 后者对「中文正文 + 代码块」的混排文档会误判成 en，于是把正常的中文
    # 改写结果误报成 language，白烧一次重试。用原稿证据则不存在这个路径。
    if len(_CJK.findall(draft)) < 4 and len(_CJK.findall(out)) > 8:
        problems.append("language")
    ratio = length_units(out) / max(1, length_units(draft))
    if ratio < 0.45:
        problems.append("too_short")
    if ratio > 2.6:
        problems.append("too_long")
    return {
        "problems": problems,
        "copy_rate": round(copy, 3),
        "missing_numbers": missing,
        "added_numbers": added,
        "length_ratio": round(ratio, 3),
        "chars": len(out),
    }


def detect_lang(text: str) -> str:
    return "zh" if cjk_ratio(text) > 0.25 else "en"


# ------------------------------------------------------------------ 调用上游

def _opener():
    # 本机代理会把 127.0.0.1 拦成 502，这里显式直连
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def complete(prompt: str, base: str = DEFAULT_BASE, model: str = DEFAULT_MODEL,
             n_predict: int = 2048, timeout: float = 900) -> dict:
    """打上游的 `/completion`（**不是** chat）。返回原始 JSON。"""
    body = {
        "model": model,
        "prompt": prompt,
        "n_predict": int(n_predict),
        "stream": False,
        **SAMPLING,
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/completion",
        json.dumps(body).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    try:
        with _opener().open(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise HumanizeError(
            f"上游返回 {exc.code}：{detail or exc.reason}\n"
            f"（模型 '{model}' 是否已在 config/llama-swap.yaml 注册？）") from exc
    except Exception as exc:  # noqa: BLE001
        raise HumanizeError(f"连不上上游 {base}：{exc}") from exc


def rewrite_once(draft: str, base: str = DEFAULT_BASE, model: str = DEFAULT_MODEL,
                 pf: dict | None = None, timeout: float = 900) -> tuple[str, dict]:
    """一次改写 + 检查 + 按需重采样，返回 (文本, 检查结果)。"""
    pf = pf or load_prompt_format()
    prompt = build_prompt(draft, pf)
    lang = detect_lang(draft)
    tries = LANG_MAX_TRY if lang == "en" else MAX_TRY
    best = ("", None)
    # n_predict：约 2.5 倍，落在 256..2048（作者的规则）
    n_predict = max(256, min(2048, int(length_units(draft) * 2.5) + 64))
    for attempt in range(tries):
        res = complete(prompt, base=base, model=model,
                       n_predict=n_predict, timeout=timeout)
        text = (res.get("content") or "").strip()
        chk = inspect(draft, text, lang)
        chk["attempt"] = attempt + 1
        chk["forwarded"] = bool(res.get("stop"))
        # 打分挑最好的一次：缺数字/抄稿/语言漂移最少的优先。
        # `too_long` 必须算「硬伤」：实测这段在 8GB 档位（Q3-QAT、temp 1.0、
        # top_p 0.95、无 repeat_penalty）会偶发退化成 4 倍长的胡编内容
        # （编出一整张 REBOL 配置表 + 192.168.0.3 之类的假数据）。
        # 它只被标进 problems 却不触发重试，于是退化结果被当成成品交付 ——
        # 这正是「去 AI 味」最不能接受的结果。加上之后退化样本会被重采样替换掉。
        hard = [p for p in chk["problems"]
                if p in ("empty", "prompt_echo", "chat_markers", "copy",
                         "missing_numbers", "language", "too_short", "too_long")]
        score = (len(hard), len(chk["missing_numbers"]), chk["copy_rate"])
        if best[1] is None or score < best[1]["_score"]:
            chk["_score"] = score
            best = (text, chk)
        if not hard:
            break
    text, chk = best
    chk.pop("_score", None)
    return text, chk


# ------------------------------------------------------------------ 分块

_KEEP_LINE = (
    re.compile(r"^\s{0,3}#{1,6}\s"),            # 标题
    re.compile(r"^\s*>"),                        # 引用
    re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$"),   # 分隔线
    re.compile(r"^\s*(?:!?\[[^\]]*\]\([^)]*\))\s*$"),   # 纯链接/图片行
    re.compile(r"^\s*</?[A-Za-z][^>]*>"),        # HTML
    re.compile(r"^\s*\$\$"),
    re.compile(r"^\s*\|"),                       # 表格
)


def _is_keep_line(line: str) -> bool:
    return any(p.match(line) for p in _KEEP_LINE)


def split_blocks(text: str) -> list[dict]:
    """把 Markdown/纯文本切成块，标出哪些块要原样保留。

    保留：围栏代码、缩进代码、标题、表格、引用、HTML、分隔线、纯链接行、
    `$$` 数学、front matter。其余按空行切段，归为可改写块。

    返回 `[{"kind": "keep"|"prose", "text": ...}]`。
    """
    lines = text.split("\n")
    blocks: list[dict] = []
    buf: list[str] = []
    i = 0
    n = len(lines)

    def flush_prose():
        nonlocal buf
        if not buf:
            return
        seg = "\n".join(buf).strip("\n")
        if seg.strip():
            blocks.append({"kind": "prose", "text": seg})
        buf = []

    # front matter（文件最开头的 --- ... ---）
    if n and lines[0].strip() == "---":
        for j in range(1, n):
            if lines[j].strip() in ("---", "..."):
                blocks.append({"kind": "keep", "text": "\n".join(lines[:j + 1])})
                i = j + 1
                break

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 围栏代码块：整块保留
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            flush_prose()
            mark = fence.group(1)
            chunk = [line]
            i += 1
            while i < n and not re.match(r"^\s*" + re.escape(mark[0]) + r"{" + str(len(mark)) + r",}\s*$", lines[i]):
                chunk.append(lines[i])
                i += 1
            if i < n:
                chunk.append(lines[i])
                i += 1
            blocks.append({"kind": "keep", "text": "\n".join(chunk)})
            continue

        # 缩进代码块（4 空格 / Tab）
        if re.match(r"^(\t| {4})\S", line):
            flush_prose()
            chunk = []
            while i < n and (re.match(r"^(\t| {4})", lines[i]) or not lines[i].strip()):
                if not lines[i].strip() and not (i + 1 < n and re.match(r"^(\t| {4})", lines[i + 1])):
                    break
                chunk.append(lines[i])
                i += 1
            blocks.append({"kind": "keep", "text": "\n".join(chunk).rstrip()})
            continue

        # 空行 → 段落分隔
        if not stripped:
            flush_prose()
            i += 1
            continue

        # 表格：连续的行
        if stripped.startswith("|"):
            flush_prose()
            chunk = []
            while i < n and lines[i].strip().startswith("|"):
                chunk.append(lines[i])
                i += 1
            blocks.append({"kind": "keep", "text": "\n".join(chunk)})
            continue

        # 标题 / 引用 / 分隔线 / HTML / $$ / 纯链接：单行保留
        if _is_keep_line(line):
            flush_prose()
            blocks.append({"kind": "keep", "text": line.rstrip()})
            i += 1
            continue

        buf.append(line)
        i += 1

    flush_prose()
    return blocks


def plan_pieces(text: str, words: int = PIECE_WORDS, cjk: int = PIECE_CJK) -> list[dict]:
    """把文档排成「改写任务」序列，长文按可改写块聚合到目标长度。

    返回 `[{"kind": "keep"|"prose", "text": ...}]`，prose 的是真正要发模型的。
    绝不跨标题/代码/表格聚合 —— 那会把结构搅乱。
    """
    out: list[dict] = []
    pending: list[str] = []
    pending_units = 0

    def flush():
        nonlocal pending, pending_units
        if pending:
            out.append({"kind": "prose", "text": "\n\n".join(pending)})
        pending, pending_units = [], 0

    for b in split_blocks(text):
        if b["kind"] == "keep":
            flush()
            out.append(b)
            continue
        # 列表项也当 prose，但保留 bullet —— 模型被训练成会保留
        u = length_units(b["text"])
        if pending and pending_units + u > max(words, cjk):
            flush()
        pending.append(b["text"])
        pending_units += u
    flush()
    return out


def humanize(text: str, base: str = DEFAULT_BASE, model: str = DEFAULT_MODEL,
             on_event=None, timeout: float = 900) -> dict:
    """把整篇文本去 AI 味。

    `on_event(ev)` 会在过程中被调用（分块进度），用于流式回传。
    返回 `{"text":..., "pieces":[...], "summary":{...}}`。
    """
    pf = load_prompt_format()
    if not check_prompt_format(pf):
        raise HumanizeError("prompt_format.json 指纹不对（文件被改过？）")
    plan = plan_pieces(text)
    prose_idx = [i for i, p in enumerate(plan) if p["kind"] == "prose"]

    def emit(ev):
        if on_event:
            try:
                on_event(ev)
            except Exception:  # noqa: BLE001
                pass

    emit({"type": "plan", "pieces": len(prose_idx),
          "kept": len(plan) - len(prose_idx)})

    result: list[str] = []
    pieces_meta: list[dict] = []
    started = time.time()
    flagged = 0
    done = 0

    for idx, blk in enumerate(plan):
        if blk["kind"] == "keep":
            result.append(blk["text"])
            continue
        done += 1
        emit({"type": "piece_start", "i": done, "n": len(prose_idx),
              "preview": blk["text"][:80]})
        t0 = time.time()
        out, chk = rewrite_once(blk["text"], base=base, model=model,
                                pf=pf, timeout=timeout)
        if chk["problems"]:
            flagged += 1
        meta = {
            "i": done, "seconds": round(time.time() - t0, 1), **chk,
            "draft_head": blk["text"][:60],
        }
        pieces_meta.append(meta)
        result.append(out)
        emit({"type": "piece_done", "i": done, "n": len(prose_idx),
              "text": out, "check": meta})

    final = "\n\n".join(x for x in result if x is not None)
    summary = {
        "pieces": len(prose_idx),
        "kept": len(plan) - len(prose_idx),
        "flagged": flagged,
        "seconds": round(time.time() - started, 1),
        "model": model,
        "chars_in": len(text),
        "chars_out": len(final),
    }
    emit({"type": "done", "text": final, "pieces": pieces_meta,
          "summary": summary})
    return {"text": final, "pieces": pieces_meta, "summary": summary}
