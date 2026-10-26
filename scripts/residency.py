#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型驻留管理 ——「谁占着显存、闲置多久了、多久没用自动卸载」。

为什么需要它
------------
llama.cpp 一个 `llama-server` 进程只能服务一个模型，llama-swap 负责按请求里的
model 字段自动拉起/换出后端进程。但 **llama-swap 默认 `globalTTL: 0`（永不自动
卸载）**，于是：一个 5~6GB 的模型只要跑过一次就会一直挂在显存里，8G 卡上
直接决定别的模型还能不能加载。

手动卸载本来就支持，缺的是两件事：

  1. **卸载要确认真的生效。** 以前请求回 200 就报成功，而 `service_ctl` 直接
     拿它当进程退出码 —— 自动化脚本拿到 exit 0 以为显存释放了，其实模型还
     挂着，下一次加载必炸（见 `swap_ctl.unload` 的注释）。
  2. **闲置自动卸载要能统一配。** 只有 humanizer-12b 单独配了 `ttl: 600`，
     其余 7 个模型全靠 `globalTTL: 0`。而且 humanizer 改写长文要跑几分钟，
     这期间用户在界面上聊别的模型 → 5.5 + 6.2 GiB 双驻留，8G 卡必炸。
     所以需要一个**全局策略**：所有模型统一闲置多久卸载，个别模型可覆盖，
     常用的那个可以设成「永不自动卸载」。

实现选择
--------
llama-swap 自带 model 级 `ttl` 与 `globalTTL`，本来可以直接配。但那有两个
问题：改完要重启 llama-swap 才生效；而且**看不到「还有多久会被卸」**。
所以这里在桥里跑一个轻量 reaper：桥本来就看得见每一个推理请求，顺手记一下
最后使用时间，定期把超时的卸掉。好处是策略热改、状态可查询，且不依赖
llama-swap 的具体版本。

策略文件
--------
`config/residency.json`：

    {
      "auto_unload": true,          # 总开关
      "idle_ttl": 1800,             # 默认闲置上限（秒）
      "check_interval": 60,         # reaper 检查间隔（秒）
      "per_model_ttl": {"humanizer-12b": 600},   # 个别覆盖
      "protected": ["qwen35-4b-heretic"]        # 永不自动卸载
    }

改完热生效（reaper 每次循环重新读）。

CLI
---
    python scripts\\residency.py status [--json]
    python scripts\\residency.py list
    python scripts\\residency.py get
    python scripts\\residency.py set idle_ttl=900 auto_unload=false
    python scripts\\residency.py unload [名字] [--force]
    python scripts\\residency.py reap
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swap_ctl  # noqa: E402

#: 策略文件。可用 LLAMA_RESIDENCY_POLICY 覆盖 —— 测试与临时实验用它隔离，
#: 免得把真实 config 改脏。
POLICY_PATH = (os.environ.get("LLAMA_RESIDENCY_POLICY")
               or os.path.join(ROOT, "config", "residency.json"))

#: 默认策略。idle_ttl 单位是秒。
#: protected 里的模型**永不自动卸载**（仍可手动卸）—— 一般填最常用的那个。
DEFAULT_POLICY = {
    "auto_unload": True,
    "idle_ttl": 1800,
    "check_interval": 60,
    "per_model_ttl": {
        "humanizer-12b": 600,      # 低频工具，写完稿子就该让位
        "galtransl-7b": 600,
    },
    "protected": [
        "qwen35-4b-heretic",       # 日常聊天主力，别被自动卸掉
    ],
}

#: 策略里允许直接改的标量键
_SCALAR_KEYS = ("auto_unload", "idle_ttl", "check_interval")


def _coerce_policy(raw):
    """把读到的 json 兜成完整结构，缺字段用默认值补齐。

    以 `_` 开头的键视为注释（如 `_说明`），原样保留 —— 否则第一次用 API
    改策略就会把 config 里人工写的说明抹掉。
    """
    pol = dict(DEFAULT_POLICY)
    comments = {}
    if isinstance(raw, dict):
        for k in _SCALAR_KEYS:
            if k in raw:
                pol[k] = raw[k]
        pol["per_model_ttl"] = dict(DEFAULT_POLICY["per_model_ttl"])
        if isinstance(raw.get("per_model_ttl"), dict):
            pol["per_model_ttl"].update(raw["per_model_ttl"])
        if isinstance(raw.get("protected"), list):
            pol["protected"] = list(raw["protected"])
        comments = {k: v for k, v in raw.items()
                    if isinstance(k, str) and k.startswith("_")}

    try:
        pol["idle_ttl"] = int(pol["idle_ttl"])
    except (TypeError, ValueError):
        pol["idle_ttl"] = DEFAULT_POLICY["idle_ttl"]
    try:
        pol["check_interval"] = int(pol["check_interval"])
    except (TypeError, ValueError):
        pol["check_interval"] = DEFAULT_POLICY["check_interval"]
    pol["auto_unload"] = bool(pol["auto_unload"])
    pol.update(comments)
    return pol


