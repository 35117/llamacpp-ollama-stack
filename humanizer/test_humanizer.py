#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「去 AI 味」离线自检 —— 不需要起服务、不需要模型。

跑法：
    python humanizer\\test_humanizer.py          # 纯离线（快，秒级）
    python humanizer\\test_humanizer.py --live   # 额外打一次真实上游（需服务在跑）

这里锁住的是**踩过的坑**，不是覆盖率：
  1. prompt 指纹（改坏了 prompt 组装，模型立刻跑偏）
  2. 数字归一（1,250 / 480k / 31.7万），错一个就会误报「数字丢失」
  3. 语言检测对「中文正文 + 代码块/表格」的混排文档不能判成英文
     —— 判错会白烧重试，并把 language 误报成问题
  4. inspect：4 倍超长要判 too_long；中文稿的汉字输出不算语言漂移
  5. rewrite_once：拿到退化样本（超长/胡编）必须重采样，且取好的那次
  6. --live 额外锁住 llama-swap 的卸载契约（定向卸载不能连坐别的模型）
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import humanizer_core as hz  # noqa: E402

PASS = 0
FAIL = 0
F = chr(96) * 3      # ```


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}\n       got  = {got!r}\n       want = {want!r}")


def check_true(name, cond, detail=""):
    check(name + (f"  ({detail})" if detail else ""), bool(cond), True)


# ---------------------------------------------------------------- 1. 指纹
def t_fingerprint():
    print("[1] prompt 指纹")
    check("sha256 前 16 位", hz.check_prompt_format(), True)


# ---------------------------------------------------------------- 2. 数字
def t_numbers():
    print("[2] 数字归一")
    check("1,250 / 480k / 31.7万",
          sorted(hz.normalize_numbers("1,250 元 与 480k，以及 31.7万 和 2030 年")),
          ["1250", "2030", "317000", "480000"])
    # 「万」不能被记两遍：既报 31.7 又报 317000 会造成「数字丢失」误报
    check("万 只记一遍", "31.7" in hz.normalize_numbers("31.7万"), False)


# ---------------------------------------------------------------- 3. 语言
def t_lang():
    print("[3] 语言检测（混排回归）")
    check("纯中文", hz.detect_lang("在当今时代，人工智能已经成为变革力量，值得注意。"), "zh")
    check("纯英文", hz.detect_lang("In this world AI has become a force, widely adopted."), "en")
    # 回归：短中文正文 + 代码块 + 表格，旧实现（分母=非空白字符数）会算成 en
    mixed = ("# 标题\n\n这是第一段。\n\n" + F + "python\nprint(1)\n" + F +
             "\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n结尾段落，数字 1,250 和 480k。")
    check("短中文+代码+表格", hz.detect_lang(mixed), "zh")
    readme = ("本项目提供轻量的本地推理服务，支持多种协议与模型热切换。安装方式如下。\n\n"
              + F + "bash\ngit clone https://example.com/repo.git\ncd repo\n"
              "pip install -r requirements.txt\npython setup.py build_ext --inplace\n" + F +
              "\n\n启动完成后可通过浏览器访问默认端口，配置项说明见下表。")
    check("中文 README + 大代码块", hz.detect_lang(readme), "zh")


# ---------------------------------------------------------------- 4. inspect
def t_inspect():
    print("[4] inspect 判据")
    zh = "本项目的目标是提供一个轻量的本地推理服务，支持多种协议与模型热切换。"
    paraphrase = "这个项目做了一个轻量的本地推理服务，能热切换模型，协议也支持好几种。"
    check("正常改写无问题", hz.inspect(zh, paraphrase)["problems"], [])
    check_true("同文对比算抄稿", "copy" in hz.inspect(zh, zh)["problems"])

    probs = hz.inspect(zh, zh * 4)["problems"]
    check_true("4 倍超长判 too_long", "too_long" in probs, str(probs))

    # 中文稿跑出中文，绝不是语言漂移（旧实现依赖 detect_lang，混排时会误报）
    probs = hz.inspect("启动完成后可以通过浏览器访问默认端口。",
                       "启动之后，用浏览器就能看到默认端口。")["problems"]
    check_true("中文稿不报 language", "language" not in probs, str(probs))

    # 英文稿跑出成片中文才报（判据看原稿有没有汉字，不看 detect_lang）
    probs = hz.inspect("Start the service and open the default port in a browser.",
                       "启动服务之后，在浏览器里打开默认端口就可以了，非常方便快捷。")["problems"]
    check_true("英文稿跑中文报 language", "language" in probs, str(probs))

    probs = hz.inspect(zh, zh[:6])["problems"]
    check_true("砍太狠判 too_short", "too_short" in probs, str(probs))


