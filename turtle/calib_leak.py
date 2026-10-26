"""提示「泄露把关」的校准测试 —— 用人工标注的样本量化把关模型准不准。

背景：`make_hint` 的防泄露靠 `judge.check_leak()`（语义把关）。
实测发现它**假阳性偏高**：会把「那些被砍断的人之前认识这个男人吗？」
这类与谜底无关的提问也判成「泄露」。本轮就是给这个把关打分。

样本：20 条人工写的提示（10 条显然泄露 + 10 条显然安全），
用真实题库的汤底做对照，判据不含糊。

指标：
  准确率 / 精确率 / 召回率
    · 精确率低 → 老是错杀，玩家拿到的是千篇一律的兜底话术
    · 召回率低 → 漏掉真泄露，玩家一个提示就把谜底端走了（更糟）
  在这件事上**召回率优先**（宁可错杀），但也别低到把功能废掉。

用法：
    python turtle\\calib_leak.py --models qwen35-4b-heretic,qwen35-9b-heretic
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from judge import DEFAULT_BASE, JudgeError, check_leak  # noqa: E402

PUZZLE_FILE = ROOT / "puzzles" / "classic.json"
RESULTS = ROOT / "results"

# (题目 id, 提示, 人工标注是否泄露)
# 「安全」那 10 条特意写成**和剧情有关、但汤底里根本没写**的旁支细节 ——
# 这才是把关最容易错杀的一类（写成「狗粮什么牌子」就太好判了，测不出问题）。
CASES = [
    # ------------------------------ 明显泄露：直接点破汤底里的事实
    ("hiccup", "他要水其实是为了止住打嗝吗？", True),
    ("hiccup", "酒保掏枪是想吓他吗？", True),
    ("elevator", "他个子矮，够不到顶楼的按钮吗？", True),
    ("funeral", "她杀死姐姐，是为了再办一场葬礼吗？", True),
    ("windowcleaner", "他擦的是大楼内侧的玻璃吗？", True),
    ("snowtracks", "那行脚印是雪停之后才留下的吗？", True),
    ("dogbark", "狗不叫是因为老人没有再起身吗？", True),
    ("owncall", "那通电话是从她自己的屋子里打出去的吗？", True),
    ("icecube", "毒是下在冰块里的吗？", True),
    ("lamp", "他提灯是为了让别人看见他吗？", True),
    # ------------------------------ 安全：沾剧情、但汤底里没写
    ("hiccup", "他那天晚上是一个人来的吗？", False),
    ("elevator", "他在这栋楼里住了多久了？", False),
    ("funeral", "那场葬礼是在殡仪馆办的吗？", False),
    ("windowcleaner", "他做这份工作很多年了吗？", False),
    ("snowtracks", "他在这座山上住了多久了？", False),
    ("dogbark", "那条狗平时会跟着老人出门吗？", False),
    ("owncall", "她家的门当时锁着吗？", False),
    ("icecube", "他们两个人互相认识吗？", False),
    ("lamp", "他提的那盏灯是什么样子的？", False),
    ("funeral", "妹妹那天是坐什么车去葬礼的？", False),
]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="泄露把关的校准测试")
    ap.add_argument("--models", default="qwen35-4b-heretic,qwen35-9b-heretic")
    ap.add_argument("--puzzles", default=str(PUZZLE_FILE))
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--out-dir", default=str(RESULTS))
    args = ap.parse_args()

    by_id = {p["id"]: p for p in
             json.loads(Path(args.puzzles).read_text(encoding="utf-8"))["puzzles"]}
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    print(f"样本 {len(CASES)} 条（泄露 {sum(1 for c in CASES if c[2])} / "
          f"安全 {sum(1 for c in CASES if not c[2])}）× 把关模型 {len(models)} 个\n")

    summaries, rows_by_model = [], {}
    for m in models:
        rows = []
        tp = fp = tn = fn = 0
        print(f"### {m}")
        for pid, hint, truth in CASES:
            try:
                leaked, verdict = check_leak(by_id[pid]["bottom"], hint,
                                             model=m, base=args.base)
            except JudgeError as exc:
                rows.append({"puzzle": pid, "hint": hint, "truth": truth,
                             "error": str(exc)})
                print(f"  失败 {exc}")
                continue
            if truth and leaked:
                tp += 1
            elif truth and not leaked:
                fn += 1
            elif not truth and leaked:
                fp += 1
            else:
                tn += 1
            rows.append({"puzzle": pid, "hint": hint, "truth": truth,
                         "verdict": verdict, "say_leak": leaked,
                         "correct": leaked == truth})
            mark = "OK " if leaked == truth else "XX "
            if leaked != truth:
                print(f"  {mark}[{pid}] 标注={'泄露' if truth else '安全'} "
                      f"判={verdict}  {hint}")
        n = tp + fp + tn + fn
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        acc = (tp + tn) / n if n else 0.0
        s = {"model": m, "n": n, "tp": tp, "fp": fp, "tn": tn, "fn": fn,
             "accuracy": acc, "precision": prec, "recall": rec}
        summaries.append(s)
        rows_by_model[m] = rows
        print(f"  -> 准确率 {acc*100:.0f}%  精确率 {prec*100:.0f}%  "
              f"召回率 {rec*100:.0f}%  (漏检 {fn} / 错杀 {fp})\n")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"leakcalib-{stamp}.json").write_text(
        json.dumps({"summaries": summaries, "rows": rows_by_model},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    from judge import PROMPT_LEAK_GUARD
    L = ["# 泄露把关（check_leak）校准", "",
         f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
         f"- 样本：{len(CASES)} 条人工标注（泄露 10 / 安全 10）", "",
         "| 把关模型 | 准确率 | 精确率（低=老错杀） | 召回率（低=漏放行） | 漏检 | 错杀 |",
         "|---|---|---|---|---|---|"]
    for s in summaries:
        L.append(f"| {s['model']} | {s['accuracy']*100:.0f}% "
                 f"| **{s['precision']*100:.0f}%** | {s['recall']*100:.0f}% "
                 f"| {s['fn']} | {s['fp']} |")
    L += ["", "> 召回率优先（漏检 = 玩家一个提示就把谜底端走），",
          "> 但精确率太低会让功能退化成「永远是兜底话术」。", ""]
    (out_dir / "leakcalib-latest.md").write_text("\n".join(L), encoding="utf-8")

    print("=" * 66)
    for s in summaries:
        print(f"{s['model']:32s} 准确 {s['accuracy']*100:3.0f}%  "
              f"精确 {s['precision']*100:3.0f}%  召回 {s['recall']*100:3.0f}%")
    print(f"\n报告：turtle/results/leakcalib-latest.md")


main()
