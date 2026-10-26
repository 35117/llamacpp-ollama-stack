# -*- coding: utf-8 -*-
"""算显存账：给定 GGUF 的架构参数，算不同量化档 + 不同上下文能不能全量入显存。

用项目自己的 gguf_probe 读元数据，不重写解析器。

为什么要它（8G 卡的真实痛点）：
  选量化档时最容易拍脑袋。一个 7B 的 Q6_K 权重 5.9G，看着「比 Q4 只大一点点」，
  但如果它是**全 MHA 无 GQA** 的架构，每 token 的 KV 是同尺寸 GQA 模型的 4 倍
  —— 8K 上下文光 KV 就要 2G，加上权重直接超出显卡容量，结果是加载失败或静默
  回退 CPU。所以选档之前先跑这个，把账算清楚。

用法：
    python scripts\\vram_budget.py [gguf] [--vram 8] [--ctx 8192]
    退出码：0 = 算完了；1 = 元数据不全；2 = 在给定 ctx 下超出预算
"""
import argparse
import os
import struct
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)

import gguf_probe as GP  # noqa: E402


def kv_meta(path):
    """返回 {key: value} 的完整 KV 表。"""
    with open(path, "rb") as f:
        if f.read(4) != GP.GGUF_MAGIC:
            raise SystemExit("不是 GGUF")
        ver, = struct.unpack("<I", f.read(4))
        n_tensors, = struct.unpack("<Q", f.read(8))
        n_kv, = struct.unpack("<Q", f.read(8))

        def rstr():
            n, = struct.unpack("<Q", f.read(8))
            return f.read(n).decode("utf-8", "replace")

        out = {}
        for _ in range(n_kv):
            k = rstr()
            t, = struct.unpack("<I", f.read(4))
            fmt, size = GP.SCALAR[t]
            if t == 8:                      # STRING
                v = rstr()
            elif t == 9:                    # ARRAY
                at, = struct.unpack("<I", f.read(4))
                n, = struct.unpack("<Q", f.read(8))
                afmt, asize = GP.SCALAR[at]
                v = [struct.unpack(afmt, f.read(asize))[0] for _ in range(n)] if at != 8 \
                    else [rstr() for _ in range(n)]
                del at, n, afmt, asize
            else:
                v = struct.unpack(fmt, f.read(size))[0]
            out[k] = v
        return ver, out


