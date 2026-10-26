#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片输入端到端自检：真的画一张图发给模型，看它能不能看见。

界面（ui/index.html）把图片按 Ollama 协议塞在**各自消息的 `images` 里（裸 base64）**，
桥再转成 OpenAI 的 image_url data URI 交给 llama-server（带 mmproj 的视觉模型）。
这条链路任何一环断了，界面上的上传按钮就只是摆设 —— 所以必须有这么一条测试。

判据：图里画的是「红圆 + 蓝方 + 数字 42」，回答里要能对上。

用法：python scripts\\test_vision.py              默认测 qwen35-4b-heretic
      python scripts\\test_vision.py <模型名>
"""
import base64
import io
import json
import os
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)

import winrun  # noqa: E402

BASE = "http://127.0.0.1:11434"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen35-4b-heretic"
PNG = os.path.join(ROOT, "logs", "vision-test.png")
# 不走代理：agent shell 的 HTTP_PROXY 会把 127.0.0.1 拦成 502
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def make_image():
    """画一张内容明确、没有歧义的图：红圆 + 蓝方 + 数字 42。"""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (512, 512), "white")
    d = ImageDraw.Draw(img)
    d.ellipse((60, 60, 240, 240), fill=(220, 30, 30))          # 红圆
    d.rectangle((300, 300, 470, 455), fill=(20, 60, 220))      # 蓝方
    font = None
    for name in ("arial.ttf", "segoeui.ttf", "calibri.ttf"):
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/" + name, 120)
            break
        except Exception:
            continue
    text = "42"
    if font:
        box = d.textbbox((0, 0), text, font=font)
        d.text(((512 - (box[2] - box[0])) / 2 - box[0], 380), text, fill="black", font=font)
    else:
        d.text((210, 390), text, fill="black")
    img.save(PNG)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def post(path, body, timeout=600):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def score(reply, thinking):
    """判据：图里画的是「红圆 + 蓝方 + 数字 42」，回答里要能对上。

    注意：**思考过程也算数**。带 `--reasoning-format deepseek` 的模型常常
    把内容全写在 thinking 里、content 反而空（第一次测就被这个骗了）。
    """
    text = (reply or "") + "\n" + (thinking or "")
    low = text.lower().replace(" ", "")
    return {
        "红": ("红" in text or "red" in low),
        "圆": ("圆" in text or "circle" in low),
        "蓝": ("蓝" in text or "blue" in low),
        "方": ("方" in text or "square" in low or "rect" in low),
        "数字42": ("42" in text or "四十二" in text),
    }


def main():
    ok = total = 0
    print("  1) 画测试图…")
    b64 = make_image()
    print(f"     {PNG}  （base64 {len(b64) // 1024} KB）")

    prompt = "请描述这张图片：有哪些形状、分别是什么颜色？图中写的数字是多少？"

    print(f"  2) 走 /api/chat（界面用的就是这条），模型 {MODEL}…")
    t0 = time.time()
    try:
        r = post("/api/chat", {
            "model": MODEL, "stream": False,
            "think": False,          # 与界面「深度思考」默认关闭一致：直接给答案
            "messages": [{"role": "user", "content": prompt, "images": [b64]}],
            "options": {"num_predict": 600, "temperature": 0.2},
        })
        msg = r.get("message") or {}
        reply, thinking = msg.get("content", ""), msg.get("thinking", "")
    except Exception as exc:
        print(f"     !! 请求失败：{exc}")
        return 1
    print(f"     用时 {time.time() - t0:.1f}s   done_reason={r.get('done_reason')}")
    print("     content  ：" + (reply.strip().replace("\n", " ")[:240] or "（空）"))
    if thinking:
        print("     thinking ：" + thinking.strip().replace("\n", " ")[:120] + "…")

    total += 1
    hits = score(reply, thinking)
    hit = sum(1 for v in hits.values() if v)
    print("     命中：" + "  ".join(f"{k}={'✓' if v else '✗'}" for k, v in hits.items()))
    if hit >= 4:
        print(f"     ✓ 模型确实看见了（{hit}/5）")
        ok += 1
    else:
        print(f"     ✗ 只对上 {hit}/5，图片可能没送到模型")

    # 关掉思考时应当直接给出答案 —— content 空说明它把话都写进 thinking 了
    total += 1
    if reply.strip():
        print("     ✓ 关闭深度思考后 content 有内容（界面里能直接看到答案）")
        ok += 1
    else:
        print("     ✗ content 为空（全在 thinking 里），界面上会显得「没回答」")

    print("  3) 同一张图走 /v1/chat/completions（OpenAI 协议）…")
    total += 1
    try:
        r = post("/v1/chat/completions", {
            "model": MODEL, "stream": False, "max_tokens": 600, "temperature": 0.2,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "图里有什么形状和颜色？数字是多少？"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
            ]}],
        })
        m2 = r["choices"][0]["message"]
        reply2 = m2.get("content") or ""
        think2 = m2.get("reasoning_content") or m2.get("thinking") or ""
    except Exception as exc:
        reply2 = think2 = ""
        print(f"     !! 请求失败：{exc}")
    hits2 = score(reply2, think2)
    hit2 = sum(1 for v in hits2.values() if v)
    if hit2 >= 4:
        print(f"     ✓ 也对上了（{hit2}/5）")
        ok += 1
    else:
        print(f"     ✗ 只对上 {hit2}/5")
        print("     " + (reply2 or think2).strip().replace("\n", " ")[:200])

    print(f"\n  图片输入：{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
