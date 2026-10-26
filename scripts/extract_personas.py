#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Ollama Modelfile 里的人格（SYSTEM 提示词）抽取成 llama-swap 可用的「人格注册表」。

背景：用户的用法是 `ollama create <tag> -f <Modelfile>`，用 SYSTEM 把人格/用途烙进模型。
llama-swap 是一模型一进程，没有「同一权重 + 多人格」的概念，所以人格放在前端桥接层注入。

产出：
  config/personas/<id>.txt        人格系统提示词（纯文本）
  config/personas.json            注册表：人格 -> 底座模型 + 提示词文件 + 采样默认值
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ollama 的 Modelfile 原始目录 —— **只作兼容兜底**。
# 本部署已经把用到的 Modelfile 复制进 config/modelfiles/，正常不需要它。
# 还留着 Ollama 的话可以用环境变量指过去；不设就只读本地副本。
OLLAMA_MODELS = os.environ.get("OLLAMA_MODELS", "")
# Modelfile 的读取顺序：先本地副本，再 Ollama 原目录。
# 本部署用 detach_from_ollama.py 把用到的 Modelfile 复制到 config/modelfiles/，
# 这样即使 Ollama 被卸载，人格也还能重新抽取。
LOCAL_MODELFILES = os.path.join(ROOT, "config", "modelfiles")
OUT_DIR = os.path.join(ROOT, "config", "personas")
REGISTRY = os.path.join(ROOT, "config", "personas.json")
MODEL_MAP = os.path.join(ROOT, "models", "model_map.json")


def find_modelfile(name):
    """按名字找 Modelfile：先本地副本（顶层→子目录），再 Ollama 原目录（同样递归）。"""
    for base in (LOCAL_MODELFILES, OLLAMA_MODELS):
        if not os.path.isdir(base):
            continue
        direct = os.path.join(base, name)
        if os.path.exists(direct):
            return direct
        for dp, dn, fn in os.walk(base):
            dn[:] = [d for d in dn if d.lower() not in ("blobs", "manifests")]
            if name in fn:
                return os.path.join(dp, name)
    return None

