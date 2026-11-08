# -*- coding: utf-8 -*-
"""验证会话里图片能落盘、能读回，以及 PATCH 只改标题不动消息。

需要服务正在运行（python scripts/service_ctl.py start）。
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:11434"
PNG1X1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
          "2mP8z8AAAwAB/gGiSg0AAAAASUVORK5CYII=")

fails = []


def check(name, ok, extra=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  " + str(extra)) if extra and not ok else ""))
    if not ok:
        fails.append(name)


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


s, d = req("POST", "/admin/chats")
cid = d.get("id")
check("新建会话", s == 201 and bool(cid), d)

msgs = [{"role": "user", "content": "看看这张图",
         "images": [{"b64": PNG1X1, "mime": "image/png", "name": "a.png", "w": 1, "h": 1}]},
        {"role": "assistant", "content": "看到了", "thinking": ""}]
s, d = req("PUT", "/admin/chats/" + cid,
           {"title": "图片持久化测试", "title_auto": True, "model": "nomic-embed", "messages": msgs})
check("PUT 保存", s == 200, (s, d))
imgs = (d.get("messages") or [{}])[0].get("images") or []
check("PUT 回显里带 images", len(imgs) == 1, d.get("messages"))

s, d = req("GET", "/admin/chats/" + cid)
imgs = (d.get("messages") or [{}])[0].get("images") or []
one = imgs[0] if imgs else {}
check("GET 读回 images", len(imgs) == 1, d.get("messages"))
check("b64 完整", one.get("b64") == PNG1X1, len(str(one.get("b64"))))
check("mime 保留", one.get("mime") == "image/png", one)
check("name / 尺寸保留", one.get("name") == "a.png" and one.get("w") == 1 and one.get("h") == 1, one)
check("title_auto 已存", d.get("title_auto") is True, d.get("title_auto"))
before_updated = d.get("updated")

s, d = req("PATCH", "/admin/chats/" + cid, {"title": "手动改的名字", "title_auto": False})
check("PATCH 改名", s == 200 and d.get("title") == "手动改的名字", (s, d))

s, d = req("GET", "/admin/chats/" + cid)
check("改名后消息没丢", len(d.get("messages") or []) == 2, len(d.get("messages") or []))
check("改名不刷新 updated（列表不乱跳）", d.get("updated") == before_updated,
      (before_updated, d.get("updated")))
check("title_auto 置 false", d.get("title_auto") is False, d.get("title_auto"))
check("改名后图片还在", len(((d["messages"][0]).get("images") or [])) == 1)

s, d = req("PATCH", "/admin/chats/" + cid, {"title": "   "})
check("空标题被拒", s == 404 or s == 400, (s, d))

# 9 张图 -> 只留 8
many = [{"b64": PNG1X1, "mime": "image/png", "name": "n%d.png" % i} for i in range(9)]
s, d = req("PUT", "/admin/chats/" + cid,
           {"title": "多图", "messages": [{"role": "user", "content": "9 张", "images": many}]})
n = len((d.get("messages") or [{}])[0].get("images") or [])
check("超过 8 张被截断", n == 8, n)

# 超大 b64 被丢
big = "A" * 5_500_000
s, d = req("PUT", "/admin/chats/" + cid,
           {"title": "大图", "messages": [{"role": "user", "content": "大图",
                                           "images": [{"b64": big, "mime": "image/png"}]}]})
n = len((d.get("messages") or [{}])[0].get("images") or [])
check("超大图被丢弃", n == 0, n)

req("DELETE", "/admin/chats/" + cid)
print()
print("%d 项失败" % len(fails) if fails else "全部通过")
sys.exit(1 if fails else 0)
