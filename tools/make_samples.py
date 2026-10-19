# -*- coding: utf-8 -*-
"""生成两张样例发票（数电票 PNG + 老票 PDF），用于试跑提取链路"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 样例发票放data/samples，可随时删除重建
OUT = Path(__file__).resolve().parent.parent / "data" / "samples"
OUT.mkdir(parents=True, exist_ok=True)

DSD_QR = "01,15,25122000000000000009,2385.00,20260115"
OLD_QR = "01,10,044031800111,07220945,55.55,20260312,47518"

import segno

# 1) 数电票 PNG：二维码 + 文字标注
png = OUT / "样例_数电票.png"
segno.make(DSD_QR, error="m").save(str(png), scale=6)

# 2) 老票 PDF：内嵌二维码图片 + 文本层
import fitz
doc = fitz.open()
page = doc.new_page(width=595, height=420)
img_bytes = None
import io
buf = io.BytesIO()
segno.make(OLD_QR, error="m").save(buf, kind="png", scale=6)
page.insert_image(fitz.Rect(20, 20, 120, 120), stream=buf.getvalue())
page.insert_text((150, 60), "电子发票（普通发票）", fontsize=14)
page.insert_text((150, 90), "发票代码：044031800111", fontsize=11)
page.insert_text((150, 110), "发票号码：07220945", fontsize=11)
page.insert_text((150, 130), "开票日期：2026年03月12日", fontsize=11)
page.insert_text((150, 150), "价税合计（小写）¥59.55", fontsize=11)
page.insert_text((150, 170), "校验码：47518", fontsize=11)
doc.save(str(OUT / "样例_老票.pdf"))
doc.close()

print("样例已生成：")
for p in OUT.iterdir():
    print("  ", p)

# 立即用提取链路验证
from extract import scan_folder
for r in scan_folder(str(OUT)):
    print(f"  提取→ 票种={r['kind'] or '未识别'} 代码={r['code']} "
          f"号码={r['number']} 日期={r['date']} 金额={r['amount']} "
          f"来源={r['source']}")