def load_policy():
    try:
        with open(POLICY_PATH, "r", encoding="utf-8") as fh:
            return _coerce_policy(json.load(fh))
    except Exception:
        return _coerce_policy(None)


def save_policy(patch):
    """改策略并落盘。以 `_` 开头的注释键保留。"""
    pol = load_policy()
    comments = {k: v for k, v in pol.items()
                if isinstance(k, str) and k.startswith("_")}
    pol.update(patch or {})
    pol = _coerce_policy(pol)
    pol.update(comments)
    os.makedirs(os.path.dirname(POLICY_PATH), exist_ok=True)
    tmp = "%s.%d.%s.tmp" % (POLICY_PATH, os.getpid(), uuid.uuid4().hex)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(pol, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, POLICY_PATH)
    return pol


def ttl_for(model, policy=None):
    """该模型的闲置上限（秒）。0 / None = 永不自动卸载。"""
    pol = policy or load_policy()
    val = (pol.get("per_model_ttl") or {}).get(model, pol.get("idle_ttl"))
    try:
        val = int(val)
    except (TypeError, ValueError):
        return 0
    return max(0, val)


def is_protected(model, policy=None):
    pol = policy or load_policy()
    return model in (pol.get("protected") or [])


# ------------------------------------------------------------ 使用记录
_lock = threading.RLock()
_last_use = {}      # 上游模型名 -> 最后一次被请求的时间戳
_seen_at = {}       # 上游模型名 -> 第一次出现在 /running 里的时间戳
_reaper = None


def note_use(model):
    """记一次使用。model 应传**上游模型名**（resolve 后的 upstream_id），
    因为 llama-swap 的 /running 里报的是它。"""
    if not model:
        return
    with _lock:
        _last_use[str(model)] = time.time()


def idle_sec(model):
    """闲置了多少秒；从没见过返回 None。"""
    with _lock:
        base = _last_use.get(model)
        if base is None:
            base = _seen_at.get(model)
        if base is None:
            return None
        return max(0.0, time.time() - base)


def _sync_seen(names):
    """给 /running 里出现、但我们没记录过使用时间的模型补一个观察时间。"""
    now = time.time()
    with _lock:
        for n in names:
            _seen_at.setdefault(n, now)


# ------------------------------------------------------------ 卸载
def unload(model=None, force=False):
    """卸载驻留模型。

    model 为空/None -> 全部；否则只卸载它。
    force=True 时忽略 protected 名单。

    返回 (ok, 说明)。ok 的含义是「确认已经不驻留了」。
    """
    if not swap_ctl.up():
        return True, "服务没在跑（llama-swap 不可达），没有可卸载的驻留模型"

    if model and not force and is_protected(model):
        return False, (f"{model} 在 protected 名单里，不会自动/误卸载；"
                       f"确实要卸就加 --force")

    return swap_ctl.unload(model)


def candidates(policy=None):
    """当前「已超时、待卸载」的模型名列表（不执行卸载）。"""
    pol = policy or load_policy()
    if not pol.get("auto_unload"):
        return []
    if not swap_ctl.up():
        return []
    names = swap_ctl.running_names()
    _sync_seen(names)
    out = []
    for n in names:
        if is_protected(n, pol):
            continue
        ttl = ttl_for(n, pol)
        if not ttl:
            continue
        idle = idle_sec(n)
        if idle is not None and idle >= ttl:
            out.append(n)
    return out


def reap(policy=None):
    """跑一次清理，返回被卸载掉的模型名列表。"""
    done = []
    for name in candidates(policy):
        ok, _msg = swap_ctl.unload(name)
        if ok:
            done.append(name)
    return done


def snapshot(policy=None):
    """给界面/CLI 用的驻留总览。"""
    pol = policy or load_policy()
    info = {"policy": pol, "swap_up": False, "resident": [], "error": None}
    try:
        if not swap_ctl.up():
            info["error"] = "llama-swap 不可达"
            return info
        info["swap_up"] = True
        raw = swap_ctl.running()
        names = [m.get("model") for m in raw if m.get("model")]
        _sync_seen(names)
        for item, name in zip(raw, names):
            ttl = ttl_for(name, pol)
            idle = idle_sec(name)
            prot = is_protected(name, pol)
            entry = {
                "model": name,
                "state": item.get("state"),
                "idle_sec": None if idle is None else round(idle),
                "ttl": ttl,
                "protected": prot,
                "unload_in_sec": None,
            }
            if idle is not None and ttl and not prot:
                entry["unload_in_sec"] = max(0, round(ttl - idle))
            info["resident"].append(entry)
    except Exception as exc:      # noqa: BLE001
        info["error"] = str(exc)
    return info