# ---------------------------------------------------------------- 底座模型
# id -> (GGUF 文件名, 说明, 是否多模态)
BASES = {
    "qwen3-r1-8b": {
        "gguf": "deepseek-r1_8b.gguf",
        "file_name": "DeepSeek R1 0528 Qwen3 8B",
        "desc": "DeepSeek-R1-0528 蒸馏 · Qwen3-8B 底座（JOSIEFIED-Qwen3 权重）",
        "ctx": 8192,
        "ollama_alias": ["deepseek-r1:8b", "goekdenizguelmez/JOSIEFIED-Qwen3:latest"],
    },
    "qwen3-8b": {
        "gguf": "qwen3_latest.gguf",
        "file_name": "Qwen3 8B",
        "desc": "Qwen3 8B 原始底座",
        "ctx": 8192,
        "ollama_alias": ["qwen3:latest", "qwen3GameTagger:latest"],
    },
    "ds-r1-heretic": {
        "gguf": "ds-r1-heretic_latest.gguf",
        "file_name": "DeepSeek R1 Distill Llama 8B Heretic",
        "desc": "DeepSeek-R1-Distill-Llama-8B 去审查版",
        "ctx": 8192,
        "ollama_alias": ["ds-r1-heretic:latest"],
    },
    "qwen35-2b": {
        "gguf": "qwen3.5_2b.gguf",
        "file_name": "Qwen3.5 2B",
        "desc": "Qwen3.5 2B（轻量，可长上下文）",
        "ctx": 32768,
        "ollama_alias": ["qwen3.5:2b"],
        # 该 GGUF 与当前 llama.cpp b11146 不兼容，详见 unsupported_reason。
        # 证据（2026-09-27 实测）：
        #   1. qwen35.rope.dimension_sections 是 3 个 [11,11,10]，同架构 4B 是 4 个 [11,11,10,0]
        #   2. 张量叫 blk.N.ssm_dt，当前 llama.cpp 要 blk.N.ssm_dt.bias
        #   3. qwen35.attention.head_count_kv 是逐层数组 [0,0,0,2,...]（0=线性注意力层），
        #      当前 llama.cpp 只认标量；改标量后 done_getting_tensors 只匹配到 320/728，
        #      说明必须由支持逐层 KV 头的新版 llama.cpp 才能读。
        # 处理：不注册（对外看不到），换新版 llama.cpp 后可解注释。
        "supported": False,
        "unsupported_reason": (
            "该 GGUF 由更新版转换器导出（逐层 attention.head_count_kv 数组），"
            "当前 llama.cpp b11146 只支持标量形式，无法加载。"
            "需要 llama.cpp 支持 per-layer head_count_kv 后再启用，或重新下载该模型的 GGUF。"
        ),
    },
    "qwen35-4b-heretic": {
        "gguf": "qwen-3.5-4b-heretic_latest.gguf",
        "file_name": "Qwen3.5 4B Heretic",
        "desc": "Qwen3.5 4B 去审查版 · 支持图像输入（含 mmproj）",
        "ctx": 32768,
        "mmproj": "qwen-3.5-4b-heretic_latest-mmproj.gguf",
        "vision": True,
        "ollama_alias": ["qwen-3.5-4b-heretic:latest"],
    },
    "qwen35-9b-heretic": {
        "gguf": "Qwen3.5-9B-heretic-v2-Q4_K_M.gguf",
        "file_name": "Qwen3.5 9B Heretic v2",
        "desc": "heretic 去审查 9B · 主力通用（对话/翻译/总结）",
        "ctx": 16384,
        "ollama_alias": ["qwen3.5-9b-heretic:latest"],
    },
    "qwen35-9b-abliterated-vision": {
        "gguf": "Huihui-Qwen3.5-9B-abliterated.Q4_K_M.gguf",
        "file_name": "Huihui Qwen3.5 9B Abliterated",
        "desc": "huihui 去审查 9B · 带 mmproj，支持图像输入",
        "ctx": 16384,
        "mmproj": "Huihui-Qwen3.5-9B-abliterated.mmproj-Q8_0.gguf",
        "vision": True,
        "ollama_alias": ["huihui-qwen3.5-9b:latest"],
    },
    "qwen35-9b-rp": {
        "gguf": "Qwen3.5-text-9B-NSFW-RP-RolePlay.IQ4_XS.gguf",
        "file_name": "Qwen3.5 text 9B NSFW RP RolePlay (IQ4_XS)",
        "desc": "9B 角色扮演微调（DGDGDG12，合并 RP LoRA）· ChatML 模板、无 thinking",
        "ctx": 16384,
        "ollama_alias": ["qwen3.5-9b-nsfw-rp:latest"],
    },
    "galtransl-7b": {
        "gguf": "GalTransl-7B-v2.6_Q6_k.gguf",
        "file_name": "GalTransl 7B v2.6.10",
        "desc": "轻小说 / 视觉小说翻译专用（日→中）",
        "ctx": 8192,
        "ollama_alias": ["GalTransl-7B-v2.6:Q6_k"],
    },
    "hunyuan-mt-7b": {
        "gguf": "Huihui-Hunyuan-MT-Chimera-7B-abliterated-GGUF_Q4_K_M.gguf",
        "file_name": "Huihui Hunyuan MT Chimera 7B Abliterated",
        "desc": "腾讯混元翻译模型（多语种互译）",
        "ctx": 16384,
        "ollama_alias": ["Huihui-Hunyuan-MT-Chimera-7B-abliterated-GGUF:Q4_K_M"],
    },
    "nomic-embed": {
        "gguf": "nomic-embed-text_latest.gguf",
        "file_name": "nomic-embed-text-v1.5",
        "desc": "文本向量化（embedding）",
        "ctx": 2048,
        "embedding": True,
        "ollama_alias": ["nomic-embed-text:latest"],
    },
}

