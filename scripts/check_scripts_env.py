# -*- coding: utf-8 -*-
"""环境兼容性自检：确认脚本在「非标准宿主」里也不会崩。

两件事：
  A) 文件格式（.bat / .vbs 的换行与编码）——
     Windows Script Host 默认按 ANSI 读 .vbs，UTF-8 无 BOM 的中文会变成
     非法字符直接报 800A0408；裸 LF 的换行 VBScript 也解析不了。
     2026-09-28 用户双击 tray.vbs 就撞上这个。
  B) 脚本在「非标准 stdout」的宿主里不崩 ——
     用户在 IDLE 里跑 detach_from_ollama.py：
        AttributeError: 'StdOutputFile' object has no attribute 'reconfigure'
     因为 IDLE 把 sys.stdout/stderr 换成了没有 reconfigure() 的对象。
     这里用等价对象替换后逐个跑一遍，防这类「换个宿主就炸」的回归。

用法：
    python scripts\\check_scripts_env.py              格式检查 + 跑全部（约 90 秒）
    python scripts\\check_scripts_env.py --format     只做格式检查（很快）
    python scripts\\check_scripts_env.py status.py    只跑指定的脚本
"""
import glob
import io
import os
import runpy
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check_file_formats():
    """返回 (问题列表, 检查文件数)。

    .bat：必须 CRLF **且纯 ASCII**。
          2026-09-29 教训：cmd.exe 按当前代码页逐字节解析批处理，chcp 切换
          前后遇到多字节字符会算错行偏移，把下一行的 REM 拼进当前命令，
          报出一串「'楋紱鍏虫帀...' 不是内部或外部命令」；裸 LF 同样不稳。
          中文一律放 Python / HTML，不要放 .bat。
    .vbs：同样必须 CRLF 且纯 ASCII —— ASCII 是所有代码页的子集，
          这样无论 WSH 按 ANSI 还是 UTF-8 读都能正确解析，比依赖 BOM 稳。
          （2026-09-28 用户双击 tray.vbs 撞上 800A0408。）

    注意：扫描是**全项目递归**的。早期版本只扫 scripts/*.bat，
    结果新增的 turtle/start.bat 裸 LF + 中文注释一路溜过门禁（2026-09-29）。
    """
    problems = []
    count = 0
    for pat, ext in ((os.path.join(ROOT, "**", "*.bat"), ".bat"),
                     (os.path.join(ROOT, "**", "*.vbs"), ".vbs")):
        for path in sorted(glob.glob(pat, recursive=True)):
            name = os.path.relpath(path, ROOT).replace(os.sep, "/")
            count += 1
            raw = open(path, "rb").read()
            lf = raw.count(b"\n") - raw.count(b"\r\n")
            if lf:
                problems.append(f"{name}: 有 {lf} 处裸 LF，应全部是 CRLF")
            bad = [(i, b) for i, b in enumerate(raw) if b > 127]
            if bad:
                problems.append(
                    f"{name}: 含 {len(bad)} 个非 ASCII 字节（首个在第 {bad[0][0]} 字节）"
                    f" —— {ext} 必须纯 ASCII：cmd/WSH 按当前代码页逐字节解析，"
                    f"多字节字符会算错行边界，把下一行拼进当前命令")
    return problems, count


class StdOutputFile(io.TextIOBase):        # 模仿 idlelib.run.StdOutputFile
    def __init__(self):
        self._n = 0

    def write(self, s):
        self._n += len(s)
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


real_out, real_err = sys.stdout, sys.stderr


def report(msg):
    real_out.write(msg + "\n")
    real_out.flush()


args = sys.argv[1:]
format_only = "--format" in args
targets = [a for a in args if not a.startswith("--")] or [
    "detach_from_ollama.py", "extract_personas.py", "gguf_fix.py",
    "list_models.py", "ollama_lib.py", "status.py", "selftest.py",
]

bad = 0
os.chdir(ROOT)

report("=== A) 脚本文件格式（换行 / 编码）===")
fmt_problems, fmt_count = check_file_formats()
for p in fmt_problems:
    report(f"  [X] {p}")
if not fmt_problems:
    report(f"  [OK] {fmt_count} 个 .bat/.vbs 全部合规（CRLF；纯 ASCII；全项目递归）")
bad += len(fmt_problems)

if not format_only:
    report("")
    report("=== B) 脚本在 IDLE 式 stdout 下是否崩溃 ===")
    for name in targets:
        path = os.path.join("scripts", name)
        sys.stdout, sys.stderr = StdOutputFile(), StdOutputFile()
        # 关键：argparse 会读 sys.argv，必须模拟成「只带脚本路径」再调用，
        # 否则本测试脚本自己的参数会被当成脚本参数，得到假的「退出码 2」。
        saved_argv = sys.argv
        sys.argv = [path]
        verdict, detail = "OK", ""
        try:
            runpy.run_path(path, run_name="__main__")
        except SystemExit as exc:
            if exc.code not in (0, None):
                verdict, detail = "EXIT", f"退出码 {exc.code}"
                if name == "selftest.py":
                    # 服务没跑时它本来就会以 1 退出，这里只关心有没有崩
                    detail += "（服务未启动，正常；本检查只看会不会崩）"
        except AttributeError as exc:
            verdict, detail = "FAIL", f"AttributeError: {exc}"
            bad += 1
        except BaseException as exc:            # noqa: BLE001 - 就是想抓全部
            verdict, detail = "ERROR", f"{type(exc).__name__}: {exc}"
            if "reconfigure" in str(exc):
                bad += 1
        finally:
            sys.argv = saved_argv
            sys.stdout, sys.stderr = real_out, real_err
        report(f"  [{verdict:>5}] {name:<26}{detail}")

report("")
report("全部检查通过" if not bad else f"!! 有 {bad} 处问题")
sys.exit(1 if bad else 0)
