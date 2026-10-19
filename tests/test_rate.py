# -*- coding: utf-8 -*-
"""风控（访问过于频繁）处理自测 —— 全部离线，不碰真实平台。

覆盖：
1. _rate_modal 能认出仿平台的风控模态框（带遮罩 + 确定按钮）；
2. 不会把页面常驻说明文案（「每份发票每天最多可查验5次」等）误判为风控；
3. _close_rate_modal 能点掉按钮并清掉拦截点击的遮罩；
4. run_batch 命中限流时**暂停并重试同一张**，而不是继续在弹层后面硬撞；
5. 连续多次仍限流 → 停批，且把未跑的发票补成「未执行」而不是丢掉。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playwright.sync_api import sync_playwright      # noqa: E402

import checker as ck_mod                              # noqa: E402
from checker import Checker, detect_engine            # noqa: E402

# 假截图写系统临时目录，绝不污染项目目录
_SHOTS_TEST = str(Path(tempfile.mkdtemp(prefix="invoice_shots_")))

bad = 0


def check(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra else ""))


# 仿平台页面：常驻说明文案 +（隐藏的）风控模态框 + 半透明遮罩
HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
 #side{width:300px} #mask{position:fixed;inset:0;background:#000;opacity:.45;
   display:none} #rateBox{position:fixed;left:200px;top:150px;width:320px;
   background:#fff;display:none;z-index:9001}
 .btn{display:inline-block;padding:6px 14px;background:#2b6cb0;color:#fff}
</style></head><body>
 <div id="side">
   <p>1、当日开具发票当日可查验。</p>
   <p>2、每份发票每天最多可查验5次。</p>
   <p>4、如遇个别浏览器版本无法查验，建议更换浏览器。</p>
 </div>
 <input class="t-input__inner" value="">
 <div id="mask"></div>
 <div id="rateBox" class="layui-layer">
   <div class="ttl">提示</div>
   <div class="bd">您的访问过于频繁，请稍后再试</div>
   <div><span class="btn" id="ok">确定</span></div>
 </div>
<script>
 document.getElementById('mask').style.display = 'block';
 document.getElementById('rateBox').style.display = 'block';
 document.getElementById('ok').onclick = () => {
   document.getElementById('rateBox').style.display = 'none';
 };
 document.getElementById('mask').onclick = null;
</script></body></html>"""


def fake_ok(rec):
    return {"file": rec["file"], "idx": rec["idx"], "number": f"N{rec['idx']}",
            "result": "一致", "detail": "查验通过", "shot": "", "kind": "数电票"}


def fake_rate(rec):
    return {"file": rec["file"], "idx": rec["idx"], "number": f"N{rec['idx']}",
            "result": "平台限流", "detail": "平台提示「您的访问过于频繁，请稍后再试」",
            "shot": "", "kind": "数电票"}


