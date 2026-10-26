# llamacpp-ollama-stack

**用 llama.cpp 栈复刻 Ollama 体验 —— 桥 + 人格 + 8GB 显存工程实录。**

保留 Ollama 的客户端生态（Continue / Roo Code / n8n / 任何说 Ollama 协议的客户端一行代码都不用改），
把引擎换成 llama.cpp + llama-swap：你要的是 `-ngl`、KV 量化、`min_p`、`--chat-template` 这些
精细控制，以及**在 8GB 显存上真的能同时伺候多个模型**。

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

Ollama 把引擎和协议焊在一起。你能调的东西止步于 Modelfile 的 `PARAMETER`。
而 8GB 显存上，真正决定「能不能跑」的是这些：

| 你需要调的 | Ollama | 这里 |
|---|---|---|
| KV 缓存量化（`-ctk q8_0`，直接减半显存） | 不可控 | ✅ |
| 每模型独立的 `-c` / `-b` / `-ub` | 全局 | ✅ 每个模型单独写 |
| `min_p` / `top_n_sigma` 等采样器 | 部分 | ✅ 透传任意字段 |
| `--chat-template` / `--reasoning-format` | 不可控 | ✅ |
| 一次只驻留一个模型的**硬约束** | 默认允许多个并存 | ✅ |

### 2. 为什么不用 llama.cpp 原生 router mode？

llama.cpp 现在有 `llama serve` 的 router mode（自动发现 GGUF、按需加载、`--models-max` 超限 LRU 驱逐）。
它很好，但四个地方对不上：

1. **没有空闲 TTL 卸载** —— 只有 `--models-max N` 的容量驱逐，没有「闲着 30 分钟就让位」。
2. **只管 llama.cpp 自己的模型** —— 不能接 vLLM / sd.cpp / 任何 OpenAI 兼容后端。
3. **不做 Ollama 协议翻译** —— 只有 OpenAI 协议。
4. **8GB 卡上「一次只驻留一个」是硬需求**，不是可选优化。

### 3. 为什么不用已有的 Ollama 兼容桥？

`anantshri/llama-swap` 这个分叉确实**已经完整实现了 Ollama 端点翻译（含流式）** —— 这一点我承认，
它不是重复造轮子能解决的。但它：

- 是 **Go 项目的分叉**，功能靠 cherry-pick 上游，不跟踪 main
- README 自述「自用，不保证对别人可用」
- **没有「人格」这个概念**（对它的 changelog 全文检索 `persona` / `Modelfile`，0 命中）

而人格系统恰好是本仓库的核心之一。所以定位很清楚：**不要跟它比「有没有 Ollama 桥」，
要比「桥上面还长着什么」。**

---

## 三个人无我有的点

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

托盘还管：三态图标、开机自启（HKCU Run）、右键启停。

---

## 工具链（这部分别人真没有）

| 脚本 | 作用 |
|---|---|
| `vram_budget.py <gguf> --ctx 8192` | **不加载模型**，纯读 GGUF 元数据算 KV 与显存账。支持混合注意力（SWA 分层计算） |
| `smoke_model.py [模型]` | 新模型冒烟：真加载一次 + tok/s + 显存余量 + **思考泄漏检测** |
| `selftest.py` | 15 项端到端协议测试（Ollama + OpenAI 两套协议都验） |
| `gguf_probe.py <gguf>` | 纯 Python 读 GGUF 头，零依赖 |

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

### 4. 起服务

```bat
scripts\tray.vbs                 :: 双击启托盘（日常用这个）
python scripts\service_ctl.py start     :: 或者纯命令行
```

打开 <http://127.0.0.1:11434/ui>。

### 5. 加模型 / 加人格

- **加模型**：`.gguf` 放 `models/` → `config/llama-swap.yaml` 加一条 → 重启
- **加人格**：Modelfile 放 `config/modelfiles/` → `scripts\apply-personas.bat`；

---

## 常用命令

```bat
python scripts\service_ctl.py start|stop|restart|status [--json]
python scripts\service_ctl.py unload [模型名]          :: 卸模型释放显存（省略=全卸）
python scripts\service_ctl.py residency status         :: 谁占着显存、闲置多久
python scripts\service_ctl.py residency --set idle_ttl=900
python scripts\service_ctl.py autostart on|off|status

python scripts\residency.py get|set K=V|status|list|unload|reap

python scripts\vram_budget.py models\xxx.gguf --ctx 8192
python scripts\smoke_model.py [模型名]
python scripts\selftest.py                    :: 15 项端到端
```

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

MIT，见 [LICENSE](LICENSE)。llama.cpp / llama-swap 均为 MIT，但本仓库不分发它们的二进制。
