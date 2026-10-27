#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从模板生成 config/llama-swap.yaml —— 把 {{ROOT}} 换成本机实际路径。

为什么需要这一步：`config/llama-swap.yaml` 里每个模型都要写 `-m <绝对路径>`，
而绝对路径因机器而异。所以仓库里只放带占位符的 `.template`，真实文件由本脚本生成
（真实文件不入库）。

顺带做两件有用的检查：
  1. `models/` 里有哪些 `.gguf` 没在配置里声明（漏配）
  2. 配置里声明了但文件不存在的 `.gguf`（悬空引用 —— 启动才会报错，很难查）

用法：
    python scripts\\init_config.py           # 生成（已有文件会先备份）
    python scripts\\init_config.py --check   # 只检查，不写文件
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "config", "llama-swap.yaml.template")
TARGET = os.path.join(ROOT, "config", "llama-swap.yaml")
MODELS = os.path.join(ROOT, "models")


def find_ggufs():
    if not os.path.isdir(MODELS):
        return []
    return sorted(f for f in os.listdir(MODELS) if f.lower().endswith(".gguf"))


def referenced(text):
    """配置里引用到的 gguf 文件名（去重）。"""
    names = set()
    for m in re.finditer(r"([^\s/\\]+\.gguf)", text):
        names.add(m.group(1))
    return names


#: yaml 里一个 model 块的定位锚（本项目模板的固定结构）
_RE_MODEL_KEY = re.compile(r'^  "([A-Za-z0-9._-]+)":\s*$')
_RE_NAME = re.compile(r'^    name:\s*"?([^"\n]*)"?\s*$')
_RE_DESC = re.compile(r'^    description:\s*"([^"\n]*)"')
_RE_GGUF = re.compile(r'-m\s+\S*?([^/\\\s]+\.gguf)')
_RE_CTX = re.compile(r'(?:^|\s)-c\s+(\d+)')
#: yaml 里 aliases 两种写法都要认：块式（- "名字"）与行内式（["a", "b"]）
_RE_ALIAS_HEAD = re.compile(r'^    aliases:\s*$')
_RE_ALIAS_ITEM = re.compile(r'^      -\s*"?([^"\n]+?)"?\s*$')
_RE_ALIAS_INLINE = re.compile(r'^    aliases:\s*\[(.*)\]\s*$')


def derive_bases(yaml_text):
    """从渲染后的 llama-swap.yaml 反推 bases（底座表）。

    为什么需要这一步：桥的 Registry 只认 personas.json 里的 bases，
    而 llama-swap 认的是 yaml。两边不对齐的话，yaml 里声明了模型、桥里却
    「模型不存在」。初始化时按 yaml 生成 bases，克隆下来就能直接调用。
    """
    bases = {}
    cur = None
    in_alias = False
    for line in yaml_text.splitlines():
        m = _RE_MODEL_KEY.match(line)
        if m:
            cur = {"gguf": None, "file_name": m.group(1), "desc": "",
                   "ctx": None, "ollama_alias": [], "params": {}}
            bases[m.group(1)] = cur
            in_alias = False
            continue
        if cur is None:
            continue

        # aliases 段落要整段吃掉，否则 `- "名字"` 里的引号名字会被当成普通行漏掉
        if _RE_ALIAS_HEAD.match(line):
            in_alias = True
            continue
        if in_alias:
            m = _RE_ALIAS_ITEM.match(line)
            if m:
                cur["ollama_alias"].append(m.group(1).strip())
                continue
            in_alias = False
        m = _RE_ALIAS_INLINE.match(line)
        if m:
            for part in m.group(1).split(","):
                name = part.strip().strip('"').strip("'")
                if name:
                    cur["ollama_alias"].append(name)
            continue

        m = _RE_NAME.match(line)
        if m and m.group(1).strip():
            cur["file_name"] = m.group(1).strip()
            continue
        m = _RE_DESC.match(line)
        if m and not cur["desc"]:
            cur["desc"] = m.group(1).strip()
            continue
        m = _RE_GGUF.search(line)
        if m and not cur["gguf"]:
            cur["gguf"] = m.group(1)
            continue
        m = _RE_CTX.search(line)
        if m and cur["ctx"] is None:
            cur["ctx"] = int(m.group(1))
            continue

    models_dir = os.path.join(ROOT, "models")
    for bid, b in bases.items():
        if not b["gguf"]:
            continue          # 没写 -m 的条目不是真模型（保留但不标 supported）
        ok = os.path.exists(os.path.join(models_dir, b["gguf"]))
        b["supported"] = ok
        b["unsupported_reason"] = (None if ok else
                                   f"权重文件不在 models/ 里：{b['gguf']}（已删除或尚未下载）")
    return bases


