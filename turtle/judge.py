"""海龟汤判题器 —— Jev 式的有界决策，但不用装 Jev。

原理：Jev 类模型值钱的那层壳就是「输出被限死在一组有界选项里 + 给出概率」。
llama.cpp 的 `grammar`(GBNF) 能原生做到同一件事，而我们本地已经有
Qwen 底座（和 Jev 派系同源），所以直接用它，省掉一整套新部署。

实测（build 11146，75 条标注探针）：
  - grammar 约束下单次判题约 0.16s（4B）/ 1s（8B），输出必然是给定标签之一
  - 决定准确率的是「提示词里的边界定义 + few-shot」，不是 grammar 本身
  - 「部分对」对 4B 是毒药：四分类 67% vs 二分类 87%
  - 8B（当年那批已删）在二元判断上更强：二分类 93.5% vs 4B 的 87.1%；
    但 2026-10-02 换成两个 9B 重测后，**三个模型的判题能力统计上不再有差异**
    （配对 McNemar p=0.69~1.00），于是默认仍是 4B —— 见下面 DEFAULT_* 的注释。
  - 但 8B 在三分类上没有优势（78.1% vs 4B 80.8%）——「否 vs 无关」的边界
    是任务定义问题，不是模型能力问题

三种模式（mode）：
  binary —— 是 / 否，最稳，推荐默认；「无关」并入「否」
  three  —— 是 / 否 / 无关，保留「这事汤里没提」这层信息，但准确率低一档
  four   —— 是 / 否 / 无关 / 部分对，信息最全但小模型易误判

对外接口：
    judge(surface, bottom, question, model=..., mode="binary") -> dict
    decide(system, user, labels, ...)                          -> dict  通用有界决策
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen35-4b-heretic"

# 出提示 / 给猜测打分用的模型。
# 2026-10-02 本机模型换代（8B 系列权重已删，换成两个 9B）后重测：
#   判题：4B 92.3% / 9B-heretic 93.2% / 9B-abliterated 93.2%
#         —— 两两配对 McNemar p=0.69~1.00，**统计上无差异**（见 turtle/results/）
#   打分：4B MAE 4.9（36 例）优于 9B-heretic 6.5
#   把关：4B 100% 准（20 条校准）/ 9B-heretic 95% / 9B-abliterated 75%（错杀一半）
# 结论：三个角色都用 4B —— 不输 9B，显存只占一半，热态还快 25%。
# 角色默认值集中在 `turtle/models.json`，玩家可以在界面上改。
DEFAULT_HINT_MODEL = "qwen35-4b-heretic"
DEFAULT_GRADE_MODEL = "qwen35-4b-heretic"

HEAD = "你是海龟汤游戏的主持人。玩家会针对一个谜题提出一个问题。"

# ---------------------------------------------------------------- 提示词
# 三分类：重点强化「否 vs 无关」的边界（实测中最大的错误来源）
PROMPT_THREE = HEAD + """
你只能从下列词里选一个作答：

是 —— 真相支持这个提问的说法。
否 —— 真相能够推出这个提问的说法是错的。
无关 —— 真相里根本没有可用来判断这件事的信息。

判断「否」和「无关」时，抓住这条界线：
只要真相提供了线索、能推出提问是错的，就答「否」；
只有当真相对此完全空白、无从判断时，才答「无关」。

示例：
真相是「枪里其实没有子弹」，提问「枪里有子弹吗」 → 否（真相明确否定了）
真相是「他和酒保是陌生人」，提问「他常来这家酒吧吗」 → 否（从陌生可推出不常来）
真相只讲了两人一见钟情，提问「他以前是不是她的男友」 → 否（一见钟情即初次相遇）
真相只讲了他如何打嗝止嗝，提问「他今年多大」 → 无关（真相完全没提）

只输出结论词，不要解释。"""

# 二分类：把「无关」并入「否」。
# 加了针对「表层关联推断」的示例 —— 实测这是最大的错误来源：
#   「妹妹恨姐姐吗」答"是"（看到"杀死姐姐"就推断出恨）
#   「男人消失与妹妹有关吗」答"是"（因为妹妹在找他）
# 两条真相里都没写，正确答案都是「否」。
PROMPT_BINARY = HEAD + """
玩家提出的是一个是非问题，你只需要判断：真相支持这个说法吗？

是 —— 真相支持这个提问的说法。
否 —— 真相不支持这个提问的说法（包括真相明确否定它、以及真相根本没提供相关信息）。

注意：只依据真相里写出来的内容判断。**不要从常识或前后关联去推断** ——
真相没写的事，就是「否」。

