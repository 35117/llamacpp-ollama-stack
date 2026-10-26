"""海龟汤判题基准 —— 用标注题库量化「方案 A」（改造现有模型）够不够用。

跑法：
    python turtle/bench.py                                  # 默认 4B × 三分类
    python turtle/bench.py --models qwen35-4b-heretic,qwen35-9b-heretic --modes three,binary

产出：
    turtle/results/bench-<时间戳>.json   原始逐条结果
    turtle/results/bench-<时间戳>.md     可读报告（含混淆矩阵与错误明细）
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from judge import DEFAULT_BASE, DEFAULT_MODEL, MODES, JudgeError, judge  # noqa: E402

ROOT = Path(__file__).resolve().parent

MODE_NAME = {
    "binary": "二分类（是/否）",
    "three": "三分类（是/否/无关）",
    "four": "四分类（+部分对）",
}


def load_puzzles(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["puzzles"]


def run_combo(model, mode, puzzles, base, with_probs, limit, quiet):
    """跑一个 (模型, 模式) 组合。只统计期望标签落在该模式标签集内的探针。"""
    labels = MODES[mode]["labels"]
    rows = []
    skipped = 0
    for pz in puzzles:
        probes = pz["probes"][:limit] if limit else pz["probes"]
        for pr in probes:
            if pr["a"] not in labels:
                skipped += 1
                continue
            rec = {
                "puzzle": pz["id"],
                "question": pr["q"],
                "expected": pr["a"],
                "hard": bool(pr.get("hard")),
                "difficulty": pz.get("difficulty", "?"),
                "source": pr.get("source", "hand"),
            }
            try:
                res = judge(pz["surface"], pz["bottom"], pr["q"],
                            model=model, mode=mode, base=base, with_probs=with_probs)
                rec["predicted"] = res["label"]
                rec["raw"] = res["raw"]
                rec["ms"] = res["ms"]
                if with_probs:
                    rec["probs"] = res.get("probs") or {}
            except JudgeError as exc:
                rec["predicted"] = None
                rec["error"] = str(exc)
                rec["ms"] = None
            rec["correct"] = rec["predicted"] == rec["expected"]
            rows.append(rec)
            if not quiet:
                mark = "OK " if rec["correct"] else "XX "
                print(f"  {mark}[{pz['id']}] {pr['q'][:24]:26s} "
                      f"期望={rec['expected']} 预测={rec['predicted']} ({rec['ms']}ms)")
    return rows, skipped


def summarize(model, mode, rows, labels):
    total = len(rows)
    correct = sum(1 for r in rows if r["correct"])
    failed = [r for r in rows if r["predicted"] is None]

    per_label = {}
    for lab in labels:
        sub = [r for r in rows if r["expected"] == lab]
        per_label[lab] = {"n": len(sub),
                          "correct": sum(1 for r in sub if r["correct"])}

    conf = {exp: Counter() for exp in labels}
    for r in rows:
        conf[r["expected"]][r["predicted"] or "<失败>"] += 1

    per_puzzle = defaultdict(lambda: {"n": 0, "correct": 0})
    for r in rows:
        per_puzzle[r["puzzle"]]["n"] += 1
        per_puzzle[r["puzzle"]]["correct"] += int(r["correct"])

    # 按难度 / 按探针来源分组 —— 用于「小模型扛不扛得住复杂题」的判断
    per_diff = defaultdict(lambda: {"n": 0, "correct": 0})
    per_source = defaultdict(lambda: {"n": 0, "correct": 0})
    for r in rows:
        per_diff[r["difficulty"]]["n"] += 1
        per_diff[r["difficulty"]]["correct"] += int(r["correct"])
        per_source[r.get("source", "hand")]["n"] += 1
        per_source[r.get("source", "hand")]["correct"] += int(r["correct"])

    times = [r["ms"] for r in rows if r["ms"] is not None]
    hard = [r for r in rows if r["hard"]]
    easy = [r for r in rows if not r["hard"]]
    prior = Counter(r["expected"] for r in rows)
    baseline = sum((c / total) ** 2 for c in prior.values()) if total else 0.0

    return {
        "model": model, "mode": mode, "total": total, "correct": correct,
        "accuracy": correct / total if total else 0.0,
        "label_prior_accuracy": baseline, "failed_calls": len(failed),
        "hard_accuracy": (sum(1 for r in hard if r["correct"]) / len(hard)) if hard else None,
        "hard_n": len(hard),
        "easy_accuracy": (sum(1 for r in easy if r["correct"]) / len(easy)) if easy else None,
        "easy_n": len(easy),
        "ms_first": times[0] if times else None,
        "ms_mean": (sum(times[1:]) / len(times[1:])) if len(times) > 1 else None,
        "per_label": per_label,
        "confusion": {k: dict(v) for k, v in conf.items()},
        "per_puzzle": dict(per_puzzle),
        "per_difficulty": dict(per_diff),
        "per_source": dict(per_source),
    }


def render_report(summaries, rows_by_key, meta):
    L = []
    L.append("# 海龟汤判题基准报告")
    L.append("")
    L.append(f"- 生成时间：{meta['time']}")
    L.append(f"- 题库：`{meta['puzzle_file']}`（{meta['n_puzzles']} 题 / {meta['n_probes']} 探针）")
    L.append("- 判题方式：llama.cpp `grammar` 把输出锁死在候选标签内，temperature=0，关闭思考")
    L.append("- 判题器：`turtle/judge.py`　基准脚本：`turtle/bench.py`")
    L.append("")
    L.append("## 总览")
    L.append("")
    L.append("| 模型 | 模式 | 准确率 | 对比随机基线 | 客观题 | 主观标注 | 首次调用 | 平均耗时 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for s in summaries:
        hard = f"{s['hard_accuracy']*100:.1f}% ({s['hard_n']})" if s["hard_accuracy"] is not None else "—"
        easy = f"{s['easy_accuracy']*100:.1f}% ({s['easy_n']})" if s["easy_accuracy"] is not None else "—"
        mean = f"{s['ms_mean']:.0f}ms" if s["ms_mean"] else "—"
        L.append(f"| {s['model']} | {MODE_NAME.get(s['mode'], s['mode'])} "
                 f"| **{s['accuracy']*100:.1f}%** ({s['correct']}/{s['total']}) "
                 f"| {s['label_prior_accuracy']*100:.1f}% | {easy} | {hard} "
                 f"| {s['ms_first']}ms | {mean} |")
    L.append("")
    L.append("> 「随机基线」= 按标签出现频率随机猜的期望准确率，"
             "二分类约 50%，三分类约 33%–40%。")
    L.append("> 「主观标注」= 题库里 `hard=true` 的条目（如「部分对」这类带主观性的判断），"
             "单列出来看，别让它们拉低对客观题的判断。")
    L.append("")

    for s in summaries:
        key = f"{s['model']} × {s['mode']}"
        L.append(f"## {s['model']} · {MODE_NAME.get(s['mode'], s['mode'])}")
        L.append("")
        L.append("### 按标签")
        L.append("")
        L.append("| 标签 | 正确 | 总数 | 准确率 |")
        L.append("|---|---|---|---|")
        for lab, d in s["per_label"].items():
            acc = (d["correct"] / d["n"] * 100) if d["n"] else 0.0
            L.append(f"| {lab} | {d['correct']} | {d['n']} | {acc:.1f}% |")
        L.append("")
        labels = list(s["per_label"])
        L.append("### 混淆矩阵")
        L.append("")
        L.append("| 期望＼预测 | " + " | ".join(labels) + " | 失败 |")
        L.append("|---" * (len(labels) + 2) + "|")
        for exp in labels:
            row = s["confusion"].get(exp, {})
            cells = [str(row.get(lab, 0)) for lab in labels]
            L.append(f"| **{exp}** | " + " | ".join(cells) + f" | {row.get('<失败>', 0)} |")
        L.append("")
        ORDER = ("easy", "medium", "hard", "?")
        diffs = sorted(s["per_difficulty"], key=lambda x: ORDER.index(x) if x in ORDER else 9)
        L.append("### 按难度")
        L.append("")
        L.append("| 难度 | 正确 | 总数 | 准确率 |")
        L.append("|---|---|---|---|")
        for dk in diffs:
            d = s["per_difficulty"][dk]
            acc = (d["correct"] / d["n"] * 100) if d["n"] else 0.0
            L.append(f"| {dk} | {d['correct']} | {d['n']} | {acc:.1f}% |")
        L.append("")
        if len(s["per_source"]) > 1:
            L.append("### 按探针来源")
            L.append("")
            L.append("| 来源 | 正确 | 总数 | 准确率 |")
            L.append("|---|---|---|---|")
            for sk, d in sorted(s["per_source"].items()):
                acc = (d["correct"] / d["n"] * 100) if d["n"] else 0.0
                label = "人工标注" if sk == "hand" else ("自动生成" if sk == "auto" else sk)
                L.append(f"| {label} | {d['correct']} | {d['n']} | {acc:.1f}% |")
            L.append("")
        L.append("### 按题目")
        L.append("")
        L.append("| 题目 | 难度 | 正确 | 总数 | 准确率 |")
        L.append("|---|---|---|---|---|")
        diff_of = {}
        for r in rows_by_key[key]:
            diff_of[r["puzzle"]] = r.get("difficulty", "?")
        for pid, d in sorted(s["per_puzzle"].items(),
                             key=lambda kv: (ORDER.index(diff_of.get(kv[0], "?"))
                                             if diff_of.get(kv[0], "?") in ORDER else 9,
                                             kv[0])):
            acc = (d["correct"] / d["n"] * 100) if d["n"] else 0.0
            L.append(f"| {pid} | {diff_of.get(pid, '?')} | {d['correct']} | {d['n']} "
                     f"| {acc:.1f}% |")
        L.append("")
        errs = [r for r in rows_by_key[key] if not r["correct"]]
        if errs:
            L.append("### 错误明细")
            L.append("")
            L.append("| 题目 | 难度 | 提问 | 期望 | 预测 | 主观标注 |")
            L.append("|---|---|---|---|---|---|")
            for r in errs:
                L.append(f"| {r['puzzle']} | {r.get('difficulty', '?')} "
                         f"| {r['question']} | {r['expected']} "
                         f"| {r['predicted'] or '失败'} | {'是' if r['hard'] else ''} |")
            L.append("")
    return "\n".join(L)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="海龟汤判题基准")
    ap.add_argument("--models", default=DEFAULT_MODEL)
    ap.add_argument("--modes", default="three", help="逗号分隔：binary,three,four")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--puzzles", default=str(ROOT / "puzzles" / "classic.json"))
    ap.add_argument("--out-dir", default=str(ROOT / "results"))
    ap.add_argument("--limit", type=int, default=0, help="每题只用前 N 个探针（调试）")
    ap.add_argument("--difficulty", default=None,
                    help="只看指定难度的题，逗号分隔：easy,medium,hard")
    ap.add_argument("--probs", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    puzzles = load_puzzles(args.puzzles)
    if args.difficulty:
        want = {x.strip() for x in args.difficulty.split(",") if x.strip()}
        puzzles = [p for p in puzzles if p.get("difficulty") in want]
    if not puzzles:
        raise SystemExit("筛选后没有题目")
    if args.seed:
        random.Random(args.seed).shuffle(puzzles)
    n_probes = sum(len(p["probes"][:args.limit] if args.limit else p["probes"])
                   for p in puzzles)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    print(f"题库：{len(puzzles)} 题 / {n_probes} 探针")
    print(f"组合：{len(models)} 模型 × {len(modes)} 模式")
    print("=" * 72)

    summaries, rows_by_key = [], {}
    for model in models:
        for mode in modes:
            labels = MODES[mode]["labels"]
            print(f"\n### {model} × {MODE_NAME.get(mode, mode)}")
            t0 = time.time()
            rows, skipped = run_combo(model, mode, puzzles, args.base,
                                      args.probs, args.limit, args.quiet)
            s = summarize(model, mode, rows, labels)
            summaries.append(s)
            rows_by_key[f"{model} × {mode}"] = rows
            print(f"  -> 准确率 {s['accuracy']*100:.1f}% ({s['correct']}/{s['total']})"
                  f"，跳过 {skipped} 条（期望标签不在此模式内），耗时 {time.time()-t0:.0f}s")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    meta = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "puzzle_file": args.puzzles,
        "n_puzzles": len(puzzles),
        "n_probes": n_probes,
    }
    report = render_report(summaries, rows_by_key, meta)
    (out_dir / f"bench-{stamp}.json").write_text(
        json.dumps({"meta": meta, "summaries": summaries, "rows": rows_by_key},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"bench-{stamp}.md").write_text(report, encoding="utf-8")
    (out_dir / "latest.md").write_text(report, encoding="utf-8")

    print("\n" + "=" * 72)
    for s in summaries:
        print(f"{s['model']:22s} {s['mode']:7s} 准确率 {s['accuracy']*100:5.1f}%  "
              f"(随机基线 {s['label_prior_accuracy']*100:.1f}%)")
    print(f"\n报告已写入 turtle/results/bench-{stamp}.md")


if __name__ == "__main__":
    main()
