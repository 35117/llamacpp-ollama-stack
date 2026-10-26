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


def bootstrap_personas():
    """首次运行时用示例人格初始化 config/personas.json。

    不这么做的话桥会因「缺少 config/personas.json」直接退出 —— 克隆下来跑不起来。
    已有文件绝不覆盖（那是用户自己的人设）。
    """
    dst_json = os.path.join(ROOT, "config", "personas.json")
    src_json = os.path.join(ROOT, "config", "personas.example.json")
    dst_dir = os.path.join(ROOT, "config", "personas")
    src_dir = os.path.join(ROOT, "config", "personas.example")

    if os.path.exists(dst_json):
        return "config/personas.json 已存在，未覆盖"
    if not os.path.exists(src_json):
        return "缺少示例文件，跳过"

    os.makedirs(dst_dir, exist_ok=True)
    if os.path.isdir(src_dir):
        for name in os.listdir(src_dir):
            s, d = os.path.join(src_dir, name), os.path.join(dst_dir, name)
            if os.path.isfile(s) and not os.path.exists(d):
                shutil.copy2(s, d)
    shutil.copy2(src_json, dst_json)
    return "已从示例初始化 config/personas.json（记得改 bases 里的 gguf 文件名）"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="从模板生成 llama-swap 配置")
    ap.add_argument("--check", action="store_true", help="只检查，不写文件")
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

    print(f"  仓库根目录   {root_posix}")
    print(f"  models/ 里   {len(ggufs)} 个 .gguf")
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
        print("\n  人格配置     " + ("已存在" if has_personas
                                     else "缺失（正式运行会从示例初始化）"))
        print("\n  --check 模式，未写文件")
        return 0

    if os.path.exists(TARGET):
        bak = f"{TARGET}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(TARGET, bak)
        print(f"\n  已备份原配置 -> {os.path.basename(bak)}")

    with open(TARGET, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)
    print(f"  已写入 {os.path.relpath(TARGET, ROOT)}")
    print(f"  人格配置     {bootstrap_personas()}")

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
