"""自动生成题库的 checkpoints（猜测打分用的关键要素）。

加题时只需要写 `surface` + `bottom`，检查点交给它生成 —— 省掉手写负担。

用法：
    python turtle\\gen_checkpoints.py --dry-run        # 只打印，不落盘（先看质量）
    python turtle\\gen_checkpoints.py                 # 只补**缺失** checkpoints 的题
    python turtle\\gen_checkpoints.py --only funeral   # 只处理某一题
    python turtle\\gen_checkpoints.py --force          # 全部重新生成（覆盖已有的）

⚠️ 自动生成只是「起草」：写完请人工扫一眼，把含糊或多余的条目删掉 ——
检查点直接决定猜测打分的分数是否合理，值得花一分钟看。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from judge import DEFAULT_BASE, DEFAULT_HINT_MODEL, JudgeError, _chat_once  # noqa: E402

PUZZLE_FILE = ROOT / "puzzles" / "classic.json"

PROMPT = """下面是一道海龟汤的「汤底」（真相）。请把它拆成 3 到 4 条**关键要素**，
用途是判断玩家的猜测说中了多少。

要求：
1. 每条都是「缺了它就不算猜到真相」的要点，不要写可有可无的细节
2. 每条都要能被清晰地回答「玩家的话里有没有体现这一条？」
3. 一条一行，直接写出内容，不要编号、不要解释、不要引号
4. 每条不超过 40 字

【汤底】{bottom}

每行一条："""


def parse_lines(text):
    """把模型输出拆成条目：去掉编号、引号、空行和明显的前后缀。"""
    items = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^\s*(?:[-*•]|\d+[.、)]|第[一二三四五六七八九十]+[条点])[.、:：\s]*",
                      "", line)
        line = line.strip().strip("「」“”\"'").strip()
        line = re.sub(r"^要点[一二三四五六七八九十\d]*[：:]\s*", "", line)
        if len(line) >= 4:
            items.append(line)
    return items[:5]


def generate(bottom, model, base, retry=2):
    """生成一组检查点；条数不合理就重试。"""
    for _ in range(retry + 1):
        text, ms = _chat_once("", PROMPT.format(bottom=bottom), model, base,
                              timeout=300, temperature=0.3, max_tokens=260)
        items = parse_lines(text)
        if 2 <= len(items) <= 5:
            return items, text, ms
    return items, text, ms


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="自动生成题库的 checkpoints")
    ap.add_argument("--model", default=DEFAULT_HINT_MODEL)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--only", default=None, help="只处理指定 id")
    ap.add_argument("--force", action="store_true", help="覆盖已有的检查点")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写回")
    args = ap.parse_args()

    data = json.loads(PUZZLE_FILE.read_text(encoding="utf-8"))
    targets = []
    for pz in data["puzzles"]:
        if args.only and pz["id"] != args.only:
            continue
        if pz.get("checkpoints") and not args.force:
            continue
        targets.append(pz)

    if not targets:
        print("没有需要生成的题（都有检查点了；要重生成加 --force）")
        return

    print(f"待处理 {len(targets)} 题，模型 {args.model}\n")
    changed = 0
    for pz in targets:
        print("=" * 74)
        print(f"【{pz['id']}】{pz['title']}")
        print(f"  汤底：{pz['bottom'][:64]}…")
        old = pz.get("checkpoints") or []
        try:
            items, raw, ms = generate(pz["bottom"], args.model, args.base)
        except JudgeError as exc:
            print(f"  生成失败：{exc}")
            continue
        if old:
            print("  原有检查点：")
            for c in old:
                print(f"    · {c}")
            print("  新生成：")
        for c in items:
            print(f"    ✓ {c}")
        print(f"  （{len(items)} 条，{ms}ms）")
        if len(items) < 2:
            print("  ⚠️ 条数太少，跳过不写回")
            continue
        if not args.dry_run:
            pz["checkpoints"] = items
            changed += 1
        print()

    if args.dry_run:
        print("（--dry-run：没有写回题库）")
        return

    if changed:
        note = data.get("schema_note", "")
        if "checkpoints" not in note:
            data["schema_note"] = note + (
                " checkpoints: 玩家猜答案时用的关键要素，逐条二值判断、命中率即分数。"
                "【可选】—— 不写则退回「一致/不一致」的二值判定。")
        PUZZLE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        back = json.loads(PUZZLE_FILE.read_text(encoding="utf-8"))
        ok = sum(1 for p in back["puzzles"] if p.get("checkpoints"))
        print(f"已写回 {changed} 题；题库现有 {ok}/{len(back['puzzles'])} 题配了检查点")
        print("⚠️ 建议人工扫一眼上面的条目 —— 含糊或多余的删掉，它直接影响分数是否合理。")
    else:
        print("没有改动")


main()
