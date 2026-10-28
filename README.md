# llamacpp-ollama-stack

**用 llama.cpp 栈复刻 Ollama 体验 —— 桥 + 人格 + 8GB 显存工程实录。**

保留 Ollama 的客户端生态（Continue / Roo Code / n8n / 任何说 Ollama 协议的客户端一行代码都不用改），
把引擎换成 llama.cpp + llama-swap：换来 `-ngl`、KV 量化、`min_p`、`--chat-template` 这些
精细控制，以及在 8GB 显存上按需轮换多个模型 —— 一次只驻留一个，闲置到点就让位。

```
客户端 (Ollama / OpenAI 协议)
        │
        ▼  127.0.0.1:11434
┌───────────────────────────────────────┐
│  ollama_bridge.py  （纯标准库，1.4k 行）│  ← 本仓库的主角
│  /api/chat  /api/generate  /api/tags   │
│  /api/show  /api/ps  /api/embed        │
│  /v1/* 透传 · /ui · /admin/*           │
│  ├─ 人格注入（同权重多个人格，零重载）  │
│  └─ 驻留管理（闲置自动卸载）            │
└───────────────────────────────────────┘
        │  127.0.0.1:11435
        ▼
┌───────────────────────────────────────┐
│  llama-swap（Go，单二进制）             │  按 model 字段拉起/换出后端进程
└───────────────────────────────────────┘
        │  11500+
        ▼
   llama-server × N（一次驻留一个）
```

---

## 为什么不直接用现成的？

这是这个仓库必须回答的第一个问题。三个候选，三条不同的理由。

### 1. 为什么不用 Ollama？

先说清楚：Ollama 能调的东西比我最初以为的多 —— Modelfile 里可以 `PARAMETER num_ctx` /
`num_gpu` / `min_p`，服务端能用 `OLLAMA_KV_CACHE_TYPE` 量化 KV、用
`OLLAMA_MAX_LOADED_MODELS` 限制驻留数、用 `OLLAMA_KEEP_ALIVE` 控制卸载。
它不是「不可控」，而是**控制粒度停在服务级和建模型时**：

| 维度 | Ollama | 这里 |
|---|---|---|
| KV 量化 | 服务级环境变量，全机统一一档 | 每个模型单独写 `-ctk/-ctv`，A 模型 q8_0、B 模型 f16 可以共存 |
| 上下文 / 层卸载 | Modelfile `PARAMETER`，固化进模型，改要重建 | yaml 里每个模型写 `-c` / `-ngl`，改完重启即生效 |
| 采样参数 | Modelfile 固化 + 请求 `options` 里的一部分 | `/v1/*` 纯透传，llama.cpp 支持什么就能传什么 |
| 引擎版本 | 跟着 Ollama 的发布节奏走，内部打包的 llama.cpp 由它决定 | `bin/` 里放哪个构建就是哪个，新模型支持自己决定 |
| 后端 | 只有它自带的那套引擎 | llama-swap 后面接什么都行（vLLM / sd.cpp / …） |
| 空闲卸载 | `OLLAMA_KEEP_ALIVE` 全局一档 | 每模型 TTL + `protected` 名单 + 剩余时间可查 |

一句话：我要的是**每模型独立**的显存参数，以及能自己决定引擎版本。

> ⚠️ Ollama 迭代很快，上面是我 2026-10 照文档核对的。如果你发现哪条已经不准，
> 开 issue，我改。

### 2. 为什么不用 llama.cpp 原生 router mode？

llama.cpp 现在有 `llama serve` 的 router mode（自动发现 GGUF、按需加载、`--models-max` 超限 LRU 驱逐）。
它很好，但四个地方对不上：

1. **没有空闲 TTL 卸载** —— 只有 `--models-max N` 的容量驱逐，没有「闲着 30 分钟就让位」。
   （这条是我照 router 的 4 个 flag 推的，没看到时间维度；如果你手上的版本已经加了，那这条作废。）
2. **只管 llama.cpp 自己的模型** —— 不能接 vLLM / sd.cpp / 任何 OpenAI 兼容后端。
3. **不做 Ollama 协议翻译** —— 只有 OpenAI 协议。
4. **8GB 卡上「一次只驻留一个」是硬需求**，不是可选优化。

### 3. 为什么不用已有的 Ollama 兼容桥？

