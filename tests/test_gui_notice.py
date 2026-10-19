# -*- coding: utf-8 -*-
"""界面冒烟测试：提示条（限流暂停倒计时）+ 中断收尾，全离线。"""
import sys
import tkinter as tk
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main as m            # noqa: E402
import report               # noqa: E402

bad = 0


def check(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra else ""))


# 屏蔽真实弹窗与浏览器
m.messagebox.showinfo = lambda *a, **k: None
m.messagebox.showwarning = lambda *a, **k: None
m.messagebox.showerror = lambda *a, **k: None
import webbrowser             # noqa: E402
webbrowser.open = lambda *a, **k: None

# ⚠ 关键：把输出目录指向系统临时目录，绝不允许写到项目里（会覆盖真实结果）
TMP = Path(tempfile.mkdtemp(prefix="invoice_gui_"))
m.ROOT = TMP
m.DATA_DIR = TMP
m.SHOTS_DIR = TMP / "查验截图"
m.RAW_SHOTS = TMP / "shots"
m.SHOTS_DIR.mkdir(parents=True, exist_ok=True)
m.RAW_SHOTS.mkdir(parents=True, exist_ok=True)

root = tk.Tk()
root.withdraw()
app = m.App(root)
root.update()

print("=== 1. 提示条：暂停 → 倒计时 → 恢复 ===")
check("初始不显示提示条", not app._notice_shown)
app.q.put(("notice", "pause", 60, "平台提示「您的访问过于频繁，请稍后再试」"))
root.update()
app._poll_queue()
root.update()
check("暂停时提示条出现", app._notice_shown)
check("提示条文案含暂停秒数", "60" in app.notice_lbl.cget("text"),
      app.notice_lbl.cget("text")[:40])
app.q.put(("notice", "countdown", 37, ""))
root.update()
app._poll_queue()
root.update()
check("倒计时会刷新剩余秒数", "37" in app.notice_lbl.cget("text"),
      app.notice_lbl.cget("text")[:40])
app.q.put(("notice", "resume", 0, ""))
root.update()
app._poll_queue()
root.update()
check("恢复后提示条变绿（恢复正常）", app.notice_lbl.cget("bg") == "#1a7f37",
      app.notice_lbl.cget("bg"))
app._hide_notice()
root.update()
check("可隐藏提示条", not app._notice_shown)

print("\n=== 2. 收尾：平台限流中断也要出报告并提示重跑 ===")
class FakeCk:
    stop_reason = "平台限流"
    last_error = ""

app.ck = FakeCk()
app.records = [
    {"file": "a.pdf", "idx": 1, "number": "N1", "kind": "数电票", "date": "20260101"},
    {"file": "b.pdf", "idx": 2, "number": "N2", "kind": "数电票", "date": "20260102"},
]
app.results = [
    {"file": "a.pdf", "idx": 1, "number": "N1", "kind": "数电票",
     "result": "一致", "detail": "查验通过", "shot": ""},
    {"file": "b.pdf", "idx": 2, "number": "N2", "kind": "数电票",
     "result": "平台限流", "detail": "平台提示「您的访问过于频繁，请稍后再试」",
     "shot": ""},
    {"file": "c.pdf", "idx": 3, "number": "N3", "kind": "数电票",
     "result": "未执行", "detail": "因平台限流中断，本张尚未查验", "shot": ""},
]
import report as _rp    # noqa: E402
_orig = _rp.write_html


def _spy(results, out_path, **kw):
    _spy.seen = [(r.get("idx"), r.get("result")) for r in results]
    return _orig(results, out_path, **kw)


_rp.write_html = _spy
m.report.write_html = _spy
try:
    app._finished()
finally:
    _rp.write_html = _orig
    m.report.write_html = _orig
root.update()
check("中断时仍生成报告（含所有行）", len(getattr(_spy, "seen", [])) == 3,
      str(getattr(_spy, "seen", None)))
check("状态栏提示风控中断并给出重跑指引",
      "风控中断" in app.var_status.get(), app.var_status.get()[:60])
check("提示条已在收尾时隐藏", not app._notice_shown)
check("已恢复可再次开始", str(app.btn_start["state"]) == "normal",
      str(app.btn_start["state"]))

print("\n=== 3. Excel/HTML 支持新状态 ===")
check("Excel 有「平台限流」配色", "平台限流" in report.STATUS_FILL)
check("Excel 有「未执行」配色", "未执行" in report.STATUS_FILL)
check("HTML 有「平台限流」文案配色", "平台限流" in report.STATUS_COLOR)

print("\n=== 4. 上一轮结果自动归档（不静默覆盖）===")
(TMP / "查验结果.xlsx").write_bytes(b"old-result")
(TMP / "查验结果.html").write_text("old-html", encoding="utf-8")
dst = app._rotate_prev_outputs()
check("归档目录已创建", bool(dst) and Path(dst).exists(), str(dst))
check("旧结果已搬进归档目录",
      dst is not None and (Path(dst) / "查验结果.xlsx").exists()
      and not (TMP / "查验结果.xlsx").exists())
check("归档目录名带时间戳",
      dst is not None and len(Path(dst).name) == 15, str(Path(dst).name))

print("\n=== 5. 输出都落在临时目录（没碰真实结果）===")
real = Path(__file__).resolve().parent.parent
check("项目 data/ 未被只读测试写入",
      not (real / "data" / "_test_gui_out").exists()
      and TMP.resolve() != (real / "data").resolve(),
      str(TMP))
check("临时目录在系统 temp 下，不在项目内",
      "invoice_gui_" in TMP.name, TMP.name)

root.destroy()
import shutil                                                # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n失败项：{bad}")
sys.exit(1 if bad else 0)
