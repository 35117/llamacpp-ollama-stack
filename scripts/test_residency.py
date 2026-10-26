# -*- coding: utf-8 -*-
"""端到端验证 residency：定向卸载 / 全部卸载 / 闲置超时自动卸载 / protected。

真起一个 mock llama-swap（提供 /running、/unload、/api/models/unload/{id}），
把 residency 指向它，然后走真实 HTTP 调用路径验证行为。
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

MOCK_PORT = 11437
STATE = {"running": [], "unloaded": []}
FAILED = []


class MockSwap(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _running_payload(self):
        return {"running": [{"model": m, "state": "ready"} for m in STATE["running"]]}

    def do_GET(self):
        if self.path.startswith("/running"):
            return self._json(self._running_payload())
        if self.path.startswith("/api/version"):
            return self._json({"version": "mock-v260"})
        if self.path.startswith("/unload"):
            # ⚠ 真实 llama-swap 的这个端点是「卸载全部」，只认 GET
            STATE["unloaded"].append(("*all*", time.time()))
            STATE["running"] = []
            return self._json({"ok": True})
        return self._json({})

    def do_POST(self):
        if self.path.startswith("/api/models/unload/"):
            name = self.path.rsplit("/", 1)[-1]
            import urllib.parse
            name = urllib.parse.unquote(name)
            if name not in STATE["running"]:
                return self._json({"error": "not running"}, status=404)
            STATE["unloaded"].append((name, time.time()))
            STATE["running"] = [m for m in STATE["running"] if m != name]
            return self._json({"ok": True})
        return self._json({})


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def main():
    srv = HTTPServer(("127.0.0.1", MOCK_PORT), MockSwap)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    os.environ["LLAMA_SWAP_BASE"] = "http://127.0.0.1:%d" % MOCK_PORT
    import residency
    import swap_ctl
    swap_ctl.SWAP_BASE = os.environ["LLAMA_SWAP_BASE"]

    # 用临时策略文件，避免动到真实 config
    tmp_policy = os.path.join(ROOT, "logs", "_test_residency_policy.json")
    residency.POLICY_PATH = tmp_policy

    # ---------------- 1. swap 可达性
    check("swap 探活 up()", residency and swap_ctl.up() is True)

    # ---------------- 2. 定向卸载
    STATE["running"] = ["humanizer-12b", "qwen35-4b-heretic"]
    ok, msg = residency.unload("humanizer-12b")
    check("定向卸载只卸指定的那个",
          ok and STATE["running"] == ["qwen35-4b-heretic"], msg)

    # ---------------- 3. 卸载不存在的名字 -> 不应崩，应明确说明
    ok, msg = residency.unload("no-such-model")
    check("卸载不存在的名字 -> 不崩溃且给出说明",
          isinstance(ok, bool) and "no-such-model" in msg, msg)

    # ---------------- 4. 全部卸载
    STATE["running"] = ["a", "b"]
    ok, msg = residency.unload(None)
    check("全部卸载 -> 驻留清空", ok and STATE["running"] == [], msg)

    # ---------------- 5. 闲置超时 -> candidates() 命中
    #   ⚠ 别挑 galtransl-7b：它在 per_model_ttl 里有覆盖（600s），
    #     这里要测的是「走默认 idle_ttl」的路径，所以挑一个没有覆盖的名字。
    TARGET = "hunyuan-mt-7b"
    STATE["running"] = [TARGET]
    residency.save_policy({"idle_ttl": 2})
    residency.note_use(TARGET)
    check("刚用过 -> 不算超时", residency.candidates() == [],
          str(residency.candidates()))
    # 把最后使用时间往前拨 10 秒，模拟闲置（> idle_ttl=2）
    with residency._lock:
        residency._last_use[TARGET] = time.time() - 10
    check("闲置超过 TTL -> 进入待卸载", residency.candidates() == [TARGET],
          str(residency.candidates()))

    # ---------------- 6. protected 模型永不自动卸载
    STATE["running"] = ["qwen35-4b-heretic"]
    with residency._lock:
        residency._last_use["qwen35-4b-heretic"] = time.time() - 99999
        residency._seen_at["qwen35-4b-heretic"] = time.time() - 99999
    check("protected 模型不进入待卸载",
          residency.candidates() == [], str(residency.candidates()))
    ok, msg = residency.unload("qwen35-4b-heretic")
    check("protected 模型手动卸载被拦下（除非 --force）", ok is False, msg)
    ok, msg = residency.unload("qwen35-4b-heretic", force=True)
    check("protected 模型加 --force 可强卸", ok is True, msg)

    # ---------------- 7. reap() 真的执行卸载
    STATE["running"] = ["galtransl-7b"]
    with residency._lock:
        residency._last_use["galtransl-7b"] = time.time() - 99999
        residency._seen_at["galtransl-7b"] = time.time() - 99999
    done = residency.reap()
    check("reap() 执行卸载并清空驻留",
          done == ["galtransl-7b"] and STATE["running"] == [], str(done))

    # ---------------- 8. 策略热改生效
    residency.save_policy({"idle_ttl": 4242})
    check("set 后 get 立即反映",
          residency.load_policy()["idle_ttl"] == 4242)

    # ---------------- 9. snapshot() 结构完整
    STATE["running"] = ["galtransl-7b"]
    snap = residency.snapshot()
    r = snap["resident"][0]
    check("snapshot 含 model/idle_sec/ttl/protected",
          snap["swap_up"] and r["model"] == "galtransl-7b"
          and r["protected"] is False and isinstance(r["ttl"], int),
          json.dumps(r, ensure_ascii=False))

    srv.shutdown()

    print()
    if FAILED:
        print("  失败项: " + ", ".join(FAILED))
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