`anantshri/llama-swap` 这个分叉确实**已经完整实现了 Ollama 端点翻译（含流式）** —— 这一点我承认，
它不是重复造轮子能解决的。但它：

- 是 **Go 项目的分叉**，功能靠 cherry-pick 上游，不跟踪 main
- README 自述「自用，不保证对别人可用」
- **没有「人格」这个概念**（对它的 changelog 全文检索 `persona` / `Modelfile`，0 命中）

而人格系统恰好是本仓库的核心之一。所以这个仓库不比「有没有 Ollama 桥」—— 那一层别人
已经做完了；比的是「桥上还长着什么」。

---

## 三个核心设计

这三块是这个仓库真正花时间的地方 —— 也是我自己踩过坑、才想清楚该怎么做的地方。
同类项目各覆盖了多少，我列在文末「与同类项目的关系」，那一节比这一节更能说明
这个仓库的位置。

### 一、人格系统：同一份权重，多个人格，切换零重载

Ollama 的 `Modelfile` 用 `FROM` + `SYSTEM` 把人格**烙进模型**，改底座要重新 `ollama create`
（会覆盖同名 tag 的权重和提示词 —— 这个坑踩过，丢过一次模型）。

这里的做法是**运行时注入**：

```
config/modelfiles/<名字>            ← 你写的 Modelfile（原始出处）
        │  scripts/apply-personas.bat
        ▼
config/personas.json + config/personas/<id>.txt   ← 生成物
        │
        ▼  界面里改的覆盖层（优先，永不被生成覆盖）
config/personas-local/index.json
```

- 人格 id 对外就是一个模型名，客户端照常 `model: "eri"` 调用
- **界面里可以改人格的底座模型**，还支持「同底座的其他人格一起改过去」（批量只发 base，绝不带 system）
- 新增/编辑人格在 Web 界面完成，不用碰文件

### 二、显存驻留管理：闲置自动卸载 + 精确倒计时

llama-swap 默认 `globalTTL: 0` = **永不自动卸载**。一个 5–6GB 模型跑过一次就一直占着，
8GB 卡上直接决定别的模型能不能加载。

`config/residency.json` 一份策略，热生效：

```json
{
  "auto_unload": true,
  "idle_ttl": 1800,
  "per_model_ttl": { "humanizer-12b": 600 },
  "protected": ["qwen35-4b-heretic"]
}
```

- `protected` = 永不自动卸载（仍可手动卸）—— 放最常用的聊天模型，免得每次开口等 10 秒重载
- **能查「还有多久被卸」**，不是瞎等：`resident[].idle_sec` / `unload_in_sec`
- 手动卸载：`service_ctl.py unload [名字]`、界面左侧栏面板、`/admin/residency`

> **为什么在自己这层做 reaper，而不是直接配 llama-swap 的 `globalTTL`？**
> 因为要热改、要倒计时可见、要不依赖 llama-swap 的具体版本。
> 代价是 `llama-swap.yaml` 的 `globalTTL` 必须保持 `0`，否则两套机制会打架
> （llama-swap 可能卸掉你想 protected 的那个）。

### 三、Windows 托盘：解决「脚本起的服务会被按进程树回收」

这个坑很具体：从脚本/CI/agent 里 `Popen` 出来的服务，会在**那条命令结束时被按进程树清理**。
`CREATE_BREAKAWAY_FROM_JOB` 救不了（实测 `_in_job()` 是 False，它对付不了按树清理）。

解法是**让托盘做服务的父进程**：`service_ctl.py` 检测到托盘在跑，就通过 `PostMessage(WM_COMMAND)`
把启停转交给它。于是服务挂在用户自己的进程下，不受任何命令生命周期影响。

托盘还管：四态图标（绿=正常、黄=半开、灰=已停、蓝=启动中）、开机自启（HKCU Run）、右键启停。

---

## 工具链

| 脚本 | 作用 |
|---|---|
| `vram_budget.py <gguf> --ctx 8192` | **不加载模型**，纯读 GGUF 元数据算 KV 与显存账。支持混合注意力（SWA 分层计算） |
| `smoke_model.py [模型]` | 新模型冒烟：真加载一次 + tok/s + 显存余量 + **思考泄漏检测** |
| `selftest.py` | 15 项端到端协议测试（Ollama + OpenAI 两套协议都验） |
| `gguf_probe.py <gguf>` | 纯 Python 读 GGUF 头，零依赖 |