# ------------------------------------------------------------ reaper 线程
def start_reaper(interval=None, log=None):
    """启动后台清理线程（幂等）。返回线程对象。"""
    global _reaper
    if _reaper is not None and _reaper.is_alive():
        return _reaper
    pol = load_policy()

    def loop():
        while True:
            # 每轮重读策略，这样改完 config 不用重启服务就生效
            iv = interval or load_policy().get("check_interval") or 60
            time.sleep(max(5, iv))
            try:
                for name in reap():
                    msg = "[residency] 闲置超时，已卸载 %s" % name
                    if log:
                        log(msg)
                    else:
                        sys.stderr.write(msg + "\n")
                        sys.stderr.flush()
            except Exception:      # noqa: BLE001  reaper 自己绝不能挂掉
                pass

    _ = pol
    _reaper = threading.Thread(target=loop, daemon=True, name="residency-reaper")
    _reaper.start()
    return _reaper


# ------------------------------------------------------------ CLI
def _parse_assign(items):
    """['idle_ttl=900','auto_unload=false'] -> {'idle_ttl':900,'auto_unload':False}"""
    patch = {}
    for item in items:
        if "=" not in item:
            raise ValueError("需要 K=V 形式，收到: %s" % item)
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip()
        low = v.lower()
        if low in ("true", "false"):
            v = (low == "true")
        else:
            try:
                v = int(v)
            except ValueError:
                pass
        patch[k] = v
    return patch


def _fmt_secs(n):
    if n is None:
        return "未知"
    if n < 60:
        return "%ds" % n
    if n < 3600:
        return "%dm%02ds" % (n // 60, n % 60)
    return "%dh%02dm" % (n // 3600, (n % 3600) // 60)


def print_residency(snap):
    pol = snap.get("policy") or {}
    print("  模型驻留")
    print(f"    闲置自动卸载: {'开' if pol.get('auto_unload') else '关'}"
          f"    默认上限: {_fmt_secs(pol.get('idle_ttl'))}"
          f"    检查间隔: {_fmt_secs(pol.get('check_interval'))}")
    if snap.get("error"):
        print(f"    !! {snap['error']}")
        return
    if not snap.get("resident"):
        print("    驻留: 无（显存已释放）")
        return
    for r in snap["resident"]:
        if r.get("protected"):
            tail = "protected，不会自动卸载"
        elif not r.get("ttl"):
            tail = "未设上限"
        else:
            tail = "%s 后自动卸载" % _fmt_secs(r["unload_in_sec"])
        print(f"    ● {r['model']}  闲置 {_fmt_secs(r['idle_sec'])}  → {tail}")


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="模型驻留管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="驻留概览").add_argument(
        "--json", action="store_true")
    sub.add_parser("list", help="只看驻留的模型名")
    sub.add_parser("get", help="打印当前策略")
    sub.add_parser("reap", help="立刻跑一次闲置清理")

    p_set = sub.add_parser("set", help="改策略，例如 set idle_ttl=900")
    p_set.add_argument("assign", nargs="+", metavar="K=V")

    p_u = sub.add_parser("unload", help="卸载驻留模型（省略名字 = 全部）")
    p_u.add_argument("model", nargs="?", default=None)
    p_u.add_argument("--force", action="store_true",
                     help="忽略 protected 名单，强行卸载")

    args = ap.parse_args(argv)

    if args.cmd == "set":
        try:
            patch = _parse_assign(args.assign)
        except ValueError as exc:
            print("  !! %s" % exc)
            return 2
        pol = save_policy(patch)
        print("  已更新 config\\residency.json")
        print(json.dumps({k: v for k, v in pol.items()
                          if not str(k).startswith("_")},
                         ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "get":
        pol = load_policy()
        print(json.dumps({k: v for k, v in pol.items()
                          if not str(k).startswith("_")},
                         ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "reap":
        done = reap()
        print("  已卸载: " + (", ".join(done) if done else "无（没有闲置超时的）"))
        return 0

    if args.cmd == "unload":
        ok, msg = unload(args.model, force=args.force)
        print("  " + msg)
        return 0 if ok else 1

    snap = snapshot()
    if args.cmd == "list":
        if not snap["resident"]:
            print("  当前没有模型驻留（显存已释放）")
            return 0
        for r in snap["resident"]:
            tail = "" if r["unload_in_sec"] is None else \
            f"（{_fmt_secs(r['unload_in_sec'])}后自动卸载）"
            tag = " [protected]" if r["protected"] else ""
            print(f"  ● {r['model']}{tag}  闲置 {_fmt_secs(r['idle_sec'])}")
        return 0

    if getattr(args, "json", False):
        print(json.dumps(snap, ensure_ascii=False, indent=2))
    else:
        print_residency(snap)
    return 0 if not snap.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
