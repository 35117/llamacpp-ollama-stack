"""海龟汤游戏服务的端到端自检。

自己拉起 server.py、逐项验接口、再收掉进程，退出码即结论。
用法： python turtle/test_server.py          （需要 11434 的底座已在跑）
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 11441  # 用不常见端口，避开正在手工调试的 11440
BASE = f"http://127.0.0.1:{PORT}"
UPSTREAM = "http://127.0.0.1:11434"

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""))
    return cond


def call(path, body=None, timeout=300, method=None):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method or ("POST" if body is not None else "GET"),
        headers={"Content-Type": "application/json"})
    try:
        with _opener.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw}


def wait_ready(proc, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return False
        try:
            with _opener.open(BASE + "/api/config", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    # 上游通不通
    try:
        with _opener.open(UPSTREAM + "/api/tags", timeout=5) as r:
            ok_up = r.status == 200
    except Exception:  # noqa: BLE001
        ok_up = False
    if not check("上游 11434 可用", ok_up, "先跑 scripts/service_ctl.py start"):
        sys.exit(1)

    log = ROOT.parent / "logs" / "turtle-selftest.log"
    log.parent.mkdir(exist_ok=True)
    with log.open("w", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py"), "--port", str(PORT)],
            cwd=str(ROOT.parent), stdout=lf, stderr=lf,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            if not check("游戏服务已就绪", wait_ready(proc), f"日志见 {log}"):
                return

            # 先热身一次，把模型拉进显存——否则冷启动的十几秒会被误判成「太慢」
            call("/api/ask", {"puzzle_id": "hiccup", "question": "热身"}, timeout=600)
            print("  --- 接口（模型已热身）---")

            st, cfg = call("/api/config")
            check("GET /api/config", st == 200 and "model" in cfg and "puzzles" in cfg,
                  f"{cfg.get('model')} × {cfg.get('mode')}")
            mode_labels = cfg.get("mode_labels") or []

            st, pl = call("/api/puzzles")
            check("GET /api/puzzles 返回难度与推荐模型",
                  st == 200 and pl.get("count", 0) >= 30
                  and all(p.get("difficulty") in ("easy", "medium", "hard")
                          for p in pl.get("puzzles", []))
                  and all(p.get("recommended_model") for p in pl.get("puzzles", [])),
                  f"{pl.get('count')} 题 {pl.get('difficulty_counts')}")
            check("GET /api/config 带 model_desc / model_tiers",
                  bool(cfg.get("model_desc")) and isinstance(cfg.get("model_tiers"), dict),
                  f"{cfg.get('model_desc')}")

            st, ml = call("/api/models")
            check("GET /api/models 返回候选模型与角色设置",
                  st == 200 and len(ml.get("models") or []) >= 2
                  and {"judge", "hint", "grade"} <= set(ml.get("roles") or {}),
                  f"{len(ml.get('models') or [])} 个候选 {ml.get('roles')}")
            check("候选模型带评测说明（界面上要显示给人看）",
                  all(m.get("note") for m in (ml.get("models") or [])),
                  "、".join(m["id"] for m in (ml.get("models") or [])))

            st, se = call("/api/settings", {"judge_model": "qwen35-4b-heretic"})
            check("POST /api/settings 切换判题模型", st == 200
                  and (se.get("effective") or {}).get("judge") == "qwen35-4b-heretic",
                  str(se.get("effective")))
            st, _ = call("/api/settings", {"judge_model": "no-such-model"})
            check("POST /api/settings 拒绝未知模型", st == 400)
            call("/api/settings", {"judge_model": "auto"})      # 还原，免得影响后面的用例
            st, _ = call("/api/settings", {})
            check("POST /api/settings 空请求返回 400", st == 400)

            st, new = call("/api/new", {"puzzle_id": "hiccup"})
            check("POST /api/new 返回汤面", st == 200 and bool(new.get("surface")))
            check("POST /api/new 带难度与判题模型",
                  new.get("difficulty") == "easy" and bool(new.get("model")),
                  f"{new.get('difficulty')} / {new.get('model')}")
            check("POST /api/new 不泄露汤底（关键）",
                  "bottom" not in new and "汤底" not in json.dumps(new, ensure_ascii=False))

            tt = time.time()
            st, ask = call("/api/ask", {"puzzle_id": "hiccup",
                                        "question": "男人是因为口渴才要水的吗？"})
            check("POST /api/ask 判题成功", st == 200 and ask.get("label") in mode_labels,
                  f"label={ask.get('label')} 期望=否 耗时={ask.get('ms')}ms")
            check("POST /api/ask 结果正确", ask.get("label") == "否",
                  f"实得 {ask.get('label')}")
            check("POST /api/ask 热态 5s 内返回", time.time() - tt < 5,
                  f"{(time.time() - tt) * 1000:.0f}ms")

            st, bad = call("/api/ask", {"puzzle_id": "hiccup", "question": "   "})
            check("POST /api/ask 拒绝空提问", st == 400)

            # 提示功能：不泄露汤底是硬要求，次数上限要生效
            puzzle_data = json.loads(
                (ROOT / "puzzles" / "classic.json").read_text(encoding="utf-8"))
            leak_words = next(p for p in puzzle_data["puzzles"]
                              if p["id"] == "hiccup").get("leak_words", [])
            asked = ["男人是因为口渴才要水的吗？", "酒保是想伤害这个男人吗？"]
            st, h1 = call("/api/hint", {"puzzle_id": "hiccup", "asked": asked})
            hit = [w for w in leak_words if w in (h1.get("text") or "")]
            check("POST /api/hint 给出提示", st == 200 and bool(h1.get("text")),
                  f"tries={h1.get('tries')} fallback={h1.get('fallback')} "
                  f"{h1.get('ms')}ms")
            check("POST /api/hint 提示不泄露汤底（关键）", not hit,
                  f"命中泄露词 {hit}" if hit else (h1.get("text") or "")[:44])
            st, h2 = call("/api/hint", {"puzzle_id": "hiccup", "asked": asked})
            check("POST /api/hint 计次 2/3", st == 200 and h2.get("used") == 2)
            st, h3 = call("/api/hint", {"puzzle_id": "hiccup", "asked": asked})
            check("POST /api/hint 计次 3/3", st == 200 and h3.get("used") == 3)
            st, h4 = call("/api/hint", {"puzzle_id": "hiccup", "asked": asked})
            check("POST /api/hint 超出上限返回 429", st == 429)

            # 冷启动保护：玩家一个都没问时不该调模型，直接给起步引导。
            # 实测这条路径风险最高 —— 模型没有「玩家探索到哪」可依据，会直接
            # 围着谜底核心发问（「妹妹制造第二次葬礼是为了让那个男人再次出现吗？」）
            st, h0 = call("/api/hint", {"puzzle_id": "match", "asked": []})
            check("POST /api/hint 未提问时给起步引导（不调模型）",
                  st == 200 and h0.get("reason") == "no_questions_yet",
                  f"reason={h0.get('reason')} tries={h0.get('tries')}")
            match_lw = next(p for p in puzzle_data["puzzles"]
                            if p["id"] == "match").get("leak_words", [])
            hit0 = [w for w in match_lw if w in (h0.get("text") or "")]
            check("起步引导不含泄露词", not hit0,
                  f"命中 {hit0}" if hit0 else (h0.get("text") or "")[:32])

            st, rev = call("/api/reveal", {"puzzle_id": "hiccup"})
            check("POST /api/reveal 返回汤底", st == 200 and bool(rev.get("bottom")))

            st, gs = call("/api/guess", {"puzzle_id": "hiccup",
                                         "guess": "他打嗝，酒保用枪吓他把嗝吓停了"})
            check("POST /api/guess 给出百分比",
                  st == 200 and isinstance(gs.get("score"), int),
                  f"score={gs.get('score')}% {gs.get('level')} "
                  f"{gs.get('hit')}/{gs.get('total')}")

            try:
                with _opener.open(BASE + "/", timeout=5) as r:
                    html = r.read().decode("utf-8")
                check("GET / 返回页面", r.status == 200 and "海龟汤" in html)
            except Exception as exc:  # noqa: BLE001
                check("GET / 返回页面", False, str(exc))

            st, _ = call("/api/nope", {})
            check("未知路由返回 404", st == 404)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            print("  [已停止游戏服务]")

    print()
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
