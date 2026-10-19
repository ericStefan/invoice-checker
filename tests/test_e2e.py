# -*- coding: utf-8 -*-
"""端到端冒烟测试：先写识别清单 Excel → 读取导入 → 在真实平台跑完整查验。
验证：Excel 导入链路 + 表单填充 + 验证码识别 + 结果解析 + 全内容截图 + 关闭弹窗。
注意：成功提交才计当日查验次数（每天限 5 次），验证码错误不消耗。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright
import report
from checker import Checker, detect_engine

# 产物写临时目录，绝不污染项目根
_TMP = Path(tempfile.mkdtemp(prefix="invoice_e2e_"))
SHOTS_DIR = Path(__file__).resolve().parent.parent / "data" / "shots_e2e"

# 合成票号：不消耗真实发票的当日 5 次额度，平台会返回「查无此票」，
# 正好用来验证：验证码错误→刷新重试→拿到明确结果→截图→关窗 的完整链路
SRC = {"file": "test_synth.pdf", "kind": "数电票", "code": "",
       "number": "25122000000000000009", "date": "20260115",
       "amount": "1000.00", "amount_alt": "", "checkcode": ""}

_scan = _TMP / "tmp_scan.xlsx"
report.write_excel([SRC], str(_scan))            # 模拟「导出识别清单」
recs = report.read_scan_excel(str(_scan))        # 模拟「导入Excel清单」
REC = recs[0]
print("导入结果:", {k: REC[k] for k in
                    ("file", "kind", "number", "date", "amount", "amount_alt")})

ck = Checker(shots_dir=str(SHOTS_DIR), headless=False, manual=None, log=print,
             auto_captcha_tries=8,             # OCR 约 3 成命中，多给几次机会
             channel=detect_engine())          # 优先本机 Chrome/Edge 内核
with sync_playwright() as pw:
    b = ck._launch(pw)
    page = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
    r = ck.check_one(REC, page)
    dlg_visible = False
    try:
        dlg_visible = page.locator(".t-dialog:visible").count() > 0
    except Exception:
        pass
    b.close()

print("\n=== 端到端结果 ===")
print("结果:", r["result"])
print("明细:", r["detail"])
print("截图:", r["shot"])
print("弹窗已关闭:", not dlg_visible)