# ---------------------------------------------------------------- 人格表
# (人格 id, Modelfile 文件名, 底座 id, 中文说明)
PERSONAS = [
    ("deepseek-r1-8b",   "Deepseek",         "qwen3-r1-8b",    "默认 · 中文作答"),
    ("nonhuman",         "nonhuman",         "qwen3-r1-8b",    "文段分析者"),
    ("translate",        "translate",        "qwen3-r1-8b",    "翻译师（英→中 · 二次元）"),
    ("at-translate",     "ATTranslate",      "qwen3-r1-8b",    "成人用语翻译（中→英）"),
    ("deletag",          "deletag",          "qwen3-r1-8b",    "标签分类师（去服装标签）"),
    ("jisiefied-qwen3",  "Qwen",             "qwen3-r1-8b",    "JOSIEFIED 默认 · 中文作答"),
    ("qwen-nsfw",        "QwenNSFW",         "qwen3-r1-8b",    "无限制应答"),
    ("modelfile-3",      "modelfile3",       "qwen3-r1-8b",    "旧版自定义人格"),
    ("maoniang-r1",      "MaoNiangDeepseek", "qwen3-r1-8b",    "猫娘 · R1 8B 底座"),
    ("eri",              "XiaoNanLiangQwen", "qwen3-r1-8b",    "绘里"),
    ("yuka",             "yuka",             "qwen3-r1-8b",    "早濑优香（蔚蓝档案）"),
    # 注意：人格 id 不能和底座 id 重名，否则在注册表里会互相覆盖。
    # 底座 ds-r1-heretic 是干净权重，这个带「中文作答」人格的用 -zh 后缀区分。
    ("ds-r1-heretic-zh", "ds-heretic",       "ds-r1-heretic",  "默认 · 中文作答"),
    ("xiaoyu",           "Xili",             "qwen3-r1-8b",    "丛雨 / 小雨（千恋＊万花）· R1 8B 底座"),
    ("xiaoyu-heretic",   "ds1",              "ds-r1-heretic",  "丛雨 / 小雨 · heretic 底座"),
    ("maoniang-qwen3",   "MaoNiangQwen",     "qwen3-8b",       "猫娘 · Qwen3 8B 底座"),
    ("game-tagger",      "GameTagger",       "qwen3-8b",       "游戏分类师"),
    ("xiaoyu-rp",        "XiliRP",           "qwen35-9b-rp",   "丛雨 · RP 9B（圆括号格式 · 精简）"),
    ("miaoling-rp",      "MiaoLingRP",       "qwen35-9b-rp",   "喵铃 · 机械猫娘工程师（圆括号格式）"),
]

# 底座缺失、无法迁移的 Modelfile
SKIP = {
    "gemma1": "FROM hf.co/mradermacher/DeepSeek-R1-Distill-Llama-8B-heretic-i1-GGUF:Q4_K_M（该权重不在本地库中）",
    "modelfilellama": "FROM llama3.1:8b（llama3.1 8B 不在本地库中）",
    "ds1 - 副本": "与 ds1 内容重复",
    "modelfile3.txt": "与 modelfile3 内容重复",
    "新文件 3.txt": "非 Modelfile",
}

# Modelfile 的 FROM 写法 -> 允许的底座（用于交叉校验，防止映射写错）
FROM_ALLOWED = {
    "deepseek-r1": {"qwen3-r1-8b"},
    "deepseek-r1:8b": {"qwen3-r1-8b"},
    "goekdenizguelmez/josiefied-qwen3": {"qwen3-r1-8b"},
    "hf.co/mradermacher/huihui-hunyuan-mt-chimera-7b-abliterated-gguf:q4_k_m": {"hunyuan-mt-7b"},
    "ds-r1-heretic": {"ds-r1-heretic"},
    "ds-r1-heretic:latest": {"ds-r1-heretic"},
    "qwen3:latest": {"qwen3-8b"},
    "qwen3gametagger": {"qwen3-8b"},
    "qwen35-9b-rp": {"qwen35-9b-rp"},
}