# ---------------------------------------------------------------- 5. 重采样
def t_retry():
    print("[5] rewrite_once 重采样")
    draft = "本项目提供一个轻量的本地推理服务，支持多种协议与模型热切换，方便部署。"
    clean = "这个项目弄了个轻量本地推理服务，协议和模型都能热切，部署挺省事。"

    real = hz.complete

    def make(seq):
        box = {"i": 0}

        def fake(prompt, base=None, model=None, n_predict=2048, timeout=900):
            i = min(box["i"], len(seq) - 1)
            box["i"] += 1
            return {"content": seq[i], "stop": True}
        return fake, box

    # a) 第一次就干净 -> 只调一次
    fake, box = make([clean])
    hz.complete = fake
    try:
        text, chk = hz.rewrite_once(draft)
    finally:
        hz.complete = real
    check("干净结果只调一次", box["i"], 1)
    check("attempt==1", chk["attempt"], 1)
    check("返回干净文本", text, clean)

    # b) 第一次退化（4 倍超长胡编）-> 必须重采样并采用第二次
    degenerate = draft * 4
    fake, box = make([degenerate, clean])
    hz.complete = fake
    try:
        text, chk = hz.rewrite_once(draft)
    finally:
        hz.complete = real
    check("退化后重采样", box["i"], 2)
    check("采用第 2 次的文本", text, clean)
    check("attempt==2", chk["attempt"], 2)
    check("最终无问题", chk["problems"], [])


# ---------------------------------------------------------------- live
LIVE_DRAFT = (
    "# 部署说明\n\n"
    "本项目的目标是提供一个轻量的本地推理服务，支持多种协议与模型热切换。"
    "安装方式如下，先克隆仓库，再安装依赖即可。\n\n"
    + F + "bash\ngit clone https://example.com/repo.git\npip install -r requirements.txt\n" + F +
    "\n\n启动完成后，可以通过浏览器访问默认端口。配置项说明见下表，"
    "端口号不要随意修改，否则客户端会全部连不上。\n\n"
    "| 配置项 | 默认值 |\n|---|---|\n| 端口 | 11434 |\n\n"
    "实测吞吐约为 31.7 词每秒，成本下降了 480 元每月。"
)


def t_live(base=None):
    print("[6] live（真实上游）")
    plan = hz.plan_pieces(LIVE_DRAFT)
    keep = sum(1 for p in plan if p["kind"] == "keep")
    prose = [p for p in plan if p["kind"] == "prose"]
    check_true("结构块被保留", keep >= 3, f"keep={keep}")
    check_true("有正文块可改", len(prose) >= 2, f"prose={len(prose)}")
    bad = 0
    for p in prose:
        text, chk = hz.rewrite_once(p["text"], base=base or hz.DEFAULT_BASE)
        hard = [x for x in chk["problems"]
                if x in ("empty", "prompt_echo", "chat_markers", "copy",
                         "missing_numbers", "language", "too_short", "too_long")]
        print(f"    piece {len(p['text']):>4}字 -> {len(text):>4}字 "
              f"ratio={chk['length_ratio']} copy={chk['copy_rate']} problems={chk['problems']}")
        bad += len(hard)
    check("live：没有硬伤", bad, 0)


def t_swap():
    """llama-swap 的驻留/卸载契约（--live 时跑；这里的坑都是实测踩出来的）。

    最要命的一条：**别用 `/unload?model=名字` 做定向卸载** —— 它返回 200 OK，
    但 query 参数被静默忽略，会把**所有**模型一起卸掉。定向卸载只能走
    `POST /api/models/unload/{名字}`，本例就是来锁死这个行为的。
    """
    print("[7] live：模型驻留 / 卸载（llama-swap 契约）")
    scripts = os.path.join(os.path.dirname(HERE), "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        import swap_ctl as sc
    except Exception as exc:  # noqa: BLE001
        print(f"    跳过：{exc}")
        return

    v = sc.version()
    if not v:
        print("    跳过：llama-swap 没在跑")
        return
    print(f"    swap {v.get('version')} | 当前驻留: {sc.running_names() or '无'}")

    before = sc.running_names()
    ok, msg = sc.unload("definitely-no-such-model-xyz")
    after = sc.running_names()
    check_true("卸载不存在的模型会失败", not ok, msg[:60])
    check("不存在的名字不连坐其它模型", after, before)

    ok, msg = sc.unload()
    check_true("全卸成功", ok, msg[:60])
    check("全卸后无模型驻留", sc.running_names(), [])


def main():
    t_fingerprint()
    t_numbers()
    t_lang()
    t_inspect()
    t_retry()
    if "--live" in sys.argv:
        t_live()
        t_swap()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
