# 示例：给桥加一个自定义模块（`/humanize` 去 AI 味改写）

这是本项目里唯一一个「模块化扩展」的完整实例，留着给人照抄用 ——
它演示了**不新增进程、不新增端口**，只在现有桥上挂一个前缀，就能得到
「页面 + JSON API + 流式接口」三件套。

> **本仓库默认不启用它。** 代码是完整的，但 5.59 GB 的权重没有随仓库分发，
> 所以克隆下来直接跑，`/humanize` 是 **404**（桥根本没 import 这个模块）。
> 想启用见文末「启用步骤」。

## 文件去向

| 本目录的文件 | 放回哪里 | 作用 |
|---|---|---|
| `humanizer_core.py` | `humanizer/` | 业务逻辑（纯标准库，不依赖桥） |
| `index.html` | `humanizer/` | 单文件页面，零依赖 |
| `test_humanizer.py` | `humanizer/` | 自检，27 项（`--live` 连真实服务） |
| `humanizer_web.py` | **`scripts/`** | 桥的插件：静态文件 + API 分发 |

⚠️ `humanizer_web.py` 必须放 `scripts/`：它按 `dirname(dirname(__file__))` 推算项目根，
再找 `humanizer/` 与 `models/`，同时 `import swap_ctl`（同目录）。放在这个示例目录里
是跑不起来的 —— 这里是存档，不是可执行位置。

## 接入桥的 5 个改动点

全部在 `scripts/ollama_bridge.py`。这套写法可以直接套到任何新模块上，
把 `humanize` 换成你的前缀、`humanizer_web` 换成你的模块名即可。

**1）导入（失败也不能让桥起不来）**

```python
try:
    import humanizer_web  # noqa: E402  （去 AI 味重写，挂在 /humanize 下）
except Exception:         # noqa: BLE001  —— 缺了只影响这一个功能
    humanizer_web = None
```

**2）声明前缀常量**

```python
# 「去 AI 味」重写的挂载前缀（实现在 scripts/humanizer_web.py）
HUMANIZE_PREFIX = "/humanize"
```

**3）让它进管理路由白名单**（决定请求会不会被送进 `_handle_admin`）

```python
def _is_admin_route(self, path):
    return path in ("/ui", "/ui/") or path.startswith("/ui/") \
        or path.startswith("/admin/") \
        or path == TURTLE_PREFIX or path.startswith(TURTLE_PREFIX + "/") \
        or path == HUMANIZE_PREFIX or path.startswith(HUMANIZE_PREFIX + "/")
```

**4）在 `_handle_admin` 里分派**

```python
        # ---------- 去 AI 味（挂在 /humanize，实现在 scripts/humanizer_web.py）
        if path == HUMANIZE_PREFIX or path.startswith(HUMANIZE_PREFIX + "/"):
            return self._handle_humanize(method, path, body)
```

**5）实现转发方法**

```python
    def _handle_humanize(self, method, path, body):
        """把 /humanize 与 /humanize/api/* 交给 scripts/humanizer_web.py。

        模型请求由 humanizer_web 直接发往 **llama-swap 上游（11435）**，
        不走桥自己 —— 否则桥会递归调用自己。

        `/humanize/api/stream` 是流式（ndjson）：长文逐块回传进度，
        否则用户要对着空白屏幕等一两分钟。
        """
        if humanizer_web is None:
            return self._err(
                "「去 AI 味」模块未加载（scripts/humanizer_web.py 缺失或导入失败）",
                status=503)
        base = self.upstream.base if hasattr(self.upstream, "base") else None
        name = humanizer_web.api_name(path)
        if name is None:
            rel = path[len(HUMANIZE_PREFIX):]
            data, ctype = humanizer_web.static_file(rel)
            if data is None:
                return self._err("「去 AI 味」页面不存在（应为 humanizer/index.html）",
                                 status=404)
            return self._send_bytes(data, ctype)

        # 流式：逐块把进度写出去
        if name == "stream" and method == "POST":
            self._begin_ndjson()
            try:
                for ev in humanizer_web.stream_run(body or {}, base=base):
                    self._ndjson(ev)
            except Exception as exc:  # noqa: BLE001
                import traceback
                sys.stderr.write(traceback.format_exc())
                self._ndjson({"type": "error", "error": f"服务端异常: {exc}"})
            finally:
                self._end_chunked()
            return

        status, payload = humanizer_web.handle_api(method, name, body, base=base)
        return self._send_json(payload, status)
```