def bootstrap_personas(bases=None):
    """首次运行时用示例人格初始化 config/personas.json。

    不这么做的话桥会因「缺少 config/personas.json」直接退出 —— 克隆下来跑不起来。
    已有文件绝不覆盖（那是用户自己的人设）。

    bases 由 derive_bases() 从 yaml 反推：yaml 声明了哪些模型，桥就认识哪些。
    否则新克隆只有示例里的一个样例底座，真模型全都调不到。
    """
    dst_json = os.path.join(ROOT, "config", "personas.json")
    src_json = os.path.join(ROOT, "config", "personas.example.json")
    dst_dir = os.path.join(ROOT, "config", "personas")
    src_dir = os.path.join(ROOT, "config", "personas.example")

    if os.path.exists(dst_json):
        return "config/personas.json 已存在，未覆盖"
    if not os.path.exists(src_json):
        return "缺少示例文件，跳过"

    with open(src_json, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    note = ""
    real = {k: v for k, v in (bases or {}).items() if v.get("gguf")}
    if real:
        data["bases"] = real
        data["generated_by"] = ("scripts/init_config.py（bases 按 llama-swap.yaml 的 "
                               "模型清单生成；人格部分来自示例）")
        # 示例人格原本挂在 example-model 上；那个底座已经不存在了，
        # 不重新挂载的话 Registry._build 会 KeyError（/admin/state 直接 500）。
        first = next(iter(real))
        for p in data.get("personas") or []:
            if p.get("base") not in real:
                p["base"] = first
        # 别名也要登记：客户端用 Ollama 旧名（如 GalTransl-7B-v2.6:Q6_k）调用时，
        # 桥必须先认识这个名字才转得出去，否则一律 404。
        aliases = {}
        for bid, b in real.items():
            for al in b.get("ollama_alias") or []:
                if al and al != bid:
                    aliases[al] = bid
        data["ollama_aliases"] = aliases
        note = f"，bases 已按 yaml 生成 {len(real)} 个"
        if aliases:
            note += f"、别名 {len(aliases)} 个"

    os.makedirs(dst_dir, exist_ok=True)
    if os.path.isdir(src_dir):
        for name in os.listdir(src_dir):
            s, d = os.path.join(src_dir, name), os.path.join(dst_dir, name)
            if os.path.isfile(s) and not os.path.exists(d):
                shutil.copy2(s, d)
    tmp = dst_json + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, dst_json)
    return "已从示例初始化 config/personas.json" + note


def sync_bases(bases):
    """把 yaml 里**新加**的模型补进已有的 config/personas.json。

    为什么需要：桥的 Registry 只认 personas.json 的 bases。README 说「yaml 加一条
    就能用」，但 bases 只在首次初始化时生成 —— 后来加的模型桥不认识，调用报
    「模型不存在」。所以加完 yaml 要跑一次 --sync-bases。

    只增不改不删：已有的 base（可能被你手工调过 ctx / 别名 / 不支持原因）绝不覆盖。
    """
    path = os.path.join(ROOT, "config", "personas.json")
    if not os.path.exists(path):
        return "config/personas.json 不存在（先跑一次不带参数的初始化）"

    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    have = data.get("bases") or {}
    real = {k: v for k, v in (bases or {}).items() if v.get("gguf")}

    added = sorted(k for k in real if k not in have)
    if not added:
        return "bases 已是最新，无需补充"

    for k in added:
        have[k] = real[k]
    data["bases"] = have

    aliases = data.get("ollama_aliases") or {}
    new_alias = 0
    for k in added:
        for al in real[k].get("ollama_alias") or []:
            if al and al != k and al not in aliases:
                aliases[al] = k
                new_alias += 1
    data["ollama_aliases"] = aliases

    bak = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, bak)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    msg = f"已补 {len(added)} 个 base（{', '.join(added)}）"
    if new_alias:
        msg += f"、别名 {new_alias} 个"
    return msg + "；备份 " + os.path.basename(bak)