SYSTEM_RE = re.compile(r'^\s*SYSTEM\s+("""|\'\'\'|"|\')', re.MULTILINE | re.IGNORECASE)


def parse_modelfile(path: str):
    """极简 Ollama Modelfile 解析：返回 (from, system, parameters)"""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        raw = fh.read()

    base, system, params = None, None, {}
    lines = raw.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        upper = stripped.upper()
        if upper.startswith("FROM "):
            base = stripped[5:].strip()
            i += 1
            continue
        if upper.startswith("PARAMETER "):
            rest = stripped[10:].strip()
            parts = rest.split(None, 1)
            if len(parts) == 2:
                params[parts[0]] = parts[1].strip().strip('"').strip("'")
            i += 1
            continue
        if upper.startswith("SYSTEM "):
            rest = stripped[7:].strip()
            for delim in ('"""', "'''", '"', "'"):
                if rest.startswith(delim):
                    body = rest[len(delim):]
                    # 同一行闭合
                    if body.endswith(delim) and len(body) >= len(delim):
                        system = body[: -len(delim)]
                        break
                    chunks = [body]
                    i += 1
                    closed = False
                    while i < len(lines):
                        if delim in lines[i]:
                            chunks.append(lines[i][: lines[i].index(delim)])
                            closed = True
                            break
                        chunks.append(lines[i])
                        i += 1
                    system = "\n".join(chunks)
                    if not closed:
                        print(f"  !! {os.path.basename(path)}: SYSTEM 引号未闭合", file=sys.stderr)
                    break
            else:
                system = rest
            i += 1
            continue
        i += 1

    return base, system, params


