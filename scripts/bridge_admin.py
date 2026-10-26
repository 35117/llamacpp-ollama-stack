#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 界面的后端：人格管理 + 对话历史 + 静态页面。

设计要点
--------
1. **单一数据源**：人格提示词的唯一真相是文件，不引入数据库。
   - `config/personas/<id>.txt`       由 Modelfile 自动抽取（可重新生成，会被覆盖）
   - `config/personas-local/<id>.txt` **界面上编辑/新建的人格**，优先级更高，永不被覆盖
   - `config/personas-local/index.json` 记录「界面上新建」的人格（id/底座/展示名）
   这样 `apply-personas.bat` 重新抽人格时，你在界面上改过的东西不会丢。

2. **对话历史**存 `config/chats/<id>.json`，纯文件、可备份、可手工看。

3. 只依赖标准库，和桥的其他部分保持一致。
"""
import json
import os
import re
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERSONAS_GEN = os.path.join(ROOT, "config", "personas")          # 生成物
PERSONAS_LOCAL = os.path.join(ROOT, "config", "personas-local")  # 界面编辑/新建
LOCAL_INDEX = os.path.join(PERSONAS_LOCAL, "index.json")
PERSONAS_JSON = os.path.join(ROOT, "config", "personas.json")
CHATS_DIR = os.path.join(ROOT, "config", "chats")
UI_DIR = os.path.join(ROOT, "ui")

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
MAX_SYSTEM = 200_000
MAX_MSG = 400_000


# --------------------------------------------------------------------------- 工具
def ensure_dirs():
    for d in (PERSONAS_LOCAL, CHATS_DIR):
        os.makedirs(d, exist_ok=True)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


#: 人格/会话都是「读文件 -> 改内存 -> 写回」，全程必须持锁。
#: 否则两个并发写（例如边改人格边存会话）会交错覆盖，丢记录甚至写坏文件。
_IO_LOCK = threading.RLock()


def _tmp_path(path):
    """唯一的临时文件名。

    坑：以前固定叫 `<目标>.tmp`。两个并发写会同时 open 同一个 tmp，
    互相截断交错写入，os.replace 之后落盘的是两份内容混在一起的结果。
    """
    return "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex)


def _write_json(path, obj):
    tmp = _tmp_path(path)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _write_text(path, text):
    tmp = _tmp_path(path)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


# --------------------------------------------------------------------------- 人格
def load_override(pid):
    """返回界面覆盖/新建的系统提示词；没有则 None。"""
    p = os.path.join(PERSONAS_LOCAL, pid + ".txt")
    if os.path.exists(p):
        return _read_text(p)
    return None


def local_index():
    return _read_json(LOCAL_INDEX, {"personas": []}).get("personas", [])


def _save_local_index(items):
    ensure_dirs()
    # 按 id 去重（后写覆盖先写），避免任何路径下重复记录堆积
    seen = {}
    for r in items:
        if isinstance(r, dict) and r.get("id"):
            seen[r["id"]] = r
    _write_json(LOCAL_INDEX, {"personas": list(seen.values()),
                              "note": "界面上新建的人格。提示词在 config/personas-local/<id>.txt，"
                                      "不会被 apply-personas 覆盖。"})


def overlay():
    """给 Registry / snapshot 用的叠加数据：{id: {system, base, label, origin}}。

    origin: 'override' = 改过自动抽取的人格；'local' = 界面上新建的。
    - `system` 为 None 表示「提示词没改」，调用方自己去读 `config/personas/<file>`；
    - `base` 为 None 表示「底座没改」。

    记录存在 `config/personas-local/index.json`。**必须存在这儿**，不能改
    `config/personas.json` —— 那是 extract_personas 的生成物，
    下次跑 apply-personas.bat 就把改动冲掉了。
    """
    out = {}
    reg = _read_json(PERSONAS_JSON, {})
    gen = {p["id"] for p in reg.get("personas", [])}
    recs = {}
    for r in local_index():
        pid = r.get("id")
        if pid:
            recs[pid] = r

    # 1) 自动抽取的人格：只有被改过（提示词 或 底座）才进 overlay
    for p in reg.get("personas", []):
        pid = p["id"]
        ov = load_override(pid)
        rec = recs.get(pid) or {}
        newbase = rec.get("base")
        if ov is None and not newbase:
            continue
        out[pid] = {
            "system": ov,
            "origin": "override",
            "label": rec.get("label") or p.get("label"),
            "base": newbase or None,
        }

    # 2) 界面上新建的人格
    for pid, rec in recs.items():
        if pid in gen:
            continue
        ov = load_override(pid)
        if ov is None:
            continue
        out[pid] = {"system": ov, "origin": "local",
                    "label": rec.get("label") or pid, "base": rec.get("base")}
    return out


def _base_available(b):
    """权重在不在盘上。

    extract_personas 已经会按文件存在与否标 supported，这里再查一次是**双保险**：
    用户删了模型但没重跑抽取时，界面同样能立刻看出「这个底座没权重了」。
    """
    need = [n for n in (b.get("gguf"), b.get("mmproj")) if n]
    if not need:
        return False
    return all(os.path.exists(os.path.join(ROOT, "models", n)) for n in need)


def _base_downloading(b):
    """权重是不是正下到一半（有 .part 但正式文件还没到）。"""
    g = b.get("gguf") or ""
    if not g:
        return False
    p = os.path.join(ROOT, "models", g)
    return (not os.path.exists(p)) and os.path.exists(p + ".part")


def _check_base(base, reg):
    """校验一个底座能不能挂人格。返回 (ok, 错误信息)。"""
    b = (reg.get("bases") or {}).get(base)
    if not b:
        return False, f"底座 '{base}' 不存在"
    if b.get("supported") is False:
        return False, f"底座 '{base}' 已停用：{b.get('unsupported_reason') or '不可用'}"
    if not _base_available(b):
        return False, f"底座 '{base}' 的权重文件不在 models/ 里（已删除或尚未下载）"
    return True, None


def snapshot(registry):
    """给界面的完整状态：底座 + 人格（含来源标记）。
    registry 用来拿「某个 id 是否对外可用」的判断口径。
    """
    reg = _read_json(PERSONAS_JSON, {})
    ov = overlay()
    bases = []
    base_vision = {}
    for bid, b in reg.get("bases", {}).items():
        # 界面要据此决定「能不能贴图」以及给不让看的模型加提示，
        # 所以每个底座/人格都带上 vision 标记（人格继承其底座）。
        base_vision[bid] = bool(b.get("vision"))
        avail = _base_available(b)
        supported = (b.get("supported") is not False) and avail
        reason = b.get("unsupported_reason")
        if not avail and not reason:
            reason = ("正在下载（models/%s.part），下完自动可用" % b.get("gguf")
                      if _base_downloading(b)
                      else "权重文件不在 models/ 里（已删除或尚未下载）")
        bases.append({
            "id": bid,
            "label": b.get("desc") or bid,
            "gguf": b.get("gguf"),
            "ctx": b.get("ctx"),
            "vision": bool(b.get("vision")),
            "embedding": bool(b.get("embedding")),   # 向量模型不能当对话底座
            "supported": supported,
            "unsupported_reason": reason,
            "has_personas": any(p["base"] == bid for p in reg.get("personas", [])),
        })

    personas = []
    for p in reg.get("personas", []):
        pid = p["id"]
        o = ov.get(pid) or {}
        # 底座可以被界面改掉（records 存在 personas-local/index.json），
        # 外面那层 overlay() 已经把覆盖值合进来了。
        base = o.get("base") or p["base"]
        system = o.get("system")
        if system is None:                     # 只改了底座，提示词还是自动抽取的
            system = _read_text(os.path.join(ROOT, "config", p["system_file"]))
            origin = "generated"
        else:
            origin = "override"
        personas.append({
            "id": pid,
            "base": base,
            "base_default": p["base"],          # 原始底座，界面用来提示「已改」
            "base_overridden": base != p["base"],
            "label": p.get("label") or pid,
            "vision": base_vision.get(base, False),   # 继承底座
            "system": system,
            "chars": len(system or ""),
            "origin": origin,                      # generated | override | local
            "source_modelfile": p.get("source_modelfile"),
            "deletable": False,                    # 自动抽取的不能删，只能「还原」
        })
    for rec in local_index():
        pid = rec.get("id")
        if not pid or any(x["id"] == pid for x in personas):
            continue
        o = ov.get(pid)
        system = o["system"] if o else load_override(pid) or ""
        personas.append({
            "id": pid,
            "base": rec.get("base"),
            "label": rec.get("label") or pid,
            "vision": base_vision.get(rec.get("base"), False),   # 继承底座
            "system": system,
            "chars": len(system),
            "origin": "local",
            "source_modelfile": None,
            "deletable": True,
            "created": rec.get("created"),
        })
    # x["base"] 可能是 None（记录被手工改坏 / 旧版本遗留），
    # str 与 None 无法比较，会让整个 /admin/state 直接 500。
    personas.sort(key=lambda x: (x["base"] or "", x["id"]))
    return {"bases": bases, "personas": personas,
            "paths": {"personas": PERSONAS_LOCAL, "chats": CHATS_DIR}}


def _known_ids():
    reg = _read_json(PERSONAS_JSON, {})
    ids = set(reg.get("bases", {}).keys())
    ids |= {p["id"] for p in reg.get("personas", [])}
    ids |= {k for k in (reg.get("ollama_aliases") or {})}
    # 界面上新建的人格不在 personas.json 里，必须单独算进来 ——
    # 否则用同一个 id 再建一次会「成功」，把旧记录悄悄顶掉（踩过）。
    ids |= {r.get("id") for r in local_index() if r.get("id")}
    return ids, set(reg.get("bases", {}).keys())


def create_persona(body):
    """界面新建人格。必须挂在一个已注册的底座上。"""
    pid = (body.get("id") or "").strip()
    base = (body.get("base") or "").strip()
    label = (body.get("label") or "").strip() or pid
    system = body.get("system") or ""
    if not ID_RE.match(pid):
        return None, "id 只能用字母/数字/._-，1-48 位，且以字母或数字开头"
    if len(system) > MAX_SYSTEM:
        return None, f"系统提示词太长了（上限 {MAX_SYSTEM} 字符）"
    known, bases = _known_ids()
    if pid in known:
        return None, f"id '{pid}' 已被占用（和已有的人格/底座/别名重名）"
    if base not in bases:
        return None, f"底座 '{base}' 不存在"
    ok, err = _check_base(base, _read_json(PERSONAS_JSON, {}))
    if not ok:
        return None, err + "，不能挂人格"
    ensure_dirs()
    _write_text(os.path.join(PERSONAS_LOCAL, pid + ".txt"), system)
    items = local_index()
    items.append({"id": pid, "base": base, "label": label,
                  "created": time.strftime("%Y-%m-%d %H:%M:%S")})
    _save_local_index(items)
    return {"id": pid, "base": base, "label": label}, None


def update_persona(pid, body):
    """改人格：系统提示词、底座、展示名 —— 三样都能单独改。

    自动抽取的人格 → 改动记进 `personas-local/index.json`（提示词另外落到
    `personas-local/<id>.txt`），所以 `apply-personas.bat` 重跑也不会丢。
    界面新建的人格 → 直接改它自己的那条记录。
    """
    if not ID_RE.match(pid):
        return None, "id 不合法"
    reg = _read_json(PERSONAS_JSON, {})
    is_generated = any(p["id"] == pid for p in reg.get("personas", []))
    items = local_index()
    is_local = any(r.get("id") == pid for r in items)
    if not is_generated and not is_local:
        return None, f"没有这个人格: {pid}"

    system = body.get("system")
    if system is not None and len(system) > MAX_SYSTEM:
        return None, f"系统提示词太长了（上限 {MAX_SYSTEM} 字符）"

    base = (body.get("base") or "").strip()
    if base:
        ok, err = _check_base(base, reg)
        if not ok:
            return None, err
    label = (body.get("label") or "").strip()

    ensure_dirs()
    if system is not None:
        _write_text(os.path.join(PERSONAS_LOCAL, pid + ".txt"), system)

    if base or label:
        rec = next((r for r in items if r.get("id") == pid), None)
        if rec is None:
            rec = {"id": pid, "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            items.append(rec)
        if base:
            rec["base"] = base
        if label:
            rec["label"] = label
        if is_generated:
            rec["note"] = "改过自动抽取人格的底座/展示名"
        _save_local_index(items)

    changed = []
    if system is not None:
        changed.append("提示词")
    if base:
        changed.append("底座→" + base)
    if label:
        changed.append("展示名")
    return {"id": pid, "origin": "override" if is_generated else "local",
            "changed": changed}, None


def reset_persona(pid):
    """还原成 Modelfile 自动抽取的版本：删掉提示词覆盖，并清掉改过的底座/展示名。"""
    if not ID_RE.match(pid):
        return None, "id 不合法"
    reg = _read_json(PERSONAS_JSON, {})
    is_generated = any(p["id"] == pid for p in reg.get("personas", []))
    items = local_index()
    # 注意：自动抽取的人格**也可能**有记录（改了底座/展示名就会建一条），
    # 所以不能只看「有没有记录」就判定它是界面新建的（踩过）。
    if any(r.get("id") == pid for r in items) and not is_generated:
        return None, "这是界面上新建的人格，请用删除"
    p = os.path.join(PERSONAS_LOCAL, pid + ".txt")
    had_txt = os.path.exists(p)
    if had_txt:
        os.unlink(p)
    rest = [r for r in items if r.get("id") != pid]
    had_rec = len(rest) != len(items)
    if had_rec:
        _save_local_index(rest)
    if not had_txt and not had_rec:
        return None, "这个人格没有自定义版本"
    return {"id": pid, "origin": "generated"}, None


def delete_persona(pid):
    """只能删「界面上新建」的；自动抽取的请用还原。"""
    if not ID_RE.match(pid):
        return None, "id 不合法"
    reg = _read_json(PERSONAS_JSON, {})
    is_generated = any(p["id"] == pid for p in reg.get("personas", []))
    items = local_index()
    if is_generated or not any(r.get("id") == pid for r in items):
        return None, ("只能删除在界面上新建的人格。"
                      "由 Modelfile 自动抽取的人格请用「还原默认」。")
    p = os.path.join(PERSONAS_LOCAL, pid + ".txt")
    if os.path.exists(p):
        os.unlink(p)
    _save_local_index([r for r in items if r.get("id") != pid])
    return {"id": pid}, None


# --------------------------------------------------------------------------- 对话
def list_chats():
    ensure_dirs()
    out = []
    for f in os.listdir(CHATS_DIR):
        if not f.endswith(".json"):
            continue
        d = _read_json(os.path.join(CHATS_DIR, f), None)
        if not isinstance(d, dict):
            continue
        msgs = d.get("messages") or []
        out.append({
            "id": d.get("id") or f[:-5],
            "title": d.get("title") or "未命名对话",
            "model": d.get("model"),
            "updated": d.get("updated") or "",
            "count": len(msgs),
            "preview": next((m.get("content", "")[:60] for m in msgs
                             if m.get("role") == "user"), ""),
        })
    out.sort(key=lambda x: x.get("updated") or "", reverse=True)
    return out


def load_chat(cid):
    if not ID_RE.match(cid):
        return None, "id 不合法"
    d = _read_json(os.path.join(CHATS_DIR, cid + ".json"), None)
    if d is None:
        return None, "对话不存在"
    return d, None


def save_chat(cid, body):
    if not ID_RE.match(cid):
        return None, "id 不合法"
    msgs = body.get("messages") or []
    if not isinstance(msgs, list):
        return None, "messages 必须是数组"
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant", "system"):
            return None, "消息格式不对"
        if len(str(m.get("content") or "")) > MAX_MSG:
            return None, "单条消息太长了"
    ensure_dirs()
    rec = {
        "id": cid,
        "title": (body.get("title") or "未命名对话")[:120],
        "model": body.get("model"),
        "created": body.get("created") or time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "messages": [{"role": m.get("role"),
                      "content": m.get("content") or "",
                      "thinking": m.get("thinking") or ""} for m in msgs],
    }
    _write_json(os.path.join(CHATS_DIR, cid + ".json"), rec)
    return rec, None


def delete_chat(cid):
    if not ID_RE.match(cid):
        return None, "id 不合法"
    p = os.path.join(CHATS_DIR, cid + ".json")
    if not os.path.exists(p):
        return None, "对话不存在"
    os.unlink(p)
    return {"id": cid}, None


def new_chat_id():
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


# --------------------------------------------------------------------------- 静态页
CTYPES = {".html": "text/html; charset=utf-8",
          ".js": "application/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8",
          ".svg": "image/svg+xml", ".png": "image/png",
          ".ico": "image/x-icon", ".json": "application/json; charset=utf-8"}


def static_file(rel):
    """把 /ui/xxx 映射到 ui/xxx，并挡住路径穿越。"""
    rel = rel.lstrip("/")
    if not rel or rel.endswith("/"):
        rel += "index.html"
    full = os.path.normpath(os.path.join(UI_DIR, rel))
    # 必须带 os.sep：否则 ui 的**同前缀兄弟目录**（例如 ui-old）里的文件
    # 也能通过前缀判断。同项目 turtle_web / humanizer_web 就是带 sep 的写法。
    if not (full == UI_DIR or full.startswith(UI_DIR + os.sep)) or not os.path.isfile(full):
        return None, None
    ext = os.path.splitext(full)[1].lower()
    with open(full, "rb") as fh:
        return fh.read(), CTYPES.get(ext, "application/octet-stream")
