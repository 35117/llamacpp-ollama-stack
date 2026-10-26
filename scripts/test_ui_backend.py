# -*- coding: utf-8 -*-
"""端到端验证 Web 界面的后端：人格覆写 / 新建 / 删除 / 对话存档。

重点验证「改了提示词，模型行为真的跟着变」—— 这是整个功能的核心承诺。
"""
import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:11434"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 绕开本地代理

ok_count = 0
fail_count = 0


def call(method, path, body=None, timeout=180):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def check(name, cond, detail=""):
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  [PASS] {name}")
    else:
        fail_count += 1
        print(f"  [FAIL] {name}   {detail}")
    if detail and cond:
        print(f"         {detail}")


def chat(model, prompt, num=60):
    st, d = call("POST", "/api/chat", {
        "model": model, "stream": False, "think": False,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"num_predict": num, "temperature": 0.2},
    })
    if st != 200 or not isinstance(d, dict) or d.get("error"):
        return None, str(d)[:200]
    return (d.get("message") or {}).get("content", ""), None


def state():
    st, d = call("GET", "/admin/state")
    return d if isinstance(d, dict) else {}


def live_base():
    """挑一个当前**可用**的底座。

    别写死模型名 —— 用户随时会换模型（删旧的、加新的），
    写死的话测试会开始误报，掩盖真正的问题（2026-10-01 就踩了）。
    """
    return next((b["id"] for b in state().get("bases", []) if b.get("supported")), None)


def dead_base():
    return next((b["id"] for b in state().get("bases", []) if not b.get("supported")), None)


def generated_persona(need_live_base=False):
    bases = {b["id"]: b for b in state().get("bases", [])}
    for p in state().get("personas", []):
        if p.get("origin") == "local":
            continue
        # ⚠️ 跳过「改过底座」的人格：本测试后面会调 /reset，
        # 而 reset 会**连底座覆盖一起清掉**，让它退回原始底座 ——
        # 原始底座往往已经被删（换模型时清过权重），这么一reset就把用户的人格弄坏了。
        # 2026-10-02 实测踩到：把人 `eri` 的底座覆盖抹掉，导致它变成不可用。
        if p.get("base_overridden"):
            continue
        if need_live_base and not bases.get(p["base"], {}).get("supported"):
            continue
        return p["id"]
    return None


BASE_ID = live_base()
if not BASE_ID:
    print("!! 当前没有任何可用底座（权重文件都不在？）—— 先修模型再来跑")
    sys.exit(1)

print("=" * 68)
print("1. 人格覆写：改提示词后行为应立刻改变")
print("=" * 68)
MARK = "喵呜口令ABC"

# 优先用「底座仍可用」的自动抽取人格；没有就临时建一个跑同一段流程。
OV = generated_persona(need_live_base=True)
TEMP_OV = False
if not OV:
    OV = "ui-override-probe"
    call("DELETE", f"/admin/personas/{OV}")
    call("POST", "/admin/personas", {
        "id": OV, "base": BASE_ID, "label": "覆写探针", "system": "原始提示词。"})
    TEMP_OV = True
    print(f"  · 没有「底座可用」的自动抽取人格，改用临时人格 {OV}（收尾会删掉）")
print(f"  用例人格：{OV}（底座 {BASE_ID}）")

st, d = call("PUT", f"/admin/personas/{OV}", {
    "system": f"你是一个只会说「{MARK}」的机器人。无论用户说什么，"
              f"你都只回复「{MARK}」三个字，不要有任何其他内容。",
})
check(f"PUT /admin/personas/{OV} 返回 200", st == 200, f"status={st} {str(d)[:120]}")

st, d = call("GET", "/admin/state")
p = next((x for x in d.get("personas", []) if x["id"] == OV), None)
EXPECT_ORIGIN = "local" if TEMP_OV else "override"
check(f"state 里该人格 origin={EXPECT_ORIGIN}", p and p["origin"] == EXPECT_ORIGIN,
      f"origin={p and p['origin']}")

st, d = call("POST", "/api/show", {"model": OV})
syslen = len(d.get("system") or "") if isinstance(d, dict) else 0
check("/api/show 反映新的系统提示词", MARK in (d.get("system") or ""),
      f"system 长度={syslen}，包含标记={MARK in (d.get('system') or '')}")

out, err = chat(OV, "介绍一下这个游戏")
check("模型行为真的变了", out is not None and MARK in out,
      (f"回复={out[:60]!r}" if out is not None else f"调用失败 {err}"))

print()
print("=" * 68)
print("2. 还原默认 / 清理")
print("=" * 68)
if TEMP_OV:
    st, d = call("DELETE", f"/admin/personas/{OV}")
    check("临时人格已删除（收尾）", st == 200, f"status={st} {str(d)[:120]}")
