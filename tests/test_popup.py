# -*- coding: utf-8 -*-
"""离线验证：平台浮层提示 → 结果判定（超次数 / 限流 / 票面有误 / 不误判）"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import checker

REPEAT = ["支持通过电子发票服务平台或增值税发票管理系统开具的各类增值税发票"
          "的在线查验。", "首页", "发票常识", "操作说明", "发票查验说明",
          "查验结果说明", "1、当日开具发票当日可查验。",
          "2、每份发票每天最多可查验5次。",
          "3、可查验最近5年内电子发票服务平台或增值税发票管理系统开具的发票。",
          "4、如遇个别浏览器版本无法查验，建议更换浏览器。",
          "扫描导入", "发票代码", "*发票号码", "*开票日期", "*价税合计",
          "*验证码", "请输入验证码图片中红色文字", "点击图片刷新", "查 验"]


def make_ck(popups, capval=""):
    ck = checker.Checker(log=lambda m: None)
    ck._popup_texts = lambda page: list(popups)
    ck._popup_visible = lambda page: bool(popups)
    ck._captcha_input = lambda page: None
    page = MagicMock()
    page.inner_text.return_value = "\n".join(REPEAT)
    page.wait_for_timeout = lambda ms: None
    return ck, page


def run(popups):
    ck, page = make_ck(popups)
    ok, status, detail, text = ck._read_result(page)
    return ok, status, detail


CASES = [
    (["单张发票每天最多查验5次!"], "超次数跳过", True),
    (["单张发票每天最多可查验5次"], "超次数跳过", True),
    (["您的访问过于频繁，请稍后再试"], "平台限流", False),
    (["发票号码有误！"], "票面信息有误", True),
    (["发票代码有误！"], "票面信息有误", True),
]

print("=== 浮层提示 → 判定 ===")
bad = 0
for pops, want_status, _want_ok in CASES:
    ok, status, detail = run(pops)
    good = (status == want_status)
    bad += 0 if good else 1
    print(("PASS " if good else "FAIL "), f"{pops} -> {status} | {detail[:34]}")

print("\n=== 不误判（body 里的常驻文案）===")
# 侧栏常驻文案不得被判为超次数
ck, page = make_ck([])
ok, status, detail, text = ck._read_result(page)
good = status != "超次数跳过"
bad += 0 if good else 1
print(("PASS " if good else "FAIL "), f"无浮层时 -> {status}（不应为超次数跳过）")

print("\n=== 正则边界 ===")
for t, exp in [("2、每份发票每天最多可查验5次。", False),
               ("单张发票每天最多查验5次!", True),
               ("本平台支持查验最近5年内发票", False)]:
    got = bool(checker.LIMIT_RE.search(t))
    good = got == exp
    bad += 0 if good else 1
    print(("PASS " if good else "FAIL "), repr(t[:30]), "->", got)

print("\n=== 验证码提示解析 ===")
import captcha as cap
for p, want in [("请输入验证码图片中蓝色文字", "蓝"),
                ("请输入验证码图片中红色文字", "红"),
                ("请输入验证码图片中黄色文字", "黄"),
                ("请输入验证码图片中黑色文字", "黑"),
                ("请输入验证码文字", None)]:
    got = cap.parse_prompt(p)["color"]
    good = got == want
    bad += 0 if good else 1
    print(("PASS " if good else "FAIL "), repr(p), "-> 颜色", got)

print("\n结果:", "全部通过" if bad == 0 else f"{bad} 项失败")
sys.exit(1 if bad else 0)
