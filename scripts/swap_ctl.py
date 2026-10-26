#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llama-swap 的「模型驻留」管理：查询谁挂着、把谁卸掉。

为什么需要它：llama-swap 默认 **globalTTL: 0（不自动卸载）**，一个 5~6GB 的
模型跑过一次就会一直占着显存，8G 卡上直接影响别的模型能不能加载。
本模块把 swap 自带的卸载端点包成函数，供 `service_ctl.py`、humanizer 页面
和任何脚本复用（**别再各写一份 HTTP 调用**）。

端点行为（v260 / commit fcefa7b 实测确认，别想当然）：

    GET  /running                  -> {"running":[{"model","state","proxy",...}]}
    GET  /unload                   -> 卸载**全部**模型
    POST /api/models/unload/{名字}  -> 卸载**指定**模型（名字不存在 → 404）
    GET  /api/version              -> {"version":"v260","commit":...}

⚠️ 两个坑（都踩过）：
  1. `/unload` **只认 GET**；用 POST 打它返回 405 Method Not Allowed。
  2. 定向卸载**只能**走 `/api/models/unload/{名字}`。
     `/unload?model=名字` 看着像能用（返回 200 OK），其实 **query 参数被静默忽略**
     —— 它把**所有**模型都卸了。想只卸一个千万别用这条。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

SWAP_BASE = os.environ.get("LLAMA_SWAP_BASE", "http://127.0.0.1:11435")


def _opener():
    """显式绕开系统代理。

    本机常驻 Clash（http_proxy=http://127.0.0.1:xxxx），若走代理，
    访问 127.0.0.1 也会被转出去，表现成 502 —— 与请求本身无关。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _call(path, method="GET", data=None, base=SWAP_BASE, timeout=10):
    """返回 (status, bytes)；网络层失败返回 (None, 错误文本 bytes)。"""
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path, data=body, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with _opener().open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:      # 4xx/5xx：状态码本身有意义
        return exc.code, (exc.read() or b"")[:200]
    except Exception as exc:                   # noqa: BLE001 连不上/超时
        return None, str(exc)[:160].encode()


# ------------------------------------------------------------------ 查询

def up(base=SWAP_BASE, timeout=5):
    """llama-swap 是否在跑？

    需要它，是因为 `running()` 在「连不上」和「真没有模型」时都返回 [] ——
    不先探活就把空列表当结果，会让上层把故障报成「显存已释放」。
    """
    return version(base=base, timeout=timeout) is not None


def version(base=SWAP_BASE, timeout=5):
    """swap 版本 dict；取不到返回 None。"""
    s, b = _call("/api/version", base=base, timeout=timeout)
    if s != 200:
        return None
    try:
        return json.loads(b.decode("utf-8", "replace"))
    except ValueError:
        return None


def running(base=SWAP_BASE, timeout=8):
    """当前驻留的模型列表（原始 dict 列表）；失败返回 []。"""
    s, b = _call("/running", base=base, timeout=timeout)
    if s != 200:
        return []
    try:
        return json.loads(b.decode("utf-8", "replace")).get("running", [])
    except ValueError:
        return []


def running_names(base=SWAP_BASE, timeout=8):
    """当前驻留的模型名列表，例如 ['humanizer-12b']。"""
    return [m.get("model") for m in running(base=base, timeout=timeout) if m.get("model")]


def is_loaded(model, base=SWAP_BASE, timeout=8):
    """指定模型是否正驻留在显存里。"""
    return model in running_names(base=base, timeout=timeout)


# ------------------------------------------------------------------ 卸载

def _wait_until_gone(model, base=SWAP_BASE, timeout=15.0, interval=0.4):
    """轮询确认模型真的从驻留列表里消失了。

    卸载是**异步**的：swap 收到请求后要先跑 cmdStop、等进程退出（unloadTimeout
    默认 10s）。只复查一次几乎必然看到「还在」。
    """
    deadline = time.time() + timeout
    while True:
        left = running_names(base=base, timeout=5)
        gone = (not left) if model is None else (model not in left)
        if gone or time.time() >= deadline:
            return gone, left
        time.sleep(interval)


def unload(model=None, base=SWAP_BASE, timeout=60, settle=15.0):
    """卸载模型。

    model 为空/None -> 卸载全部（GET /unload）
    否则          -> 只卸载该模型（POST /api/models/unload/{名字}）

    返回 (ok, 说明)。ok 的含义是「确认已经不驻留了」—— 以前请求回 200 就
    返回 True，而 service_ctl.main 直接拿它当进程退出码，于是自动化脚本
    拿到 exit 0 以为显存释放了，其实 6GB 模型还挂着，下次加载必炸。
    """
    if not model:
        s, b = _call("/unload", base=base, timeout=timeout)
        if s != 200:
            if s is None:
                return False, (
                    "连不上 llama-swap（%s）：%s" % (base, b.decode("utf-8", "replace")))
            return False, (
                "卸载失败：HTTP %s %s" % (s, b.decode("utf-8", "replace")))
        ok, left = _wait_until_gone(None, base=base, timeout=settle)
        if ok:
            return True, "已卸载全部模型，显存已释放"
        return False, ("卸载请求已受理，但 %.0f 秒后仍驻留: %s"
                       % (settle, ", ".join(left)))

    name = urllib.parse.quote(str(model), safe="")
    s, b = _call("/api/models/unload/%s" % name, method="POST",
                 base=base, timeout=timeout)
    if s == 200:
        ok, left = _wait_until_gone(model, base=base, timeout=settle)
        if ok:
            tail = "，当前无模型驻留" if not left else "，仍驻留: " + ", ".join(left)
            return True, "已卸载 %s%s" % (model, tail)
        return False, ("卸载请求已受理，但 %s 仍在驻留（可能还在处理请求）" % model)
    if s == 404:
        # 404 的语义是「该模型当前不在 running 列表」，**不是**「不存在这个
        # 模型」。对已经停掉的模型发卸载会走到这里，报「没有名为 X 的模型」
        # 是误导 —— 而且会返回 exit 1，让「幂等卸载」的脚本误判为失败。
        cur = running_names(base=base, timeout=5)
        return True, ("%s 本来就没驻留（swap 404）；现在驻留: %s"
                      % (model, ", ".join(cur) or "无"))
    if s is None:
        return False, (
            "连不上 llama-swap（%s）：%s" % (base, b.decode("utf-8", "replace")))
    return False, (
        "卸载 %s 失败：HTTP %s %s" % (model, s, b.decode("utf-8", "replace")))


def main():
    import argparse

    ap = argparse.ArgumentParser(description="llama-swap 模型驻留管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出当前驻留的模型")
    p_u = sub.add_parser("unload", help="卸载模型（省略名字 = 全部）")
    p_u.add_argument("model", nargs="?", default=None)
    args = ap.parse_args()

    if args.cmd == "list":
        v = version()
        print(f"llama-swap {v.get('version') if v else '?'} @ {SWAP_BASE}")
        rs = running()
        if not rs:
            print("  当前没有模型驻留（显存已释放）")
        for m in rs:
            print(f"  ● {m.get('model')}  [{m.get('state')}]  {m.get('proxy') or ''}")
        return 0

    ok, msg = unload(args.model)
    print("  " + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
