#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GGUF 元数据兼容修复 —— 只重写头部元数据，张量数据整段原样搬运。

背景
----
Ollama 的 model blob 就是 GGUF，本部署用硬链接直接复用。但个别模型是用别的
llama.cpp 版本配套的转换器导出的，元数据写法与当前 llama.cpp（本部署为 b11146）
不一致，加载时会直接报错。已知三类差异：

  1) 整型数组长度不对
     qwen3.5_2b.gguf:
       key qwen35.rope.dimension_sections has wrong array length; expected 4, got 3
     同架构的 4B 写的是 [11, 11, 10, 0]。

  2) 张量名字不同
     qwen3.5_2b.gguf 里叫 blk.N.ssm_dt，当前 llama.cpp 要 blk.N.ssm_dt.bias
     4B 用的就是后者。

  3) 元数据的「表达形式」不同（数组 vs 标量）
     qwen3.5_2b.gguf 的 qwen35.attention.head_count_kv 是逐层数组
       [0,0,0,2, 0,0,0,2, ...]      （0 = 线性注意力层，2 = 全注意力层）
     4B 是标量 4。当前 llama.cpp 只认标量，读数组会得到 0，于是
       tensor 'blk.3.attn_k.weight' has wrong shape; expected 2048, 0, got 2048, 512
     2B 的注意力层 kv_dim 实测 512 = 2 x key_length(256)，所以标量应取 2。

做法
----
GGUF 里张量偏移是「相对数据段起点」的，所以只要保证数据段起点与重排后的元数据对齐，
就能把张量数据整段搬过去、字节级不变。本工具因此：
  1. 解析头部，每个 KV 的原始字节切片都留着（不改的原样写回，避免序列化误差）；
  2. 只替换目标 key 的 value 字节 / 只替换目标张量的名字字节；
  3. 写新文件：头 + KV + tensor_infos + 对齐填充 + 原数据段（整段拷贝）；
  4. 严格校验：KV 数、张量数、数据段长度不变；每个未改名的张量的
     偏移/维度/类型必须逐字节相同。

