# -*- coding: utf-8 -*-
"""诊断用：只走「一次」填单+提交，把平台实际返回的页面文本与截图落盘。
用于确认"未识别到结果内容"时平台到底返回了什么（是否超限/是否校验失败）。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from checker import Checker, detect_engine

_SHOTS = tempfile.mkdtemp(prefix="invoice_diag_")

REC = {"file": "diag.pdf", "kind": "数电票", "code": "",
       "number": "25122000000000000009", "date": "20260115",
       "amount": "1000.00", "amount_alt": "", "checkcode": ""}

ck = Checker(shots_dir=_SHOTS, headless=False, manual=None, log=print,
             auto_captcha_tries=1, channel=detect_engine())
with sync_playwright() as pw:
    b = ck._launch(pw)
    page = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
    try:
        ck._goto_form(page, REC)
        ck._fill_form(page, REC, REC["amount"])
        img, png = ck._grab_captcha(page)
        cap_in = ck._captcha_input(page)
        print("验证码图:", img is not None, "| 输入框:", cap_in is not None)
        if img and cap_in:
            import captcha as cap
            solved = cap.solve(png, "")
            txt = solved.get("text") if solved else None
            print("OCR:", txt)
            if txt:
                from checker import _fill
                _fill(page, cap_in, txt)
                ck._click_submit(page)
                page.wait_for_timeout(9000)
        p = ck._dump_page(page, REC, "诊断")
        print("页面文本快照:", p)
        try:
            print("提交后验证码输入框内容:", repr(cap_in.input_value()))
        except Exception as e:
            print("读取验证码输入框失败:", e)
        _diag = str(Path(_SHOTS) / "diag.png")
        page.screenshot(path=_diag, full_page=True)
        print("截图:", _diag)
        body = page.inner_text("body")
        keys = [l.strip() for l in body.splitlines() if l.strip()]
        print("--- 页面关键行 ---")
        for l in keys:
            if any(k in l for k in ("验证码", "错误", "次数", "查验", "请输入", "限")):
                print("  ", l[:100])
        try:
            print("弹窗数:", page.locator(".t-dialog:visible").count())
        except Exception:
            pass
    finally:
        b.close()