> 这些工具和 llama.cpp 自带的 `--fit` / `llama-fit-params`、以及一些客户端的显存估算
> 有重叠，用途不完全一样：`vram_budget.py` 是**不加载模型**就先算账（纯读 GGUF 元数据），
> 用来回答「这个量化档在这张卡上塞不塞得下、ctx 能开多大」，而不是「怎么把某个模型
> 塞进当前空闲显存」。它处理混合注意力（SWA）时按层分别算 KV —— 这一点是我估出
> 246GB 那种离谱数字之后才改对的。

`vram_budget.py` 为什么重要 —— 一个真实的翻车案例：

> `GalTransl-7B-v2.6_Q6_k` 看着只比 Q4 大一点点，但它是**全 MHA 无 GQA** 架构
> （`kv_heads=32, head_dim=128`），每 token 的 KV 是同尺寸 GQA 模型的 4 倍。
> 权重 5.91 GiB + 8K 上下文 KV 2.00 GiB = **7.91 GiB**，8G 卡装不下。
> 而且 `-ngl 99` 是显式写的，`--fit` 不会帮你缩层 —— 结果就是加载失败。
> 压到 4K 后 6.91 GiB，实测正常。

**8GB 卡上的实测账**（q8_0 KV，含权重）：

| 模型 | 权重 | ctx | KV | 合计 | 判定 |
|---|---|---|---|---|---|
| `qwen35-9b-heretic` | 5.24 | 16384 | 1.00 | 6.24 | 安全 |
| `galtransl-7b` | 5.91 | 4096 | 1.00 | 6.91 | 紧 |
| `humanizer-12b` | 5.20 | 8192 | 0.22 | 5.42 | 安全（SWA 混合注意力，KV 极省） |

---

## 快速开始

### 0. 前置

- Windows 10/11（托盘是 Win32 实现；桥本身跨平台）
- Python **3.11+**（托盘需要 `pywin32`）
- 一块能跑得动你模型的 N 卡

### 1. 拿二进制

