import hashlib, json, os, re, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PF = json.load(open(os.path.join(ROOT, "models", "prompt_format.json"),
                    encoding="utf-8"))
build = lambda d: PF["instr"] + "\n\n" + d.strip() + PF["sep"]
assert hashlib.sha256(build("X").encode()).hexdigest()[:16] == "cc51d66b4c593fbe", "FAIL: prompt format"

DRAFT = """Hi Nadia,

I'm handing off the Meridian Bank loan audit to you. We've completed the review of 88 files, with 12 files remaining. The outstanding files are in the "Pending Review" folder in the Meridian Bank audit workspace, organized by borrower name.

The remaining items are primarily commercial real estate loans and include several files requiring confirmation of updated income statements, collateral valuations, and covenant documentation. I've flagged the missing items in the audit tracker and added notes for the files that may need follow-up with Meridian Bank's loan operations team.

The deadline is Friday, March 14. Please prioritize the files marked "High" in the tracker, especially MB-2047, MB-2091, and MB-2110, since those are scheduled for management review first. I'll be available through Wednesday afternoon to answer questions or provide context on any earlier decisions.

Thanks for taking this over."""

ZH = """各位好，今天想跟大家同步一下这次版本更新的情况。本次更新一共涉及 37 个功能点，其中 12 个是用户呼声比较高的。

首先是我们一直在做的性能优化。在上一版本中，模型加载的平均耗时是 4.2 秒，在本次更新之后下降到了 2.8 秒，降幅大约为 33%。这个数字是在 8GB 显存、batch size 为 1 的条件下测得的，测试机型覆盖了 3 款设备。

其次是接口层面的变化。我们在 /api/generate 中新增了一个 raw 参数，当这个参数为 true 时，系统会直接返回未经包装的文本。这主要是为了方便对接一些自定义的脚本，此前有用户反馈希望能够拿到最原始的输出。

最后需要说明的是，本次更新不包含任何数据迁移，也就是说你的历史会话记录是完整保留的。"""

def run(draft, url, npredict=2048):
    body = {"prompt": build(draft), "temperature": 1.0, "top_p": 0.95, "top_k": 0, "min_p": 0,
            "repeat_penalty": 1.0, "n_predict": npredict}
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    t0 = __import__("time").time()
    r = json.load(urllib.request.urlopen(req, timeout=900))
    dt = __import__("time").time() - t0
    return r["content"].strip(), r.get("timings", {}), dt

def check(out, draft, unit="word"):
    problems = []
    toks = out.split() if unit == "word" else list(out)
    if len(toks) < 40:
        problems.append("output too short")
    if "### Rewritten" in out or out.startswith("Rewrite the text below"):
        problems.append("prompt echoed")
    if re.search(r"<start_of_turn>|<end_of_turn>|<\|turn", out):
        problems.append("chat markers")
    n = 5 if unit == "word" else 6
    def grams(t):
        if unit == "word":
            w = t.lower().split()
            return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}
        w = list(t)
        return {"".join(w[i:i + n]) for i in range(len(w) - n + 1)}
    ov = grams(out)
    cop = len(grams(draft) & ov) / max(1, len(ov))
    if cop > 0.5:
        problems.append(f"copies draft ({cop:.0%})")
    dnums = set(re.findall(r"\d+", draft))
    missing = sorted(dnums - set(re.findall(r"\d+", out)))
    added = sorted(set(re.findall(r"\d+", out)) - dnums)
    return problems, cop, missing, added

URL = "http://127.0.0.1:11440/completion"

print("=" * 70)
print("[1] EN self-test (official draft)")
out, tm, dt = run(DRAFT, URL)
print(out)
print("-" * 70)
p, cop, miss, add = check(out, DRAFT)
tm = tm or {}
print(f"time {dt:.1f}s  prompt {tm.get('prompt_n')} tok  gen {tm.get('predicted_n')} tok  "
      f"{tm.get('predicted_per_second', 0):.1f} tok/s  copy {cop:.0%}")
print("PASS" if not p else "FAIL: " + "; ".join(p))
if miss: print("NOTE missing numbers:", miss)
if add:  print("NOTE added numbers:", add)

print("=" * 70)
print("[2] ZH test")
out2, tm2, dt2 = run(ZH, URL)
print(out2)
print("-" * 70)
p2, cop2, miss2, add2 = check(out2, ZH, "char")
tm2 = tm2 or {}
print(f"time {dt2:.1f}s  prompt {tm2.get('prompt_n')} tok  gen {tm2.get('predicted_n')} tok  "
      f"{tm2.get('predicted_per_second', 0):.1f} tok/s  copy {cop2:.0%}")
print("PASS" if not p2 else "FAIL: " + "; ".join(p2))
if miss2: print("NOTE missing numbers:", miss2)
if add2:  print("NOTE added numbers:", add2)
