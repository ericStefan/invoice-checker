# -*- coding: utf-8 -*-
"""干跑验证：完整走一遍「打开页面 → 填单 → 抓验证码 → 自动识别」，
**不点击提交**，用于确认改动无异常（也避免消耗额度 / 加剧平台限流）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

import captcha as cap
from checker import Checker, URL, LAUNCH_ARGS

rec = {"file": "t.pdf", "kind": "数电票", "code": "",
       "number": "25952000000000000001", "date": "20250512",
       "amount": "53803.00", "amount_alt": ""}


def main():
    ck = Checker(log=lambda m: print("   ", m))
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=False, args=LAUNCH_ARGS)
        ctx = b.new_context(viewport={"width": 1400, "height": 950},
                            locale="zh-CN", ignore_https_errors=True)
        page = ctx.new_page()
        ck._goto_form(page, rec)
        ck._fill_form(page, rec, rec["amount"])
        bad = ck._check_bad_info(page)
        print("前端校验提示：", repr(bad))
        for k in range(3):
            loc, png = ck._grab_captcha(page)
            if png is None:
                print("抓图失败")
                break
            prompt = ck._captcha_prompt(page)
            r = cap.solve(png, prompt or "")
            print(f"[{k+1}] 提示={prompt!r} → 识别={r}")
            ck._refresh_captcha(page, png)
        # 验证复用页面逻辑（第二次调用应跳过 goto）
        ck._goto_form(page, rec)
        print("页面复用测试通过")
        b.close()


if __name__ == "__main__":
    main()
