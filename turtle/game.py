"""海龟汤的业务逻辑（与 HTTP 无关）。

被两条路共用，避免两份实现漂移：
  - `turtle/server.py`      独立运行  → http://127.0.0.1:11440
  - `scripts/turtle_web.py` 挂在桥上  → http://127.0.0.1:11434/turtle

所有方法统一返回 `(status, payload_dict)`，由调用方决定怎么发出去。

**三个角色可以分别指定模型**（判题 / 提示 / 打分），见 `resolve()`：
  判题 judge —— 每问一句都要用，是热路径
  提示 hint  —— 生成类任务（没有 grammar 兜底），最容易出事
  打分 grade —— 玩家报答案时才用
默认值来自 `turtle/models.json` 的 `role_defaults`，玩家可以在界面上改。
"""
from __future__ import annotations

import json
import random
import threading
from collections import Counter
from pathlib import Path

from judge import (  # noqa: E402
    DEFAULT_BASE, DEFAULT_HINT_MODEL, MODES, JudgeError,
    grade_guess, judge, make_hint,
)

HERE = Path(__file__).resolve().parent
DEFAULT_PUZZLE_FILE = HERE / "puzzles" / "classic.json"
MODELS_FILE = HERE / "models.json"
MAX_HINTS = 3
MAX_ASKED = 8          # 提示只参考最近 8 个提问

# 没用难度分级时的兜底判题模型（题库 model_tiers 会覆盖它）
DEFAULT_JUDGE_MODEL = "qwen35-4b-heretic"

ROLES = ("judge", "hint", "grade")
ROLE_LABELS = {"judge": "判题", "hint": "提示", "grade": "打分"}
AUTO = "auto"


