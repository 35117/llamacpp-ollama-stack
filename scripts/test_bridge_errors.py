# -*- coding: utf-8 -*-
"""端到端验证桥的响应状态机。

真起一个 mock 上游 + 真起桥进程，用真实 HTTP 请求验证：
  1. 上游报错时，流式接口回**真正的错误状态码**（以前是 200 + body 里的 error）
  2. 上游报错时，不会往 chunked 流里写第二组 HTTP 状态行（以前会破坏协议）
  3. 正常路径仍然是 200 + 合法 NDJSON
  4. /admin/residency 返回可解析的驻留状态
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCK_PORT = 11436
BRIDGE_PORT = 11444
FAILED = []


class MockUpstream(BaseHTTPRequestHandler):
    """model 名字里含 'boom' -> 返回 503；否则返回一段正常 SSE。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _fail(self):
        payload = json.dumps({"error": {"message": "mock upstream exploded",
                                        "code": 503}}).encode()
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _sse(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        events = [
            {"choices": [{"delta": {"content": "你好"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            "DONE",
        ]
        for ev in events:
            if ev == "DONE":
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")

    def do_POST(self):
        body = self._read()
        # sentinel 放在消息/prompt 里：model 名会被桥改写成上游 id，
        # 进不到 mock 的判断逻辑
        blob = json.dumps(body, ensure_ascii=False)
        if "BOOM" in blob:
            return self._fail()
        return self._sse(body)

    def do_GET(self):
        if self.path.startswith("/running"):
            payload = json.dumps({"running": []}).encode()
        elif self.path.startswith("/api/version"):
            payload = json.dumps({"version": "mock"}).encode()
        elif self.path.startswith("/unload"):
            payload = b"{}"
        else:
            payload = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def fetch(url, data=None, timeout=15):
    """返回 (status, body_bytes)。"""
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None,
        method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def main():
    mocksrv = HTTPServer(("127.0.0.1", MOCK_PORT), MockUpstream)
    threading.Thread(target=mocksrv.serve_forever, daemon=True).start()

    cfg_path = os.path.join(ROOT, "logs", "_test_bridge_cfg.json")
    policy_path = os.path.join(ROOT, "logs", "_test_bridge_policy.json")
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"listen_host": "127.0.0.1",
                   "listen_port": BRIDGE_PORT,
                   "upstream": "http://127.0.0.1:%d" % MOCK_PORT,
                   "log_requests": False}, fh)

    # 把驻留策略重定向到临时文件，避免测试写脏真实 config
    env = dict(os.environ)
    env["LLAMA_RESIDENCY_POLICY"] = policy_path

    logpath = os.path.join(ROOT, "logs", "_test_bridge.log")
    logfh = open(logpath, "wb", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "scripts", "ollama_bridge.py"),
         "-config", cfg_path],
        cwd=ROOT, stdout=logfh, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, env=env)
    try:
        base = "http://127.0.0.1:%d" % BRIDGE_PORT
        # 等桥起来
        up = False
        for _ in range(60):
            try:
                s, _b = fetch(base + "/api/version")
                if s == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.25)
        check("桥能启动并响应 /api/version", up)

        if not up:
            print("\n  桥没起来，看 %s" % logpath)
            return 1

        # 取一个真实存在的模型名
        s, b = fetch(base + "/api/tags")
        models = []
        if s == 200:
            models = [m.get("name") or m.get("model")
                      for m in (json.loads(b).get("models") or [])]
        check("/api/tags 返回模型列表", bool(models), f"{len(models)} 个")
        if not models:
            return 1
        good = models[0]
        print(f"    用模型名做验证: {good}")

        # ---- 1. 正常流式：应为 200 + NDJSON
        s, b = fetch(base + "/api/chat",
                     {"model": good, "messages": [{"role": "user", "content": "hi"}],
                      "stream": True})
        lines = [l for l in b.decode("utf-8", "replace").splitlines() if l.strip()]
        ok_nd = bool(lines) and all(l.startswith("{") for l in lines)
        check("流式 /api/chat 正常路径 = 200 + NDJSON",
              s == 200 and ok_nd, f"status={s}, {len(lines)} 行")

        # ---- 2. 上游报错：应回 503，而不是 200 带 error
        s, b = fetch(base + "/api/chat",
                     {"model": good,
                      "messages": [{"role": "user", "content": "BOOM"}],
                      "stream": True})
        check("流式 /api/chat 上游报错 -> 回 503", s == 503, f"status={s}")

        # ---- 3. /v1 透传：上游报错也应回真状态码
        s, b = fetch(base + "/v1/chat/completions",
                     {"model": good,
                      "messages": [{"role": "user", "content": "BOOM"}]})
        check("/v1/chat/completions 上游报错 -> 回 503", s == 503, f"status={s}")

        # ---- 4. /api/generate 报错路径
        s, b = fetch(base + "/api/generate", {"model": good, "prompt": "BOOM"})
        check("/api/generate 上游报错 -> 回 503", s == 503, f"status={s}")

        # ---- 5. 驻留状态接口
        s, b = fetch(base + "/admin/residency")
        ok = False
        detail = f"status={s}"
        try:
            j = json.loads(b.decode("utf-8"))
            ok = s == 200 and "policy" in j and "resident" in j
            detail += f", keys={sorted(j.keys())}"
        except Exception as exc:
            detail += f", parse-error={exc}"
        check("/admin/residency 返回可解析状态", ok, detail)

        # ---- 6. 类型兜底：model 为非字符串不应把连接打死
        s, b = fetch(base + "/api/chat", {"model": 123, "messages": []})
        check("model 为数字 -> 400/404 而非连接重置",
              s in (400, 404), f"status={s}")
        s, b = fetch(base + "/api/chat", {"model": {"a": 1}, "messages": []})
        check("model 为对象 -> 400/404 而非连接重置",
              s in (400, 404), f"status={s}")

        # ---- 7. 策略热改
        s, b = fetch(base + "/admin/residency",
                     {"action": "set", "policy": {"idle_ttl": 1234}})
        ok = False
        if s == 200:
            try:
                ok = json.loads(b.decode("utf-8"))["policy"]["idle_ttl"] == 1234
            except Exception:
                ok = False
        check("/admin/residency set 能改策略", ok, f"status={s}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        logfh.close()
        mocksrv.shutdown()

    print()
    if FAILED:
        print("  失败项: " + ", ".join(FAILED))
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
