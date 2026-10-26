"""对比各模型「生成不泄露汤底的提示」的能力。

判题是**有界决策**（grammar 锁死标签），提示是**生成任务**（没有兜底）——
所以两者要分开选模型。这个脚本量化提示的质量：

  首答泄露率 —— 第一次生成就说出谜底（原始生成能力，越低越好）
  终态泄露率 —— 走完整套四层保险后仍然泄露（实际交付效果，越低越好）
  兜底率     —— 重试用完、降级成通用话术（越高越说明模型不行，玩家体验差）
  平均尝试数 —— 1.0 = 一次就过

⚠️ 两阶段设计（重要）：
  第一阶段只用候选模型生成；第二阶段才用固定的裁判模型统一判定。
  否则裁判（9B）与候选（可能是 4B）交替调用，llama-swap 每次都要换载模型，
  8G 卡上会慢到不可用。

⚠️ 泄露判定用**两个固定的独立模型**，取「任一判泄露即算泄露」的严格口径，
  避免「自己评自己」偏松。这两个裁判本身若也是候选，对应那行会偏宽松 —— 输出里会标注。

用法：
    python turtle\\bench_hint.py --models qwen35-4b-heretic,qwen35-9b-heretic
    python turtle\\bench_hint.py --n 8 --show
    python turtle\\bench_hint.py --rejudge results/hint-XXX.json   # 只重判不重生成
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from judge import (  # noqa: E402
    DEFAULT_BASE, FALLBACK_HINT, PROMPT_HINT, START_HINT, JudgeError,
    _chat_once, _tidy_hint, check_leak, make_hint,
)

PUZZLE_FILE = ROOT / "puzzles" / "classic.json"
RESULTS = ROOT / "results"

# 固定的泄露裁判（不随候选模型变化）
# ⚠️ 依据 `calib_leak.py` 的校准：4B 把关 100% 准（0 错杀 0 漏检），
#    9B-heretic 95%（错杀 1），9B-abliterated 只有 75%（错杀 5，太严）。
#    所以裁判用 4B + 9B-heretic，并把「任一判泄露」和「都判泄露」两种口径都报出来。
LEAK_JUDGES = ("qwen35-4b-heretic", "qwen35-9b-heretic")


def load_puzzles(path, only=None, n=None, seed=0):
    puzzles = json.loads(Path(path).read_text(encoding="utf-8"))["puzzles"]
    if only:
        want = {x.strip() for x in only.split(",") if x.strip()}
        puzzles = [p for p in puzzles if p["id"] in want]
    if n and len(puzzles) > n:
        puzzles = random.Random(seed).sample(puzzles, n)
    return puzzles


def generate_all(models, puzzles, base, show=False, timeout=300):
    """阶段一：只用候选模型生成，不做判定（避免模型来回换载）。"""
    rows = {}
    for m in models:
        rows[m] = []
        print(f"### 生成：{m}", flush=True)
        for pz in puzzles:
            asked = [pr["q"] for pr in (pz.get("probes") or [])[:2]]
            rec = {"puzzle": pz["id"], "difficulty": pz.get("difficulty")}
            try:
                user = PROMPT_HINT.format(surface=pz["surface"], bottom=pz["bottom"],
                                          asked="；".join(asked))
                t0 = time.time()
                raw, _ = _chat_once("", user, m, base, timeout=timeout)
                rec["first_text"] = _tidy_hint(raw)
                rec["first_ms"] = int((time.time() - t0) * 1000)

                res = make_hint(pz["surface"], pz["bottom"], asked, [],
                                model=m, base=base, timeout=timeout)
                rec["final_text"] = res["text"]
                rec["tries"] = res["tries"]
                rec["fallback"] = res["fallback"]
                rec["pipeline_ms"] = res["ms"]
            except JudgeError as exc:
                rec["error"] = str(exc)
            rows[m].append(rec)
            if show and "error" not in rec:
                print(f"  [{pz['id']}] 首答：{rec['first_text']}")
                print(f"             终态：{rec['final_text']}"
                      f"（尝试{rec['tries']}次 兜底={rec['fallback']}）")
    return rows


def judge_all(rows, puzzles_by_id, base, judges, timeout=300):
    """阶段二：用固定的裁判模型统一判定所有已生成的文本。

    同时记录两种口径：
      严格（strict）—— 任一裁判判「泄露」即算泄露（偏保守，适合当红线）
      共识（consensus）—— 全部裁判都判「泄露」才算（偏保守地认为没泄露）
    实测口径差异很大（`calib_leak.py`：4B 把关 100% 准，9B 会错杀），
    所以两个都报，免得被单个裁判带偏。
    """
    for recs in rows.values():
        for rec in recs:
            rec.pop("first_votes", None)
            rec.pop("final_votes", None)
            rec.pop("first_leak", None)
            rec.pop("final_leak", None)
    for jm in judges:
        print(f"### 判定：{jm}", flush=True)
        for m, recs in rows.items():
            for rec in recs:
                if "error" in rec:
                    continue
                pz = puzzles_by_id[rec["puzzle"]]
                for key in ("first", "final"):
                    text = rec.get(f"{key}_text") or ""
                    votes = rec.setdefault(f"{key}_votes", {})
                    if not text or text in (FALLBACK_HINT, START_HINT):
                        votes["-"] = "固定话术"
                        continue
                    try:
                        leaked, verdict = check_leak(pz["bottom"], text,
                                                     model=jm, base=base, timeout=timeout)
                    except JudgeError:
                        leaked, verdict = False, None
                    votes[jm] = verdict or "?"
                    rec[f"{key}_strict"] = rec.get(f"{key}_strict", False) or leaked
    # 汇总两种口径
    for recs in rows.values():
        for rec in recs:
            for key in ("first", "final"):
                v = rec.get(f"{key}_votes") or {}
                verdicts = [x for x in v.values() if x in ("泄露", "安全")]
                rec[f"{key}_strict"] = any(x == "泄露" for x in verdicts)
                rec[f"{key}_consensus"] = bool(verdicts) and all(
                    x == "泄露" for x in verdicts)
    return rows


def summarize(model, recs):
    ok = [r for r in recs if "error" not in r]
    n = len(recs)
    return {
        "model": model, "n": n, "errors": n - len(ok),
        "first_leak": sum(1 for r in ok if r.get("first_strict")),
        "first_leak_consensus": sum(1 for r in ok if r.get("first_consensus")),
        "final_leak": sum(1 for r in ok if r.get("final_strict")),
        "final_leak_consensus": sum(1 for r in ok if r.get("final_consensus")),
        "fallback": sum(1 for r in ok if r.get("fallback")),
        "retried": sum(1 for r in ok if (r.get("tries") or 0) > 1),
        "tries_sum": sum(r.get("tries") or 0 for r in ok),
        "first_ms": (sum(r["first_ms"] for r in ok) / len(ok)) if ok else None,
    }


def render(summaries, rows, meta):
    L = ["# 提示生成能力对比", "",
         f"- 生成时间：{meta['time']}",
         f"- 题目：{meta['n_puzzles']} 道（`{meta['puzzle_file']}`）",
         f"- 泄露裁判（固定）：{'、'.join(meta['leak_judges'])}",
         "- 口径：「严格」= 任一裁判判泄露即算；「共识」= 裁判都判泄露才算",
         "- ⚠️ 裁判本身也有误差（见 `calib_leak.py` 的校准：4B 把关 100% 准，",
         "  9B-abliterated 会错杀一半），所以**两组数字都要看**，别只看一组。",
         "", "## 总览", "",
         "| 模型 | 首答泄露(严/共识) | 终态泄露(严/共识) | 兜底率 | 需重试 | 平均尝试 | 首答耗时 |",
         "|---|---|---|---|---|---|---|"]
    for s in summaries:
        L.append(f"| {s['model']} | {s['first_leak']}/{s['first_leak_consensus']} "
                 f"| **{s['final_leak']}/{s['final_leak_consensus']}** "
                 f"| {s['fallback']}/{s['n']} | {s['retried']}/{s['n']} "
                 f"| {s['tries_sum']/s['n']:.2f} | {s['first_ms']:.0f}ms |")
    L += ["", "> 「首答泄露」= 原始生成能力；「终态泄露」= 交付后玩家实际会撞到的比例；",
          "> 「兜底率」高说明模型老被拦，玩家拿到的是千篇一律的通用话术。", ""]
    for s in summaries:
        L += [f"## {s['model']}", "",
              "| 题目 | 首答泄露 | 终态泄露 | 尝试 | 兜底 | 首答内容 |",
              "|---|---|---|---|---|---|"]
        for r in rows[s["model"]]:
            f = (r.get("first_text") or r.get("error", "")).replace("|", "／")
            L.append(f"| {r['puzzle']} | {'是' if r.get('first_strict') else ''} "
                     f"| {'是' if r.get('final_strict') else ''} | {r.get('tries','')} "
                     f"| {'是' if r.get('fallback') else ''} | {f[:46]} |")
        L.append("")
    return "\n".join(L)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="提示生成能力对比")
    ap.add_argument("--models", default="qwen35-4b-heretic,qwen35-9b-heretic")
    ap.add_argument("--puzzles", default=str(PUZZLE_FILE))
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--only", default=None)
    ap.add_argument("--seed", type=int, default=20261002)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--rejudge", default=None, help="只对已有的 json 重新判定，不重生成")
    ap.add_argument("--judges", default=",".join(LEAK_JUDGES),
                    help="泄露裁判（默认 4B+9B-heretic）。⚠️ calib_leak.py 实测 4B 把关最准")
    ap.add_argument("--out-dir", default=str(RESULTS))
    args = ap.parse_args()

    puzzles = load_puzzles(args.puzzles, args.only, args.n or None, args.seed)
    by_id = {p["id"]: p for p in puzzles}
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    judges = [j.strip() for j in args.judges.split(",") if j.strip()]
    print(f"题目 {len(puzzles)} 道 × 候选模型 {len(models)} 个")
    print(f"泄露裁判（固定）：{'、'.join(judges)}")

    if args.rejudge:
        src = json.loads(Path(args.rejudge).read_text(encoding="utf-8"))
        rows = src["rows"]
        rows = judge_all(rows, by_id, args.base, judges)
    else:
        rows = generate_all(models, puzzles, args.base, args.show)
        print()
        rows = judge_all(rows, by_id, args.base, judges)

    summaries = [summarize(m, rows[m]) for m in rows]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    meta = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "puzzle_file": args.puzzles, "n_puzzles": len(puzzles),
            "leak_judges": judges}
    report = render(summaries, rows, meta)
    (out_dir / f"hint-{stamp}.md").write_text(report, encoding="utf-8")
    (out_dir / "hint-latest.md").write_text(report, encoding="utf-8")
    (out_dir / f"hint-{stamp}.json").write_text(
        json.dumps({"meta": meta, "summaries": summaries, "rows": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 68)
    for s in summaries:
        tag = "  ← 也是裁判之一，本行偏松" if s["model"] in LEAK_JUDGES else ""
        print(f"{s['model']:32s} 首答泄露 {s['first_leak']}/{s['n']}  "
              f"终态 {s['final_leak']}/{s['n']}  兜底 {s['fallback']}/{s['n']}"
              f"  平均尝试 {s['tries_sum']/s['n']:.2f}{tag}")
    print(f"\n报告：turtle/results/hint-{stamp}.md")


main()
