# -*- coding: utf-8 -*-
"""自测：依赖导入 + 二维码字段解析 + 报告生成"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

print("1) 依赖导入...", end=" ")
import cv2, fitz, pyzbar, openpyxl, PIL  # noqa
ok = True
try:
    import ddddocr
    print("ddddocr=OK", end=" ")
except Exception as e:
    ok = False
    print(f"ddddocr=FAIL({e})", end=" ")
print()

print("2) 二维码解析自测...", end=" ")
import io
import segno
from PIL import Image
from extract import _parse_qr, _decode_qr_pil

cases = [
    # 老票：代码+号码+不含税金额+日期+校验码
    ("01,10,044031800111,07220945,55.55,20200312,47518",
     {"code": "044031800111", "number": "07220945", "date": "20200312",
      "amount": "55.55", "checkcode": "47518"}),
    # 数电票：20 位号码+价税合计+日期
    ("01,15,25122000000000000009,2385.00,20260115",
     {"code": "", "number": "25122000000000000009", "date": "20260115",
      "amount": "2385.00", "checkcode": ""}),
]
for text, expect in cases:
    got = _parse_qr(text)
    for k, v in expect.items():
        assert got[k] == v, f"QR解析失败: {text} 字段{k} 期望{v} 得到{got[k]}"
    # 生成真实二维码再走解码链路
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=6)
    decoded = _decode_qr_pil(Image.open(buf))
    assert decoded and _parse_qr(decoded[0])["number"] == expect["number"], \
        f"二维码解码失败: {text}"
print("OK")

print("3) 报告生成自测...", end=" ")
import tempfile
from report import write_excel, write_html
recs = [
    {"file": "a.pdf", "kind": "数电票", "code": "", "number": "25122000000000000009",
     "date": "20260115", "amount": "2385.00", "checkcode": "",
     "result": "一致", "detail": "查验通过：发票信息一致", "shot": ""},
    {"file": "b.jpg", "kind": "增值税发票", "code": "044031800111",
     "number": "07220945", "date": "20200312", "amount": "55.55",
     "checkcode": "47518", "result": "冲红", "detail": "该发票已红冲", "shot": ""},
    {"file": "c.png", "kind": "未知", "code": "", "number": "", "date": "",
     "amount": "", "checkcode": "", "result": "查验失败",
     "detail": "未识别到二维码，请人工补录字段", "shot": ""},
]
# 产物写临时目录，绝不污染项目根
_tmp = tempfile.TemporaryDirectory()
write_excel(recs, str(Path(_tmp.name) / "test_结果.xlsx"))
write_html(recs, str(Path(_tmp.name) / "test_结果.html"))
_tmp.cleanup()
print("OK")

print("\n=== 全部自测通过 ===")
