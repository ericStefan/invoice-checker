# -*- coding: utf-8 -*-
"""
发票字段提取模块
策略：
  1. 优先解码发票左上角二维码（最可靠，能拿到 代码/号码/日期/金额/校验码）
  2. PDF 优先读文本层；图片只做二维码，二维码失败则标记"需人工补录"
  3. 自动区分数电票（20 位号码、无发票代码）与老票（10/12 位代码 + 8 位号码）
"""
import re
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# 需要提取的字段
FIELDS = ["file", "kind", "code", "number", "date", "amount", "amount_alt",
          "checkcode", "source", "note"]


# 二维码 XML 标签 → 记录字段。数电票的二维码内容**就是一段完整 XML**
# （含 InvoiceNumber / IssueTime / TotalTaxIncludedAmount 等标签），
# 这是最权威的数据源：正文文本层受字段顺序、水印、注释层影响，
# 正则很容易抓错（实测事故：35 张不同发票被提取成同一个号码）。
_XML_TAGS = {
    "number": ["InvoiceNumber", "invoiceNo", "fpHm", "FpHm",
               "invoiceNumber", "发票号码"],
    "code": ["InvoiceCode", "fpDm", "FpDm", "invoiceCode", "发票代码"],
    "date": ["IssueTime", "issueTime", "IssueDate", "issueDate",
             "kpRq", "Kprq", "开票日期"],
    "amount": ["TotalTaxIncludedAmount", "totalTaxIncludedAmount",
               "AmountWithTax", "amountWithTax", "jsHj", "价税合计"],
    "amount_alt": ["TotalAmount", "totalAmount", "AmountWithoutTax",
                   "amountWithoutTax", "hjJe", "合计金额", "不含税金额"],
    "checkcode": ["CheckCode", "checkCode", "jyM", "校验码"],
}
_URL_KEYS = {
    "number": ["fphm", "invoiceNumber", "invoiceNo"],
    "code": ["fpdm", "invoiceCode"],
    "date": ["kprq", "issueDate", "issueTime"],
    "amount": ["kjje", "je", "amount", "totalTaxIncludedAmount"],
    "checkcode": ["jym", "checkCode"],
}


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _norm_amount(s) -> str:
    m = re.search(r"(\d+(?:\.\d+)?)", str(s or "").replace(",", ""))
    return m.group(1) if m else ""


def _norm_qr(info: dict) -> dict:
    """规范化二维码解析结果：号码/代码/日期/金额统一格式，剔除明显噪声"""
    out = dict(info)
    n = _digits(out.get("number", ""))
    # 数电票 18~20 位；老票 8 位。其余长度视为解析噪声，宁缺毋滥
    out["number"] = n if (len(n) == 8 or 18 <= len(n) <= 20) else ""
    c = _digits(out.get("code", ""))
    out["code"] = c if len(c) in (10, 12) else ""
    d = _digits(out.get("date", ""))
    out["date"] = d[:8] if len(d) >= 8 else ""
    out["amount"] = _norm_amount(out.get("amount", ""))
    out["amount_alt"] = _norm_amount(out.get("amount_alt", ""))
    out["checkcode"] = _digits(out.get("checkcode", ""))[-6:]
    return out


def _parse_qr_xml(text: str):
    """解析二维码中的 XML / URL 查询串。无有效标签时返回 None。"""
    src = str(text or "")
    if not src or ("<" not in src and "?" not in src):
        return None
    out = {"code": "", "number": "", "date": "", "amount": "",
           "amount_alt": "", "checkcode": ""}
    hit = False
    for key, tags in _XML_TAGS.items():
        for tag in tags:
            m = re.search(
                rf"<[^<>]*{re.escape(tag)}[^<>]*>\s*([^<]+?)\s*</", src, re.I)
            if m and m.group(1).strip():
                out[key] = m.group(1).strip()
                hit = True
                break
    if not hit:
        q = {}
        for k, v in re.findall(r"[?&]([A-Za-z_]+)=([^&#]+)", src):
            q.setdefault(k.lower(), v)
        if q:
            for key, keys in _URL_KEYS.items():
                for k in keys:
                    if k.lower() in q:
                        out[key] = q[k.lower()]
                        hit = True
                        break
    if not hit:
        return None
    return _norm_qr(out)