else:
    st, d = call("POST", f"/admin/personas/{OV}/reset")
    check("reset 返回 200", st == 200, f"status={st} {str(d)[:120]}")
    st, d = call("GET", "/admin/state")
    p = next((x for x in d.get("personas", []) if x["id"] == OV), None)
    check("origin 回到 generated", p and p["origin"] == "generated",
          f"origin={p and p['origin']}")
    out, err = chat(OV, "介绍一下这个游戏")
    check("行为回到原人格（不再只说标记）", out is not None and MARK not in out,
          (f"回复={out[:60]!r}" if out is not None else f"调用失败 {err}"))

print()
print("=" * 68)
print("3. 新建人格 → 能被调用 → 能删除")
print("=" * 68)
NEW_ID = "ui-test-persona"
call("DELETE", f"/admin/personas/{NEW_ID}")     # 清掉上次可能的残留
st, d = call("POST", "/admin/personas", {
    "id": NEW_ID, "base": BASE_ID, "label": "界面测试人格",
    "system": f"你只会回复「{MARK}」。",
})
check("POST /admin/personas 返回 201", st == 201, f"status={st} {str(d)[:140]}")

st, d = call("GET", "/api/tags")
names = [m["model"] for m in d.get("models", [])]
# 列表接口**只列真模型**：人格/别名不列，否则外部客户端里 7 个模型会显示成 33 个。
# 但「不列 ≠ 不能调用」—— 下面立刻验证按名字直接调用照样通。
check("底座出现在对外列表里", BASE_ID in names, f"当前 {len(names)} 个名称")
check("人格不出现在对外列表里（只列真模型）", NEW_ID not in names,
      f"{NEW_ID} 在列表里 = {NEW_ID in names}")
out, err = chat(NEW_ID, "你好")
check("但人格仍可按名字直接调用", out is not None,
      (f"回复={out[:50]!r}" if out else f"失败 {err}"))

st, d = call("POST", "/admin/personas", {"id": NEW_ID, "base": BASE_ID, "system": "x"})
check("重复 id 会被拒绝", st == 400, f"status={st} {str(d)[:120]}")
st, d = call("POST", "/admin/personas", {"id": "bad id!", "base": BASE_ID, "system": "x"})
check("非法 id 会被拒绝", st == 400, f"status={st} {str(d)[:120]}")
DEAD = dead_base()
if DEAD:
    st, d = call("POST", "/admin/personas", {"id": "ok-id", "base": DEAD, "system": "x"})
    check(f"挂到不可用底座（{DEAD}）会被拒绝", st == 400, f"status={st} {str(d)[:120]}")
else:
    print("  [SKIP] 当前没有「不可用」的底座，跳过该项")

st, d = call("DELETE", f"/admin/personas/{NEW_ID}")
check("DELETE 返回 200", st == 200, f"status={st} {str(d)[:120]}")
st, d = call("GET", "/api/tags")
names = [m["model"] for m in d.get("models", [])]
check("删除后列表里没有、也不能再调用", NEW_ID not in names
      and chat(NEW_ID, "你好")[0] is None)
GEN_ANY = generated_persona()
if GEN_ANY:
    st, d = call("DELETE", f"/admin/personas/{GEN_ANY}")
    check("自动抽取的人格不允许删除", st == 400, f"status={st} {str(d)[:140]}")

print()
print("=" * 68)
print("4. 对话存档")
print("=" * 68)
st, d = call("POST", "/admin/chats")
cid = d.get("id") if isinstance(d, dict) else None
check("POST /admin/chats 返回新 id", st == 201 and cid, f"id={cid}")

payload = {"title": "接口测试会话", "model": "qwen35-4b-heretic", "messages": [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好呀", "thinking": "用户在打招呼"},
]}
st, d = call("PUT", f"/admin/chats/{cid}", payload)
check("PUT 保存会话", st == 200, f"status={st}")
st, d = call("GET", f"/admin/chats/{cid}")
msgs = d.get("messages", []) if isinstance(d, dict) else []
check("GET 读回内容一致", len(msgs) == 2 and msgs[1].get("thinking") == "用户在打招呼",
      f"messages={len(msgs)}")
st, d = call("GET", "/admin/chats")
ids = [c["id"] for c in d.get("chats", [])]
check("出现在会话列表里", cid in ids, f"列表 {len(ids)} 条")
st, d = call("DELETE", f"/admin/chats/{cid}")
check("DELETE 会话", st == 200, f"status={st}")
st, d = call("GET", f"/admin/chats/{cid}")
check("删除后读不到", st == 404, f"status={st}")

print()
print("=" * 68)
print("5. 路径穿越防护")
print("=" * 68)
for bad in ["../config/personas.json", "..%2F..%2Fetc%2Fpasswd", "a/../../b"]:
    st, d = call("GET", f"/admin/chats/{bad}")
    check(f"拒绝 {bad[:34]}", st in (400, 404), f"status={st}")
st, raw = call("GET", "/ui/../config/personas.json")
check("静态页拒绝路径穿越", "personas" not in str(raw)[:200], f"{str(raw)[:80]}")

print()
print("=" * 68)
print(f"合计：{ok_count} 通过 / {fail_count} 失败")
print("=" * 68)
sys.exit(1 if fail_count else 0)
