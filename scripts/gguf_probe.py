#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯 Python GGUF 头解析器（无第三方依赖）
用途：校验 Ollama blob 是否为完整可用的 GGUF，并读出架构/量化/上下文长度。
"""
import json
import os
import struct
import sys

GGUF_MAGIC = b"GGUF"

# value_type -> (struct fmt, size)
SCALAR = {
    0: ("<B", 1),   # UINT8
    1: ("<b", 1),   # INT8
    2: ("<H", 2),   # UINT16
    3: ("<h", 2),   # INT16
    4: ("<I", 4),   # UINT32
    5: ("<i", 4),   # INT32
    6: ("<f", 4),   # FLOAT32
    7: ("<?", 1),   # BOOL
    8: (None, 0),   # STRING
    9: (None, 0),   # ARRAY
    10: ("<Q", 8),  # UINT64
    11: ("<q", 8),  # INT64
    12: ("<d", 8),  # FLOAT64
}


class Reader:
    def __init__(self, fh):
        self.fh = fh

    def _read(self, n):
        b = self.fh.read(n)
        if len(b) != n:
            raise EOFError("unexpected EOF")
        return b

    def u32(self):
        return struct.unpack("<I", self._read(4))[0]

    def u64(self):
        return struct.unpack("<Q", self._read(8))[0]

    def string(self):
        n = self.u64()
        return self._read(n).decode("utf-8", errors="replace")

    def value(self, t):
        if t == 8:
            return self.string()
        if t == 9:
            et = self.u32()
            n = self.u64()
            # 字符串数组可能非常大（词表），只取前若干项做展示
            if et == 8:
                out = []
                for i in range(n):
                    s = self.string()
                    if i < 8:
                        out.append(s)
                return {"__array_of_string__": n, "sample": out}
            fmt, size = SCALAR[et]
            out = []
            for i in range(n):
                if i < 8:
                    out.append(struct.unpack(fmt, self._read(size))[0])
                else:
                    self.fh.seek(size, os.SEEK_CUR)
            return {"__array__": n, "sample": out}
        fmt, size = SCALAR[t]
        return struct.unpack(fmt, self._read(size))[0]


def probe(path, verbose=False):
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        magic = fh.read(4)
        if magic != GGUF_MAGIC:
            return {"file": path, "ok": False, "error": f"not GGUF, magic={magic!r}"}
        r = Reader(fh)
        version = r.u32()
        n_tensors = r.u64()
        n_kv = r.u64()

        kv = {}
        for _ in range(n_kv):
            k = r.string()
            t = r.u32()
            kv[k] = r.value(t)

        tensors = []
        for _ in range(n_tensors):
            tname = r.string()
            nd = r.u32()
            dims = [r.u64() for _ in range(nd)]
            ttype = r.u32()
            offset = r.u64()
            tensors.append({"name": tname, "dims": dims, "type": ttype, "offset": offset})
        data_start = fh.tell()

    arch = kv.get("general.architecture")
    # 参数量：所有张量元素数的总和 —— 准确值，不用经验公式估
    n_params = 0
    for t in tensors:
        n = 1
        for d in t["dims"]:
            n *= d
        n_params += n

    res = {
        "file": path,
        "ok": True,
        "size": size,
        "gguf_version": version,
        "n_tensors": n_tensors,
        "n_kv": n_kv,
        "n_params": n_params,
        "arch": arch,
        "name": kv.get("general.name"),
        "quant": kv.get("general.file_type"),
        "data_start": data_start,
        "tensor_data_bytes": size - data_start,
        "kv": kv,
        "tensors": tensors,
    }
    if arch:
        for suffix in ("context_length", "embedding_length", "block_count", "attention.head_count"):
            key = f"{arch}.{suffix}"
            if key in kv:
                res[key.split(".")[-1]] = kv[key]
    # 一致性校验：张量占据的最大范围 + 数据段起点 vs 文件大小
    if tensors:
        max_end = 0
        for t in tensors:
            # 用最后一维行长度粗估上限不现实，这里只校验 offset 是否落在数据段内
            if t["offset"] >= res["tensor_data_bytes"]:
                res["warn"] = f"tensor {t['name']} offset {t['offset']} beyond data section"
        res["max_tensor_offset"] = max(max_end, max(t["offset"] for t in tensors))
    if "tokenizer.ggml.tokens" in kv and isinstance(kv["tokenizer.ggml.tokens"], dict):
        res["vocab_size"] = kv["tokenizer.ggml.tokens"]["__array_of_string__"]
    for probe_key in ("general.quantization_version", "split.count", "split.no"):
        if probe_key in kv:
            res[probe_key] = kv[probe_key]
    if verbose:
        res["full_kv"] = {k: (v if not isinstance(v, dict) else v) for k, v in kv.items()}
    return res


if __name__ == "__main__":
    for p in sys.argv[1:]:
        info = probe(p)
        if not info["ok"]:
            print(json.dumps(info, ensure_ascii=False, indent=2))
            continue
        slim = {k: v for k, v in info.items() if k not in ("kv", "tensors", "full_kv")}
        print(json.dumps(slim, ensure_ascii=False, indent=2))
        print("-" * 60)