class Game:
    """一局的会话状态。本地单人玩，进程内共享一份即可。"""

    def __init__(self, model=None, hint_model=None, grade_model=None,
                 mode="binary", base=DEFAULT_BASE, max_hints=MAX_HINTS,
                 puzzle_file=None, models_file=None, fallback_model=None):
        if mode not in MODES:
            raise ValueError(f"未知 mode={mode}，可选：{list(MODES)}")
        self.model = model                    # 构造时显式指定 = 强制判题模型
        self.fallback_model = fallback_model or DEFAULT_JUDGE_MODEL
        self.mode = mode
        self.base = base
        self.max_hints = max_hints
        self.puzzle_file = str(puzzle_file or DEFAULT_PUZZLE_FILE)
        self.models_file = str(models_file or MODELS_FILE)

        self.puzzles, self.model_tiers = self._load()
        self.by_id = {p["id"]: p for p in self.puzzles}

        self.model_catalog = self._load_models()
        self.candidates = [m["id"] for m in self.model_catalog.get("models", [])]
        self.auto_meta = self.model_catalog.get("auto") or {}
        self.role_defaults = {r: (self.model_catalog.get("role_defaults") or {}).get(r)
                              or AUTO for r in ROLES}
        # 玩家在界面上的选择（可被 /api/settings 改）
        self.settings = dict(self.role_defaults)

        # 兼容旧的 hint_model 参数：给了就当作提示与打分的默认
        if hint_model:
            self.role_defaults["hint"] = hint_model
            self.settings["hint"] = hint_model
            if grade_model is None:
                self.role_defaults["grade"] = hint_model
                self.settings["grade"] = hint_model
        if grade_model:
            self.role_defaults["grade"] = grade_model
            self.settings["grade"] = grade_model

        self._lock = threading.Lock()
        self._hint_counts = {}

    # ------------------------------------------------------------ 加载
    def _load(self):
        data = json.loads(Path(self.puzzle_file).read_text(encoding="utf-8"))
        puzzles = data["puzzles"]
        if not puzzles:
            raise ValueError(f"题库是空的：{self.puzzle_file}")
        return puzzles, (data.get("model_tiers") or {})

    def _load_models(self):
        try:
            return json.loads(Path(self.models_file).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            # 清单缺失不该让游戏起不来：退化成「不限制候选模型」
            return {"models": [], "auto": {}, "role_defaults": {}}

    # ------------------------------------------------- 角色 → 模型
    def model_for(self, pz):
        """这道题该用哪个模型判题（题库 model_tiers 按难度给）。"""
        if self.model:
            return self.model
        tier = (self.model_tiers or {}).get(pz.get("difficulty"))
        if isinstance(tier, dict) and tier.get("model"):
            return tier["model"]
        if isinstance(tier, str):
            return tier
        return self.fallback_model

    def resolve(self, role, pz=None, override=None):
        """确定某个角色这次用哪个模型。

        优先级：请求里显式传的 override > 玩家在界面上的设置 > 角色默认。
        `auto` 的含义随角色而变：判题=按题目难度取题库 model_tiers；
        提示/打分=各自的角色默认模型。
        """
        want = (override or "").strip() or self.settings.get(role) or AUTO
        if want != AUTO:
            return want
        if role == "judge":
            return self.model_for(pz) if pz is not None else \
                (self.model or self.fallback_model)
        return self.role_defaults.get(role) or DEFAULT_HINT_MODEL

    def _pick_model(self, role, pz=None, override=None):
        """解析 + 校验；返回 (模型, 错误信息)。"""
        m = self.resolve(role, pz, override)
        if self.candidates and m not in self.candidates:
            return None, (f"未知模型 {m}；可用：{'、'.join(self.candidates)}")
        return m, None

    def puzzle_meta(self, pz):
        """对外可读的题目元信息（含难度与推荐模型）。"""
        return {
            "id": pz["id"],
            "title": pz["title"],
            "difficulty": pz.get("difficulty"),
            "recommended_model": self.model_for(pz),
            "small_model_ok": pz.get("small_model_ok"),
            "probe_count": len(pz.get("probes") or []),
            "checkpoint_count": len(pz.get("checkpoints") or []),
        }

    def candidate_meta(self):
        return [{k: m.get(k) for k in
                 ("id", "label", "size_gb", "vision", "note")}
                for m in self.model_catalog.get("models", [])]

    def _pick(self, pid):
        """按 id 取题；没给或给错就随机取一题。"""
        if pid and pid in self.by_id:
            return self.by_id[pid]
        return random.choice(self.puzzles)

    # ------------------------------------------------------------ 业务
    def config(self, _body=None):
        roles = {r: self.resolve(r, self.puzzles[0]) for r in ROLES}
        return 200, {
            "model": self.model or self.fallback_model,
            "model_desc": self._describe(roles),
            "auto_model": self.settings.get("judge", AUTO) == AUTO,
            "model_tiers": self.model_tiers or {},
            "roles": dict(self.settings),
            "effective": roles,
            "role_defaults": dict(self.role_defaults),
            "role_labels": ROLE_LABELS,
            "models": self.candidate_meta(),
            "auto": self.auto_meta,
            "mode": self.mode,
            "mode_labels": list(MODES[self.mode]["labels"]),
            "max_hints": self.max_hints,
            "puzzles": [self.puzzle_meta(p) for p in self.puzzles],
        }

    def _describe(self, roles):
        names = {m["id"]: m.get("label") or m["id"]
                 for m in self.model_catalog.get("models", [])}
        if all(roles[r] == roles["judge"] for r in ROLES):
            short = names.get(roles["judge"], roles["judge"])
            return short + ("（按难度自动）" if self.settings.get("judge") == AUTO else "")
        return "、".join(f"{ROLE_LABELS[r]} {names.get(roles[r], roles[r])}"
                         for r in ROLES)

    def models_list(self, _body=None):
        """可选模型清单 + 当前各角色用谁。

        `auto` 是特殊值：判题时表示「按题目难度取题库 model_tiers」。
        """
        eff = {r: self.resolve(r, self.puzzles[0]) for r in ROLES}
        return 200, {
            "roles": dict(self.settings),
            "effective": eff,
            "role_defaults": dict(self.role_defaults),
            "role_labels": ROLE_LABELS,
            "auto": self.auto_meta,
            "models": self.candidate_meta(),
            "model_tiers": self.model_tiers or {},
        }

    def set_settings(self, body):
        """手动切换模型。body 形如 {"judge_model": "...", "hint_model": "auto"}。

        三个角色都可以单独设成某个模型 id，或 `auto`（回落到默认策略）。
        """
        changed = {}
        for role in ROLES:
            key = f"{role}_model"
            if key not in (body or {}):
                continue
            val = (body.get(key) or AUTO).strip() or AUTO
            if val != AUTO and self.candidates and val not in self.candidates:
                return 400, {"error": f"未知模型 {val}；可用：{'、'.join(self.candidates)}"}
            self.settings[role] = val
            changed[role] = val
        if not changed:
            return 400, {"error": f"至少要给一个 {ROLES} 中的字段，例如 judge_model"}
        eff = {r: self.resolve(r, self.puzzles[0]) for r in ROLES}
        return 200, {"changed": changed, "roles": dict(self.settings),
                     "effective": eff,
                     "note": "judge 为 auto 时按题目难度取题库 model_tiers"}

    def puzzle_list(self, _body=None):
        """题库清单（含难度和推荐模型）。

        给外部调用方用（例如机器人插件）：读 `difficulty` 决定用哪个模型，
        或直接把 `recommended_model` 传回来。
        """
        dist = Counter(p.get("difficulty") or "?" for p in self.puzzles)
        return 200, {
            "count": len(self.puzzles),
            "difficulty_order": ["easy", "medium", "hard"],
            "difficulty_counts": dict(dist),
            "auto_model": self.settings.get("judge", AUTO) == AUTO,
            "model_tiers": self.model_tiers or {},
            "puzzles": [self.puzzle_meta(p) for p in self.puzzles],
        }

    def new(self, body):
        pz = self._pick(body.get("puzzle_id"))
        with self._lock:
            self._hint_counts.clear()      # 换一题 = 提示次数重置
        return 200, {
            "id": pz["id"],
            "title": pz["title"],
            "difficulty": pz.get("difficulty"),
            "model": self._pick_model("judge", pz, body.get("model"))[0],
            "surface": pz["surface"],
            "probe_count": len(pz.get("probes") or []),
            "max_hints": self.max_hints,
        }

    def ask(self, body):
        question = (body.get("question") or "").strip()
        if not question:
            return 400, {"error": "提问不能为空"}
        pz = self._pick(body.get("puzzle_id"))
        model, err = self._pick_model("judge", pz, body.get("model"))
        if err:
            return 400, {"error": err}
        res = judge(pz["surface"], pz["bottom"], question,
                    model=model, mode=self.mode, base=self.base)
        return 200, {"puzzle_id": pz["id"], "question": question, **res}

    def guess(self, body):
        """给玩家的完整猜测打分。

        有 checkpoints 的题走「逐条二值判断 → 算命中率」，能给出百分比；
        没配检查点的题退回原来的「一致/不一致」二值判定。

        ⚠️ **只对外给分数和条数，不给检查点内容** —— 检查点就是汤底拆解，
        猜完就展示等于把答案念给玩家听（体感极差）。要点留到揭晓时公布。
        """
        guess = (body.get("guess") or "").strip()
        if not guess:
            return 400, {"error": "猜测不能为空"}
        pz = self._pick(body.get("puzzle_id"))
        model, err = self._pick_model("grade", pz, body.get("model"))
        if err:
            return 400, {"error": err}
        res = grade_guess(pz["surface"], pz["bottom"], guess,
                          pz.get("checkpoints") or [],
                          model=model, base=self.base)
        # 检查点内容一律不外传：玩家猜一半，把剩下的念出来就是念答案。
        # 只给分数与条数；要看明细得等揭晓（reveal 会一并给出 checkpoints）。
        hidden = ("hit_list", "miss_list", "details")
        out = {k: v for k, v in res.items() if k not in hidden}
        return 200, {"puzzle_id": pz["id"], "guess": guess, "model": model, **out}

    def hint(self, body):
        pz = self._pick(body.get("puzzle_id"))
        pid = pz["id"]
        asked = [str(q) for q in (body.get("asked") or []) if str(q).strip()][-MAX_ASKED:]
        with self._lock:
            used = self._hint_counts.get(pid, 0)
            if used >= self.max_hints:
                return 429, {
                    "error": f"这道题的提示已经用完了（{used}/{self.max_hints}），再想想吧",
                    "used": used, "limit": self.max_hints,
                }
            self._hint_counts[pid] = used + 1
        model, err = self._pick_model("hint", pz, body.get("model"))
        if err:
            return 400, {"error": err}
        res = make_hint(pz["surface"], pz["bottom"], asked,
                        pz.get("leak_words") or [],
                        model=model, base=self.base)
        # 不回传 attempts：里面装的是被拦下的泄露原文
        out = {k: v for k, v in res.items() if k != "attempts"}
        return 200, {"puzzle_id": pid, "asked": asked, "model": model,
                     "used": used + 1, "limit": self.max_hints, **out}

    def reveal(self, body):
        """揭晓汤底 —— 这时候才公布「真相要点」（检查点）。"""
        pz = self._pick(body.get("puzzle_id"))
        return 200, {"id": pz["id"], "title": pz["title"],
                     "surface": pz["surface"], "bottom": pz["bottom"],
                     "checkpoints": pz.get("checkpoints") or []}

    def warmup(self, _body=None):
        """预热显存，免得玩家第一次提问白等冷加载。

        只预热**热路径**（判题）用的那个模型 —— 8G 卡上同时暖两个模型
        会互相把对方挤掉，等于白暖。其余角色若用别的模型，首次会懒加载
        （返回里会点名是哪几个）。
        """
        pz = self.puzzles[0]
        hot = self.resolve("judge", pz)
        try:
            judge(pz["surface"], pz["bottom"], "热身",
                  model=hot, mode=self.mode, base=self.base, timeout=600)
        except JudgeError as exc:
            return 502, {"ok": False, "error": str(exc)}
        lazy = []
        for role in ("hint", "grade"):
            m = self.resolve(role, pz)
            if m != hot and m not in lazy:
                lazy.append(m)
        return 200, {"ok": True, "model": hot, "lazy_models": lazy,
                     "note": "只预热判题模型（热路径）；lazy_models 里的第一次会慢一拍"}

    # ------------------------------------------------------------ 分发
    ROUTES = {
        ("GET", "config"): "config",
        ("GET", "puzzles"): "puzzle_list",
        ("GET", "models"): "models_list",
        ("POST", "settings"): "set_settings",
        ("POST", "new"): "new",
        ("POST", "ask"): "ask",
        ("POST", "guess"): "guess",
        ("POST", "hint"): "hint",
        ("POST", "reveal"): "reveal",
        ("POST", "warmup"): "warmup",
    }

    def dispatch(self, method, name, body):
        """按 (method, name) 找处理函数，返回 (status, payload)。"""
        attr = self.ROUTES.get((method, name))
        if not attr:
            return 404, {"error": f"no such route: {method} {name}"}
        try:
            return getattr(self, attr)(body or {})
        except JudgeError as exc:
            return 502, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return 500, {"error": f"服务端异常: {exc}"}
