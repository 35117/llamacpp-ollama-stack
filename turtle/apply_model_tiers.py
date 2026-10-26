"""把 bench.py 的实测结果落回题库：每题的 small_model_ok + 顶层 model_tiers。

用途：让调用方（聊天界面 / 机器人插件）能按难度挑模型 ——
简单题用小模型（快、省显存），复杂题用大模型（准）。

⚠️ 本脚本**如实反映实测**，不预设「难题一定需要大模型」：
   跑完会发现难度标签和「小模型判题准确率」并不相关（见 judge_recommendation），
   那就不该硬把难题派给大模型。数据说什么就写什么。

做法：
  1. 读最近一次 bench 的 JSON 结果，算每个模型的「每题 / 每难度」准确率；
  2. 逐题写 `small_model_ok` 与 `measured`（实测数字）；
  3. 按难度汇总，决定每一档用哪个模型 → 顶层 `model_tiers`；
  4. 另写 `judge_recommendation`，把「是/否」拆分、弱项题目等结论固化下来。

用法：
    python turtle\\apply_model_tiers.py --dry-run
    python turtle\\apply_model_tiers.py --threshold 0.85
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PUZZLE_FILE = ROOT / "puzzles" / "classic.json"
RESULTS = ROOT / "results"
ORDER = ("easy", "medium", "hard")


def latest_bench():
    files = sorted(RESULTS.glob("bench-*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("找不到 bench 结果，先跑 turtle/bench.py")
    return files[-1]


def pct(ok, n):
    return round(ok / n, 4) if n else None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="把实测结果落回题库（难度 → 模型）")
    ap.add_argument("--bench", default=None, help="指定 bench JSON（默认取最新）")
    ap.add_argument("--small", default="qwen35-4b-heretic")
    ap.add_argument("--big", default="qwen35-9b-heretic")
    ap.add_argument("--threshold", type=float, default=0.85,
                    help="小模型达标线（默认 0.85）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bench_file = Path(args.bench) if args.bench else latest_bench()
    bench = json.loads(bench_file.read_text(encoding="utf-8"))
    print(f"基准结果：{bench_file.name}")

    rows = []
    for key, rl in bench["rows"].items():
        model = key.split(" × ")[0]
        for r in rl:
            r["_model"] = model
            rows.append(r)

    per = defaultdict(lambda: defaultdict(lambda: [0, 0]))     # pid -> model -> [ok,n]
    agg = defaultdict(lambda: [0, 0])                          # 小模型 难度 -> [ok,n]
    agg_yes = defaultdict(lambda: [0, 0])
    agg_no = defaultdict(lambda: [0, 0])
    overall = defaultdict(lambda: [0, 0])                      # 模型 -> [ok,n]
    diff_of = {}
    for r in rows:
        m, pid, d = r["_model"], r["puzzle"], r.get("difficulty", "?")
        diff_of[pid] = d
        per[pid][m][1] += 1
        per[pid][m][0] += int(r["correct"])
        overall[m][1] += 1
        overall[m][0] += int(r["correct"])
        if m == args.small:
            agg[d][1] += 1
            agg[d][0] += int(r["correct"])
            tgt = agg_yes if r["expected"] == "是" else agg_no
            tgt[d][1] += 1
            tgt[d][0] += int(r["correct"])

    data = json.loads(PUZZLE_FILE.read_text(encoding="utf-8"))
    flagged = []
    print(f"\n{'题目':<16}{'难度':<8}{'小模型':<18}{'大模型':<18}判定")
    print("-" * 74)
    for pz in data["puzzles"]:
        pid = pz["id"]
        s_ok, s_n = per[pid].get(args.small, [0, 0])
        b_ok, b_n = per[pid].get(args.big, [0, 0])
        acc = (s_ok / s_n) if s_n else 0.0
        okflag = bool(s_n >= 6 and acc >= args.threshold)
        pz["small_model_ok"] = okflag
        pz["measured"] = {
            "n": s_n,
            args.small: pct(s_ok, s_n),
            args.big: pct(b_ok, b_n),
        }
        if not okflag:
            flagged.append(pid)
        print(f"{pid:<16}{pz.get('difficulty','?'):<8}"
              f"{s_ok}/{s_n} ({acc*100:5.1f}%)    "
              f"{b_ok}/{b_n} ({(b_ok/b_n*100) if b_n else 0:5.1f}%)    "
              f"{'小模型够用' if okflag else '建议大模型'}")

    tiers = {}
    print("\n按难度汇总（小模型）：")
    for d in ORDER:
        ok, n = agg.get(d, [0, 0])
        acc = (ok / n) if n else 0.0
        y_ok, y_n = agg_yes.get(d, [0, 0])
        n_ok, n_n = agg_no.get(d, [0, 0])
        model = args.small if acc >= args.threshold else args.big
        tiers[d] = {
            "model": model,
            "n": n,
            "small_accuracy": pct(ok, n),
            "small_accuracy_on_yes": pct(y_ok, y_n),
            "small_accuracy_on_no": pct(n_ok, n_n),
            "why": (f"小模型（{args.small}）整体 {acc*100:.1f}%（{ok}/{n}）"
                    f"，其中「是」题 {pct(y_ok, y_n)*100:.1f}%、"
                    f"「否」题 {pct(n_ok, n_n)*100:.1f}%"),
        }
        print(f"  {d:<8} {acc*100:5.1f}% ({ok}/{n})  "
              f"是 {pct(y_ok,y_n)*100:5.1f}% / 否 {pct(n_ok,n_n)*100:5.1f}%  -> {model}")

    data["model_tiers"] = tiers
    data["judge_recommendation"] = {
        "default_model": args.small,
        "overall": {m: pct(*overall[m]) for m in overall},
        "source": bench_file.name,
        "threshold": args.threshold,
        "note": ("难度标签反映的是「这题对**玩家**有多难」，实测与「小模型判题准确率」"
                 "并不相关（三个档位的小模型准确率几乎持平）—— 所以默认判题不按难度换模型。"
                 "小模型的短板集中在「否」题：真相里没写的事，它倾向于答「是」；"
                 "而「是」题几乎 100% 正确。"),
        "flagged_puzzles": flagged,
        "flagged_note": (f"这些题小模型实测低于 {args.threshold:.0%}，建议判题时改用 "
                         f"{args.big}。⚠️ 每题只有约 10 条探针，单题达标与否有噪声，"
                         "别当硬结论；题库扩容后重跑本脚本。"),
        "big_model_gain": ("换用大模型整体约 +%.1f 个百分点，收益集中在少数「否定型」题。"
                           % ((pct(*overall[args.big]) - pct(*overall[args.small])) * 100)),
    }

    note = data.get("schema_note", "")
    if "model_tiers" not in note:
        data["schema_note"] = note + (
            " model_tiers / judge_recommendation / small_model_ok / measured："
            "由 apply_model_tiers.py 依据 bench 实测生成，供调用方按难度与实测表现选模型。")

    if args.dry_run:
        print("\n（--dry-run：没有写回）")
        return

    PUZZLE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    back = json.loads(PUZZLE_FILE.read_text(encoding="utf-8"))
    assert back.get("model_tiers"), "model_tiers 没写上"
    assert back.get("judge_recommendation"), "judge_recommendation 没写上"
    assert all("small_model_ok" in p for p in back["puzzles"]), "有题缺 small_model_ok"
    n_ok = sum(1 for p in back["puzzles"] if p["small_model_ok"])
    print(f"\n[OK] 已写回 {len(back['puzzles'])} 题；小模型达标 {n_ok}/{len(back['puzzles'])}")
    print("     model_tiers: " + "、".join(
        f"{k}→{v['model']}" for k, v in back["model_tiers"].items()))
    print(f"     建议大模型的题：{flagged}")


main()