def main():
    # IDLE 等环境会把 sys.stdout 换成没有 reconfigure() 的对象，
    # 直接调用会 AttributeError 把整个脚本打断（用户从 IDLE 跑时踩到过）。
    # 这里兜底：设不了就跳过，中文输出交给宿主环境自己的编码处理。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    os.makedirs(OUT_DIR, exist_ok=True)

    # 读取 ollama_lib 的产物，拿到各 base 模型的采样默认值（params blob）
    ollama_params = {}
    if os.path.exists(MODEL_MAP):
        with open(MODEL_MAP, "r", encoding="utf-8") as fh:
            mm = json.load(fh)
        for m in mm["models"]:
            txt = (m.get("params_text") or "").strip()
            if not txt:
                continue
            try:
                p = json.loads(txt)
            except Exception:
                continue
            ollama_params[m["ollama_id"]] = p

    registry = {"generated_by": "extract_personas.py", "bases": {}, "personas": []}

    # --- 基底模型自身的默认采样值，用同权重 Ollama 模型的 params
    for bid, b in BASES.items():
        p = {}
        for alias in b.get("ollama_alias", []) + [bid]:
            if alias in ollama_params:
                src = ollama_params[alias]
                p = {
                    "temperature": src.get("temperature"),
                    "top_p": src.get("top_p"),
                    "top_k": src.get("top_k"),
                    "repeat_penalty": src.get("repeat_penalty") or src.get("repeat_last_n"),
                    "presence_penalty": src.get("presence_penalty"),
                }
                p = {k: v for k, v in p.items() if v is not None}
                break
        registry["bases"][bid] = dict(b, params=p)

    # --- 权重到底在不在盘上：**动态判定**
    # 用户换了模型（删旧的、加新的）但没重跑本脚本时，注册表里会残留指向
    # 已删文件的底座。界面必须能一眼看出「这个底座没权重了」，
    # 而不是让人挂上去、发消息时才报错。
    models_dir = os.path.join(ROOT, "models")
    for bid, d in registry["bases"].items():
        if d.get("supported") is False:
            continue        # 表里已明确声明不支持（例如与运行时库不兼容），保留原判与原因
        gguf = d.get("gguf") or ""
        # 正在下载：说清楚是「下到一半」，别报成「文件不见了」
        if gguf and not os.path.exists(os.path.join(models_dir, gguf)) \
                and os.path.exists(os.path.join(models_dir, gguf + ".part")):
            d["supported"] = False
            d["unsupported_reason"] = f"正在下载（models/{gguf}.part），下完自动可用"
            continue
        need = [n for n in (d.get("gguf"), d.get("mmproj")) if n]
        missing = [n for n in need if not os.path.exists(os.path.join(models_dir, n))]
        if missing:
            d["supported"] = False
            d["unsupported_reason"] = ("权重文件不在 models/ 里：" + "、".join(missing)
                                       + "（已删除或尚未下载）")

    seen_bases = set()
    for pid, mf, bid, label in PERSONAS:
        src = find_modelfile(mf)
        if not src:
            print(f"  !! 找不到 Modelfile: {mf}（已查 {LOCAL_MODELFILES} 与 "
                  f"{OLLAMA_MODELS}）-> 跳过 {pid}", file=sys.stderr)
            continue
        frm, system, params = parse_modelfile(src)
        base_info = BASES[bid]

        # 一致性校验：Modelfile 的 FROM 是否真的指向我们给的底座
        warn = None
        if frm:
            allowed = FROM_ALLOWED.get(frm.strip().lower())
            if allowed is None:
                warn = f"FROM {frm} 未在 FROM_ALLOWED 中登记，请人工确认底座映射"
            elif bid not in allowed:
                warn = f"FROM {frm} 与指定底座 {bid} 不符（允许: {sorted(allowed)}）"

        out_txt = os.path.join(OUT_DIR, pid + ".txt")
        with open(out_txt, "w", encoding="utf-8") as fh:
            fh.write((system or "").strip() + "\n")

        entry = {
            "id": pid,
            "label": label,
            "base": bid,
            "source_modelfile": mf,
            "source_from": frm,
            "system_file": f"personas/{pid}.txt",
            "system_chars": len((system or "").strip()),
            "system_preview": (system or "").strip().replace("\n", " ")[:40],
            "params": {k: v for k, v in params.items()},
        }
        if warn:
            entry["warning"] = warn
            print(f"  !! {pid}: {warn}", file=sys.stderr)
        registry["personas"].append(entry)
        seen_bases.add(bid)

    registry["skipped"] = SKIP

    # Ollama 原生模型名 -> 本部署里的模型/人格 id。
    # 目的：以前把 Ollama 当后端调用的程序，把 base_url 指到 11434 就能照旧工作，
    # 连 model 名都不用改。注意这些是「旧名字承接」，与上面的 personas 是两回事。
    registry["ollama_aliases"] = {
        "deepseek-r1:8b": "deepseek-r1-8b",
        "goekdenizguelmez/JOSIEFIED-Qwen3:latest": "jisiefied-qwen3",
        "qwen3:latest": "qwen3-8b",
        "qwen3GameTagger:latest": "game-tagger",
        "ds-r1-heretic:latest": "ds-r1-heretic-zh",
        "qwen3.5:2b": "qwen35-2b",
        "qwen-3.5-4b-heretic:latest": "qwen35-4b-heretic",
        "GalTransl-7B-v2.6:Q6_k": "galtransl-7b",
        "Huihui-Hunyuan-MT-Chimera-7B-abliterated-GGUF:Q4_K_M": "hunyuan-mt-7b",
        "nomic-embed-text:latest": "nomic-embed",
    }

    with open(REGISTRY, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, ensure_ascii=False, indent=2)

    print(f"底座模型: {len(registry['bases'])}   人格: {len(registry['personas'])}\n")
    print(f"{'人格 id':<18}{'底座':<17}{'字数':>6}  说明")
    print("-" * 74)
    for p in registry["personas"]:
        print(f"{p['id']:<18}{p['base']:<17}{p['system_chars']:>6}  {p['label']}")
    print("\n底座模型默认采样值（来自 Ollama params）:")
    for bid, b in registry["bases"].items():
        print(f"  {bid:<18} ctx={b['ctx']:<6} {b['params']}")
    print("\n未迁移的 Modelfile:")
    for k, v in SKIP.items():
        print(f"  - {k}: {v}")


if __name__ == "__main__":
    main()
