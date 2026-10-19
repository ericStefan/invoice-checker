# -*- coding: utf-8 -*-
"""实测「挑简单验证码」策略：连续刷图并统计识别置信度分布。

用一张今日已超限的票（填单不会消耗额度、也不会有成功结果干扰），
填完表单后连续刷新验证码图，记录每张图的识别结果与置信度，
从而回答："到底有多少比例的验证码是能被可靠识别的？"
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

import captcha as cap
from checker import Checker, CONF_GOOD, CONF_OK

REC = {"file": "probe", "kind": "数电票", "code": "",
       "number": "25952000000000000001", "date": "20250512",
       "amount": "53803.00", "amount_alt": "47436.28", "checkcode": ""}
N = int(sys.argv[1]) if len(sys.argv) > 1 else 15

with sync_playwright() as pw:
    ck = Checker(shots_dir=tempfile.mkdtemp(prefix="invoice_probe_"),
                 headless=False,
                 log=lambda m: print("   ", m), channel="chrome")
    browser = ck._launch(pw)
    ctx = browser.new_context(viewport={"width": 1400, "height": 900},
                              locale="zh-CN", timezone_id="Asia/Shanghai",
                              ignore_https_errors=True)
    ck._setup_ctx(ctx)
    page = ctx.new_page()
    try:
        ck._goto_form(page, REC)
        ck._fill_form(page, REC, REC["amount"])
        page.wait_for_timeout(600)
        rows = []
        img_loc, png = ck._grab_captcha(page)
        for i in range(1, N + 1):
            if img_loc is None or png is None:
                print(f"#{i}: 未取到验证码图")
                break
            prompt = ck._captcha_prompt(page)
            s = cap.solve(png, prompt or "")
            conf = s["conf"] if s else 0.0
            text = s["text"] if s else ""
            rows.append(conf)
            mark = ("★可提交" if conf >= CONF_GOOD else
                    ("·可用" if conf >= CONF_OK else " 换图"))
            print(f"#{i:>2} conf={conf:.2f} {mark}  text={text!r:<8} "
                  f"提示={prompt or '(空)'}")
            if i < N:
                ck._refresh_captcha(page, png, confirm=False)
                page.wait_for_timeout(700)
                img_loc, png = ck._grab_captcha(page)
        if rows:
            good = sum(1 for c in rows if c >= CONF_GOOD)
            ok = sum(1 for c in rows if CONF_OK <= c < CONF_GOOD)
            low = sum(1 for c in rows if c < CONF_OK)
            print(f"\n共 {len(rows)} 张：可提交(≥{CONF_GOOD}) {good} 张 "
                  f"({good/len(rows)*100:.0f}%)，可用 {ok} 张，换图 {low} 张")
            print(f"平均置信度 {sum(rows)/len(rows):.2f}，最高 {max(rows):.2f}")
    finally:
        browser.close()