def bootstrap_residency():
    """首次运行时用示例策略初始化 config/residency.json。

    residency.json 是**运行时可改**的（residency.py set 会写它），所以它不入库；
    仓库只带 example。用户克隆后没有这个文件也能跑（走内置默认值），
    但初始化出来更直观，且能保留 example 里的注释键。
    """
    dst = os.path.join(ROOT, "config", "residency.json")
    src = os.path.join(ROOT, "config", "residency.example.json")
    if os.path.exists(dst):
        return "config/residency.json 已存在，未覆盖"
    if not os.path.exists(src):
        return "缺少示例策略，跳过（将使用内置默认值）"
    shutil.copy2(src, dst)
    return "已从示例初始化 config/residency.json"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="从模板生成 llama-swap 配置")
    ap.add_argument("--check", action="store_true", help="只检查，不写文件")
    ap.add_argument("--sync-bases", action="store_true",
                    help="把 yaml 里新加的模型补进已有的 config/personas.json"
                         "（只增不改不删）")
    args = ap.parse_args()

    if not os.path.exists(TEMPLATE):
        print(f"!! 缺少模板：{TEMPLATE}")
        return 2

    tpl = open(TEMPLATE, "r", encoding="utf-8").read()
    # llama-swap 在 Windows 上接受正斜杠，统一用它避免转义问题
    root_posix = ROOT.replace("\\", "/")

    header = (
        "# ============================================================================\n"
        "#  llama-swap 配置 —— 由 scripts/init_config.py 从 .template 生成\n"
        "# ----------------------------------------------------------------------------\n"
        f"#  生成时间  {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"#  仓库根    {root_posix}\n"
        "#\n"
        "#  ⚠ 本文件含本机绝对路径，不入版本库（见 .gitignore）。\n"
        "#  ⚠ 换机器 / 换目录后重新跑一次 init_config.py，不要手改路径。\n"
        "#    需要改模型参数，改 .template 再重新生成，或直接改本文件（会被下次生成覆盖）。\n"
        "# ============================================================================\n"
    )

    out = tpl.replace("{{GENERATED_HEADER}}", header.rstrip("\n"), 1)
    out = out.replace("{{ROOT}}", root_posix)

    for marker in ("{{ROOT}}", "{{GENERATED_HEADER}}"):
        if marker in out:
            print(f"!! 模板里还有未替换的 {marker}，请检查模板")
            return 2

    ggufs = find_ggufs()
    refs = referenced(out)
    missing_file = sorted(r for r in refs if r not in ggufs)
    not_used = sorted(g for g in ggufs if g not in refs)

    models_dir = os.path.join(ROOT, "models")
    print(f"  仓库根目录   {root_posix}")
    if os.path.isdir(models_dir):
        print(f"  models/ 里   {len(ggufs)} 个 .gguf")
    else:
        print("  models/      **目录不存在**（先把 .gguf 放进去）")
    print(f"  配置里引用   {len(refs)} 个")

    if missing_file:
        print("\n  ⚠ 配置里声明了但文件不存在（启动该模型时会失败）：")
        for m in missing_file:
            print(f"      - {m}")
    if not_used:
        print("\n  ⚠ models/ 里有但配置没声明（可用但调用不到）：")
        for g in not_used:
            print(f"      - {g}")

    if args.check:
        has_personas = os.path.exists(os.path.join(ROOT, "config", "personas.json"))
        has_policy = os.path.exists(os.path.join(ROOT, "config", "residency.json"))
        print("\n  人格配置     " + ("已存在" if has_personas
                                     else "缺失（正式运行会从示例初始化）"))
        print("  驻留策略     " + ("已存在" if has_policy
                                 else "缺失（正式运行会从示例初始化）"))
        if has_personas:
            # yaml 里有、但 personas.json 的 bases 里没有 -> 桥调不到（需要 --sync-bases）
            with open(os.path.join(ROOT, "config", "personas.json"),
                      "r", encoding="utf-8") as fh:
                have = (json.load(fh).get("bases") or {})
            miss = sorted(k for k, v in derive_bases(out).items()
                          if v.get("gguf") and k not in have)
            if miss:
                print("  底座对齐     yaml 里有 %d 个模型未登记到桥（辅助模型如纯 completion"
                      " / 仅供内部调用者可忽略）：" % len(miss))
                for k in miss:
                    print(f"      - {k}")
                print("               想让客户端能调用就跑 --sync-bases 补上")
            else:
                print("  底座对齐     桥已认识 yaml 里全部模型")
        print("\n  --check 模式，未写文件")
        return 0

    if os.path.exists(TARGET):
        bak = f"{TARGET}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(TARGET, bak)
        print(f"\n  已备份原配置 -> {os.path.basename(bak)}")

    with open(TARGET, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)
    print(f"  已写入 {os.path.relpath(TARGET, ROOT)}")
    print(f"  人格配置     {bootstrap_personas(derive_bases(out))}")
    if args.sync_bases:
        print(f"  补充底座     {sync_bases(derive_bases(out))}")
    else:
        # 不自动补：personas.json 是用户的文件，静默改写容易让人莫名其妙。
        # 但必须提示，否则「yaml 加了模型却调不到」会很难查。
        try:
            with open(os.path.join(ROOT, "config", "personas.json"),
                      "r", encoding="utf-8") as fh:
                have = (json.load(fh).get("bases") or {})
            miss = sorted(k for k, v in derive_bases(out).items()
                          if v.get("gguf") and k not in have)
        except Exception:      # noqa: BLE001
            miss = []
        if miss:
            print("  底座对齐     yaml 里有 %d 个模型未登记到桥（辅助模型可忽略；"
                  "想让客户端能调用就跑 --sync-bases）：%s"
                  % (len(miss), ", ".join(miss)))
    print(f"  驻留策略     {bootstrap_residency()}")

    # 有 llama-swap.exe 就顺手校验一遍，省得带着坏配置去启动
    exe = os.path.join(ROOT, "bin", "llama-swap.exe")
    if os.path.exists(exe):
        import subprocess
        try:
            r = subprocess.run([exe, "-config", TARGET, "-validate"],
                               capture_output=True, text=True, timeout=30,
                               creationflags=0x08000000)
            msg = (r.stdout or "").strip() or (r.stderr or "").strip()
            print(f"  校验：{msg[:300]}")
        except Exception as exc:      # noqa: BLE001
            print(f"  校验跳过：{exc}")
    else:
        print("  （bin/llama-swap.exe 不存在，跳过校验）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