示例：
真相：「他打嗝不止，进酒吧要水想止嗝。酒保掏枪吓他，打嗝立刻停了。」
提问「他是因为口渴才要水的吗」→ 否（他要了水，但动机不是口渴，核心主张错了）
真相：「她为了再见到那个男人，杀死了姐姐。」
提问「妹妹恨姐姐吗」→ 否（真相只说了她的动机是制造葬礼，没提恨）
真相：「她杀姐姐是为了制造葬礼。男人为什么消失，真相没有交代。」
提问「男人消失与妹妹有关吗」→ 否（真相没写男人消失的原因，不能因为妹妹在找他就说有）

只输出 是 或 否。"""

# 四分类：加回「部分对」，并说明不能滥用
PROMPT_FOUR = HEAD + """
你只能从下列词里选一个作答：

是 —— 真相支持这个提问的说法。
否 —— 真相能够推出这个提问的说法是错的。
无关 —— 真相里根本没有可用来判断这件事的信息。
部分对 —— 提问一半说对了、一半说错了。

判断「否」和「无关」时：只要真相提供了线索、能推出提问是错的，就答「否」；
只有当真相对此完全空白、无从判断时，才答「无关」。
只有当提问确实一半对一半错时才用「部分对」，不要滥用。

示例：
真相是「枪里其实没有子弹」，提问「枪里有子弹吗」 → 否
真相只讲了他如何打嗝止嗝，提问「他今年多大」 → 无关
真相是「他住在12楼」，提问「他住在8楼和12楼吗」 → 部分对

只输出结论词，不要解释。"""

# 玩家直接报完整答案时用：判断猜测与真相是否一致
PROMPT_GUESS = """你在主持海龟汤游戏。玩家会给出一个对「汤底（真相）」的完整猜测，
你要判断这个猜测与真相是否一致。

一致 —— 猜测说清了真相的关键，与汤底描述相符。
不一致 —— 猜测与汤底描述矛盾，或者漏掉了关键。

