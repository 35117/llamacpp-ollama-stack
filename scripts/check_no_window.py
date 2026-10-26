#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""防止「启动/停止时弹出一串黑框」的回归检查。

背景：从 `pythonw.exe`（无控制台的托盘）里启动 netstat / taskkill / tasklist /
nvidia-smi 这类控制台程序时，**不加 `CREATE_NO_WINDOW` 就会各自新建一个控制台窗口**。
一次「停止服务」原来是 6 次外部调用 → 6 个黑框一闪，用户报过这个问题。

本检查两道：
  A) 静态：`scripts/*.py` 里所有 `subprocess.run/Popen` 都必须带 `creationflags=`。
     控制台辅助程序应一律走 `winrun.run()`；只有「启动长期进程」那几处直接用 Popen。
  B) 动态：在**无控制台**的父进程下各起一个探针，验证
     不加标志 → 子进程确实拿到了控制台；加了 → 没有。
     （只在无控制台的父进程下做才有意义，所以 B 会把自己用 pythonw 重跑一遍。）

用法：
    python scripts\\check_no_window.py            静态 + 动态
    python scripts\\check_no_window.py --static   只跑静态
"""
import ast
import glob
import json
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
CREATE_NO_WINDOW = 0x08000000

# 允许直接用 subprocess 的文件：
#   winrun.py            —— 唯一出口，creationflags 在关键字字典里合成
#   check_no_window.py   —— 本文件，动态探针**故意**不加标志（那是对照组），
#                           用 pythonw 重启自己也必须用裸 subprocess
ALLOWED = {"winrun.py", "check_no_window.py"}


# 会让 CREATE_NO_WINDOW **失效**的标志（MSDN：与该标志同用时它被忽略）。
# 于是控制台程序照样弹出一个可见窗口 —— llama-swap 就这样留下过一个空的常驻黑框。
NEVER_COMBINE = ("DETACHED_PROCESS", "CREATE_NEW_CONSOLE")


def check_flag_conflicts():
    """静态规则：任何文件都不许用 DETACHED_PROCESS / CREATE_NEW_CONSOLE。

    用 AST 查 `ast.Name`，所以**注释里提到它们不算数** —— 我们恰恰要在注释里
    讲清楚为什么不能用。
    """
    problems = []
    for path in sorted(glob.glob(os.path.join(SCRIPTS, "*.py"))):
        name = os.path.basename(path)
        if name in ALLOWED:
            continue
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in NEVER_COMBINE:
                problems.append(
                    f"{name}:{node.lineno}  用了 {node.id} —— 它会让 CREATE_NO_WINDOW 失效，"
                    f"控制台程序会弹出可见窗口。只用 CREATE_NO_WINDOW。")
    return problems


def static_check():
    problems = list(check_flag_conflicts())
    checked = 0
    for path in sorted(glob.glob(os.path.join(SCRIPTS, "*.py"))):
        name = os.path.basename(path)
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            problems.append(f"{name}: 语法错误 {exc}")
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr in ("run", "Popen", "call",
                                                                 "check_output", "check_call")):
                continue
            if not (isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
                continue
            checked += 1
            kwargs = {k.arg for k in node.keywords}
            if "creationflags" in kwargs:
                continue
            if name in ALLOWED:
                continue
            problems.append(
                f"{name}:{node.lineno}  subprocess.{fn.attr} 没有 creationflags "
                f"→ {lines[node.lineno - 1].strip()[:70]}")
    return checked, problems


def dynamic_check():
    """在无控制台的环境里，比较几种标志下子进程有没有拿到控制台。

    第三种（DETACHED_PROCESS + CREATE_NO_WINDOW）是**反面教材**：
    用来在这台机器上验证「叠了反而弹窗」这条规则，只作参考值，不参与判定。
    """
    code = ("import ctypes;"
            "print(ctypes.windll.kernel32.GetConsoleWindow())")
    py = sys.executable
    if py.lower().endswith("pythonw.exe"):
        py = py[:-len("pythonw.exe")] + "python.exe"
    if not os.path.exists(py):
        return None, "找不到 python.exe，跳过动态检查"

    def probe(flags):
        r = subprocess.run([py, "-c", code], capture_output=True, timeout=60,
                           creationflags=flags)
        return int(r.stdout.decode("utf-8", "replace").strip() or 0)

    # 注意：这里只用 python 当探针，**测不出** DETACHED_PROCESS 那个坑 ——
    # python 不会自己分配控制台，句柄一直是 0；`cmd.exe` / llama-swap 会。
    # 所以「叠了 DETACHED_PROCESS 会不会弹窗」由
    # scripts\test_start_no_window.py（跑真实启动、枚举真实窗口）来把关。
    return {"plain": probe(0), "hidden": probe(CREATE_NO_WINDOW)}, None


def main():
    static_only = "--static" in sys.argv

    print("=== A) 静态检查：subprocess 调用是否都带了 creationflags ===")
    checked, problems = static_check()
    print(f"  扫描到 {checked} 处 subprocess 调用")
    for p in problems:
        print(f"  [X] {p}")
    if not problems:
        print("  [OK] 全部带了 creationflags（或走 winrun）")

    dyn_ok = True
    if not static_only:
        print("\n=== B) 动态检查：无控制台父进程下，子进程是否真的被创建了控制台 ===")
        if os.environ.get("NO_WINDOW_CHECK_CHILD") == "1":
            (res, err) = dynamic_check()
            print("__DYNAMIC__" + json.dumps({"res": res, "err": err}))
        else:
            # 自己用 pythonw 重跑一遍 —— 只有这样父进程才是「无控制台」，
            # 才复现得出用户看到的现象。
            pyw = sys.executable
            if pyw.lower().endswith("python.exe"):
                cand = pyw[:-len("python.exe")] + "pythonw.exe"
                if os.path.exists(cand):
                    pyw = cand
            env = dict(os.environ, NO_WINDOW_CHECK_CHILD="1", PYTHONUTF8="1")
            # 注意别再传 --static：子进程要靠环境变量走动态分支，否则不会输出结果
            out = subprocess.run([pyw, os.path.abspath(__file__)],
                                 capture_output=True, timeout=120, env=env)
            text = out.stdout.decode("utf-8", "replace")
            line = next((l for l in text.splitlines() if l.startswith("__DYNAMIC__")), None)
            if not line:
                print("  [X] 拿不到动态检查结果")
                print("      " + (out.stderr.decode("utf-8", "replace")[:300] or "（无 stderr）"))
                dyn_ok = False
            else:
                data = json.loads(line[len("__DYNAMIC__"):])
                res, err = data["res"], data["err"]
                if err or not res:
                    print(f"  [X] {err or '无结果'}")
                    dyn_ok = False
                else:
                    plain = res["plain"]
                    hidden = res["hidden"]
                    print(f"  不用 CREATE_NO_WINDOW → 控制台句柄 = {plain}"
                          f"  {'（确实新建了控制台，就是那个黑框）' if plain else '（无）'}")
                    print(f"  只用 CREATE_NO_WINDOW → 控制台句柄 = {hidden}"
                          f"  {'（无，正确）' if hidden == 0 else '（居然有？）'}")
                    print("  提示：「真实启动会不会弹窗」由 "
                          "scripts\\test_start_no_window.py 把关")
                    if plain and hidden == 0:
                        print("  [OK] 现象与修复都对得上")
                    else:
                        print("  [X] 与预期不符")
                        dyn_ok = False

    bad = bool(problems) or not dyn_ok
    print("\n" + ("有问题，见上。" if bad else "全部通过：不会再闪控制台窗口。"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
