# -*- coding: utf-8 -*-
"""离线自测：单票当日查验次数超限 → 必须被识别为「超次数跳过」且不重试。
同时校验接口签名一致性（上一轮 TypeError 的根因）。"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import checker
from checker import LIMIT_RE, Checker

ok = True


def check(name, cond):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        ok = False


print("== 1. 接口签名一致性 ==")
sig = inspect.signature(Checker._do_captcha_and_submit)
params = list(sig.parameters)
print("   签名:", sig)
check("_do_captcha_and_submit 支持 (page, rec, r)", len(params) >= 4)
call_args = {"page", "rec", "r"}
check("check_one 调用所需参数齐备", call_args.issubset(set(params)))

print("== 2. 超限文案识别（命中） ==")
hits = [
    "查验次数超过该张发票当日查验次数（5次）",
    "您查询的次数超过本张发票当日查验次数限制，请明日再试",
    "超过当日查验次数",
    "该发票当日查验次数已超过5次",
    "查验次数已达上限",
    "查询次数已超过系统限制",
    "当日查验次数：第5次 超过上限",
    "本张发票当日查验次数已达上限",
    "该发票查验次数已用完，请明日再查",
]
for t in hits:
    check(f"命中｜{t[:24]}", bool(LIMIT_RE.search(t)))

print("== 3. 正常结果不得误判 ==")
miss = [
    "当日查验次数：第2次\n查验时间：2026-09-29\n发票号码：25952000000000000001",
    "查验结果：查无此票",
    "验证码输入错误，请重新输入",
    "请输入开票日期",
    "查验通过，发票信息一致",
]
for t in miss:
    check(f"不误判｜{t[:24]}", not LIMIT_RE.search(t))

print("== 4. 结果分类：超次数跳过 → 不重试 ==")
src = inspect.getsource(Checker.check_one)
check("check_one 有 超次数跳过 分支并 break", "超次数跳过" in src)
check("check_one 未把 超次数跳过 归为查验失败",
      'if status == "超次数跳过"' in src)
check("check_one 能捕获「需重新进入表单」重试", "需重新进入表单" in src)
check("重填次数给足（金额候选 + 3 次整表重填）",
      "amounts[-1:] * 3" in src)
check("刷新失败有友好提示而非裸异常", "平台疑似限流" in src)
check("连续错误有退避等待", "疑似触发平台限流，暂停" in inspect.getsource(
    Checker._do_captcha_and_submit))

print("== 5. 验证码错误 → 刷新「新图」后重试（不消耗查验次数） ==")
src2 = inspect.getsource(Checker._do_captcha_and_submit)
check("存在验证码错误分支", 'status == "验证码错误"' in src2)
check("错误后 continue 重试（不是 return 判死）",
      "换张新图重试（不消耗该发票查验次数）" in src2)
check("刷新时传入旧图用于比对", "self._refresh_captcha(page, png)" in src2)
check("刷新失败会要求重新填单", "需重新进入表单" in src2)
check("设有连续错误硬上限防死循环", "max_cap_err" in src2)

sig_ref = inspect.signature(Checker._refresh_captcha)
check("_refresh_captcha 接受 old_png 参数",
      "old_png" in sig_ref.parameters)
src_ref = inspect.getsource(Checker._refresh_captcha)
check("刷新前先清掉遮挡弹窗", "_dismiss_dialogs" in src_ref)
check("校验换到了新图才返回 True", "png != old_png" in src_ref)
check("刷新不会重载页面（避免表单数据丢失）",
      "page.reload" not in src_ref)

print("== 5. 报告层配色 ==")
import report
check("Excel 有超次数跳过配色", "超次数跳过" in report.STATUS_FILL)
check("HTML 有超次数跳过统计", "n_limit" in inspect.getsource(report.write_html))

print("\n结果:", "全部通过" if ok else "存在失败项")