注意
----
原文件若是指向 Ollama blob 的硬链接，**绝不能原地改**（会连带改坏 Ollama 的模型）。
main() 会先 unlink 断开链接，再写回同名新文件；blob 本体不受影响，随时可恢复。
"""
import argparse
import os
import re
import shutil
import struct
import sys

MAGIC = b"GGUF"
ALIGN_DEFAULT = 32

SCALAR = {
    0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2),
    4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 7: ("<?", 1),
    10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8),
}
INT32, UINT32 = 5, 4

# ---------------------------------------------------------------------------
# 元数据修复表
#   {"kind": "array",  "elem": <type>, "value": [...]}   改写整型数组
#   {"kind": "scalar", "type": <type>, "value": x}        改写标量（可跨类型）
# 只有「当前形式与目标不一致」时才动手，因此可反复安全运行。
#
# 目前为空：曾用于 qwen3.5_2b.gguf 的那三条规则（dimension_sections 补位、
# ssm_dt 改名、head_count_kv 数组转标量）已被验证为「必要但不充分」——
# 前两条改完后仍卡在张量匹配，第三条把逐层 KV 头信息丢掉了（只匹配到 320/728）。
# 那个模型已从本部署移除，规则一并清空，避免误用后误以为已修好。
# 需要时按上面 docstring 记录的三类差异重新添加。
# ---------------------------------------------------------------------------
KNOWN_FIXES = {}

# ---------------------------------------------------------------------------
# 张量改名表：正则 -> 替换模板。只动元数据里的名字，
# 张量数据位置/维度/类型一概不变。
# 留空的原因同上；格式示例：
#   (re.compile(r"^blk\.(\d+)\.ssm_dt$"), r"blk.\1.ssm_dt.bias"),
# ---------------------------------------------------------------------------
TENSOR_RENAMES = []


def align(n, a):
    return (n + a - 1) // a * a


class GGUFHeader:
    """只解析头部：KV 与 tensor_info 都保存为原始字节切片。

    性能注意：全程只 open 一次文件句柄。早期版本用「另开句柄 + seek 读切片」，
    728 个张量就是 1400+ 次 open，在装了杀软的机器上要跑几十秒（实测踩过）。
    """

    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if fh.read(4) != MAGIC:
                raise ValueError(f"{path} 不是 GGUF 文件")
            self.version = struct.unpack("<I", fh.read(4))[0]
            self.tensor_count = struct.unpack("<Q", fh.read(8))[0]
            self.kv_count = struct.unpack("<Q", fh.read(8))[0]
            self.prefix = struct.pack("<4sIQQ", MAGIC, self.version,
                                      self.tensor_count, self.kv_count)

            self.kvs = [self._read_kv(fh) for _ in range(self.kv_count)]

            self.tensor_infos = [self._read_tensor_info(fh)
                                 for _ in range(self.tensor_count)]
            self.infos_end = fh.tell()

            self.alignment = ALIGN_DEFAULT
            for kv in self.kvs:
                if kv["key"] == "general.alignment" and kv["type"] in SCALAR:
                    fmt, sz = SCALAR[kv["type"]]
                    self.alignment = struct.unpack(fmt, kv["value_bytes"][:sz])[0]
                    break
            self.data_start = align(self.infos_end, self.alignment)

    @staticmethod
    def _slice(fh, a, b):
        """在同一句柄上取 [a,b) 字节，取完把位置还原到 b。"""
        fh.seek(a)
        data = fh.read(b - a)
        fh.seek(b)
        return data

    # ---------------------------------------------------------------- KV
    def _read_kv(self, fh):
        key_start = fh.tell()
        klen = struct.unpack("<Q", fh.read(8))[0]
        key = fh.read(klen).decode("utf-8", "replace")
        key_bytes = self._slice(fh, key_start, fh.tell())
        vtype = struct.unpack("<I", fh.read(4))[0]
        vstart = fh.tell()
        array_len = elem_type = None

        if vtype == 8:
            n = struct.unpack("<Q", fh.read(8))[0]
            fh.seek(n, os.SEEK_CUR)
        elif vtype == 9:
            elem_type = struct.unpack("<I", fh.read(4))[0]
            array_len = struct.unpack("<Q", fh.read(8))[0]
            if elem_type == 8:
                for _ in range(array_len):
                    n = struct.unpack("<Q", fh.read(8))[0]
                    fh.seek(n, os.SEEK_CUR)
            elif elem_type in SCALAR:
                fh.seek(array_len * SCALAR[elem_type][1], os.SEEK_CUR)
            else:
                raise ValueError(f"未知数组元素类型 {elem_type} (key={key})")
        elif vtype in SCALAR:
            fh.seek(SCALAR[vtype][1], os.SEEK_CUR)
        else:
            raise ValueError(f"未知值类型 {vtype} (key={key})")

        vend = fh.tell()
        entry = {"key": key, "type": vtype, "elem_type": elem_type,
                 "array_len": array_len, "key_bytes": key_bytes,
                 "value_bytes": self._slice(fh, vstart, vend)}
        if vtype in SCALAR:
            fmt, sz = SCALAR[vtype]
            entry["scalar_value"] = struct.unpack(fmt, entry["value_bytes"][:sz])[0]
        if vtype == 9 and elem_type in SCALAR and array_len is not None and array_len <= 64:
            fmt, sz = SCALAR[elem_type]
            base = 4 + 8
            entry["array_values"] = [
                struct.unpack(fmt, entry["value_bytes"][base + i * sz:
                                                        base + (i + 1) * sz])[0]
                for i in range(array_len)]
        return entry

    # ------------------------------------------------------- tensor_info
    def _read_tensor_info(self, fh):
        """name_bytes = 8字节长度 + 名字；tail_bytes = 维度/类型/偏移，原样保留。"""
        nstart = fh.tell()
        nlen = struct.unpack("<Q", fh.read(8))[0]
        name = fh.read(nlen).decode("utf-8", "replace")
        name_bytes = self._slice(fh, nstart, fh.tell())
        tstart = fh.tell()
        nd = struct.unpack("<I", fh.read(4))[0]
        dims = [struct.unpack("<Q", fh.read(8))[0] for _ in range(nd)]
        ttype = struct.unpack("<I", fh.read(4))[0]
        offset = struct.unpack("<Q", fh.read(8))[0]
        tail_bytes = self._slice(fh, tstart, fh.tell())
        return {"name": name, "dims": dims, "type": ttype, "offset": offset,
                "name_bytes": name_bytes, "tail_bytes": tail_bytes}

    @staticmethod
    def encode_tensor_name(name):
        raw = name.encode("utf-8")
        return struct.pack("<Q", len(raw)) + raw

    def find(self, key):
        for kv in self.kvs:
            if kv["key"] == key:
                return kv
        return None


def encode_fix(spec):
    if spec["kind"] == "array":
        fmt, _sz = SCALAR[spec["elem"]]
        return (struct.pack("<I", spec["elem"]) + struct.pack("<Q", len(spec["value"]))
                + b"".join(struct.pack(fmt, int(v)) for v in spec["value"]))
    fmt, _sz = SCALAR[spec["type"]]
    return struct.pack(fmt, spec["value"])


def fix_is_satisfied(kv, spec):
    if spec["kind"] == "array":
        return (kv["type"] == 9 and kv["elem_type"] == spec["elem"]
                and kv["array_len"] == len(spec["value"])
                and kv.get("array_values") == list(spec["value"]))
    return kv["type"] == spec["type"] and kv.get("scalar_value") == spec["value"]


def plan(path):
    """返回 (todo 列表, header, 张量改名映射)。"""
    hdr = GGUFHeader(path)
    todo = []
    for key, spec in KNOWN_FIXES.items():
        kv = hdr.find(key)
        if kv is None:
            continue
        if fix_is_satisfied(kv, spec):
            cur = kv.get("array_values", kv.get("scalar_value"))
            todo.append({"key": key, "action": "ok", "reason": f"已是目标形式 {cur}"})
            continue
        cur = kv.get("array_values", kv.get("scalar_value",
                                            f"type={kv['type']}"))
        want = spec["value"]
        todo.append({"key": key, "action": "patch",
                     "reason": f"{cur} -> {want}（type {kv['type']} -> "
                               f"{spec['elem'] if spec['kind'] == 'array' else spec['type']}）"})

    present = {ti["name"] for ti in hdr.tensor_infos}
    renames = {}
    for ti in hdr.tensor_infos:
        for pat, repl in TENSOR_RENAMES:
            if not pat.match(ti["name"]):
                continue
            new = pat.sub(repl, ti["name"])
            if new in present:
                todo.append({"key": ti["name"], "action": "ok",
                             "reason": f"目标名 {new} 已存在，无需改名"})
            else:
                renames[ti["name"]] = new
            break
    if renames:
        sample = list(renames.items())[:2]
        todo.append({"key": "(tensor names)", "action": "rename",
                     "reason": f"{len(renames)} 个张量改名，例如 "
                               + "; ".join(f"{a} -> {b}" for a, b in sample)})
    return todo, hdr, renames


def patch_file(path, out_path, verbose=True):
    todo, hdr, renames = plan(path)
    if not [t for t in todo if t["action"] in ("patch", "rename")]:
        return {"path": path, "changed": False, "todo": todo}

    if verbose:
        print(f"  修复 {os.path.basename(path)}")
        for t in todo:
            print(f"    - {t['key']}: {t['action']} ({t['reason']})")

    tmp = out_path + ".tmp"
    patch_keys = {t["key"] for t in todo if t["action"] == "patch"}

    with open(tmp, "wb") as out:
        out.write(hdr.prefix)
        for kv in hdr.kvs:
            out.write(kv["key_bytes"])
            if kv["key"] in patch_keys:
                spec = KNOWN_FIXES[kv["key"]]
                # 注意：这里写的是 GGUF 的「值类型」。数组型 KV 的值类型固定是 9(ARRAY)，
                # 元素类型在值内部。曾经错写成 spec["elem"]，导致解析器把 28 字节的数组
                # 当 4 字节标量读，后续整段元数据错位 24 字节（已修，勿回退）。
                out.write(struct.pack("<I", 9 if spec["kind"] == "array"
                                      else spec["type"]))
                out.write(encode_fix(spec))
            else:
                out.write(struct.pack("<I", kv["type"]))
                out.write(kv["value_bytes"])
        for ti in hdr.tensor_infos:
            new_name = renames.get(ti["name"])
            out.write(GGUFHeader.encode_tensor_name(new_name) if new_name
                      else ti["name_bytes"])
            out.write(ti["tail_bytes"])

        pos = out.tell()
        out.write(b"\x00" * (align(pos, hdr.alignment) - pos))

        with open(path, "rb") as src:                      # 原数据段整段搬运
            src.seek(hdr.data_start)
            shutil.copyfileobj(src, out, length=8 << 20)

    os.replace(tmp, out_path)

    # ------------------------------------------------------------- 校验
    after = GGUFHeader(out_path)
    assert after.kv_count == hdr.kv_count, "KV 数量变化了"
    assert after.tensor_count == hdr.tensor_count, "张量数量变化了"
    assert after.data_start == align(after.infos_end, after.alignment)
    assert hdr.size - hdr.data_start == after.size - after.data_start, \
        "数据段长度变化了"
    for key in patch_keys:
        kv = after.find(key)
        assert kv and fix_is_satisfied(kv, KNOWN_FIXES[key]), f"{key} 修复未生效"
    new_names = {ti["name"] for ti in after.tensor_infos}
    for old, new in renames.items():
        assert new in new_names and old not in new_names, f"改名失败 {old}"
    for b, a in zip(hdr.tensor_infos, after.tensor_infos):
        assert b["offset"] == a["offset"] and b["dims"] == a["dims"] \
            and b["tail_bytes"] == a["tail_bytes"], f"{b['name']} 的尾部信息被动过"

    return {"path": path, "out": out_path, "changed": True, "todo": todo,
            "orig_size": hdr.size, "new_size": after.size,
            "renames": len(renames)}


def main():
    # IDLE 等环境会把 sys.stdout 换成没有 reconfigure() 的对象，
    # 直接调用会 AttributeError 把整个脚本打断（用户从 IDLE 跑时踩到过）。
    # 这里兜底：设不了就跳过，中文输出交给宿主环境自己的编码处理。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="要检查的 .gguf；缺省扫描 models\\*.gguf")
    ap.add_argument("--check", action="store_true", help="只报告，不写文件")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    models_dir = os.path.join(root, "models")
    files = args.files or sorted(
        os.path.join(models_dir, f) for f in os.listdir(models_dir)
        if f.lower().endswith(".gguf"))

    n_fixed = 0
    for path in files:
        name = os.path.basename(path)
        try:
            todo, _hdr, _ren = plan(path)
        except Exception as exc:
            print(f"  [SKIP] {name}: {exc}")
            continue
        needs = [t for t in todo if t["action"] in ("patch", "rename")]
        if not needs:
            print(f"  [ OK ] {name}  无需修复")
            continue
        if args.check:
            print(f"  [FIX ] {name} 需要修复:")
            for t in needs:
                print(f"           {t['key']}: {t['reason']}")
            continue

        nlink = os.stat(path).st_nlink
        # 先写到临时文件，确认成功后再断开硬链接、替换原件。
        # 顺序反了会在 patch 读源文件时找不到文件（这个坑踩过一次）。
        tmp_out = path + ".fixing"
        try:
            res = patch_file(path, tmp_out)
        except BaseException:
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)
            raise
        if nlink > 1:
            os.unlink(path)
            print(f"    已断开硬链接（Ollama blob 本体保留，nlink was {nlink}）")
        os.replace(tmp_out, path)
        print(f"    -> 已重写 {res['orig_size']/2**20:.0f}MB -> "
              f"{res['new_size']/2**20:.0f}MB，张量改名 {res['renames']} 个")
        n_fixed += 1

    print(f"\n检查 {len(files)} 个文件，修复 {n_fixed} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