从 [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases) 下载 CUDA 构建，
解压到 `bin/`（需要 `llama-server.exe`）；
再从 [llama-swap releases](https://github.com/mostlygeek/llama-swap/releases) 放 `llama-swap.exe` 进去。

> 本仓库**不分发**任何二进制。`bin/` 里有 492MB 的 `cublasLt64_13.dll`，
> 且 CUDA 属于 NVIDIA 专有许可，必须你自己获取。

### 2. 放模型

把 `.gguf` 放进 `models/`。

### 3. 生成配置

```bat
python scripts\init_config.py
```

它会把模板里的占位路径替换成本机实际路径，并列出 `models/` 里发现但未在配置中声明的 `.gguf`。

同时会做两件首次初始化的事（都已存在则跳过）：从示例生成 `config/personas.json`
（**`bases` 按 yaml 里的模型清单自动生成**，所以 yaml 声明了什么，桥就认识什么），
以及生成 `config/residency.json`。

### 4. 起服务

```bat
scripts\tray.vbs                 :: 双击启托盘（日常用这个）
python scripts\service_ctl.py start     :: 或者纯命令行
```

打开 <http://127.0.0.1:11434/ui>。

### 5. 加模型 / 加人格

- **加模型**：`.gguf` 放 `models/` → `config/llama-swap.yaml` 加一条 → `python scripts\init_config.py --sync-bases` → 重启
- **加人格**：Modelfile 放 `config/modelfiles/` → `scripts\apply-personas.bat`；

> `--sync-bases` 为什么必须跑：桥对外暴露哪些模型名，取决于 `config/personas.json` 的
> `bases`（llama-swap 那边只认 yaml）。只改 yaml 的话，客户端调用会报「模型不存在」。
> 这条命令**只增不改不删** —— 你手工调过的 `ctx`、别名、`supported` 都不会被覆盖，
> 且会先备份。纯内部用的辅助模型（例如只走 `/completion` 的去 AI 味改写器）可以不登记，
> 桥就不会把它当成对外模型暴露。

---

## 常用命令

```bat
python scripts\service_ctl.py start|stop|restart|status [--json]
python scripts\service_ctl.py unload [模型名]          :: 卸模型释放显存（省略=全卸）
python scripts\service_ctl.py residency status         :: 谁占着显存、闲置多久
python scripts\service_ctl.py residency --set idle_ttl=900
python scripts\service_ctl.py autostart on|off|status

python scripts\residency.py get|set K=V|status|list|unload|reap

python scripts\init_config.py              :: 生成 llama-swap 配置（首次初始化）
python scripts\init_config.py --check      :: 只体检：悬空引用、底座是否对齐
python scripts\init_config.py --sync-bases :: 把 yaml 新加的模型补进桥（只增不改）

python scripts\vram_budget.py models\xxx.gguf --ctx 8192
python scripts\smoke_model.py [模型名]
python scripts\selftest.py                    :: 15 项端到端
```

---

## 与同类项目的关系

发布前（2026-10）查过一轮同类项目，只列我认为最相关的几个。**这类信息会过时**，
如果你知道更贴近的，欢迎开 issue 指出来，我改文档。

| 项目 | 与本仓库重叠的部分 |
|---|---|
| [mostlygeek/llama-swap](https://github.com/mostlygeek/llama-swap) | 多模型路由、按 model 字段热换后端、`ttl` 自动卸载、`POST /api/models/unload/{名字}`。本仓库直接用它的二进制，不重复实现 |
| [anantshri/llama-swap](https://github.com/anantshri/llama-swap) | 已经实现了 Ollama 端点翻译（含流式）。差别是本仓库不 fork 那个 Go 项目，而是把人存放桥这一层，并且带 15 项协议自测 |
| llama.cpp 原生 router mode | 按需加载、`--models-max` 的 LRU 驱逐、自带 WebUI、`--fit` 自动拟合显存。差别见上文第 2 节四条 |
| [ggml-org/Llama-Windows](https://github.com/ggml-org/Llama-Windows) | Windows 托盘、开机自启、通知。本仓库的托盘多做的一件事是**作为服务父进程**，绕开「脚本起的进程会被按进程树回收」 |
| [ollama/ollama](https://github.com/ollama/ollama) | `/api/*` 协议本身。本仓库是**消费**这个协议，不是替代它的引擎 |

说清楚一点：如果哪天上游把「人格 + 空闲 TTL 卸载」也做进 router mode，这个仓库的存在
意义就会变小。这是个自用的工程记录，不是要跟谁抢地盘。

---

## 已知限制

- **托盘是 Windows 专用。** 桥、`service_ctl`、`residency` 本身跨平台，托盘那部分不跨。
- **一次只驻留一个模型。** 这是 8GB 卡的取舍，不是 bug。要并发请用 llama-swap 的 `groups`。
- **人格不会「烙进」权重。** 换底座后人格立刻跟着走，这是设计意图；但如果你需要导出一个
  独立模型文件给别的机器，那得自己 `ollama create`。
- **`config/personas.json` 是生成物。** 手改会被 `apply-personas.bat` 覆盖 —— 要持久化请改
  `config/personas-local/`（界面会自动写这里）。
- **不提供鉴权。** 桥默认只监听 `127.0.0.1`。改成 `0.0.0.0` 前请自己想清楚 ——
  局域网内谁都能驱动你的 GPU、读写你的人格和会话。

## 测试

```
python scripts\selftest.py            15 项  协议端到端
python scripts\test_ui_backend.py     27 项  界面后端（含路径穿越防护）
python scripts\test_bridge_errors.py  10 项  响应状态机（mock 上游，不需要 GPU）
python scripts\test_residency.py      11 项  卸载与闲置判定（mock llama-swap）
python scripts\test_lifecycle_live.py 真实启停服务（需要模型与显存）
```

前四套不需要 GPU 或权重，可以直接在 CI 跑。

## 目录

```
scripts/    桥、服务控制、驻留管理、托盘、工具链
config/     配置模板与人格（真实人格与路径不入库）
ui/         单文件聊天界面（零依赖）
humanizer/  示例模块：去 AI 味改写（纯 completion 模型，挂 /humanize）
turtle/     示例模块：海龟汤（本地 LLM 判题，挂 /turtle）
```

## License

本仓库代码以 MIT 发布，见 [LICENSE](LICENSE)。

第三方组件（llama.cpp / llama-swap / CUDA / LLVM OpenMP）的许可与获取方式见
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) —— 本仓库不分发它们的二进制，
也不分发任何模型权重。