def kv_terms(kv, arch):
    """返回 (global_terms, swa_terms, swa_window)。

    terms = Σ(每层 kv_heads × 该层 key 维度)，单位「元素/token」。

    ⚠ 混合注意力（gemma4 这类）必须分开算：SWA 层只保留最近 `window` 个
    token，它的占用是**常数**，不随 ctx 线性增长。早先版本把所有层的 kv_heads
    直接求和再乘 ctx，8K 上下文估出 246GB —— 差了三个数量级，据此做的选档
    决策全是错的。
    """
    def g(k, d=None):
        return kv.get(k.format(a=arch), d)

    L = g("{a}.block_count") or 0
    nh = g("{a}.attention.head_count")
    nhkv = g("{a}.attention.head_count_kv")
    emb = g("{a}.embedding_length")
    kl = g("{a}.attention.key_length") or (emb // nh if nh and emb else 0)
    pat = g("{a}.attention.sliding_window_pattern")
    window = g("{a}.attention.sliding_window") or 0

    # 逐层数组：按 sliding_window_pattern 拆成全局层与 SWA 层两组
    if isinstance(nhkv, (list, tuple)) and pat and len(nhkv) == L and len(pat) == L:
        kls = g("{a}.attention.key_length_swa") or kl
        glob = sum(h * kl for h, p in zip(nhkv, pat) if not p)
        swa = sum(h * kls for h, p in zip(nhkv, pat) if p)
        return glob, swa, window

    # 标量：没有 SWA 概念
    if not nhkv and nh:
        nhkv = nh          # 纯 MHA：kv_heads 等于 head_count
    return (L or 0) * (nhkv or 0) * kl, 0, 0


def kv_bytes(glob_terms, swa_terms, window, ctx, per_elem=2.0):
    """KV 缓存字节数。per_elem 是每个元素的字节数：f16=2, q8_0=1, q4_0=0.5。"""
    return (glob_terms * ctx + swa_terms * min(ctx, window)) * 2 * per_elem


# (显示名, 每元素字节数)
QUANTS = (("f16", 2.0), ("q8_0", 1.0), ("q4_0", 0.5))

# CUDA 上下文 + compute buffer + logits 的开销。8G 卡实测约 0.9 GiB。
OVERHEAD_GIB = 0.9


def main():
    ap = argparse.ArgumentParser(description="算 GGUF 的 KV 缓存与显存账")
    ap.add_argument("gguf", nargs="?", default=None)
    ap.add_argument("--vram", type=float, default=8.0,
                    help="显卡可用显存 GiB，默认 8")
    ap.add_argument("--ctx", type=int, default=None,
                    help="重点核算的上下文长度")
    args = ap.parse_args()

    path = args.gguf or os.path.join(ROOT, "models",
                                     "Qwen3.5-9B-heretic-v2-Q4_K_M.gguf")
    ver, kv = kv_meta(path)
    arch = kv.get("general.architecture", "")

    def g(k, d=None):
        return kv.get(k.format(a=arch), d)

    L = g("{a}.block_count")
    nh = g("{a}.attention.head_count")
    nhkv = g("{a}.attention.head_count_kv")
    emb = g("{a}.embedding_length")
    hd = g("{a}.attention.key_length") or (emb // nh if nh and emb else None)

    shown_kv = ("逐层数组(%d 层)" % len(nhkv)) if isinstance(nhkv, (list, tuple)) else nhkv
    print(f"  {os.path.basename(path)}")
    print(f"  arch={arch}  L={L}  heads={nh}  kv_heads={shown_kv}  head_dim={hd}  "
          f"训练上下文={g('{a}.context_length')}")

    glob, swa, window = kv_terms(kv, arch)
    if not glob and not swa:
        print("  元数据不全，算不了")
        return 1

    if swa:
        n_swa = sum(1 for p in (g("{a}.attention.sliding_window_pattern") or []) if p)
        print(f"  ⚠ 混合注意力：{L - n_swa} 层全局 + {n_swa} 层 SWA"
              f"（窗口 {window}，K/V 维度 {g('{a}.attention.key_length_swa')}）"
              f" —— SWA 部分占用恒定，不随上下文增长")

    wbytes = os.path.getsize(path)

    print()
    print("  各上下文下的 KV 占用 / 含权重的总量（GiB）：")
    print("        ctx" + "".join(("%14s" % (n + " / 总量")) for n, _ in QUANTS))
    for c in (4096, 8192, 12288, 16384, 24576, 32768):
        cells = []
        for _, pe in QUANTS:
            kvb = kv_bytes(glob, swa, window, c, pe)
            cells.append("%6.2f %6.2f" % (kvb / 2**30, (kvb + wbytes) / 2**30))
        print(f"    {c:>7}  " + "  ".join(cells))
    print(f"  （权重按文件大小算 {wbytes / 2**30:.2f} GiB）")

    if args.ctx:
        kvb = kv_bytes(glob, swa, window, args.ctx, 1.0)      # 按 q8_0 核算
        total = (kvb + wbytes) / 2**30
        print()
        print(f"  重点核算：ctx={args.ctx} @ q8_0")
        print(f"    权重 {wbytes / 2**30:.2f} + KV {kvb / 2**30:.2f} = {total:.2f} GiB"
              f" / 可用 {args.vram:.2f} GiB（余量按 {OVERHEAD_GIB:.1f} GiB 计）")
        if total + OVERHEAD_GIB > args.vram:
            print("    ⚠ 超出预算 —— 要么降 ctx，要么换更小的量化档")
            return 2
        print("    ✓ 放得下")
    return 0


if __name__ == "__main__":
    sys.exit(main())
