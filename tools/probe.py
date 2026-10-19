# -*- coding: utf-8 -*-
"""
选择器探测：打开查验平台，把输入框/按钮/页签/验证码元素的实际结构打出来，
用于校准 checker.py 里的 SEL 字典。用法：python probe.py
"""
from playwright.sync_api import sync_playwright

URL = "https://inv-veri.chinatax.gov.cn/fpcygzfw/national-invoice-check"

JS = """() => {
  const out = [];
  document.querySelectorAll('input,button,a,[role=tab],[class*=tab],[class*=btn]')
    .forEach(el => {
      const t = (el.innerText || el.value || el.placeholder || '').trim().slice(0, 30);
      if (!t && el.tagName !== 'INPUT') return;
      out.push({tag: el.tagName, type: el.type || '', cls: (el.className || '').toString().slice(0, 60),
                id: el.id, ph: el.placeholder || '', text: t});
    });
  document.querySelectorAll('img').forEach(el => {
    if ((el.src || '').match(/captcha|code|verify|kaptcha/i) || el.className.toString().match(/captcha|verify/i))
      out.push({tag: 'IMG', src: el.src.slice(0, 80), cls: el.className.toString(), id: el.id});
  });
  return out;
}"""


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=False)
        page = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        for it in page.evaluate(JS):
            print(it)
        page.wait_for_timeout(60000)   # 留 60 秒人工观察（点页签等）
        for it in page.evaluate(JS):
            print("AFTER:", it)
        b.close()


if __name__ == "__main__":
    main()