def _parse_qr(text: str) -> dict:
    """解析二维码内容（两种形态都支持）：
    1) 数电票：XML（<InvoiceNumber> 等标签）或 URL 查询串 —— 最权威
    2) 老票：逗号分隔，如 01,10,044031800111,07220945,55.55,20200312,47518
           版本,票种,发票代码(10/12位),号码(8位),不含税金额,日期,校验码
    """
    xml = _parse_qr_xml(text)
    if xml and (xml.get("number") or xml.get("code")):
        return xml
    parts = [p.strip() for p in text.replace("，", ",").split(",") if p.strip()]
    out = {"code": "", "number": "", "date": "", "amount": "",
           "amount_alt": "", "checkcode": ""}
    nums, codes = [], []
    for p in parts:
        if re.fullmatch(r"\d{18,20}", p):
            out["number"] = p                      # 数电票 18~20 位号码
        elif re.fullmatch(r"\d{10}|\d{12}", p):
            out["code"] = p                        # 老票发票代码
        elif re.fullmatch(r"\d{8}", p):
            nums.append(p)
        elif re.fullmatch(r"\d+(\.\d+)?", p) and "." in p:
            if not out["amount"]:
                out["amount"] = p                  # 第一个带小数点的金额
        elif re.fullmatch(r"\d{4,6}", p):
            codes.append(p)                        # 校验码候选（4~6 位）
    # 日期与 8 位号码的区分：8 位且形如 19xx/20xx 年开头、月 01-12 的是日期
    for n in nums:
        y, m = int(n[:4]), int(n[4:6])
        if 1990 <= y <= 2100 and 1 <= m <= 12 and not out["date"]:
            out["date"] = n
        elif re.fullmatch(r"\d{8}", n) and not out["number"]:
            out["number"] = n                      # 老票 8 位号码
    if codes:
        out["checkcode"] = codes[-1]
    return out