只要猜测在关键情节上与真相相符就算「一致」，不要求逐字对应。
只输出 一致 或 不一致。"""

MODES = {
    "binary": {"labels": ("是", "否"), "prompt": PROMPT_BINARY},
    "three": {"labels": ("是", "否", "无关"), "prompt": PROMPT_THREE},
    "four": {"labels": ("是", "否", "无关", "部分对"), "prompt": PROMPT_FOUR},
}
LABELS = MODES["three"]["labels"]
GUESS_LABELS = ("一致", "不一致")

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

USER_TEMPLATE = """【汤面】{surface}
【汤底（真相）】{bottom}
【玩家提问】{question}
【你的回答】"""

GUESS_TEMPLATE = """【汤面】{surface}
【汤底（真相）】{bottom}
【玩家的完整猜测】{guess}
【你的判断】"""


class JudgeError(RuntimeError):
    """判题调用失败（桥不可达 / 模型未加载 / 返回畸形）。"""


def build_grammar(labels) -> str:
    """生成只允许这几个字面量的 GBNF。中文按 UTF-8 字节匹配，实测可用。"""
    return "root ::= " + " | ".join('"%s"' % x for x in labels)


def _extract_probs(choice, labels):
    lp = choice.get("logprobs") or {}
    merged = {}
    for item in (lp.get("content") or []):
        for cand in (item.get("top_logprobs") or []):
            tok = (cand.get("token") or "").strip()
            prob = cand.get("prob")
            if tok in labels and prob is not None and prob > merged.get(tok, 0.0):
                merged[tok] = prob
        tok = (item.get("token") or "").strip()
        prob = item.get("prob")
        if tok in labels and prob is not None:
            merged.setdefault(tok, prob)
    return merged


def decide(system, user, labels, model=DEFAULT_MODEL, base=DEFAULT_BASE,
           with_probs=False, timeout=300, extra=None):
    """通用有界决策：给定 system + user，用 grammar 把输出锁死在 labels 内。

    judge() 和玩家猜测判定都建立在它之上。
    """
    payload = {
        "model": model,
        "stream": False,
        "temperature": 0,
        "max_tokens": 8,
        "chat_template_kwargs": {"enable_thinking": False},
        "grammar": build_grammar(labels),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if with_probs:
        payload["logprobs"] = True
        payload["top_logprobs"] = len(labels) + 1
    if extra:
        payload.update(extra)

    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with _opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise JudgeError(f"调用 {base} 失败: {exc}") from exc
    ms = int((time.time() - t0) * 1000)

    if "error" in data:
        raise JudgeError(f"上游返回错误: {data['error']}")

    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    raw = (msg.get("content") or "").strip()
    if not raw and msg.get("reasoning_content"):
        # 这套部署的已知坑：思考型模型有时把内容全写进 reasoning_content
        raw = (msg.get("reasoning_content") or "").strip()

    out = {
        "label": raw if raw in labels else None,
        "raw": raw,
        "ms": ms,
        "model": model,
    }
    if with_probs:
        out["probs"] = _extract_probs(choice, labels)
    return out


def judge(surface, bottom, question, model=DEFAULT_MODEL, mode="binary",
          base=DEFAULT_BASE, with_probs=False, timeout=300, extra=None):
    """对单个玩家提问做一次有界决策。

    返回 dict：label / raw / ms / model / mode（with_probs=True 时另有 probs）
    """
    if mode not in MODES:
        raise JudgeError(f"未知 mode={mode}，可选：{list(MODES)}")
    conf = MODES[mode]
    out = decide(conf["prompt"],
                 USER_TEMPLATE.format(surface=surface, bottom=bottom,
                                      question=question),
                 conf["labels"], model=model, base=base,
                 with_probs=with_probs, timeout=timeout, extra=extra)
    out["mode"] = mode
    return out


def judge_guess(surface, bottom, guess, model=DEFAULT_MODEL, base=DEFAULT_BASE,
                timeout=300):
    """判断玩家给出的完整猜测是否与汤底一致（二值，无检查点时的退路）。"""
    return decide(PROMPT_GUESS,
                  GUESS_TEMPLATE.format(surface=surface, bottom=bottom, guess=guess),
                  GUESS_LABELS, model=model, base=base, timeout=timeout)


# ---------------------------------------------------------------- 猜测打分
# 「给玩家的猜测打几分」试过三种做法，用葬礼题 + 5 个不同接近度的猜测实测：
#   A 直接让模型输出 0-100 —— 最差。4B 把「妹妹想继承遗产」这种完全无关的猜测
#     打了 50 分。小模型的数值估计校准极差（裸 Qwen3 4B 校准分仅 29.1）。
#   B 让模型选分级再映射   —— 同样把「完全无关」打成 50 分，实际只有两档有区分度。
#   C 检查点法（采用）     —— 把真相拆成几条关键要素、逐条做**二值判断**，
#     命中率就是分数。4B 给出 100/75/50/25/0 的完美单调序列，还能告诉玩家漏了哪条。
# 结论：**分数必须是算出来的，不是模型估出来的。**
PROMPT_CHECKPOINT = """玩家对海龟汤谜底给出了一个猜测。判断他的猜测里**有没有体现**下面这条信息。

【汤底（真相）】{bottom}
【玩家的猜测】{guess}
【要检查的信息】{cp}

玩家的话里体现了这条信息吗？只输出 是 或 否。"""

CHECK_LABELS = ("是", "否")


def level_of(score):
    """把 0-100 的分数说成人话。"""
    if score >= 100:
        return "完全正确"
    if score >= 75:
        return "大部分正确"
    if score >= 50:
        return "一半正确"
    if score >= 25:
        return "一点正确"
    if score > 0:
        return "沾到一点边"
    return "还没对上"


def grade_guess(surface, bottom, guess, checkpoints, model=DEFAULT_HINT_MODEL,
                base=DEFAULT_BASE, timeout=300):
    """按检查点给玩家猜测打分。

    返回 dict：
        score       0-100（= 命中的检查点比例，算出来的）
        hit/total   命中条数 / 总条数
        level       人话等级
        hit_list    说对了哪些
        miss_list   还差哪些
        details     每条检查点的判定明细（含耗时）
        graded      False = 没有检查点，退回了二值判定
    """
    cps = [str(c).strip() for c in (checkpoints or []) if str(c).strip()]
    if not cps:
        res = judge_guess(surface, bottom, guess, model=model, base=base,
                          timeout=timeout)
        ok = res["label"] == GUESS_LABELS[0]
        return {
            "score": 100 if ok else 0, "hit": int(ok), "total": 1,
            "level": "完全正确" if ok else "还没对上",
            "hit_list": [], "miss_list": [], "details": [], "graded": False,
            "label": res["label"], "ms": res["ms"],
        }

    details = []
    for cp in cps:
        r = decide(PROMPT_CHECKPOINT.format(bottom=bottom, guess=guess, cp=cp),
                   "", CHECK_LABELS, model=model, base=base, timeout=timeout)
        details.append({"cp": cp, "hit": r["label"] == CHECK_LABELS[0],
                        "ms": r["ms"]})

    got = sum(1 for d in details if d["hit"])
    score = round(got / len(details) * 100)
    return {
        "score": score, "hit": got, "total": len(details),
        "level": level_of(score),
        "hit_list": [d["cp"] for d in details if d["hit"]],
        "miss_list": [d["cp"] for d in details if not d["hit"]],
        "details": details, "graded": True,
        "ms": sum(d["ms"] for d in details),
    }


# ---------------------------------------------------------------- 提示功能
# 提示是**生成任务**，没有 grammar 兜底，所以实测过泄露率：
#   自由生成提示：4B 1/5、8B 2/5 泄露（8B 反而更差）
#   引导性问题（让玩家直接拿去问）：8B 更差，3/5
#   引导性问题 + 关键词自检重试：4B 0/5、8B 2/5
# 结论：必须四层保险（① 无提问不调模型 → ② 语义把关 → ③ 关键词预筛 → ④ 兜底），
# 且**用 4B 生成更稳**（DEFAULT_HINT_MODEL 已在文件顶部定义）。
PROMPT_HINT = """你在主持一场海龟汤游戏。玩家已经问了一些问题，但卡住了。

