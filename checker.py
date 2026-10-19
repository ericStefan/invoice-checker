# -*- coding: utf-8 -*-
"""
查验流程模块：Playwright 有头浏览器逐张查验
- 日历组件：直接用 JS 给输入框赋值并派发事件，绕开日历选择
- 金额口径失败自动换一种口径重试一次
- 验证码：自动识别 N 次 → 人工点选兜底
- 结果分类：查验一致 / 冲红或作废 / 信息不一致 / 查验失败
"""
import io
import random
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = "https://inv-veri.chinatax.gov.cn/fpcygzfw/national-invoice-check"

# 第三方统计/广告域名（拦截以提速，绝不碰税务平台自身域名）
BLOCK_HOSTS = ("hm.baidu.com", "cnzz.com", "google-analytics.com",
               "googletagmanager.com", "doubleclick.net", "51.la",
               "umeng.com", "umengcloud.com", "tajs.qq.com")

LAUNCH_ARGS = [
    "--start-maximized",
    "--disable-blink-features=AutomationControlled",
    # 关键：窗口被工具窗口遮挡时 Chromium 会节流后台渲染 → 页面“半天加载不出来”
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--no-first-run",
    "--no-default-browser-check",
]


def detect_engine() -> str | None:
    """检测本机浏览器内核，返回 "chrome" / "msedge" / None。
    用本机内核加载平台页面更顺（沿用系统代理设置）。
    注意：不能用 %ProgramFiles% 环境变量——实测该机未设置，会导致误判为未安装。"""
    import os
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    pf86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    local = os.path.expanduser("~")
    cands = [
        ("chrome", os.path.join(pf, r"Google\Chrome\Application\chrome.exe")),
        ("chrome", os.path.join(pf86, r"Google\Chrome\Application\chrome.exe")),
        ("chrome", os.path.join(local, r"AppData\Local\Google\Chrome"
                                      r"\Application\chrome.exe")),
        ("msedge", os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe")),
        ("msedge", os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe")),
    ]
    for name, p in cands:
        try:
            if Path(p).exists():
                return name
        except Exception:
            continue
    return None


# ---------- 页面选择器（2026-09-28 实测校准，TDesign 新版平台） ----------
# 表单为单一表单，5 个 t-input__inner 依次：发票代码/发票号码/开票日期/金额/验证码
# 验证码为「图片+文字输入」，点图片刷新；查验按钮文字为「查 验」带空格
SEL = {
    "inputs": "input.t-input__inner",
    "date":      ["input[placeholder='YYYYMMDD']"],
    "captcha_img": ["img[src^='data:image']", "img[src*='captcha']",
                    "img[src*='verify']"],
    "submit":    ["text=/查\\s*验/"],
    "refresh":   ["text=点击图片刷新"],
    "tab_dianzi": ["text=数电"],
    "tab_old":    ["text=增值税发票"],
}

RESULT_KEYWORDS = {
    "冲红": ["红冲", "红字", "已冲红"],
    "作废": ["作废", "已作废"],
    "不一致": ["不一致", "查验不一致"],
    # 查无此票必须**独立于"一致"**：平台在详情视图里也会给出
    # 「查验结果：查无此票」，旧版按票面状态无条件判"一致"（已实测踩坑）。
    # 它代表"发票信息有误或该票不存在"，属于需要人工复核的一类。
    "查无此票": ["查无此票", "无此票"],
    "一致": ["查验通过", "信息一致", "查验成功"],
    "失败": ["不存在", "错误", "失败"],
}

# 单张发票当日查验次数超限（平台限制：每天 5 次）。命中即跳过本张、不再重试，
# 避免程序把它当成"验证码失败"反复刷新空耗。
#
# ⚠ 关键：平台**侧边栏**常驻文案「2、每份发票每天最多可查验5次。」必须排除，
#   否则每轮都会误判。真实弹窗文案是「单张发票每天最多查验5次!」（实测 2026-09-30），
#   故只认「单张发票」前缀或明确的"超过…次"表述。
LIMIT_RE = re.compile(
    r"单张发票[^。\n\r；;]{0,20}(最多|超过|已达|上限|次)"
    r"|超过[^。\n\r；;]{0,12}(查验次数|查询次数|五次|5\s*次)"
    r"|(查验次数|查询次数)[^。\n\r；;]{0,12}(超过|已达上限|已用尽|已用完)"
    r"|(查验|查询)次数[^。\n\r；;]{0,8}(用完|用尽|超限)"
    r"|(已达|达到|超出)[^。\n\r；;]{0,10}(上限|限制)[^。\n\r；;]{0,6}(次数)?")

# 票面信息错误（前端校验）：重试无意义 → 立即判该张失败，不再空转
BADINFO_RE = re.compile(
    r"(发票号码|发票代码|开票日期|开票金额|开具金额|价税合计|校验码)"
    r"[^。\n\r；;]{0,8}(有误|不正确|不合法|格式错误|错误)")

# 平台必填未填（可重试：多半是填单时序问题）
EMPTY_RE = re.compile(
    r"请输入(发票号码|发票代码|开票日期|开具金额|价税合计|校验码)")

# 平台频率限制（实测 2026-09-30：短时间内高频提交会返回
# 「您的访问过于频繁，请稍后再试」→ 此后提交可能被静默丢弃）
RATE_LIMIT_RE = re.compile(
    r"(访问(过于)?频繁|请求(过于)?频繁|请稍后再试|访问受限|操作频繁"
    r"|网络繁忙|系统繁忙)")

# 平台提示浮层容器（实测：会遮挡并拦截点击，必须主动清理）
POPUP_SEL = ".popup-container"

# ---------- 平台风控（限流）处理参数（2026-09-30 用户反馈修复） ----------
# 现象：平台会弹「您的访问过于频繁，请稍后再试」的**模态框**（带半透明遮罩，
#       会拦截点击）。它既不在 .popup-container（普通提示浮层）里，也不在
#       .t-dialog（结果弹窗）里，所以旧版完全没识别到 →
#       程序照常在弹层后面继续填单/提交，越撞越重，界面看着像"卡住还在跑"。
# 处理：命中即关弹层 + **暂停整批**（退避等待），然后重试同一张；
#       连续多次仍限流则停止本批（保留已完成结果，可稍后用导入Excel重跑）。
RATE_MODAL_SEL = ("[class*=layer],[class*=layui],[class*=mask],[class*=shade],"
                  "[class*=modal],[class*=dialog],[class*=popup],[class*=alert],"
                  "[class*=tip],[class*=notice],[class*=message],[role=dialog]")
RATE_PAUSE_WAITS = [60, 180, 300]   # 每次暂停的秒数（逐次拉长）
RATE_MAX_PAUSE = 3                  # 连续暂停超过此次数 → 停止整批

# ---------- 验证码「择优」策略参数（2026-09-30 实测调整） ----------
# 思路：**不强行识别每一张验证码**，而是只挑「识别清晰」的那张提交。
# 依据：刷新验证码只是重新取图，**不消耗发票的当日查验次数**（零成本）；
#       而填错一次会消耗一次平台尝试、拖慢整张速度、并可能触发频率风控。
# 所以"换一张更简单的图"永远比"硬填一张没把握的图"划算。
CONF_GOOD = 0.60      # ≥ 此置信度：认定"简单验证码"，直接提交
CONF_OK = 0.42        # ≥ 此置信度：已换过几张仍无更好的 → 可作为可用结果
PICK_MAX = 8          # 单张发票最多换 8 次图去找"简单验证码"


def _find(page, candidates, timeout=3000):
    """按候选选择器列表找到第一个可见元素"""
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


def _fill(page, loc, value):
    """输入框通用填充：JS 赋值 + 派发事件 + JS 失焦（不用鼠标点击空白处，
    避免误点菜单/链接跳页——已实测踩坑）"""
    try:
        loc.click(timeout=2000)
    except Exception:
        pass
    loc.evaluate(
        """(el, v) => {
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, v);
            el.dispatchEvent(new Event('input',  {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new Event('blur',   {bubbles: true}));
        }""", value)
    try:
        page.evaluate("() => document.activeElement && document.activeElement.blur()")
    except Exception:
        pass


def _fill_date(page, loc, raw):
    """日期专用（已实测）：TDesign 日期组件不接受键盘输入与 JS 赋值，
    只能走日历面板点选：
      1. 点年下拉 → 选目标年；2. 点月下拉 → 选目标月；
      3. 点日期格（排除 additional/disabled 格）。
    通过校验提示「请输入开票日期」是否消失来判定是否生效。"""
    raw = re.sub(r"\D", "", raw)
    if len(raw) != 8:
        return
    y, m, d = int(raw[:4]), int(raw[4:6]), int(raw[6:])

    def _hint_gone() -> bool:
        try:
            return "请输入开票日期" not in page.inner_text("body")
        except Exception:
            return True

    def _click_option(page, allowed: set) -> bool:
        """在下拉/面板里找文本精确匹配（去空格）的可见选项并点击。
        严格限定作用域，避免误点页面其他元素导致跳页。"""
        scopes = [".t-select-option:visible",
                  ".t-date-picker__panel .t-date-picker__cell-inner:visible"]
        for sel in scopes:
            els = page.locator(sel)
            for i in range(min(els.count(), 300)):
                try:
                    el = els.nth(i)
                    t = re.sub(r"\s+", "", el.inner_text(timeout=300) or "")
                    if t in allowed:
                        el.click(timeout=1000)
                        return True
                except Exception:
                    continue
        return False

    try:
        loc.click(timeout=2000)
        page.wait_for_timeout(600)
        panel = page.locator(".t-date-picker__panel").first
        panel.wait_for(state="visible", timeout=3000)

        # 1) 选年
        page.locator(".t-date-picker__header-controller-year").first.click()
        page.wait_for_timeout(500)
        _click_option(page, {str(y), f"{y}年"})
        page.wait_for_timeout(500)

        # 2) 选月（选项文本可能是 1 / 01 / 1月 / 01月）
        page.locator(".t-date-picker__header-controller-month").first.click()
        page.wait_for_timeout(500)
        _click_option(page, {str(m), f"{m:02d}", f"{m}月", f"{m:02d}月"})
        page.wait_for_timeout(500)

        # 3) 点日期格（排除相邻月与禁用格）
        day_cells = panel.locator(
            ".t-date-picker__cell:not([class*='additional'])"
            ":not([class*='disabled']) .t-date-picker__cell-inner")
        hit = day_cells.filter(has_text=re.compile(rf"^{d}$")).first
        hit.click(timeout=2000)
        page.wait_for_timeout(500)
    except Exception:
        pass
    if _hint_gone():
        return
    # 兜底：JS 赋值（个别老版本页面可能吃这一套）
    try:
        loc.evaluate(
            """(el, v) => {
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, v);
                el.dispatchEvent(new Event('input',  {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""", raw)
        page.evaluate("() => document.activeElement && document.activeElement.blur()")
        page.wait_for_timeout(300)
    except Exception:
        pass


def _classify_result(text: str) -> tuple[str, str]:
    """按关键词给结果分级；返回 (状态, 明细)"""
    t = text.replace(" ", "")
    for status, kws in RESULT_KEYWORDS.items():
        for kw in kws:
            if kw in t:
                detail = _extract_detail(text)
                return status, detail
    return "", ""


def _extract_detail(text: str) -> str:
    """截取结果关键句（含关键词的一行/前后片段）"""
    for line in text.splitlines():
        line = line.strip()
        if any(kw in line for kws in RESULT_KEYWORDS.values() for kw in kws):
            return line[:120]
    return text.strip()[:120]


class ManualSolver:
    """人工输入桥：工作线程请求 → GUI 主线程弹窗 → 返回文本/点"""
    def __init__(self):
        import queue
        self.q_req = queue.Queue()
        self.q_res = queue.Queue()

    def request(self, img_bytes, prompt, mode_default="click", guess=None):
        self.q_req.put({"img": img_bytes, "prompt": prompt,
                        "mode": mode_default, "guess": guess})
        return self.q_res.get()   # 阻塞直到 GUI 回填


class Checker:
    def __init__(self, shots_dir="shots", headless=False,
                 manual: ManualSolver | None = None,
                 log=print, auto_captcha_tries=4, manual_captcha_tries=3,
                 channel: str | None = None, notice=None):
        self.shots = Path(shots_dir)
        self.shots.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.manual = manual
        self.log = log
        self.auto_tries = auto_captcha_tries
        self.manual_tries = manual_captcha_tries
        self.channel = channel          # "chrome" = 用本机 Chrome 内核
        self.notice = notice            # 通知界面（限流暂停/恢复/倒计时）
        self._stop = False
        self._rate_flag = ""            # JS alert 捕获到的限流原文
        self.stop_reason = ""           # 本批中断原因（"" = 正常跑完）
        self.last_error = ""            # 异常中断的具体信息（供界面展示）

    def stop(self):
        self._stop = True

    # ---------------- 单张查验 ----------------
    def check_one(self, rec: dict, page) -> dict:
        """rec 含 code/number/date/amount/amount_alt/checkcode"""
        r = dict(rec)
        r["result"], r["detail"], r["shot"] = "查验失败", "未执行", ""
        try:
            amounts = self._amount_candidates(rec)
            # 末尾额外几次整表重填：应对"表单没填上/页面跳失/验证码图刷不动"等异常
            attempts = amounts + amounts[-1:] * 3
            limit_recheck = 0       # "超次数"冗余复核已用次数（最多 1 次）
            for attempt, amount in enumerate(attempts):
                try:
                    self._goto_form(page, rec)
                    self._fill_form(page, rec, amount)
                    # 填单后先看有没有前端校验错误（票面信息有误 = 重试无用）
                    bad = self._check_bad_info(page)
                    if bad:
                        self.log(f"    ⚠ 平台校验未通过：{bad}")
                        r["result"] = "票面信息有误"
                        r["detail"] = (f"平台校验未通过：{bad}"
                                       f"（请核对号码/代码/日期/金额与票面是否一致）")
                        break
                    ok, status, detail, text = self._do_captcha_and_submit(page, rec, r)
                except RuntimeError as e:
                    msg = str(e)
                    if ("页面意外跳转" in msg or "需重新进入表单" in msg) \
                            and attempt < len(attempts) - 1:
                        self.log("    页面异常，重新进入表单重试…")
                        continue
                    raise
                if not ok:                      # 验证码始终没过 / 表单校验失败
                    if status == "平台限流":
                        # 立刻结束本张（不浪费额度、不换金额口径重试），
                        # 由 run_batch 暂停整批后重新走这张
                        r["result"], r["detail"] = "平台限流", detail
                        break
                    if status == "表单校验失败" and attempt < len(attempts) - 1:
                        # 表单没填上（时序问题）→ 重填再试，不消耗查验次数
                        self.log(f"    表单校验未通过（{detail}），重新填单重试…")
                        continue
                    if status == "表单校验失败":
                        r["result"] = "查验失败"
                        r["detail"] = f"表单未通过平台校验（{detail}）"
                    else:
                        r["result"], r["detail"] = "查验失败", detail
                    break
                if status == "不一致" and attempt < len(amounts) - 1:
                    self.log(f"    金额口径({amount})不一致，换口径重试…")
                    r["shot"] = ""       # 允许最终那次结果覆盖截图
                    continue
                if status == "超次数跳过":
                    # ---- 冗余复核（用户反馈"超次数"出现得过于频繁）----
                    # 重新完整填单提交一次，只有**再次**得到同一提示才坐实。
                    # 安全性：平台在业务校验阶段就拦截超限，不会真的消耗额度，
                    #         所以复核不会造成任何损失（这也是"再查一次"可行
                    #         而"查有误信息"不可行的原因）。
                    if limit_recheck < 1:
                        limit_recheck += 1
                        # 先兜底写入结果：万一复核途中异常退出，也不会丢状态
                        r["result"], r["detail"] = status, detail
                        r["shot"] = ""      # 丢弃本轮截图，避免留存"误判"的证据
                        self.log("    ⚠ 提示当日查验次数已用尽 → 自动二次复核一次…")
                        continue
                    self.log("    ✓ 二次复核结果一致，确认该票今日次数确实已用尽")
                    r["result"] = status
                    r["detail"] = detail + "（已自动二次复核确认）"
                    break
                r["result"] = "查验失败" if status == "失败" else status
                r["detail"] = detail
                break
        except RuntimeError as e:
            msg = str(e)
            if "需重新进入表单" in msg:
                r["result"] = "查验失败"
                r["detail"] = ("验证码图片始终刷不出新图（平台疑似限流），"
                               "建议稍后重跑或改用「验证码人工输入」模式")
            else:
                r["result"], r["detail"] = "查验失败", f"流程异常: {msg}"
        except Exception as e:
            r["result"], r["detail"] = "查验失败", f"流程异常: {e}"
        # 兜底：正常路径已在读取结果时立即截图并关窗；异常路径在此补齐
        self._capture_and_close(page, rec, r, r.get("result"))
        return r

    @staticmethod
    def _amount_candidates(rec) -> list:
        """金额候选：数电优先价税合计，老票优先不含税；失败换另一个"""
        a, b = rec.get("amount") or "", rec.get("amount_alt") or ""
        if rec.get("kind") == "数电票":
            first, second = a, b
        else:
            first, second = a, b
        cands = [x for x in (first, second) if x]
        return cands or [""]

    def _goto_form(self, page, rec):
        """打开查验页（带重试，平台偶发慢/限流）。单一表单自动按号码
        长度适配数电票/老票，无需点页签。

        ⚡ 提速：若当前**已经停在表单页且表单可交互**，直接复用、不重载
        —— 每张省 5~10 秒的整页加载时间（批量查验时体感差别很大）。"""
        self._rate_flag = ""            # 每张票重新判定风控状态
        try:
            if "national-invoice-check" in (page.url or "") and \
                    page.locator("input.t-input__inner").first.is_visible(
                        timeout=800):
                self._dismiss_dialogs(page)
                self.log("    复用当前页面，直接填单…")
                return
        except Exception:
            pass
        last = None
        for attempt in range(3):
            try:
                self.log("    正在打开查验平台页面…")
                page.goto(URL, wait_until="domcontentloaded", timeout=90000)
                # Vue 应用渲染要数秒，必须等表单真正出现
                page.locator("input.t-input__inner").first.wait_for(
                    state="visible", timeout=25000)
                self.log("    页面加载完成，开始填单…")
                break
            except Exception as e:
                last = e
                self.log(f"    页面第 {attempt + 1} 次加载失败，"
                         f"正在重试…（{type(e).__name__}）")
                if attempt == 2:
                    raise RuntimeError("页面加载失败") from e
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
        page.wait_for_timeout(600)

    def _form_fields(self, page) -> dict:
        """按「字段标签」映射到输入框（不再按 nth 序号，避免数电票/老票
        表单字段数量变化导致错位——已实测号码被填进代码框）。
        返回 {'发票代码': loc, '发票号码': loc, '开票日期': loc,
              '金额': loc, '验证码': loc}（缺什么没什么）"""
        labels = page.evaluate(
            """() => {
              const KEYS = ['发票代码','发票号码','开票日期','开具金额','价税合计','验证码'];
              const ins = Array.from(document.querySelectorAll('input.t-input__inner'))
                .filter(i => i.offsetParent !== null && !i.readOnly);
              return ins.map(i => {
                let n = i, hops = 0, lab = '';
                while (n && hops < 7) {
                  const t = n.innerText || '';
                  for (const k of KEYS) if (t.includes(k)) { lab = k; break; }
                  if (lab) break;
                  n = n.parentElement; hops++;
                }
                return lab;
              });
            }""")
        ins = page.locator("input.t-input__inner:visible:not([readonly])")
        fields = {}
        for i, lab in enumerate(labels):
            if not lab:
                continue
            key = "金额" if lab in ("开具金额", "价税合计") else lab
            fields.setdefault(key, ins.nth(i))
        # 日期框是 readonly，被上面的过滤排除了 → 按 placeholder 单独补
        d = page.locator("input[placeholder='YYYYMMDD']")
        if d.count() > 0:
            fields.setdefault("开票日期", d.first)
        # 验证码框标签藏得深，但 form-item 有专属类名 t-form-item__yzm
        yzm = page.locator(".t-form-item__yzm input.t-input__inner")
        if yzm.count() > 0:
            fields["验证码"] = yzm.first
        return fields

    def _fill_form(self, page, rec, amount):
        """按标签填表；数电票（20 位号码）不填发票代码"""
        fields = self._form_fields(page)
        if "发票号码" not in fields:
            raise RuntimeError("找不到发票号码输入框，页面可能改版或加载失败")
        _fill(page, fields["发票号码"], rec["number"])
        # 老票才填发票代码；数电票即使页面有代码框也留空（平台按号码长度识别）
        if rec.get("code") and rec.get("kind") != "数电票" and "发票代码" in fields:
            _fill(page, fields["发票代码"], rec["code"])
        if "开票日期" in fields:
            _fill_date(page, fields["开票日期"], rec.get("date", ""))
        if amount and "金额" in fields:
            _fill(page, fields["金额"], amount)
        page.wait_for_timeout(250)

    def _check_bad_info(self, page) -> str:
        """填单后检查平台前端校验：返回错误文本（票面信息有误）或空串。
        实测平台会在字段右侧显示 tip_common_wrong（如「发票号码有误！」）。
        这类错误重试无意义 → 直接判该张失败，避免无谓空转。
        顺带清掉可能弹出的浮层（否则会拦截后续点击）。"""
        try:
            t = page.evaluate(
                """() => {
                    let out = '';
                    document.querySelectorAll('.tip_common_wrong').forEach(el => {
                      const s = (el.innerText || '').trim();
                      if (s && s.length < 60) out += ' ' + s;
                    });
                    return out.trim();
                }""") or ""
        except Exception:
            t = ""
        probe = (t + " " + " ".join(self._popup_texts(page))).strip()
        if probe:
            self._close_popups(page)
        m = BADINFO_RE.search(probe)
        return m.group(0) if m else ""

    # ---------------- 验证码 + 提交 ----------------
    def _ensure_captcha_loaded(self, page):
        """验证码懒加载：占位可能是图片也可能是文字链接（平台两种形态
        都出现过），两种都点；之后等真验证码图出现"""
        for _ in range(4):
            if self._find_captcha_img(page) is not None:
                return True
            clicked = False
            # 1) 文字形态：「点击获取验证码」「点击验证码」
            for sel in ["text=点击获取验证码", "text=点击验证码"]:
                try:
                    ph = page.locator(sel).first
                    if ph.is_visible(timeout=600):
                        ph.click()
                        clicked = True
                        break
                except Exception:
                    continue
            # 2) 图片形态：占位图 / 验证码图本身（点图也会刷新）
            if not clicked:
                for sel in ["img[src*='code.']", "img[src*='code?']",
                            "img[src*='captcha']", "img[src^='data:image']"]:
                    try:
                        ph = page.locator(sel).first
                        if ph.is_visible(timeout=500):
                            ph.click()
                            clicked = True
                            break
                    except Exception:
                        continue
            if not clicked:
                return False
            page.wait_for_timeout(1500)
        return self._find_captcha_img(page) is not None

    def _captcha_input(self, page):
        """验证码输入框：按标签定位"""
        return self._form_fields(page).get("验证码")

    def _find_captcha_img(self, page):
        """找真正的验证码图：data-uri 图中宽度≥80、离验证码输入框最近的那张
        （DOM 第一张 base64 图可能是税徽 logo——已实测踩坑）"""
        cap_in = self._captcha_input(page)
        in_box = cap_in.bounding_box() if cap_in is not None else None
        imgs = page.locator("img[src^='data:image']")
        best, best_d = None, 1e9
        for i in range(imgs.count()):
            loc = imgs.nth(i)
            try:
                box = loc.bounding_box()
            except Exception:
                continue
            if not box or box["width"] < 80 or box["height"] < 25:
                continue
            d = 0
            if in_box:
                d = abs(box["y"] - in_box["y"]) + abs(box["x"] - in_box["x"])
            if d < best_d:
                best, best_d = loc, d
        return best

    def _popup_texts(self, page) -> list:
        """读取平台**可见**的提示浮层文本（.popup-container > .popup-message）。
        实测这类浮层会以固定定位遮住表单并**拦截点击**，且出现几秒后可能自动
        消失 —— 所以必须"看到就记下来"，不能只看最终页面文本。"""
        try:
            return page.evaluate(
                """() => {
                  const out = [];
                  document.querySelectorAll('.popup-container').forEach(el => {
                    const st = getComputedStyle(el);
                    if (st.display === 'none' || st.visibility === 'hidden')
                      return;
                    const m = el.querySelector('.popup-message');
                    const t = m ? (m.innerText || '').trim() : '';
                    if (t) out.push(t);
                  });
                  return out;
                }""")
        except Exception:
            return []

    def _popup_visible(self, page) -> bool:
        try:
            return page.evaluate(
                """() => {
                  let n = 0;
                  document.querySelectorAll('.popup-container').forEach(el => {
                    const st = getComputedStyle(el);
                    if (st.display !== 'none' && st.visibility !== 'hidden')
                      n++;
                  });
                  return n > 0;
                }""")
        except Exception:
            return False

    def _close_popups(self, page) -> bool:
        """关掉平台提示浮层：优先点面板里的「确定/取消」，点不到就直接隐藏容器。
        返回是否原本有可见浮层（用于日志/判定）。"""
        had = self._popup_visible(page)
        if not had:
            return False
        try:
            page.evaluate(
                """() => {
                  document.querySelectorAll('.popup-container').forEach(el => {
                    const st = getComputedStyle(el);
                    if (st.display === 'none') return;
                    const btns = Array.from(
                      el.querySelectorAll('.popup-panel button'));
                    const pick = btns.find(b => (b.innerText || '')
                                  .replace(/\\s+/g, '').includes('确定'))
                              || btns[btns.length - 1];
                    if (pick) { try { pick.click(); } catch (e) {} }
                    // 兜底：若点击无效（无按钮/被拦），直接隐藏，避免遮挡后续操作
                    setTimeout(() => {
                      if (getComputedStyle(el).display !== 'none') {
                        el.style.display = 'none';
                      }
                    }, 120);
                  });
                }""")
            page.wait_for_timeout(400)
        except Exception:
            pass
        # 彻底兜底：仍可见就隐藏
        if self._popup_visible(page):
            try:
                page.evaluate(
                    """() => document.querySelectorAll('.popup-container')
                        .forEach(el => { el.style.display = 'none'; })""")
                page.wait_for_timeout(200)
            except Exception:
                pass
        return had

    def _rate_modal(self, page) -> str:
        """探测平台自己弹的**风控模态框**（「您的访问过于频繁，请稍后再试」），
        返回命中原文（'' = 没有）。

        为什么单独做：这类框是页面里的 HTML 模态层（老式 UI 库风格），
        带半透明遮罩 → **会拦截点击**，而且不在 .popup-container / .t-dialog
        之列。旧版只按"本张失败"处理，于是继续在弹层后面填单提交，
        越撞越重（用户 2026-09-30 反馈："弹出框后面还在执行"）。
        """
        if self._rate_flag:
            return self._rate_flag
        js = """(sel) => {
          const RE = /(访问[^，。！？\\n]{0,4}频繁|请求[^，。！？\\n]{0,4}频繁|操作[^，。！？\\n]{0,4}频繁|请稍后再试|访问受限|系统繁忙|网络繁忙|过于频繁)/;
          // 页面常驻的说明文案要排除（如"如遇…无法查验，建议更换浏览器"）
          const STATIC = /(最多可查验|查验次数|当日开具|可查验最近|浏览器版本)/;
          const BTN = /^(确定|确\\s*定|关闭|关\\s*闭|我知道了|知道了|好|OK)$/;
          const vis = el => {
            const st = getComputedStyle(el);
            if (st.display === 'none' || st.visibility === 'hidden') return false;
            if (parseFloat(st.opacity || '1') === 0) return false;
            const r = el.getBoundingClientRect();
            return r.width > 60 && r.height > 24;
          };
          // 必须像"弹框"：里面有确认按钮，或自身/近祖先是高 z-index 定位层
          const modalish = el => {
            const bs = Array.from(el.querySelectorAll(
                'button, a, [class*=btn], input[type=button], input[type=submit]'))
              .map(b => (b.innerText || b.value || '').replace(/\\s+/g, ''));
            if (bs.some(t => BTN.test(t))) return true;
            let p = el;
            for (let k = 0; k < 4 && p; k++, p = p.parentElement) {
              const st = getComputedStyle(p);
              if ((st.position === 'fixed' || st.position === 'absolute')
                  && parseInt(st.zIndex || '0', 10) >= 100) return true;
            }
            return false;
          };
          const hits = [];
          document.querySelectorAll(sel).forEach(el => {
            if (!vis(el)) return;
            const t = (el.innerText || '').replace(/\\s+/g, '');
            if (!t || t.length > 120) return;
            if (!RE.test(t) || STATIC.test(t)) return;
            if (!modalish(el)) return;
            hits.push(t);
          });
          if (!hits.length) return '';
          hits.sort((a, b) => a.length - b.length);
          return hits[0];
        }"""
        try:
            t = page.evaluate(js, RATE_MODAL_SEL) or ""
        except Exception:
            return ""
        if t:
            self._rate_flag = t
            try:
                self.log(f"    ⛔ 检测到平台风控弹层：{t[:40]}")
            except Exception:
                pass
        return t

    def _close_rate_modal(self, page) -> bool:
        """关掉风控模态框：优先点「确定/关闭」，再把半透明遮罩清掉。
        必须清遮罩 —— 遮罩会拦截点击，导致后续按钮根本点不动。"""
        if not (self._rate_flag or self._rate_modal(page)):
            return False            # 没有风控框时不要动页面上的遮罩
        js = """(sel) => {
          const RE = /(访问[^，。！？\\n]{0,4}频繁|请稍后再试|访问受限|系统繁忙|网络繁忙|过于频繁)/;
          const STATIC = /(最多可查验|查验次数|当日开具|可查验最近|浏览器版本)/;
          const vis = el => {
            const st = getComputedStyle(el);
            if (st.display === 'none' || st.visibility === 'hidden') return false;
            const r = el.getBoundingClientRect();
            return r.width > 60 && r.height > 24;
          };
          const BTN = /^(确定|确\\s*定|关闭|关\\s*闭|我知道了|知道了|好|OK)$/;
          let n = 0;
          document.querySelectorAll(sel).forEach(el => {
            if (!vis(el)) return;
            const t = (el.innerText || '').replace(/\\s+/g, '');
            if (!t || t.length > 120 || !RE.test(t) || STATIC.test(t)) return;
            // 注意：老式 UI 库的按钮常常不是 <button>（可能是 a / span / div）
            const btns = Array.from(el.querySelectorAll(
                'button, a, [class*=btn], input[type=button],'
                + ' input[type=submit], span, div'))
              .filter(b => {
                const s = (b.innerText || b.value || '').replace(/\\s+/g, '');
                return s && s.length <= 8 && BTN.test(s);
              });
            if (btns.length) {
              try { btns[btns.length - 1].click(); n++; } catch (e) {}
            }
          });
          // 清遮罩：半透明/整屏的蒙层会拦截点击（实体面板不动）
          document.querySelectorAll(
            '[class*=mask], [class*=shade], [class*=overlay], [class*=backdrop],'
            + ' [id*=mask], [id*=shade], [id*=overlay]'
          ).forEach(el => {
            const st = getComputedStyle(el);
            if (st.display === 'none' || st.visibility === 'hidden') return;
            if (st.position !== 'fixed' && st.position !== 'absolute') return;
            const r = el.getBoundingClientRect();
            const big = r.width * r.height >= innerWidth * innerHeight * 0.35;
            const translucent = parseFloat(st.opacity || '1') <= 0.9;
            if (!(big || translucent)) return;
            try { el.style.display = 'none'; n++; } catch (e) {}
          });
          return n;
        }"""
        try:
            n = page.evaluate(js, RATE_MODAL_SEL) or 0
        except Exception:
            n = 0
        try:
            page.wait_for_timeout(300)
        except Exception:
            pass
        # 仍可见 → 直接隐藏命中的模态层（兜底，避免遮罩常驻）
        if n and self._rate_modal(page):
            try:
                page.evaluate("""(sel) => {
                  const RE = /(访问[^，。！？\\n]{0,4}频繁|请稍后再试|过于频繁|系统繁忙|网络繁忙)/;
                  document.querySelectorAll(sel).forEach(el => {
                    const t = (el.innerText || '').replace(/\\s+/g, '');
                    if (t && t.length <= 120 && RE.test(t)) {
                      el.style.display = 'none';
                    }
                  });
                }""", RATE_MODAL_SEL)
                page.wait_for_timeout(200)
            except Exception:
                pass
        return bool(n)

    def _install_dialog_hook(self, page):
        """捕获浏览器原生 alert/confirm 文案。有些风控是 window.alert 弹的，
        不接管就会被静默吞掉（Playwright 默认自动关掉），拿不到证据。"""
        def _on_dialog(dlg):
            try:
                msg = (dlg.message or "").strip()
                if msg:
                    self.log(f"    ⓘ 平台弹窗：{msg[:60]}")
                if RATE_LIMIT_RE.search(msg):
                    self._rate_flag = msg
                dlg.accept()
            except Exception:
                pass
        try:
            page.on("dialog", _on_dialog)
        except Exception:
            pass

    def _sleep_countdown(self, seconds: int) -> bool:
        """暂停等待；每秒把剩余秒数推给界面做倒计时，可被「停止」随时打断。
        返回 True = 等满了，False = 用户中途停止。"""
        for left in range(seconds, 0, -1):
            if self._stop:
                return False
            if self.notice:
                try:
                    self.notice("countdown", left, "")
                except Exception:
                    pass
            if left == seconds or left % 20 == 0:
                self.log(f"    ⏸ 暂停等待中…剩余 {left} 秒")
            time.sleep(1)
        return True

    def _dismiss_dialogs(self, page) -> bool:
        """关掉遮住表单的提示浮层（超次数/风控/验证码错误/结果窗口），
        保证验证码图可点、表单可操作（这是「刷新没生效」的根因之一）"""
        done = self._close_popups(page)
        # 平台风控模态框（带遮罩，拦截一切点击）→ 一并关掉
        try:
            if self._rate_modal(page):
                done = self._close_rate_modal(page) or done
        except Exception:
            pass
        try:
            if page.locator(".t-dialog:visible").count() == 0:
                return done
        except Exception:
            return done
        try:
            return self._close_result(page) or done
        except Exception:
            return done

    def _dump_page(self, page, rec, tag) -> str:
        """把页面文本落盘，便于排查未知结果形态"""
        try:
            from datetime import datetime
            logs = self.shots.parent / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            num = (rec or {}).get("number") or "unknown"
            p = logs / f"{num}_{tag}_{datetime.now():%H%M%S}.txt"
            p.write_text(page.inner_text("body"), encoding="utf-8")
            return str(p)
        except Exception:
            return ""

    def _grab_captcha(self, page, tries=4):
        """拿到验证码图（必要时触发加载），返回 (loc, png)"""
        for _ in range(tries):
            loc = self._find_captcha_img(page)
            if loc is None:
                self._ensure_captcha_loaded(page)
                loc = self._find_captcha_img(page)
            if loc is None:
                page.wait_for_timeout(1000)
                continue
            try:
                return loc, loc.screenshot()
            except Exception:
                page.wait_for_timeout(800)
                continue
        return None, None

    def _do_captcha_and_submit(self, page, rec=None, r=None):
        """填验证码并提交，直到拿到明确结果。
        rec/r 用于「结果一出立即截图+关窗」，避免上层等到最后再处理
        （已实测：延后处理会误判失败、窗口不关）。

        三类结果的处理原则：
        - 验证码错误 → 必须刷到「新图」后重试，不消耗该发票查验次数
        - 当日次数超限 → 跳过本张（上层不再重试）
        - 解析不出结果 → 落盘页面文本+继续按验证码问题重试
        """
        auto_mode = self.auto_tries > 0      # 0 = 纯人工输入模式
        manual_used = 0
        cap_err = 0
        idle = 0                # 提交未生效次数
        max_rounds = max(12, (self.auto_tries + self.manual_tries) * 3)
        max_cap_err = max(6, self.auto_tries + 2)
        rounds = 0
        while rounds < max_rounds:
            rounds += 1
            if self._stop:
                return False, "查验失败", "手动停止", ""
            # 0) 平台风控弹层优先：命中就别再往下走了。
            #    这类模态框带遮罩会拦截点击，继续填单/提交只会越撞越重，
            #    而且它并非"本张发票的问题"→ 必须交给批量层暂停整批。
            rt = self._rate_modal(page)
            if rt:
                self._close_rate_modal(page)
                return False, "平台限流", (
                    f"平台提示「{rt}」→ 本张未受理，需暂停整批后再重试"), rt
            # 每轮开清场：残留弹窗会挡住验证码图与表单
            if self._dismiss_dialogs(page):
                self.log("    已清理残留提示弹窗，继续本张查验")
            img_loc, png = self._grab_captcha(page)
            cap_in = self._captcha_input(page)
            if img_loc is None or cap_in is None:
                # 找不到验证码：可能结果已弹出（遮挡表单）→ 先读结果
                ok, status, detail, text = self._read_result(page)
                if ok:
                    self.log(f"    ✓ 平台已返回结果：{status}")
                    if rec is not None and r is not None:
                        self._capture_and_close(page, rec, r, status)
                    return ok, status, detail, text
                self.log("    ⚠ 未能加载出验证码图片，再试一次…")
                if rounds >= 3:
                    p = self._dump_page(page, rec, "无验证码")
                    if p:
                        self.log(f"    页面文本已保存便于排查：{p}")
                page.wait_for_timeout(1200)
                continue
            # ---------- 挑图阶段：只提交「识别清晰」的验证码 ----------
            # 策略：与其硬识别每一张，不如挑简单的那张。刷新验证码不消耗
            # 查验次数（零成本），填错一次却会浪费一次尝试并拖慢整张。
            solved, prompt = None, ""
            if auto_mode:
                import captcha as cap
                for pick in range(1, PICK_MAX + 1):
                    img_loc, png = self._grab_captcha(page)
                    if img_loc is None or png is None:
                        break
                    prompt = self._captcha_prompt(page)
                    s = cap.solve(png, prompt or "")
                    conf = float(s.get("conf", 0)) if s else 0.0
                    conf = max(0.0, min(1.0, conf))
                    if s and s.get("text"):
                        self.log(f"    验证码候选#{pick}「{s['text']}」"
                                 f"置信 {conf:.2f}（{s.get('why', '')}）")
                    take, why = False, ""
                    if s and s.get("text"):
                        if conf >= CONF_GOOD:
                            take, why = True, "识别清晰"
                        elif conf >= CONF_OK and pick >= 3:
                            take, why = True, f"已换 {pick - 1} 张，取本张可用结果"
                    if take:
                        self.log(f"    → 采用候选#{pick}「{s['text']}」：{why}")
                        solved = s
                        break
                    if pick >= PICK_MAX:
                        if s and s.get("text"):
                            self.log(f"    ⚠ 已换 {PICK_MAX} 张仍无清晰图，"
                                     f"用最后一张（置信 {conf:.2f}）尝试提交")
                            solved = s
                        break
                    self.log(f"    本张不适合识别（置信 {conf:.2f}），换一张更简单的…")
                    if not self._refresh_captcha(page, png, confirm=False):
                        solved = s if (s and s.get("text")) else None
                        break
                    page.wait_for_timeout(250)
            if solved is None:
                if not self.manual:
                    return False, "查验失败", "验证码始终未能识别，且无人工兜底通道", ""
                if manual_used >= self.manual_tries:
                    return False, "查验失败", "人工输入多次未通过", ""
                manual_used += 1
                self.log("    请查看弹出窗口，人工输入验证码")
                # 重新取当前页面的图（挑图阶段可能已刷新，旧图内容已失效）
                img_loc, png = self._grab_captcha(page)
                if img_loc is None or png is None:
                    return False, "查验失败", "验证码图片加载失败", ""
                prompt = self._captcha_prompt(page) or prompt
                # 先跑一次自动识别作为预填建议，用户只需核对/修正
                guess = None
                try:
                    import captcha as cap
                    g = cap.solve(png, prompt or "")
                    if g and g.get("text"):
                        guess = g["text"]
                except Exception:
                    pass
                solved = self.manual.request(
                    png, prompt or "请输入图片中的验证码文字", "input",
                    guess=guess)
                if not solved or solved.get("skip"):
                    self._refresh_captcha(page, png)
                    continue
            # 执行识别结果
            try:
                if "text" in solved and solved["text"]:
                    _fill(page, cap_in, solved["text"])
                elif "points" in solved:
                    box = img_loc.bounding_box()
                    for (x, y) in solved["points"]:
                        page.mouse.click(box["x"] + x, box["y"] + y)
                        page.wait_for_timeout(250)
                else:
                    self._refresh_captcha(page, png)
                    continue
            except Exception as e:
                self.log(f"    填写验证码失败（{str(e)[:40]}），刷新验证码重试")
                self._refresh_captcha(page, png)
                continue
            self.log("    ✓ 验证码已填入准备提交")
            page.wait_for_timeout(250)
            self._guard_on_form(page)
            if self._click_submit(page):
                self.log("    ✓ 已点击「查 验」按钮，等待平台返回结果…")
                ok, status, detail, text = self._read_result(page)
                if status == "验证码错误":
                    cap_err += 1
                    if cap_err > max_cap_err:
                        return False, "查验失败", (
                            f"验证码连续错误 {cap_err} 次仍无法通过"
                            f"（已排除清单异常，多为验证码图始终未刷新）"), ""
                    self.log(f"    ⚠ 验证码错误（第 {cap_err} 次），"
                             f"换张新图重试（不消耗该发票查验次数）")
                    # 控制提交频率：平台对高频提交会返回「访问过于频繁」并
                    # 静默丢弃后续提交，必须留出间隔
                    page.wait_for_timeout(random.randint(900, 1800))
                    # 连续多次后大概率被平台限流 → 主动退避，避免无限撞墙
                    if cap_err % 5 == 0:
                        wait = 12
                        self.log(f"    疑似触发平台限流，暂停 {wait} 秒后再试…")
                        self._dismiss_dialogs(page)
                        page.wait_for_timeout(wait * 1000)
                    if not self._refresh_captcha(page, png):
                        self.log("    验证码图无法刷新出新的，重新填单重试")
                        raise RuntimeError("需重新进入表单")
                    continue
                if status == "平台限流":
                    # 风控是**会话/IP 级**的，不是这张票的问题 → 本张立即收手，
                    # 由批量层统一暂停等待后重试（旧版在这里自己等 40/90 秒再撞，
                    # 结果是一张接一张地继续撞墙）。
                    self._close_rate_modal(page)
                    return False, "平台限流", (
                        f"{detail}（风控暂停后会自动重试本张）"), text
                if status == "提交未生效":
                    idle += 1
                    if idle >= 3:
                        return False, "查验失败", (
                            "连续多次提交后页面无任何变化，疑似被平台拦截，"
                            "建议暂停后重跑"), ""
                    self.log("    ⚠ 提交后页面无变化，暂停 3 秒后重试…")
                    page.wait_for_timeout(3000)
                    self._refresh_captcha(page, png)
                    continue
                if status == "表单校验失败":
                    # 表单没填全（如日期未生效）→ 重试，不消耗查验次数
                    self.log(f"    ⚠ 表单校验未通过（{detail}），重新填单重试")
                    return False, "表单校验失败", detail, text
                if status == "查验失败" and detail == "未能解析页面结果":
                    cap_err += 1
                    self.log("    ⚠ 未识别到结果内容，按验证码问题刷新重试")
                    # 前几次失败时落盘页面文本，便于事后确认平台到底返回了什么
                    if cap_err <= 3:
                        p = self._dump_page(page, rec, "未识别结果")
                        if p:
                            self.log(f"    页面文本已保存便于排查：{p}")
                    self._refresh_captcha(page, png)
                    continue
                # 拿到明确结果 → 立即截图 + 关窗（结果视图关掉就没了）
                self.log(f"    ✓ 平台已返回结果：{status}")
                if status == "超次数跳过":
                    self.log("    ⚠ 该发票今日查验次数已达上限（每票每天5次），跳过")
                if rec is not None and r is not None:
                    self._capture_and_close(page, rec, r, status)
                return ok, status, detail, text
            # 点不到查验按钮 → 多半表单未通过校验，刷新验证码重试
            self.log("    ⚠ 未找到可点击的「查 验」按钮，刷新验证码重试")
            self._refresh_captcha(page, png)
        return False, "查验失败", "验证码重试次数用尽", ""

    def _captcha_prompt(self, page) -> str:
        """抓验证码提示文字。实测提示元素为 span.yzm-tips，内容形如：
        「请输入验证码图片中蓝色文字」/「请输入验证码图片中红色文字」/
        「请输入验证码文字」。颜色词决定**只需要填该颜色的字符**，
        抓错必然失败，所以按优先级多路兜底。"""
        js = """() => {
            const e = document.querySelector('.yzm-tips');
            if (e && (e.innerText || '').trim())
                return e.innerText.trim();
            // 兜底：找叶子节点里含「请输入验证码」的最短文本
            let best = '';
            document.querySelectorAll('span, div, p, label').forEach(el => {
              const t = (el.innerText || '').trim();
              if (!t || t.length > 40) return;
              if (t.indexOf('请输入验证码') < 0) return;
              if (el.querySelector('*')) return;
              if (!best || t.length < best.length) best = t;
            });
            return best;
        }"""
        try:
            t = page.evaluate(js) or ""
            t = re.sub(r"\s+", "", t)
            if t:
                return t[:40]
        except Exception:
            pass
        try:
            loc = page.locator("text=请输入验证码图片").first
            txt = loc.evaluate(
                "el => (el.parentElement ? el.parentElement.innerText : el.innerText) || ''")
            return re.sub(r"\s+", "", txt)[:40]
        except Exception:
            return ""

    def _refresh_captcha(self, page, old_png: bytes | None = None,
                         confirm: bool = True) -> bool:
        """点验证码图刷新。

        confirm=True（默认）：校验真的换成了新图——拿到的还是同一张图说明
            这次点击被提示浮层挡住或被表单吞了，会导致用旧图的错误验证码
            反复提交（这正是「验证码错误后重试还是失败」的根因）。
            返回是否换到新图。
        confirm=False：主动挑图时用。只点一次 + 短等，不做 bytes 比对——
            目标是尽快换一张更简单的图，每张省下数秒（实测 6 秒 → 1 秒）。
        """
        self._dismiss_dialogs(page)
        if not confirm:
            img = self._find_captcha_img(page)
            if img:
                try:
                    img.click(timeout=1500)
                except Exception:
                    try:
                        img.click(timeout=1200, force=True)
                    except Exception:
                        pass
            page.wait_for_timeout(850)
            loc = self._find_captcha_img(page)
            if loc is None:
                self._ensure_captcha_loaded(page)
                loc = self._find_captcha_img(page)
            return loc is not None
        for i in range(4):
            img = self._find_captcha_img(page)
            if img:
                try:
                    img.click(timeout=1500)
                except Exception:
                    # 被浮层/遮罩拦截时强制点击（点击刷新的语义确定，误点风险低）
                    try:
                        img.click(timeout=1200, force=True)
                    except Exception:
                        pass
            page.wait_for_timeout(900)
            loc = self._find_captcha_img(page)
            if loc is None:
                self._ensure_captcha_loaded(page)
                loc = self._find_captcha_img(page)
            if loc is None:
                continue
            try:
                png = loc.screenshot()
            except Exception:
                continue
            if old_png is None or png != old_png:
                return True
            # 还是同一张图：多半有浮层遮挡或被限流 → 清浮层并逐步拉长等待
            self._dismiss_dialogs(page)
            page.wait_for_timeout([0, 700, 1500, 2500][min(i, 3)])
        # 仍未换新：不能重载页面（表单数据会丢）→ 交由上层重新走一遍填单
        return False

    def _click_submit(self, page) -> bool:
        """点查验按钮：限定 button 元素且文本精确为「查 验」，
        避免误点左侧菜单「发票查验说明」导致跳页（已实测踩坑）。
        点之前先清掉可能遮挡的提示浮层。"""
        self._close_popups(page)
        btns = page.locator("button", has_text=re.compile(r"查\s*验"))
        for i in range(btns.count()):
            btn = btns.nth(i)
            try:
                if not btn.is_visible(timeout=400):
                    continue
                txt = re.sub(r"\s+", "", btn.inner_text(timeout=400) or "")
                if txt == "查验":
                    try:
                        btn.click(timeout=3000)
                    except Exception:
                        btn.click(timeout=2000, force=True)
                    page.wait_for_timeout(900)      # 后续 _read_result 会轮询等待
                    return True
            except Exception:
                continue
        return False

    def _guard_on_form(self, page):
        """确认还在查验表单页，被带跑到说明页等则抛错（调用方重试）"""
        if "national-invoice-check" not in page.url:
            raise RuntimeError("页面意外跳转，需重试")

    def _read_result(self, page):
        """读提交后的结果。平台结果有三种形态（均已实测）：
        A. 查验通过/冲红/作废：发票详情视图（模态弹层，含
           「当日查验次数」「查验时间」「发票号码：xxx」、打印按钮，
           内容常超一屏）
        B. 失败/提示：t-dialog 小弹窗（「查验结果: 查无此票」等）
        C. 校验/超限类：**顶部提示浮层** .popup-container（如
           「单张发票每天最多查验5次!」「请输入发票号码！」)，
           出现几秒后自动消失 —— 必须边轮询边累积记录，
           否则会漏掉，进而被误当成"验证码错误"无限重试。
        轮询等待任一形态出现再解析。"""
        cap = None
        try:
            cap = self._captcha_input(page)
        except Exception:
            pass
        start_val = ""
        if cap is not None:
            try:
                start_val = cap.input_value() or ""
            except Exception:
                pass
        text, first_text = "", ""
        popups = []                              # 累积出现过的浮层提示
        rate_hit = ""                            # 风控模态框原文
        for i in range(26):                      # 最多等 ~9 秒
            page.wait_for_timeout(350)
            # 顺手收集浮层（一闪即逝，必须轮询累积）
            try:
                for t in self._popup_texts(page):
                    if t not in popups:
                        popups.append(t)
            except Exception:
                pass
            # 风控模态框：有的实现不在正文文本里（遮罩层/复杂结构）→ 单独探
            if not rate_hit and i % 2 == 1:
                try:
                    rate_hit = self._rate_modal(page)
                except Exception:
                    rate_hit = ""
            if rate_hit:
                break                            # 平台风控（访问过于频繁）
            if popups and LIMIT_RE.search(" ".join(popups)):
                break                            # 次数超限（浮层已抓住）
            # 验证码框被清空 = 平台静默判错 → 立即结束等待，不必等满
            if i >= 1 and cap is not None and start_val:
                try:
                    if not (cap.input_value() or ""):
                        break
                except Exception:
                    pass
            try:
                text = page.inner_text("body")
            except Exception:
                continue
            if i == 0:
                first_text = text
            # 浮层内容也并入正文判定（浮层消失后 body 里就没有了）
            probe = text + "\n" + "\n".join(popups)
            if "当日查验次数" in text and "发票号码" in text:
                break                           # A 形态出现
            if re.search(r"查验结果[：:]", text):
                break                           # B 形态出现
                # 注意：绝不能用 "查验结果" in text —— 页面常驻侧边文案
                # 「查验结果说明」会让它每轮都立刻误判（已实测踩坑）
            if LIMIT_RE.search(probe):
                break                           # 当日次数超限
            if RATE_LIMIT_RE.search(probe):
                break                           # 平台限流
            if BADINFO_RE.search(probe):
                break                           # 票面信息有误（前端校验）
            if "验证码" in text and (
                    "错误" in text or "不正确" in text or "有误" in text):
                break                           # 验证码错误提示
            # 页面长时间毫无变化、无弹层也无 loading → 提交根本没发出去，
            # 提前结束轮询去重试。阈值放宽到 8 秒：宁可多等，也不能掐断
            # 平台正在处理的正常请求（请求已发出=已计查验次数）
            if i >= 20 and text == first_text:
                try:
                    busy = page.locator(
                        ".t-dialog:visible, .popup-container:visible,"
                        " .t-loading:visible, [class*=loading]:visible"
                    ).count()
                    if busy == 0:
                        break
                except Exception:
                    break
        pop_text = " ".join(popups)
        # ---- 0) 浮层优先判定（限流 / 超次数 / 票面信息有误）----
        # 注：LIMIT_RE 已刻意排除侧栏常驻文案「每份发票每天最多可查验5次」，
        #     所以这里可以直接用正文/浮层文本判定。
        if RATE_LIMIT_RE.search(pop_text) or RATE_LIMIT_RE.search(text) or rate_hit:
            return False, "平台限流", (
                f"平台提示「{rate_hit or '您的访问过于频繁，请稍后再试'}」，"
                f"本次提交未被受理，需暂停后再试"), text
        m_lim = LIMIT_RE.search(pop_text) or LIMIT_RE.search(text)
        if m_lim:
            return True, "超次数跳过", (
                f"平台提示「{re.sub(r'\\s+', '', m_lim.group(0))[:26]}」"
                f"→ 该票今日查验次数已用尽（每票每天最多5次），请明日再查"), text
        if BADINFO_RE.search(pop_text):
            return True, "票面信息有误", (
                f"平台校验未通过：{BADINFO_RE.search(pop_text).group(0)[:40]}"
                f"（请核对发票号码/代码/日期/金额是否与票面一致）"), text
        # 验证码错误提示
        if "验证码" in text and ("错误" in text or "不正确" in text or "有误" in text):
            return False, "验证码错误", "验证码错误，自动重试", text
        # A. 发票详情视图（模态弹层）
        # ⚠ 关键修正（2026-09-30）：详情视图里**同样会出现**
        #   「查验结果：查无此票」——旧版此处无条件返回 status="一致"，
        #   导致"查无此票"被误报成查验通过（用户截图实测）。必须先从详情中
        #   解析「查验结果」字段，再兜底按票面状态判断。
        if "当日查验次数" in text and "发票号码" in text:
            m = re.search(r"查验结果[：:]\s*([^\s，。,;；\n]+)", text)
            verdict = m.group(1) if m else ""
            status, detail = "一致", "查验通过"
            if "查无此票" in verdict or "无此票" in verdict:
                status = "查无此票"
                detail = "查验结果：查无此票（请核对发票号码/开票日期/金额，或该票信息本身有误）"
            elif "不一致" in verdict:
                status, detail = "不一致", f"查验结果：{verdict}"
            elif "红" in verdict:
                status, detail = "冲红", f"查验结果：{verdict}"
            elif "作废" in verdict:
                status, detail = "作废", f"查验结果：{verdict}"
            elif re.search(r"已红冲|红冲|冲红", text):
                status, detail = "冲红", "发票已冲红"
            elif "作废" in text:
                status, detail = "作废", "发票已作废"
            elif not verdict:
                detail = "查验通过（未读到「查验结果」字段，按票面状态判定）"
            times = re.search(r"当日查验次数[：:]\s*(第?\d+次)", text)
            if times:
                detail = f"当日{times.group(1)}，{detail}"
            return True, status, detail, text
        # B. 查验结果弹窗：查验结果: 一致/不一致/查无此票/已作废…
        m = re.search(r"查验结果[：:]\s*([^\s，。,;；]+)", text)
        if m:
            v = m.group(1)
            times = re.search(r"当日查验次数[：:]\s*(第?\d+次)", text)
            prefix = f"当日{times.group(1)}，" if times else ""
            detail = f"{prefix}查验结果：{v}"
            if "查无此票" in v or "无此票" in v:
                return True, "查无此票", (
                    f"{prefix}查验结果：{v}"
                    f"（请核对发票号码/开票日期/金额，或该票信息本身有误）"), text
            if "不一致" in v:
                return True, "不一致", detail, text
            if "一致" in v:
                return True, "一致", detail, text
            if "红" in v:
                return True, "冲红", detail, text
            if "作废" in v:
                return True, "作废", detail, text
            return True, "失败", detail, text
        # 前端校验未通过：
        #   ①「…有误」= 票面信息与平台校验规则不符 → 重试无意义，直接判失败
        #   ②「请输入…」= 表单没填上（时序问题）→ 可重试
        probe = text + "\n" + pop_text
        if not re.search(r"查验结果[：:]", text):
            m = BADINFO_RE.search(probe)
            if m:
                return True, "票面信息有误", (
                    f"平台校验未通过：{m.group(0)[:40]}"
                    f"（请核对号码/代码/日期/金额与票面是否一致）"), text
            m = EMPTY_RE.search(probe)
            if m:
                return False, "表单校验失败", m.group(0), text
        status, detail = _classify_result(text)
        if status:
            return True, status, detail, text
        # 验证码框被清空 = 平台静默判错（实测：验证码错误时平台不弹提示，
        # 只把验证码输入框清空）→ 换新图重试，不消耗查验次数
        cap = None
        cur = None
        try:
            cap = self._captcha_input(page)
            cur = (cap.input_value() or "").strip() if cap is not None else None
        except Exception:
            cur = None
        if cap is not None and not cur:
            return False, "验证码错误", "验证码错误（平台未弹提示），自动换图重试", text
        if cur:
            # 填进去的验证码还在、页面又没有任何结果/浮层 → 提交很可能没被受理
            return False, "提交未生效", (
                "提交后页面无任何变化（可能被平台拦截/限流），稍后重试"), text
        if "查验" in text and len(text.strip()) < 30:
            return False, "查验失败", text.strip()[:60], text
        return False, "查验失败", "未能解析页面结果", text

    def _expand_scrollbars(self, page):
        """展开页面所有滚动容器，让超一屏的结果内容能完整入图"""
        try:
            page.evaluate(
                """() => {
                  document.querySelectorAll('*').forEach(el => {
                    const s = getComputedStyle(el);
                    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') &&
                        el.scrollHeight > el.clientHeight + 50) {
                      el.style.maxHeight = 'none';
                      el.style.height = 'auto';
                      el.style.overflow = 'visible';
                    }
                  });
                  document.querySelectorAll(
                    '.t-dialog, .t-dialog__body, [class*=dialog], [class*=modal]'
                  ).forEach(el => {
                    el.style.maxHeight = 'none';
                    el.style.height = 'auto';
                  });
                }""")
            page.wait_for_timeout(400)
        except Exception:
            pass

    def _shot_result(self, page, path: str) -> str:
        """结果截图：发票详情内容常超一屏且在弹层内部滚动（已实测），
        先展开滚动容器，再截结果弹层全内容；任何异常逐级退回。
        返回实际使用的截图方式，便于日志排查"""
        self._expand_scrollbars(page)
        for sel in [".t-dialog:visible", ".t-dialog__wrapper:visible",
                    "[class*=modal]:visible"]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=800):
                    loc.screenshot(path=path)
                    return "弹层全内容截图"
            except Exception:
                continue
        try:
            page.screenshot(path=path, full_page=True)
            return "整页截图"
        except Exception:
            pass
        page.screenshot(path=path)
        return "视口截图"

    def _close_result(self, page) -> bool:
        """关闭查验结果窗口：先点正常关闭按钮并校验；点不到就用 JS 点击；
        仍关不掉则强制移除弹层与遮罩（保证下一张能正常操作）。返回是否已关闭"""
        def _visible() -> bool:
            try:
                return page.locator(".t-dialog:visible").count() > 0
            except Exception:
                return False

        if not _visible():
            return True
        for sel in [".t-dialog:visible .t-dialog__close",
                    ".t-dialog:visible .t-icon-close",
                    ".t-dialog:visible button:has-text('关 闭')",
                    ".t-dialog:visible button:has-text('关闭')",
                    ".t-dialog:visible button:has-text('确 定')",
                    ".t-dialog:visible button:has-text('确定')"]:
            try:
                c = page.locator(sel).first
                if c.is_visible(timeout=600):
                    c.click(timeout=2000)
                    page.wait_for_timeout(500)
                    if not _visible():
                        return True
            except Exception:
                continue
        # JS 点击关闭图标
        try:
            page.evaluate(
                """() => {
                    const d = document.querySelector('.t-dialog');
                    if (!d) return;
                    const b = d.querySelector('.t-dialog__close, .t-icon-close');
                    if (b) b.click();
                }""")
            page.wait_for_timeout(400)
            if not _visible():
                return True
        except Exception:
            pass
        # 终极兜底：移除弹层与遮罩
        try:
            page.evaluate(
                """() => {
                    document.querySelectorAll(
                      '.popup-container, .t-dialog__wrap, .t-dialog__mask,'
                      + ' .t-dialog'
                    ).forEach(el => el.remove());
                    document.querySelectorAll('[class*=mask]').forEach(el => el.remove());
                }""")
            page.wait_for_timeout(300)
        except Exception:
            pass
        return not _visible()

    def _capture_and_close(self, page, rec, r, status=None):
        """查验结果落地动作（强制、幂等）：截图 → 关闭结果窗口。
        结果一出现即调用，保证任何分支都不会漏截图、漏关窗"""
        st = status or r.get("result") or "查验失败"
        if not r.get("shot"):
            fname = (f"{rec.get('number') or Path(rec['file']).stem}_"
                     f"{'OK' if st == '一致' else st}.png")
            path = self.shots / fname.replace(" ", "_")
            try:
                how = self._shot_result(page, str(path))
                r["shot"] = str(path)
                self.log(f"    ✓ 查验结果截图已保存（{how}）：{path.name}")
            except Exception as e:
                self.log(f"    ⚠ 截图保存失败：{str(e)[:80]}")
                r["shot_error"] = str(e)[:120]
        had_dialog = False
        try:
            had_dialog = page.locator(".t-dialog:visible").count() > 0
        except Exception:
            pass
        try:
            ok_closed = self._close_result(page)
        except Exception:
            ok_closed = False
        if had_dialog:
            self.log("    ✓ 已关闭查验结果窗口，准备下一张" if ok_closed
                     else "    ⚠ 结果窗口未能正常关闭（已强制移除），继续下一张")

    def _launch(self, pw):
        """启动浏览器：优先本机 Chrome 内核（加载平台页面更顺、沿用系统
        代理），失败退回内置 Chromium。"""
        kwargs = {"headless": self.headless, "args": LAUNCH_ARGS}
        if self.channel:
            name = {"chrome": "本机 Chrome", "msedge": "本机 Edge"}.get(
                self.channel, self.channel)
            try:
                b = pw.chromium.launch(channel=self.channel, **kwargs)
                self.log(f"浏览器已启动（{name}内核）")
                return b
            except Exception as e:
                self.log(f"{name}内核启动失败，改用内置内核：{str(e)[:80]}")
        b = pw.chromium.launch(**kwargs)
        self.log("浏览器已启动（内置 Chromium 内核）")
        return b

    @staticmethod
    def _setup_ctx(ctx):
        """上下文参数：中文环境 + 忽略证书问题 + 屏蔽统计脚本提速"""
        def _route(route):
            url = route.request.url
            if any(h in url for h in BLOCK_HOSTS):
                try:
                    route.abort()
                    return
                except Exception:
                    pass
            try:
                route.continue_()
            except Exception:
                pass
        try:
            ctx.route("**/*", _route)
        except Exception:
            pass

    # ---------------- 批量 ----------------
    def run_batch(self, records, on_done=None):
        """主入口：起浏览器逐张查验，返回结果列表。

        ⚠ 平台风控处理（2026-09-30 用户反馈修复）：
        平台会弹「您的访问过于频繁，请稍后再试」的模态框（带遮罩、拦截点击）。
        旧版没识别到它 → 在弹层后面继续填单提交，"越撞越重"、界面像卡死。
        现在：命中 → 关弹层 → **暂停整批**（60/180/300 秒退避，界面有倒计时）
              → 整页重开、重试同一张；连续 3 次仍限流 → 停批并保留已完成结果。
        """
        results = []
        try:
            self._run_batch_inner(records, results, on_done)
        except Exception as e:
            # ⚠ 关键兜底（2026-09-30）：**任何**中断（风控/加载失败/平台改版）
            #   都要保留已完成的结果。旧版是直接抛异常 → 上层收尾逻辑不执行 →
            #   Excel/HTML 一个都不生成，用户已查好的发票全部白查。
            self.stop_reason = self.stop_reason or f"异常中断（{type(e).__name__}）"
            self.last_error = str(e)
            self.log(f"⛔ 查验中断：{type(e).__name__}: {str(e)[:120]}")
            self.log("   已完成的结果会被保留并照常导出，可稍后用"
                     "「导入Excel清单」重跑未完成的部分")
        # 未跑完的发票补进结果表：保证清单完整，且可用「导入Excel清单」重跑
        done_idx = {r.get("idx") for r in results}
        left = [rec for rec in records if rec.get("idx") not in done_idx]
        for rec in left:
            rr = dict(rec)
            rr["shot"] = ""
            rr["result"] = "未执行"
            rr["detail"] = ("因平台限流中断，本张尚未查验"
                            if self.stop_reason == "平台限流"
                            else "本次运行未执行（可重跑）")
            results.append(rr)
        if left:
            self.log(f"=== 本次中断：{len(left)} 张未执行"
                     f"（原因：{self.stop_reason or '用户停止'}），"
                     f"已完成 {len(results) - len(left)} 张结果已保留，"
                     f"可用「导入Excel清单」重跑未执行的")
        skipped = [r.get("number", "") for r in results
                   if r.get("result") == "超次数跳过"]
        if skipped:
            self.log(f"=== 本轮结束：{len(skipped)} 张因当日查验次数用尽被"
                     f"跳过，明日重查这些：{', '.join(skipped)}")
        return results
    def _run_batch_inner(self, records, results, on_done):
        """真正的批量循环；任何异常都由 run_batch 统一兜底，保证已完成结果不丢。"""
        consecutive_fail = 0
        with sync_playwright() as pw:
            browser = self._launch(pw)
            ctx = browser.new_context(viewport={"width": 1400, "height": 900},
                                      locale="zh-CN",
                                      timezone_id="Asia/Shanghai",
                                      ignore_https_errors=True)
            self._setup_ctx(ctx)
            page = ctx.new_page()
            self._install_dialog_hook(page)
            try:
                n = len(records)
                i = 0
                rate_pauses = 0
                while i < n:
                    if self._stop:
                        break
                    rec = records[i]
                    self.log(f"[{i + 1}/{n}] {Path(rec['file']).name} "
                             f"({rec.get('kind','?')} 号码:{rec.get('number','?')})")
                    # 上轮遗留的风控遮罩会拦截一切点击 → 先清掉再开始
                    self._close_rate_modal(page)
                    r = self.check_one(rec, page)
                    self.log(f"    → {r['result']}  {r['detail'][:60]}")

                    # ---------- 平台风控：暂停整批，而不是在弹层后面硬撞 ----------
                    if r.get("result") == "平台限流":
                        rate_pauses += 1
                        if rate_pauses > RATE_MAX_PAUSE:
                            self.stop_reason = "平台限流"
                            r["detail"] = (f"{r['detail']}"
                                           f"（已暂停 {RATE_MAX_PAUSE} 次仍被限流，"
                                           f"本批停止）")
                            results.append(r)
                            if on_done:
                                on_done(r, i + 1, n)
                            self.log("    ⛔ 多次暂停后仍被平台限流 → 停止本批，"
                                     "已完成的发票结果照常保存")
                            break
                        wait = RATE_PAUSE_WAITS[min(rate_pauses - 1,
                                                    len(RATE_PAUSE_WAITS) - 1)]
                        self.log(f"    ⏸ 平台风控：暂停 {wait} 秒后重试本张"
                                 f"（第 {rate_pauses}/{RATE_MAX_PAUSE} 次）")
                        if self.notice:
                            try:
                                self.notice("pause", wait, r.get("detail", ""))
                            except Exception:
                                pass
                        if not self._sleep_countdown(wait):
                            self.stop_reason = "用户停止"
                            break
                        self._close_rate_modal(page)
                        # 限流后页面状态已不可信 → 整页重开再重试本张
                        try:
                            self._rate_flag = ""
                            page.goto(URL, wait_until="domcontentloaded",
                                      timeout=90000)
                            page.wait_for_timeout(800)
                        except Exception:
                            pass
                        if self.notice:
                            try:
                                self.notice("resume", 0, "")
                            except Exception:
                                pass
                        self.log("    ▶ 恢复查验，重试本张…")
                        continue            # 注意：不推进 i → 重试同一张发票

                    rate_pauses = 0
                    results.append(r)
                    if on_done:
                        on_done(r, i + 1, n)
                    if r.get("result") == "超次数跳过":
                        self.log(f"    ⚠ 已跳过（次数用尽），明日需重查："
                                 f"{r.get('number', '')}")
                    # 连续多张加载失败 → 说明网络/浏览器内核有问题，及早提示
                    if "页面加载" in r.get("detail", ""):
                        consecutive_fail += 1
                    else:
                        consecutive_fail = 0
                    if consecutive_fail >= 3:
                        raise RuntimeError(chr(10).join([
                            "连续 3 张发票都打不开查验平台页面（加载超时）。",
                            "请依次排查：",
                            "1) 先在浏览器里手动打开查验平台确认能访问；",
                            "2) 勾选「用本机Chrome内核」后重试；",
                            "3) 若公司网络有代理/防火墙，可能拦截了自动化浏览器，"
                            "请换网络或联系网管。"]))
                    i += 1
                    if i < n:
                        time.sleep(random.uniform(0.8, 1.8))  # 轻微间隔控频
            finally:
                browser.close()