def _decode_qr_pil(img, retry_scales=(2, 3)) -> list:
    """尝试多种预处理解码二维码，返回所有成功解码的文本（异常静默）。
    retry_scales 传空元组可禁用"放大兜底"——整页渲染的大图放大很慢，
    调用方按需关闭。"""
    from pyzbar.pyzbar import decode

    def _dec(im):
        try:
            return [d.data.decode("utf-8", "ignore") for d in decode(im)]
        except Exception:
            return []

    results = _dec(img)
    if results:
        return results
    if not retry_scales:
        return []
    # 预处理兜底：放大 / 灰度 / 二值化
    try:
        import cv2
        import numpy as np
        arr = np.array(img.convert("L"))
        if arr.size == 0:
            return []
        big = None
        for scale in retry_scales:
            big = cv2.resize(arr, None, fx=scale, fy=scale,
                             interpolation=cv2.INTER_CUBIC)
            results = _dec(big)
            if results:
                return results
        if big is not None and big.size:
            _, bw = cv2.threshold(big, 0, 255,
                                  cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            results = _dec(bw)
    except Exception:
        pass
    return results


def _pdf_page_qr(doc, zoom=2.0, max_pages=3) -> str:
    """整页渲染后解二维码（PDF 提取的第一优先级）。

    数电票的二维码虽然以图片形式存在，但常被缩放或与其它元素合并成一张
    大图，逐个 `get_images()` 取子图解码失败率高。整页渲染能还原二维码的
    实际尺寸，解码成功率显著更高。发票都是单页，多页 PDF 通常也是多张
    单据合并，取前几页即可。
    """
    import io
    from PIL import Image
    try:
        import fitz
    except Exception:
        return ""
    for pi, page in enumerate(doc):
        if pi >= max_pages:
            break
        for z in (zoom, 3.0):
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
            except Exception:
                continue
            for t in _decode_qr_pil(img, retry_scales=()):
                if t and len(t.strip()) >= 12:
                    return t
    return ""


def _classify_kind(info: dict, qr_text: str) -> str:
    """粗分票种，用于后续选择查验页签与金额口径"""
    if len(info.get("number", "")) >= 18:
        return "数电票"
    if info.get("code"):
        m = re.match(r"^0*(\d{2})", info["code"])
        return "增值税发票"
    return "未知"


def extract_from_image(path: Path) -> dict:
    rec = {"file": str(path), "source": "", "note": "", "kind": "",
           "code": "", "number": "", "date": "", "amount": "",
           "amount_alt": "", "checkcode": ""}
    try:
        from PIL import Image
        img = Image.open(path)
        texts = _decode_qr_pil(img)
        if texts:
            info = _parse_qr(texts[0])
            rec.update(info)
            rec["source"] = "二维码"
            rec["kind"] = _classify_kind(info, texts[0])
        else:
            rec["note"] = "未识别到二维码，请人工补录字段"
    except Exception as e:
        rec["note"] = f"解析异常: {e}"
    return rec


def extract_from_pdf(path: Path) -> dict:
    """PDF 提取。优先级（按可靠性从高到低）：
    1) 二维码 → 数电票二维码内含完整 XML，字段权威、不受版式影响
    2) 正文文本层 → 仅作兜底（字段顺序/水印/注释层易导致抓错字段）
    3) 内嵌图片二维码 → 最后兜底
    """
    rec = {"file": str(path), "source": "", "note": "", "kind": "",
           "code": "", "number": "", "date": "", "amount": "",
           "amount_alt": "", "checkcode": ""}
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(path)

        # ---- 1) 首选：整页渲染解二维码（数电票 = 完整 XML）----
        qr_text = _pdf_page_qr(doc)
        if qr_text:
            info = _parse_qr(qr_text)
            if info.get("number"):
                rec.update({k: v for k, v in info.items() if v})
                rec["source"] = "PDF二维码"
                rec["kind"] = _classify_kind(info, qr_text)

        # ---- 2) 兜底：正文文本层 ----
        if not rec["number"]:
            text = "".join(page.get_text() for page in doc)
            text = re.sub(r"\s+", "", text)
            m = re.search(r"发票号码[:：]?(\d{18,20}|\d{8})", text)
            if m:
                rec["number"] = m.group(1)
            m = re.search(r"发票代码[:：]?(\d{10}|\d{12})", text)
            if m:
                rec["code"] = m.group(1)
            m = re.search(r"开票日期[:：]?(\d{4})年(\d{1,2})月(\d{1,2})日", text)
            if m:
                rec["date"] = f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
            # 金额：数电/普票看"价税合计"，老票专票平台要"不含税金额"，两个都留
            m = re.search(r"[价（(小]税合计[^0-9]{0,6}?[¥￥]?([0-9,]+\.\d{2})", text)
            if m:
                rec["amount"] = m.group(1).replace(",", "")
            m = re.search(r"合\s*计[^0-9¥￥]{0,10}[¥￥]?([0-9,]+\.\d{2})", text)
            if m:
                rec["amount_alt"] = m.group(1).replace(",", "")
            m = re.search(r"校验码[:：]?\s*(\d{6,20})", text)
            if m:
                rec["checkcode"] = m.group(1)[-6:]
            if rec["number"]:
                rec["source"] = "PDF文本"
                rec["kind"] = "数电票" if len(rec["number"]) >= 18 else "增值税发票"
                # PDF 文本里若价税合计与不含税金额都有，按票种调换主次
                if rec["kind"] == "数电票":
                    rec["amount"], rec["amount_alt"] = \
                        rec["amount"] or rec["amount_alt"], rec["amount"]

        # ---- 3) 最后兜底：内嵌图片二维码 ----
        if not rec["number"] or not rec["date"]:
            import io
            from PIL import Image
            for page in doc:
                for img_info in page.get_images(full=True):
                    xref = img_info[0]
                    try:
                        pix = fitz.Pixmap(doc, xref)
                        if pix.n > 4:
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                        img = Image.open(io.BytesIO(pix.tobytes("png")))
                    except Exception:
                        continue
                    for t in _decode_qr_pil(img):
                        info = _parse_qr(t)
                        if info.get("number"):
                            rec.update({k: v or rec[k] for k, v in info.items()})
                            if not rec["source"]:
                                rec["source"] = "PDF内嵌二维码"
                            rec["kind"] = _classify_kind(info, t)
                            break
                    if rec["number"] and rec["date"]:
                        break
                if rec["number"] and rec["date"]:
                    break
        if not rec["number"]:
            rec["note"] = "未能自动提取发票号码，请人工补录"
    except Exception as e:
        rec["note"] = f"解析异常: {e}"
    return rec


def scan_folder(folder: str) -> list:
    """扫描文件夹，返回发票记录列表"""
    recs = []
    for p in sorted(Path(folder).rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() == ".pdf":
            recs.append(extract_from_pdf(p))
        elif p.suffix.lower() in IMAGE_EXT:
            recs.append(extract_from_image(p))
    for i, r in enumerate(recs, 1):
        r.setdefault("idx", i)
        r.setdefault("result", "")
        r.setdefault("detail", "")
        r.setdefault("shot", "")
    return recs


def extract_file(path: str) -> dict:
    """提取单个文件（供「添加发票文件」功能使用）"""
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        rec = extract_from_pdf(p)
    elif p.suffix.lower() in IMAGE_EXT:
        rec = extract_from_image(p)
    else:
        rec = {"file": str(p), "source": "", "note": "不支持的文件类型",
               "kind": "", "code": "", "number": "", "date": "",
               "amount": "", "amount_alt": "", "checkcode": ""}
    rec.setdefault("result", "")
    rec.setdefault("detail", "")
    rec.setdefault("shot", "")
    return rec


# ---------------- 重复发票号审计 ----------------
def _file_fingerprint(path, limit=2 << 20) -> str:
    """文件指纹（读前 2MB + 文件大小）。用于判断"是不是同一份文件被放了多次" """
    import hashlib
    import os
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            h.update(f.read(limit))
        h.update(str(os.path.getsize(path)).encode())
        return h.hexdigest()
    except Exception:
        return ""


def audit_duplicates(recs) -> dict:
    """审计发票号重复情况 —— 这是「超次数跳过率偏高」最可能的根因。

    平台规则：同一张发票每天最多查验 5 次。若清单里同一号码出现 10 次，
    无论提取是否正确都会在第 6 次开始全部"超次数"。所以必须先查清单。

    三种情况要分开看（决定能否安全合并）：
      A. 文件指纹相同          → 同一份文件被重复放入（真重复）
      B. 号码+日期+金额都一致  → 同一张发票的多个副本/多次导出（可安全合并）
      C. 号码相同但字段不一致  → **疑似提取错误**：不同发票被解析成同一个号，
                                必须人工核对，绝不能盲目合并
    """
    from collections import OrderedDict
    groups = OrderedDict()
    for r in recs:
        num = _digits(r.get("number", ""))
        if not num:
            continue
        groups.setdefault(num, []).append(r)

    out_groups = []
    for num, rows in groups.items():
        if len(rows) < 2:
            continue
        dates = sorted({str(r.get("date", "") or "") for r in rows})
        amounts = sorted({str(r.get("amount", "") or "") for r in rows})
        hashes = {_file_fingerprint(r.get("file", "")) for r in rows
                  if r.get("file")}
        same_file = len(hashes) == 1 and "" not in hashes
        consistent = len(dates) == 1 and len(amounts) == 1
        out_groups.append({
            "number": num,
            "count": len(rows),
            "files": [Path(r["file"]).name for r in rows if r.get("file")],
            "rows": [r.get("idx") for r in rows],
            "same_file": same_file,
            "consistent": consistent,
            "dates": dates,
            "amounts": amounts,
        })
    dup_rows = sum(g["count"] for g in out_groups)
    suspicious = [g for g in out_groups if not g["consistent"]]
    return {
        "groups": out_groups,
        "dup_rows": dup_rows,
        "extra_rows": dup_rows - len(out_groups),
        "suspicious_groups": suspicious,
        "suspicious": len(suspicious),
        "safe": len(out_groups) - len(suspicious),
    }


def plan_dedupe(recs, audit: dict | None = None):
    """规划去重：同一张发票只提交查验一次。

    返回 (todo, followers)：
      todo      —— 真正需要提交查验的记录（每组的第一条）
      followers —— 其余重复行，[{rec, master}]，result/截图 继承 master

    安全边界：只有"号码+日期+金额完全一致"的组才合并；
    "号码相同但字段不一致"（疑似提取错误）保持逐条查验，交由人工核对。
    """
    audit = audit or audit_duplicates(recs)
    risky = {g["number"] for g in audit["groups"] if not g["consistent"]}
    master, todo, followers = {}, [], []
    for r in recs:
        num = _digits(r.get("number", ""))
        if not num or num in risky:
            todo.append(r)              # 无号码 / 存疑组：不参与合并
            continue
        if num in master:
            followers.append({"rec": r, "master": master[num]})
        else:
            master[num] = r
            todo.append(r)
    return todo, followers
