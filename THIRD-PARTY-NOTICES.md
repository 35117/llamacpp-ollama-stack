# 第三方组件

本仓库的代码以 [MIT](LICENSE) 发布。下面这些组件**不在**本仓库内，也不由本仓库分发 ——
运行时需要你自己从官方渠道获取，各自遵守它们自己的许可。

| 组件 | 许可 | 获取方式 |
|---|---|---|
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | MIT | 官方 releases 下载 CUDA 构建，解压到 `bin/` |
| [llama-swap](https://github.com/mostlygeek/llama-swap) | MIT | 官方 releases 下载，`llama-swap.exe` 放进 `bin/` |
| CUDA / cuBLAS | NVIDIA 专有许可 | 随 CUDA Toolkit / 显卡驱动获取，**不可再分发** |
| LLVM OpenMP | Apache-2.0 WITH LLVM-exception | 随上述二进制分发 |

## 注意

- `bin/` 目录**不入版本库**（`.gitignore` 已排除）。里面有几百 MB 的 CUDA 运行库
  （`cublasLt64_13.dll` / `ggml-cuda.dll`），体积和许可义务都不适合放进仓库。
- 模型权重（`models/*.gguf`）同样不入库。**各模型的许可由各自的发布者决定**，
  不少权重带非商用或署名条款，用前请自己核对。
- 本仓库的示例人格（`config/personas.example/`）是原创文本，随 MIT 一起发布。
  你自己放进去的人格是否可分发，取决于你写的内容和你参照的来源。