启动横幅加一行（可选，纯提示）：

```python
    if humanizer_web is not None:
        sys.stderr.write(f"  去 AI 味（重写）     http://{host}:{port}/humanize\n")
```

改完**必须重启桥** —— 桥只在启动时 import 一次模块。

## 这个范例里值得抄的几个点

- **零进程**：模块就是一个「路由表 + 静态文件目录」，没有独立端口要管。
- **流式用 ndjson**：`/humanize/api/stream` 逐块回传，长文不用盯着空白页；
  复用桥里已有的 `_begin_ndjson` / `_ndjson` / `_end_chunked`，不要另写一套。
- **不发请求给桥自己**：插件把模型请求打到 **llama-swap 上游（11435）**，
  写回 11434 会形成递归。
- **一个模块挂了不影响桥**：`try/except` 包住 import，路由里再判 `is None`。
- **静态文件必须做前缀校验**：`static_file()` 里用 `os.path.normpath` +
  带 `os.sep` 的前缀判断，防止 `../` 逃逸（同 `bridge_admin.py` 的写法）。

## 启用步骤（本机要真用的话）

1. 按上表把 4 个文件放回 `humanizer/` 与 `scripts/`；
2. 按下表把权重放到 `models/humanizer-12b-Q3-QAT.gguf`
   （HuggingFace 上搜 `humanizer 12b Q3 QAT GGUF`，约 5.6 GB）；
3. 在 `config/llama-swap.yaml` 加一条（**它是纯 completion 模型，必须显式给
   `ttl` 让它闲时让位**）：

```yaml
  "humanizer-12b":
    name: "Humanizer 12B（去 AI 味）"
    description: "把 AI 稿子改写成「人写的」· 事实/数字保真 · 纯 completion 模型"
    ttl: 600
    cmd: |
      {{ROOT}}/bin/llama-server.exe
      --host 127.0.0.1 --port ${PORT}
      -m {{ROOT}}/models/humanizer-12b-Q3-QAT.gguf
      -ngl 99 -c 8192 -fa on -ctk q8_0 -ctv q8_0
      -np 1 -b 2048 -ub 512
      --metrics
    capabilities:
      context: 8192
```

4. 重启服务 → 访问 `http://127.0.0.1:11434/humanize`。

## 踩过的坑（值钱的部分）

- **它是 completion 模型，不是聊天模型**。只有 `/completion` + 字节级精确 prompt
  才有正确输出；走 `/v1/chat/completions` 会被套上通用模板，直接回
  「Sure, here is...」。prompt 拼接与 sha256 校验在 `humanizer_core.py` 里。
- **Q3-QAT + temp 1.0 + 无 repeat_penalty 会偶发退化成 4 倍长胡编**
  （编出整张配置表 + 假 IP）。所以判据里 `too_long ≥ 2.6 倍` 必须算硬伤。
- **`cjk_ratio` 的分母**只算「汉字 + 含字母的西文词」，不是非空白字符总数；
  按后者算会把夹杂代码的段落误判成语言漂移。
- **定向卸载别用 `/unload?model=名字`** —— 参数被静默忽略，会把所有模型一起卸掉；
  只能走 `POST /api/models/unload/{名字}`。`test_humanizer.py --live` 里锁了这条。
- **显存**：5.59 GB 权重在 8 GB 卡上会和常驻聊天模型抢，本项目最终就因为它
  占显存且是低频工具，把整个功能下线了（权重、代码、路由一起删）。这条也是
  这个示例留在 `examples/` 而不在运行路径上的原因。
