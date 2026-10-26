"""海龟汤游戏服务（独立运行版）。

业务逻辑全在 `turtle/game.py`，本文件只做 HTTP 包装。
挂在 llama.cpp 桥上的版本见 `scripts/turtle_web.py`（走 /turtle）——
两者共用同一套逻辑，只是入口不同。

启动：
    python turtle/server.py                 # 默认按题库 model_tiers 选判题模型（当前是 4B）
    python turtle/server.py --mode three    # 换成保留「无关」的三分类
打开：
    http://127.0.0.1:11440
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from game import MAX_HINTS, Game  # noqa: E402
from judge import DEFAULT_BASE, MODES  # noqa: E402

DEFAULT_PORT = 11440


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TurtleSoup/1.2"
    game = None
    html_path = None

    # ---------------------------------------------------------- 基础工具
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, payload, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _api(self, method, path):
        name = path[len("/api/"):].strip("/")
        status, payload = self.game.dispatch(method, name, self._body())
        self._json(payload, status)

    # ---------------------------------------------------------- 路由
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                return self._send(self.html_path.read_bytes(),
                                  "text/html; charset=utf-8")
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": f"读不到页面: {exc}"}, 500)
        if path.startswith("/api/"):
            return self._api("GET", path)
        return self._json({"error": "no such route"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            return self._api("POST", path)
        return self._json({"error": "no such route"}, 404)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="海龟汤游戏服务（独立运行）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--model", default=None,
                    help="判题模型。不指定 = 按题目难度自动选（题库 model_tiers："
                         "简单题小模型、复杂题大模型）；显式指定则整局都用它")
    ap.add_argument("--hint-model", default=None,
                    help="生成提示用的模型。不指定 = 用 turtle/models.json 里的角色默认")
    ap.add_argument("--grade-model", default=None,
                    help="给猜测打分用的模型。不指定 = 用 turtle/models.json 里的角色默认")
    ap.add_argument("--mode", default="binary", choices=list(MODES))
    ap.add_argument("--max-hints", type=int, default=MAX_HINTS,
                    help="每题最多给几次提示")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--puzzles", default=None)
    ap.add_argument("--open-browser", action="store_true",
                    help="服务起来后自动打开浏览器（供 start.bat 用）")
    args = ap.parse_args()

    game = Game(model=args.model, hint_model=args.hint_model,
                grade_model=args.grade_model, mode=args.mode,
                base=args.base, max_hints=args.max_hints,
                puzzle_file=args.puzzles)
    Handler.game = game
    Handler.html_path = ROOT / "index.html"

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    cfg = game.config()[1]
    print("=" * 66)
    print("  海龟汤游戏服务已启动（独立运行版）")
    print(f"  页面   http://{args.host}:{args.port}")
    print(f"  判题   {cfg['model_desc']} × {game.mode} 模式"
          f"（{'/'.join(MODES[game.mode]['labels'])}）")
    print(f"  提示   {cfg['effective']['hint']}（每题最多 {game.max_hints} 次，带泄露把关）")
    print(f"  打分   {cfg['effective']['grade']}")
    print(f"  题库   {game.puzzle_file}（{len(game.puzzles)} 题）")
    print(f"  上游   {args.base}")
    print("=" * 66)
    print("  Ctrl+C 停止")
    print("  提示：挂在聊天界面里的版本是 http://127.0.0.1:11434/turtle（不用开这个）")
    print("  界面里点「⚙ 模型」可以随时切换判题/提示/打分用的模型")

    threading.Thread(target=game.warmup, daemon=True).start()

    if args.open_browser:
        try:
            import webbrowser
            webbrowser.open(f"http://{args.host}:{args.port}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [提示] 自动打开浏览器失败：{exc}", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        httpd.shutdown()


if __name__ == "__main__":
    main()
