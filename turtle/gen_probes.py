"""自动生成题库的探针（probes）—— 用来跑判题准确率基准。

背景：`bench.py` 要靠「探针 = 提问 + 标准答案」才能量化判题准确率，
但新加的题往往只有汤面和汤底。手写探针很费事，这个脚本让模型从汤底自动拆。

产出会标 `"source": "auto"`，与人工审校过的探针区分开 —— 统计时能单独看。

用法：
    python turtle\\gen_probes.py --dry-run            # 只打印（先看质量）
    python turtle\\gen_probes.py --only tunnel        # 只处理某题
    python turtle\\gen_probes.py --model qwen35-9b-heretic --n 10
    python turtle\\gen_probes.py --force              # 覆盖已有探针（重生成）

⚠️ 自动生成只是「起草」。跑完基准后，把模型答错的条目拿出来人工过一遍 ——
   错的可能是模型，也可能是标注。别直接全信。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from judge import DEFAULT_BASE, JudgeError, _chat_once  # noqa: E402

PUZZLE_FILE = ROOT / "puzzles" / "classic.json"

# 踩过的坑：只写「出是非题」时，模型会输出**陈述句**而不是问句，
# 解析全被拒。改成「必须问句 + 给一行完整示例」后正常。
PROMPT = """下面是一道海龟汤的「汤底」（真相）。请根据它出 {n} 道**是非题**，
用来检验一个人是否真的读懂了真相，并给出标准答案。

出题要求：
1. 每道题都必须是**问句，以「？」结尾**（不能写成陈述句）。
2. 问题要能用「是」或「否」回答。
3. 大约一半的答案应当是「是」——问的是汤底里明确写出来的事实。
4. 另一半答案应当是「否」，要包含这两类：
   a) 汤底里明确否定、或与汤底矛盾的说法；
   b) **看上去和故事有关、汤底里却根本没写的事**（这一类最重要，
      专门用来抓「凭常识或前后关联瞎推断」的毛病）。
5. 不要问「汤底里有没有解释」这类元问题。

输出格式（每行一条，严格用竖线分隔，顺序固定为：问题|答案|依据）：
问句？|答案|依据

示例：
枪里有子弹吗？|否|汤底说枪里其实没有子弹

其中「答案」只能是 是 或 否；「依据」写明这个答案是从汤底哪一句推出来的。
不要编号、不要多余解释、不要空行。

【汤底】{bottom}

每行一条："""

FORMAT_NUDGE = ("\n\n注意：上一次的输出格式不对。必须每行都是「问句？|是或否|依据」，"
                "第一段要以「？」结尾。")


def parse_lines(text):
    """把模型输出拆成 (问题, 答案, 依据) 三元组列表。"""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", line)
        parts = re.split(r"[|｜]", line)
        if len(parts) < 2:
            continue
        q = parts[0].strip().strip("「」“”\"'").strip()
        ans = parts[1].strip().strip("「」“”\"'").strip("。.")
        why = (parts[2].strip() if len(parts) > 2 else "").strip()
        if not q.endswith(("？", "?")):
            continue                     # 只收问句，陈述句一律不要
        if ans not in ("是", "否"):
            continue
        if len(q) < 5:
            continue
        out.append((q, ans, why))
    return out


def dedupe(items):
    seen, res = set(), []
    for q, ans, why in items:
        key = re.sub(r"[\s，。？！,.?!]", "", q)
        if key in seen:
            continue
        seen.add(key)
        res.append((q, ans, why))
    return res


def generate(bottom, model, base, n, retry=3):
    """生成一批探针；条数不够就带格式提醒重试。"""
    items, text, ms = [], "", 0
    for i in range(retry + 1):
        prompt = PROMPT.format(bottom=bottom, n=n)
        if i:
            prompt += FORMAT_NUDGE
        text, ms = _chat_once("", prompt, model, base,
                              timeout=300, temperature=0.4, max_tokens=700)
        items = dedupe(parse_lines(text))
        if len(items) >= max(4, n // 2):
            break
    return items[: n + 2], text, ms


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="自动生成题库探针")
    ap.add_argument("--model", default="qwen35-9b-heretic",
                    help="生成模型（默认 9B，出题质量更好；判题另用 4B 以降低同源偏差）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--n", type=int, default=10, help="每题目标条数")
    ap.add_argument("--only", default=None)
    ap.add_argument("--difficulty", default=None, help="只处理指定难度")
    ap.add_argument("--force", action="store_true", help="覆盖已有探针")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = json.loads(PUZZLE_FILE.read_text(encoding="utf-8"))
    targets = []
    for pz in data["puzzles"]:
        if args.only and pz["id"] != args.only:
            continue
        if args.difficulty and pz.get("difficulty") != args.difficulty:
            continue
        if pz.get("probes") and not args.force:
            continue
        targets.append(pz)

    if not targets:
        print("没有需要生成探针的题")
        return

    print(f"待处理 {len(targets)} 题，生成模型 {args.model}，每题目标 {args.n} 条\n")
    changed, failed = 0, []
    for pz in targets:
        print("=" * 74)
        print(f"【{pz['id']}】{pz['title']}　[{pz.get('difficulty')}]")
        try:
            items, raw, ms = generate(pz["bottom"], args.model, args.base, args.n)
        except JudgeError as exc:
            print(f"  生成失败：{exc}")
            failed.append(pz["id"])
            continue
        if not items:
            print(f"  解析不出条目，原始输出：{raw[:200]}")
            failed.append(pz["id"])
            continue
        n_yes = sum(1 for _, a, _ in items if a == "是")
        print(f"  {len(items)} 条（是 {n_yes} / 否 {len(items)-n_yes}），{ms}ms")
        for q, a, why in items:
            print(f"    [{a}] {q}　— {why[:38]}")
        if not args.dry_run:
            pz["probes"] = [
                {"q": q, "a": a, "why": why, "source": "auto"} for q, a, why in items
            ]
            changed += 1
        print()

    if args.dry_run:
        print("（--dry-run：没有写回题库）")
        return

    if changed:
        note = data.get("schema_note", "")
        if "source" not in note:
            data["schema_note"] = note + (
                " probes[].source=auto 表示由 gen_probes.py 自动生成、未逐条人工审校，"
                "统计时可与人工标注分开看。")
        PUZZLE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        back = json.loads(PUZZLE_FILE.read_text(encoding="utf-8"))
        n = sum(len(p.get("probes") or []) for p in back["puzzles"])
        ok = sum(1 for p in back["puzzles"] if p.get("probes"))
        print(f"已写回 {changed} 题；题库共 {ok}/{len(back['puzzles'])} 题有探针，合计 {n} 条")
        if failed:
            print(f"⚠️ 这些题没生成成功，需重跑：{failed}")


main()
