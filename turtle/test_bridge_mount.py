"""验证海龟汤挂在桥上的路由（/turtle）以及原有路由未被破坏。

前提：底座已在跑（scripts\\service_ctl.py start）。
用法：python turtle\\test_bridge_mount.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:11434"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""))
    return cond


def req(path, body=None, method=None, timeout=300):
    """返回 (status, payload, ctype, body_bytes)。

    urllib 会原样发送 path、不会自行折叠 `../` —— 正好用来验证服务端防护。
    """
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(
        url, data=data, method=method or ("POST" if body is not None else "GET"),
        headers={"Content-Type": "application/json"})
    try:
        with _opener.open(r, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:  # noqa: BLE001
                payload = {}
            return resp.status, payload, ctype, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:  # noqa: BLE001
            payload = {}
        return exc.code, payload, exc.headers.get("Content-Type", ""), raw


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    st, _, _, _ = req("/api/tags", timeout=10)
    if st != 200:
        print("  [X] 桥没在跑（%s）。先执行 scripts\\service_ctl.py start" % st)
        sys.exit(1)

    print("  --- 新增：挂在桥上的海龟汤 ---")
    st, _, ctype, raw = req("/turtle")
    check("GET /turtle 返回页面",
          st == 200 and "text/html" in ctype and "海龟汤" in raw.decode("utf-8", "replace"),
          f"{st} {len(raw)}B")

    st, cfg, _, _ = req("/turtle/api/config")
    check("GET /turtle/api/config", st == 200 and "puzzles" in cfg,
          f"{cfg.get('model_desc') or cfg.get('model')} × {cfg.get('mode')}")

    # 难度标签 → 模型分级：供机器人插件按难度挑模型
    st, pl, _, _ = req("/turtle/api/puzzles")
    check("GET /turtle/api/puzzles 返回难度与推荐模型",
          st == 200 and pl.get("count", 0) >= 30
          and all(p.get("difficulty") in ("easy", "medium", "hard")
                  for p in pl.get("puzzles", []))
          and all(p.get("recommended_model") for p in pl.get("puzzles", [])),
          f"{pl.get('count')} 题 {pl.get('difficulty_counts')}")
    check("puzzles 带 small_model_ok 与 model_tiers",
          isinstance(pl.get("model_tiers"), dict) and bool(pl.get("model_tiers"))
          and all("small_model_ok" in p for p in pl.get("puzzles", [])),
          "tiers=" + "、".join(f"{k}→{v.get('model') if isinstance(v, dict) else v}"
                              for k, v in (pl.get("model_tiers") or {}).items()))

    st, newp, _, _ = req("/turtle/api/new", {"puzzle_id": "cow"})
    check("POST /turtle/api/new 带难度与判题模型",
          st == 200 and newp.get("difficulty") == "hard" and bool(newp.get("model")),
          f"{newp.get('difficulty')} / {newp.get('model')}")

    # 手动切换模型：三个角色（判题/提示/打分）可分别指定
    st, ml, _, _ = req("/turtle/api/models")
    check("GET /turtle/api/models 返回候选模型与角色设置",
          st == 200 and len(ml.get("models") or []) >= 2
          and {"judge", "hint", "grade"} <= set(ml.get("roles") or {}),
          f"{len(ml.get('models') or [])} 个候选 {ml.get('roles')}")
    st, se, _, _ = req("/turtle/api/settings", {"judge_model": "qwen35-9b-heretic",
                                                "hint_model": "auto"})
    check("POST /turtle/api/settings 切换模型",
          st == 200 and (se.get("effective") or {}).get("judge") == "qwen35-9b-heretic",
          str(se.get("effective")))
    st, _, _, _ = req("/turtle/api/settings", {"judge_model": "no-such-model"})
    check("POST /turtle/api/settings 拒绝未知模型", st == 400)
    req("/turtle/api/settings", {"judge_model": "auto"})     # 还原

    st, ask, _, _ = req("/turtle/api/ask",
                        {"puzzle_id": "hiccup", "question": "男人是因为口渴才要水的吗？"})
    check("POST /turtle/api/ask 判题正确", st == 200 and ask.get("label") == "否",
          f"label={ask.get('label')} {ask.get('ms')}ms")

    st, bad, _, _ = req("/turtle/api/ask", {"puzzle_id": "hiccup", "question": "  "})
    check("POST /turtle/api/ask 拒绝空提问", st == 400)

    st, hint, _, _ = req("/turtle/api/hint",
                         {"puzzle_id": "match", "asked": []})
    check("POST /turtle/api/hint 未提问时给起步引导",
          st == 200 and hint.get("reason") == "no_questions_yet")

    # 猜测打分：只给分数与条数；**绝不能**在猜完时返回检查点内容（= 念答案）
    st, g_part, _, _ = req("/turtle/api/guess", {
        "puzzle_id": "funeral",
        "guess": "妹妹因为想让那个男人再出现，所以杀了姐姐。"})
    check("POST /turtle/api/guess 给出百分比",
          st == 200 and isinstance(g_part.get("score"), int)
          and g_part.get("graded") is True,
          f"score={g_part.get('score')}% {g_part.get('level')} "
          f"{g_part.get('hit')}/{g_part.get('total')}")
    leak_keys = [k for k in ("hit_list", "miss_list", "details") if k in g_part]
    check("guess 不泄露检查点内容（关键）", not leak_keys,
          f"泄露了字段 {leak_keys}" if leak_keys
          else f"只给 {g_part.get('hit')}/{g_part.get('total')}，未给内容")

    st, g_full, _, _ = req("/turtle/api/guess", {
        "puzzle_id": "funeral",
        "guess": "妹妹觉得那个男人是葬礼上的宾客，想再办一场葬礼让他出现，所以杀了姐姐。"})
    st, g_none, _, _ = req("/turtle/api/guess", {
        "puzzle_id": "funeral", "guess": "妹妹想继承姐姐的遗产。"})
    check("完整猜测得分 > 部分猜测 > 无关猜测",
          (g_full.get("score") or 0) > (g_part.get("score") or 0)
          > (g_none.get("score") or 0),
          f"{g_full.get('score')}% > {g_part.get('score')}% > {g_none.get('score')}%")

    st, _, _, _ = req("/turtle/api/guess", {"puzzle_id": "funeral", "guess": "   "})
    check("guess 拒绝空猜测", st == 400)

    st, rev2, _, _ = req("/turtle/api/reveal", {"puzzle_id": "funeral"})
    check("reveal 才公布真相要点",
          st == 200 and len(rev2.get("checkpoints") or []) >= 3,
          f"{len(rev2.get('checkpoints') or [])} 条要点")

    st, rev, _, _ = req("/turtle/api/reveal", {"puzzle_id": "hiccup"})
    check("POST /turtle/api/reveal 返回汤底", st == 200 and bool(rev.get("bottom")))

    st, _, _, _ = req("/turtle/api/nope", {})
    check("未知 /turtle/api 路由返回 404", st == 404)

    print("  --- 路径穿越防护 ---")
    st, _, _, _ = req("/turtle/../../config/personas.json", timeout=10)
    check("../ 穿越被拒", st in (400, 403, 404), f"HTTP {st}")
    st, _, _, _ = req("/turtle/%2e%2e/%2e%2e/config/personas.json", timeout=10)
    check("%2e%2e 穿越被拒", st in (400, 403, 404), f"HTTP {st}")

    print("  --- 回归：原有路由不能坏 ---")
    for path, note in (("/ui", "聊天界面"), ("/api/tags", "Ollama 协议"),
                       ("/v1/models", "OpenAI 协议"),
                       ("/admin/state", "界面后端")):
        st, _, _, raw = req(path, timeout=15)
        check(f"GET {path}（{note}）", st == 200, f"{st} {len(raw)}B")

    st, _, _, _ = req("/api/version", timeout=10)
    check("GET /api/version", st == 200)

    print()
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        sys.exit(1)
    print("全部通过")


main()