【汤面】{surface}
【汤底（真相）】{bottom}
【玩家已经问过】{asked}

请给玩家一个引导性的问题，帮他把注意力引到还没考虑过的方向上。

要求：
1. 这个问题要能用「是 / 不是」回答，玩家看完可以直接拿去问你
2. 绝不能包含真相里的关键信息（原因、关键物品、关键动作都不能出现）
3. 一句话，不超过 30 个字，以「？」结尾
4. 直接输出这个问题，不要解释、不要加引号

你的问题："""

HINT_RETRY_SUFFIX = ("\n\n注意：上一次的问题里出现了真相的关键信息，"
                     "请换一个完全不同的方向，只指向玩家还没考虑过的方面。")

# 重试都用完仍然泄露时的兜底：模糊、通用，不含任何题目的专属信息
FALLBACK_HINT = "换个角度：试着问一件你还没问过的事——比如时间、地点，或者他身边有什么东西。"


def _chat_once(system, user, model, base, timeout, temperature=0.7, max_tokens=80):
    payload = {
        "model": model, "stream": False, "temperature": temperature,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": ([{"role": "system", "content": system}] if system else [])
                    + [{"role": "user", "content": user}],
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with _opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise JudgeError(f"调用 {base} 失败: {exc}") from exc
    ms = int((time.time() - t0) * 1000)
    if "error" in data:
        raise JudgeError(f"上游返回错误: {data['error']}")
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    text = (msg.get("content") or "").strip()
    if not text and msg.get("reasoning_content"):
        text = (msg.get("reasoning_content") or "").strip()
    return text, ms


def _tidy_hint(text):
    """收拾小模型的输出格式：包裹的引号、重复的问号。"""
    t = text.strip().strip("「」“”\"'　 ")
    t = re.sub(r"[?？]{2,}", "？", t)
    return t


# 玩家一个都没问就要提示时用的起步引导。
# 实测（2026-09-29）：零上下文时模型会直接围着谜底核心发问，例如
#   「妹妹制造第二次葬礼是为了让那个男人再次出现吗？」「是有人故意让他死在
#   沙漠中央吗？」—— 10 条里关键词表 0 检出、语义把关 6 条判泄露。
# 这个场景本身就是高风险的（模型没有「玩家探索到哪」可依据），索性不调模型。
START_HINT = ("先别急着要提示——这件事发生在哪里、什么时候？当事人当时处于什么状态？"
              "把这类基础问题先问清楚，谜面往往会自己露出破绽。")

# 语义把关：用有界决策判断一条提示是否说破了谜底。
# 关键词表抓不住同义改写（「再次出现」绕过了「再见到 / 想再见」），语义把关能。
# 实测同一批冷启动提示：关键词表 0/10 检出，4B 把关 6/10 且基本判对，
# 8B 把关只判 1/10（**又是小模型把关更严格**）。
#
# ⚠️ 2026-10-02 重写判据：旧版只说「说破了谜底的关键 / 指个方向」，
# 太含糊 —— 人工核对 24 条实测输出发现，它把
#   「那些被砍断的人之前认识这个男人吗？」（与谜底无关）
#   「公主醒来后是否立刻做出了决定？」（空泛）
# 这类也判成「泄露」：**假阳性约一半，但零漏检**。
# 改成「把提示和汤底逐条对照、看提示问的是不是汤底里写着的事实」后，
# 判据可操作了，且与判题用的是同一套「真相支持这个说法吗」的能力。
PROMPT_LEAK_GUARD = """你在为海龟汤游戏把关。玩家卡住时，主持人会给一句引导性的提问，
你的任务是判断这句提示有没有**把谜底的关键事实说出来**。