def main():
    eng = detect_engine()
    print(f"使用内核: channel={eng!r}")

    # ---------- 1~3：弹层识别与关闭 ----------
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel=eng)
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        ck = Checker(shots_dir=_SHOTS_TEST, headless=True,
                     log=lambda m: None, channel=eng)

        # 先只放说明文案，不放弹框
        page.set_content(HTML.replace("style.display = 'block';", ""))
        page.wait_for_timeout(500)
        print("\n=== 1. 未弹框时不应误判（页面有'最多可查验5次'等常驻文案）===")
        t = ck._rate_modal(page)
        check("常驻说明文案不误判", t == "", f"-> {t!r}")

        print("\n=== 2. 平台弹出风控框后应立刻识别 ===")
        page.evaluate("""() => {
            document.getElementById('mask').style.display = 'block';
            document.getElementById('rateBox').style.display = 'block';
        }""")
        page.wait_for_timeout(400)
        t = ck._rate_modal(page)
        check("识别到风控弹框", "频繁" in t, f"-> {t!r}")

        print("\n=== 3. 能关掉弹框并清掉遮罩（遮罩会拦截点击）===")
        closed = ck._close_rate_modal(page)
        check("执行了关闭动作", closed)
        page.wait_for_timeout(600)
        vis = page.evaluate("""() => {
            const m = document.getElementById('mask');
            const r = document.getElementById('rateBox');
            return {mask: getComputedStyle(m).display,
                    box: getComputedStyle(r).display};
        }""")
        check("遮罩已清除（不再拦截点击）", vis["mask"] == "none", str(vis))
        check("弹框已消失", vis["box"] == "none", str(vis))
        browser.close()

    # ---------- 4：限流 → 暂停 → 重试同一张 → 成功 ----------
    print("\n=== 4. 批量：限流 → 暂停 → 重试同一张 → 成功 ===")
    calls = []
    real_check_one = Checker.check_one

    def retry_check_one(self, rec, pg):
        calls.append(rec["idx"])
        if rec["idx"] == 1 and calls.count(1) <= 2:
            return fake_rate(rec)
        return fake_ok(rec)

    ck_mod.URL = "data:text/html,<h1>form</h1>"
    old_waits = ck_mod.RATE_PAUSE_WAITS
    ck_mod.RATE_PAUSE_WAITS = [1, 1, 1]
    Checker.check_one = retry_check_one
    try:
        ck2 = Checker(shots_dir=_SHOTS_TEST, headless=True,
                      log=lambda m: None, channel=eng)
        notices = []
        ck2.notice = lambda kind, sec, msg: notices.append((kind, sec))
        recs = [{"file": "a.pdf", "idx": 1, "number": "N1", "kind": "数电票"},
                {"file": "b.pdf", "idx": 2, "number": "N2", "kind": "数电票"}]
        res = ck2.run_batch(recs)
    finally:
        Checker.check_one = real_check_one
        ck_mod.RATE_PAUSE_WAITS = old_waits

    got = {r["idx"]: r["result"] for r in res}
    check("限流后自动重试同一张并成功", got.get(1) == "一致", str(got))
    check("第 2 张照常查验", got.get(2) == "一致", str(got))
    check("暂停通知已发出", any(k == "pause" for k, _ in notices),
          str(notices[:4]))
    check("倒计时通知已发出", any(k == "countdown" for k, _ in notices))
    check("恢复通知已发出", any(k == "resume" for k, _ in notices))
    check("未因限流丢掉任何发票", len(res) == 2, f"-> {len(res)} 行")

    # ---------- 5：连续超上限 → 停批 ----------
    print("\n=== 5. 连续超过上限 → 停批且保留已完成结果 ===")
    calls.clear()

    def always_rate(self, rec, pg):
        calls.append(rec["idx"])
        return fake_rate(rec)

    Checker.check_one = always_rate
    ck_mod.RATE_PAUSE_WAITS = [1, 1, 1]
    try:
        ck3 = Checker(shots_dir=_SHOTS_TEST, headless=True,
                      log=lambda m: None, channel=eng)
        recs = [{"file": "a.pdf", "idx": 1, "number": "N1", "kind": "数电票"},
                {"file": "b.pdf", "idx": 2, "number": "N2", "kind": "数电票"}]
        res3 = ck3.run_batch(recs)
    finally:
        Checker.check_one = real_check_one
        ck_mod.RATE_PAUSE_WAITS = old_waits
    check("停止原因=平台限流", ck3.stop_reason == "平台限流",
          f"-> {ck3.stop_reason!r}")
    check("停批后第 2 张未被执行", 2 not in calls, f"calls={calls}")
    left = [r for r in res3 if r["result"] == "未执行"]
    check("未跑的发票补成「未执行」", len(left) == 1 and left[0]["idx"] == 2,
          str([(r["idx"], r["result"]) for r in res3]))
    check("限流张被记录为平台限流",
          any(r["result"] == "平台限流" for r in res3))

    # ---------- 6：意外异常也要保住已完成结果 ----------
    print("\n=== 6. 意外异常中断 → 已完成结果不丢 ===")

    def boom(self, rec, pg):
        if rec["idx"] == 1:
            return fake_ok(rec)
        raise RuntimeError("模拟平台改版导致的选择器失效")

    Checker.check_one = boom
    try:
        ck4 = Checker(shots_dir=_SHOTS_TEST, headless=True,
                      log=lambda m: None, channel=eng)
        recs = [{"file": "a.pdf", "idx": 1, "number": "N1", "kind": "数电票"},
                {"file": "b.pdf", "idx": 2, "number": "N2", "kind": "数电票"},
                {"file": "c.pdf", "idx": 3, "number": "N3", "kind": "数电票"}]
        res4 = ck4.run_batch(recs)
    finally:
        Checker.check_one = real_check_one
    got4 = {r["idx"]: r["result"] for r in res4}
    check("异常前的结果被保留", got4.get(1) == "一致", str(got4))
    check("异常后的发票标「未执行」",
          got4.get(2) == "未执行" and got4.get(3) == "未执行", str(got4))
    check("未抛异常淹没结果（返回完整清单）", len(res4) == 3, f"-> {len(res4)}")
    check("中断原因已记录", "异常中断" in ck4.stop_reason, ck4.stop_reason)
    check("异常详情已记录", "选择器失效" in ck4.last_error, ck4.last_error)

    print(f"\n失败项：{bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
