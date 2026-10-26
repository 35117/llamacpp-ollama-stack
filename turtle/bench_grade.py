"""对比各模型「按检查点给玩家猜测打分」的能力。

打分是**多次有界决策**（每条检查点问一次「玩家的猜测里有没有体现这条？」），
分数 = 命中率 —— 所以它也可以单独选模型。

判据（分数必须是**算出来的**，所以好坏看三件事）：

  MAE      —— 与人工期望分的平均偏差（越小越好）
  单调性   —— 同一题内，越接近真相的猜测得分必须越高；出现倒挂即违规
  满分准确 —— 完整说中时是否稳定给出 100%

用例是手工写的（每题 4 档：完整 / 部分 / 一点 / 无关），
每档标注「人工判断它体现了哪几条检查点」，期望分由此算出。

用法：
    python turtle\\bench_grade.py --models qwen35-4b-heretic,qwen35-9b-heretic
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from judge import DEFAULT_BASE, JudgeError, grade_guess  # noqa: E402

PUZZLE_FILE = ROOT / "puzzles" / "classic.json"
RESULTS = ROOT / "results"

# (题目, 猜测, 人工判断它体现了哪几条检查点)
CASES = [
    # ---------------- funeral（4 条检查点） ----------------
    ("funeral", "妹妹觉得那个男人是来参加葬礼的宾客，她想如果再办一场葬礼、那个男人也许还会出现，"
                "于是就杀死了姐姐。", [0, 1, 2, 3]),
    ("funeral", "妹妹杀死了姐姐，因为她想再见到那个男人。", [0, 1]),
    ("funeral", "妹妹因为恨姐姐，所以把姐姐杀了。", [0]),
    ("funeral", "妹妹想继承姐姐的遗产。", []),
    # ---------------- hiccup（4 条） ----------------
    ("hiccup", "他打嗝停不下来，进酒吧要水是为了止嗝。酒保看出他在打嗝，"
               "就掏出一把没装子弹的枪吓他，把嗝吓停了。", [0, 1, 2, 3]),
    ("hiccup", "他在打嗝，要水是想止嗝，酒保掏枪吓了他一下。", [0, 1, 2]),
    ("hiccup", "他在酒吧里打嗝打得很难受。", [0]),
    ("hiccup", "他口渴了，进酒吧要了一杯水喝。", []),
    # ---------------- cow（4 条） ----------------
    ("cow", "邻居夫妻起了争执，丈夫砍断了妻子的四肢。妻子咬着地上的草一点点爬到小美家门口，"
            "用头撞门求救，可小美没开门，她最后在门外流尽了血。", [0, 1, 2, 3]),
    ("cow", "邻居丈夫砍断了妻子的四肢，妻子爬到了小美家门口。", [0, 1]),
    ("cow", "邻居夫妻那一晚吵了架。", [0]),
    ("cow", "小美做了一个关于牛的噩梦。", []),
    # ---------------- tunnel（4 条） ----------------
    ("tunnel", "他本来是盲人，这趟去做复明手术并且成功了。回程火车开进隧道、四周一下子变黑，"
               "他以为自己又瞎了，绝望之下跳了车。", [0, 1, 2, 3]),
    ("tunnel", "他原本是盲人，做了手术之后重新看见了。", [0, 1]),
    ("tunnel", "他是个盲人。", [0]),
    ("tunnel", "他很害怕坐火车，所以中途跳了车。", []),
    # ---------------- lamp（3 条） ----------------
    ("lamp", "他是盲人，这盏灯对他自己根本没用。那条街夜里没有照明，"
             "他提灯是为了让别人远远就能看见他、不至于撞上他。", [0, 1, 2]),
    ("lamp", "他是盲人，提灯是为了让别人看见他。", [0, 1]),
    ("lamp", "他是个盲人。", [0]),
    ("lamp", "他提灯是为了照亮脚下的路，好让自己看清。", []),
    # ---------------- turtlesoup（4 条） ----------------
    ("turtlesoup", "多年前他和好友出海遇难、断了食物，好友端来一碗「海龟汤」救了他，"
                   "好友后来在获救前就死了——那碗汤其实是好友用自己的肉做的，不是海龟。",
     [0, 1, 2, 3]),
    ("turtlesoup", "他和好友出海遇难断了食物，好友端来一碗汤救了他。", [0, 1]),
    ("turtlesoup", "他和好友一起出海遇难过。", [0]),
    ("turtlesoup", "他不喜欢海龟汤的味道。", []),
    # ---------------- seagrass（4 条） ----------------
    ("seagrass", "翻船后他想去救不会游泳的女友，女友说腿被湖底的水草死死缠住、让他先上岸；"
                 "他信了独自上岸，女友死了。其实那个湖底根本长不出水草——女友是骗他，"
                 "好让他放弃救援、自己活下去。", [0, 1, 2, 3]),
    ("seagrass", "翻船后他想救女友，女友却让他别管自己、先游上岸。", [0, 1]),
    ("seagrass", "他和女友在湖上划船时翻了船。", [0]),
    ("seagrass", "他因为钓不到鱼而难过。", []),
    # ---------------- windowcleaner（3 条） ----------------
    ("windowcleaner", "他擦的是大楼内侧中庭那一面的玻璃，站在楼内的走廊里作业，"
                      "所以安全绳断了也不会让他掉下去，只是工具滑了一下。", [0, 1, 2]),
    ("windowcleaner", "他当时在大楼内部作业，安全绳断了也不会掉下去。", [1, 2]),
    ("windowcleaner", "他站在大楼内部的走廊里。", [1]),
    ("windowcleaner", "他因为害怕高空作业所以辞了职。", []),
    # ---------------- snowtracks（3 条） ----------------
    ("snowtracks", "雪是后半夜才停的，所以这行脚印是今夜留下的；脚印从树林走来，"
                   "在他屋子外侧就断了、没有折返——说明留下脚印的人整夜都守在他屋子旁边。",
     [0, 1, 2]),
    ("snowtracks", "脚印在他屋子外侧就断了，那个人整夜都守在他屋子旁边。", [1, 2]),
    ("snowtracks", "雪地上那行脚印是今夜留下的。", [0]),
    ("snowtracks", "他是因为害怕山里的野兽才逃走的。", []),
]


def load_puzzles(path):
    return {p["id"]: p for p in
            json.loads(Path(path).read_text(encoding="utf-8"))["puzzles"]}


def run_model(model, by_id, base, timeout=300):
    rows = []
    for pid, guess, expect in CASES:
        pz = by_id[pid]
        cps = pz.get("checkpoints") or []
        n = len(cps)
        want = round(len(expect) / n * 100) if n else 0
        rec = {"puzzle": pid, "guess": guess, "expect": want, "n_cp": n}
        try:
            t0 = time.time()
            res = grade_guess(pz["surface"], pz["bottom"], guess, cps,
                              model=model, base=base, timeout=timeout)
            rec["score"] = res["score"]
            rec["hit"] = res["hit"]
            rec["hit_list"] = res["hit_list"]
            rec["wrong_hits"] = sorted(set(
                i for i, c in enumerate(cps) if c in res["hit_list"]) - set(expect))
            rec["missed"] = sorted(set(expect) - set(
                i for i, c in enumerate(cps) if c in res["hit_list"]))
            rec["ms"] = int((time.time() - t0) * 1000)
        except JudgeError as exc:
            rec["error"] = str(exc)
        rows.append(rec)
    return rows


def summarize(model, rows):
    ok = [r for r in rows if "error" not in r]
    mae = sum(abs(r["score"] - r["expect"]) for r in ok) / len(ok) if ok else None
    # 单调性：同一题内，按用例顺序（完整→无关）分数应严格递减
    viol = []
    by_pz = {}
    for r in ok:
        by_pz.setdefault(r["puzzle"], []).append(r)
    for pid, rs in by_pz.items():
        seq = [r["score"] for r in rs]
        if any(seq[i] <= seq[i + 1] for i in range(len(seq) - 1)):
            viol.append(f"{pid}:{seq}")
    full = [r for r in ok if r["expect"] == 100]
    return {
        "model": model, "n": len(rows), "errors": len(rows) - len(ok),
        "mae": mae,
        "mono_violations": viol,
        "full_ok": sum(1 for r in full if r["score"] == 100), "full_n": len(full),
        "ms": (sum(r["ms"] for r in ok) / len(ok)) if ok else None,
    }


def render(summaries, rows_by_model, meta):
    L = ["# 猜测打分（检查点法）能力对比", "",
         f"- 生成时间：{meta['time']}",
         f"- 用例：{meta['n']} 条（9 题 × 4 档：完整 / 部分 / 一点 / 无关）",
         "",
         "## 总览", "",
         "| 模型 | MAE（越小越好） | 满分准确 | 单调性违规 | 平均耗时 |",
         "|---|---|---|---|---|"]
    for s in summaries:
        mono = "无" if not s["mono_violations"] else "；".join(s["mono_violations"])
        L.append(f"| {s['model']} | **{s['mae']:.1f} 分** | "
                 f"{s['full_ok']}/{s['full_n']} | {mono} | {s['ms']:.0f}ms |")
    L += ["", "> MAE = 与人工期望分的平均绝对偏差。",
          "> 单调性违规 = 同一题内出现「越不像真相的猜测得分反而越高」。", ""]
    for s in summaries:
        L += [f"## {s['model']}", "",
              "| 题目 | 期望 | 实得 | 命中 | 多判 | 漏判 | 猜测 |",
              "|---|---|---|---|---|---|---|"]
        for r in rows_by_model[s["model"]]:
            if "error" in r:
                L.append(f"| {r['puzzle']} | {r['expect']} | 失败 | | | | {r['error'][:30]} |")
                continue
            L.append(f"| {r['puzzle']} | {r['expect']} | **{r['score']}** | {r['hit']}/{r['n_cp']} "
                     f"| {r['wrong_hits'] or ''} | {r['missed'] or ''} "
                     f"| {r['guess'][:34]} |")
        L.append("")
    return "\n".join(L)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="猜测打分能力对比")
    ap.add_argument("--models", default="qwen35-4b-heretic,qwen35-9b-heretic")
    ap.add_argument("--puzzles", default=str(PUZZLE_FILE))
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--out-dir", default=str(RESULTS))
    args = ap.parse_args()

    by_id = load_puzzles(args.puzzles)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    print(f"用例 {len(CASES)} 条 × 模型 {len(models)} 个\n")

    summaries, rows_by_model = [], {}
    for m in models:
        print(f"### {m}")
        t0 = time.time()
        rows = run_model(m, by_id, args.base)
        s = summarize(m, rows)
        summaries.append(s)
        rows_by_model[m] = rows
        for r in rows:
            if "error" in r:
                print(f"  {r['puzzle']:9s} 失败 {r['error'][:40]}")
            else:
                print(f"  {r['puzzle']:9s} 期望{r['expect']:3d} 实得{r['score']:3d} "
                      f"({r['hit']}/{r['n_cp']}) 多判{r['wrong_hits']} 漏判{r['missed']}")
        print(f"  -> MAE {s['mae']:.1f}，满分 {s['full_ok']}/{s['full_n']}，"
              f"单调违规 {s['mono_violations'] or '无'}，耗时 {time.time()-t0:.0f}s\n")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    meta = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "n": len(CASES)}
    report = render(summaries, rows_by_model, meta)
    (out_dir / f"grade-{stamp}.md").write_text(report, encoding="utf-8")
    (out_dir / "grade-latest.md").write_text(report, encoding="utf-8")
    (out_dir / f"grade-{stamp}.json").write_text(
        json.dumps({"meta": meta, "summaries": summaries, "rows": rows_by_model},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 62)
    for s in summaries:
        print(f"{s['model']:32s} MAE {s['mae']:5.1f}  满分 {s['full_ok']}/{s['full_n']}")
    print(f"\n报告：turtle/results/grade-{stamp}.md")


main()