判断方法：把提示和下面的「汤底（真相）」逐条对照 ——

泄露 —— 提示问到的、说到的，正是汤底里写着的**关键事实**
        （关键原因、关键物品、关键动作、关键动机）。
        玩家顺着这句话往下问，答一声「是」就把关键情节拿走了。
安全 —— 提示问的是汤底里**没有写**的东西，例如时间、地点、在场还有谁、
        当事人的身份或状态。玩家还得自己继续提问、自己推理。

只在「说破了汤底里的事实」和「只是指了条方向」之间划线。
只输出 泄露 或 安全。"""

GUARD_LABELS = ("泄露", "安全")


def check_leak(bottom, hint, model=DEFAULT_HINT_MODEL, base=DEFAULT_BASE,
               timeout=300):
    """语义把关：这条提示是否说破了谜底。返回 (是否泄露, 原始标签)。"""
    try:
        res = decide(
            PROMPT_LEAK_GUARD,
            f"【汤底（真相）】{bottom}\n【主持人给玩家的提示】{hint}\n【你的判断】",
            GUARD_LABELS, model=model, base=base, timeout=timeout)
    except JudgeError:
        return False, None      # 把关本身失败时不拦，交给关键词表与兜底
    return res["label"] == "泄露", res["label"]


def make_hint(surface, bottom, asked, leak_words, model=DEFAULT_HINT_MODEL,
              base=DEFAULT_BASE, guard_model=None, max_try=3, timeout=300):
    """生成一条**不泄露汤底**的引导性问题。

    保险分四层：
      1. 玩家一个都没问 → 直接给起步引导，不调模型（零上下文是高危场景）
      2. 语义把关：grammar 有界决策判断「是否说破谜底」，抓同义改写
      3. 关键词表：题库 leak_words 做廉价预筛
      4. 重试用完仍不合格 → 降级为通用兜底提示

    返回 dict：text / leaks / guard / tries / fallback / ms / attempts / reason
    """
    guard_model = guard_model or model
    if not asked:
        return {"text": START_HINT, "leaks": [], "guard": None, "tries": 0,
                "fallback": False, "ms": 0, "attempts": [],
                "reason": "no_questions_yet"}

    asked_text = "；".join(asked)
    attempts = []
    for i in range(max_try):
        user = PROMPT_HINT.format(surface=surface, bottom=bottom,
                                  asked=asked_text)
        if attempts:
            user += HINT_RETRY_SUFFIX
        text, ms = _chat_once("", user, model, base, timeout)
        text = _tidy_hint(text)
        hit = [w for w in (leak_words or []) if w in text]
        leaked, verdict = False, None
        if text and not hit:
            leaked, verdict = check_leak(bottom, text, model=guard_model,
                                         base=base, timeout=timeout)
        attempts.append({"text": text, "leaks": hit, "guard": verdict, "ms": ms})
        if text and not hit and not leaked:
            return {"text": text, "leaks": [], "guard": verdict, "tries": i + 1,
                    "fallback": False, "ms": ms, "attempts": attempts}
    return {"text": FALLBACK_HINT, "leaks": [], "guard": None, "tries": max_try,
            "fallback": True, "ms": attempts[-1]["ms"] if attempts else 0,
            "attempts": attempts, "reason": "all_attempts_leaked"}


def judge_puzzle(puzzle, question, **kwargs):
    """对某一题的提问判题（字段取自题库 JSON）。"""
    return judge(puzzle["surface"], puzzle["bottom"], question, **kwargs)


def _main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="海龟汤判题器（Jev 式有界决策）")
    ap.add_argument("--surface", required=True, help="汤面")
    ap.add_argument("--bottom", required=True, help="汤底（真相）")
    ap.add_argument("--question", required=True, help="玩家提问")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--mode", default="binary", choices=list(MODES))
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--probs", action="store_true", help="附带各标签概率")
    args = ap.parse_args()

    try:
        res = judge(args.surface, args.bottom, args.question, model=args.model,
                    mode=args.mode, base=args.base, with_probs=args.probs)
    except JudgeError as exc:
        print(f"[失败] {exc}")
        sys.exit(1)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
