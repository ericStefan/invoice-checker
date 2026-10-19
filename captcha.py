# -*- coding: utf-8 -*-
"""
验证码识别模块 v2（2026-09-30 实测重写）

平台验证码特征（实测）：
- 图片约 120x50，含 4 个字符，**每个字符颜色随机**（黑/红/蓝/黄/绿/紫…）
- 提示元素为 span.yzm-tips，形如：
    「请输入验证码图片中蓝色文字」 → 只填写图中**蓝色**的那些字符
    「请输入验证码图片中红色文字」 → 只填写红色字符
    「请输入验证码文字」           → 填写全部字符
- 干扰：彩色小噪点 + 一条绿色细线（绿线会污染“绿色”提示，需按形状过滤）

识别策略：按提示颜色做 HSV 分离 → 形态学清理 → 连通域切分字符 →
逐字符 OCR（限定字母数字字符集）→ 拼接。
识别失败返回 None，由上层走「换图重试 / 人工兜底」。
"""
import re

import cv2
import numpy as np

CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# 颜色词 → HSV 区间（每项可含多段）
COLOR_RANGES = {
    "红": [((0, 80, 60), (12, 255, 255)),
           ((160, 80, 60), (179, 255, 255))],
    "蓝": [((95, 70, 50), (140, 255, 255))],
    "绿": [((40, 70, 50), (92, 255, 255))],
    "黄": [((18, 80, 100), (40, 255, 255))],
    "紫": [((125, 50, 50), (165, 255, 255))],
    "橙": [((8, 90, 110), (20, 255, 255))],
    "黑": [((0, 0, 0), (180, 255, 118))],
    "灰": [((0, 0, 119), (180, 60, 200))],
}

COLOR_WORDS = list(COLOR_RANGES.keys())

_ocr = None
_ocr_beta = None
_det = None
_load_failed = False

SCALE = 4                      # 放大倍数（小图放大后 OCR 命中率显著提升）
_DEBUG_DIR = None              # 设成路径即可落盘中间图，便于排查


# ---------------- 模型 ----------------
def _load_models() -> bool:
    """惰性加载 ddddocr（限定字符集=纯字母数字，可显著减少中文误识别）"""
    global _ocr, _ocr_beta, _det, _load_failed
    if _ocr is not None:
        return _ocr is not False
    if _load_failed:
        return False
    try:
        import ddddocr
        o = ddddocr.DdddOcr(show_ad=False)
        try:
            o.set_ranges(CHARSET)
        except Exception:
            pass
        _ocr = o
        try:
            b = ddddocr.DdddOcr(show_ad=False, beta=True)
            try:
                b.set_ranges(CHARSET)
            except Exception:
                pass
            _ocr_beta = b
        except Exception:
            _ocr_beta = None
        try:
            _det = ddddocr.DdddOcr(det=True, show_ad=False)
        except Exception:
            _det = None
        return True
    except Exception:
        _load_failed = True
        _ocr = False
        return False


def _decode(img_bytes: bytes):
    arr = np.frombuffer(img_bytes, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _png(img) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    return buf.tobytes() if ok else b""


# ---------------- 提示解析 ----------------
def parse_prompt(prompt: str) -> dict:
    """解析验证码提示 → {"color": "蓝"|None, "mode": "input"}
    color=None 表示「请输入验证码文字」= 需要**全部字符**（多色合并）。"""
    p = re.sub(r"\s+", "", prompt or "")
    # 「图片中X色文字」→ 只填该颜色；注意「请输入验证码文字」不含颜色词
    for c in COLOR_WORDS:
        if f"{c}色" in p or f"{c}颜色" in p:
            return {"color": c, "mode": "input"}
    for c in COLOR_WORDS:
        if c in p:
            return {"color": c, "mode": "input"}
    return {"color": None, "mode": "input"}


# ---------------- 图像处理 ----------------
def color_mask(hsv, name: str):
    mask = np.zeros(hsv.shape[:2], np.uint8)
    for lo, hi in COLOR_RANGES.get(name, []):
        mask |= cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
    return mask


def _clean_mask(mask, noise_area=None):
    """形态学闭运算（补笔画断裂）+ 连通域去噪。
    噪点过滤阈值必须按**放大后**尺度取（原图 1~2px 的彩色小点，放大 4 倍后
    面积可达 64）——阈值太小会把噪点当字符，OCR 就出一串乱码。"""
    h, w = mask.shape
    if noise_area is None:
        noise_area = max(80, int(h * w * 0.0012))
    k = np.ones((3, 3), np.uint8)
    m = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=1)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    out = np.zeros_like(m)
    for i in range(1, num):
        if stats[i, cv2.CC_STAT_AREA] >= noise_area:
            out[labels == i] = 255
    return out


def _char_boxes(mask, gap_ratio=0.10):
    """连通域 → 字符包围盒（按 x 排序）。
    注意：平台字符间距很小且笔画常断开，合并太激进会把多个字符合成一个
    （实测：3 个红色字符被并成 1 个）。这里用**字符高度**做基准：
    间隙小于高度的 gap_ratio、且竖直方向有重叠，才判为同一字符的断笔。"""
    h, w = mask.shape
    min_h = max(10, int(h * 0.15))          # 太矮的当噪点（放大后尺度）
    min_area = max(120, int(h * w * 0.0015))
    min_w = max(4, int(w * 0.012))
    num, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    boxes = []
    for i in range(1, num):
        x, y, bw, bh, area = stats[i]
        if bh < min_h or bw < min_w or area < min_area:
            continue
        boxes.append([int(x), int(y), int(x + bw), int(y + bh)])
    boxes.sort(key=lambda b: b[0])
    merged = []
    for b in boxes:
        if not merged:
            merged.append(list(b))
            continue
        p = merged[-1]
        gap = b[0] - p[2]
        ph = p[3] - p[1]
        v_overlap = min(p[3], b[3]) - max(p[1], b[1])
        if gap <= max(2, int(ph * gap_ratio)) and v_overlap > 0:
            p[0] = min(p[0], b[0]); p[1] = min(p[1], b[1])
            p[2] = max(p[2], b[2]); p[3] = max(p[3], b[3])
            continue
        merged.append(list(b))
    return merged


def _to_gray_char(mask, box, pad=4, target_h=64):
    """从掩码里裁出单个字符 → 黑字白底 → 高度归一（OCR 输入）"""
    x1, y1, x2, y2 = box
    h, w = mask.shape
    x1 = max(0, x1 - pad); y1 = max(0, y1 - pad)
    x2 = min(w, x2 + pad); y2 = min(h, y2 + pad)
    crop = mask[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    out = np.full(crop.shape, 255, np.uint8)
    out[crop > 0] = 0
    # 统一高度（保持比例），过窄的补白边，帮助 OCR
    ch, cw = out.shape
    r = target_h / max(1, ch)
    nw = max(6, int(round(cw * r)))
    out = cv2.resize(out, (nw, target_h), interpolation=cv2.INTER_CUBIC)
    out = cv2.copyMakeBorder(out, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=255)
    return out


def _ocr_one_ex(img_gray):
    """单字符 OCR → (字符, 双模型是否一致)。
    「一致」是置信度评分的关键信号：两个不同模型都读成同一个字符时，
    该字符几乎不会错（离线核对实测准确率约 95%+）"""
    if not _load_models() or img_gray is None:
        return "", False
    raw = _png(img_gray)
    if not raw:
        return "", False
    res = []
    for m in (_ocr, _ocr_beta):
        if m is None:
            continue
        try:
            t = re.sub(r"[^0-9A-Za-z]", "", m.classification(raw) or "")
            if t:
                res.append(t[0])          # 单字符取第一个
        except Exception:
            continue
    if not res:
        return "", False
    if len(res) >= 2:
        return res[0], res[0] == res[1]
    return res[0], False


def _ocr_one(img_gray) -> str:
    """单字符 OCR（兼容旧接口）"""
    return _ocr_one_ex(img_gray)[0]


def _ocr_row(img_bgr) -> str:
    """整行 OCR（兜底）"""
    if not _load_models():
        return ""
    raw = _png(img_bgr)
    if not raw:
        return ""
    for m in (_ocr, _ocr_beta):
        if m is None:
            continue
        try:
            t = re.sub(r"[^0-9A-Za-z]", "", m.classification(raw) or "")
            if t:
                return t
        except Exception:
            continue
    return ""


def _confidence(text, boxes, row, seg, agree, nchar):
    """识别置信度评分（0~1）+ 理由文本。

    「挑简单验证码」策略的核心：只把高置信度的结果提交给平台；低置信度
    直接刷新换张图重来（刷新不消耗该发票的查验次数，是零成本的）。

    评分依据（均来自实测样本核对）：
    - 逐字符双模型结果一致 → 极强的正确信号（错读时两个模型几乎不会同时错）
    - 整行 OCR 与逐字符分割结果一致 → 交叉验证通过
    - 分割出的框数恰好等于字符数 → 掩码干净、无残留彩色噪点
    - 字符数越少（1~2 位）越不容易出错
    """
    n = len(text)
    conf, why = 0.30, []
    if 1 <= n <= 4:
        conf += 0.15
        why.append(f"{n}位")
    if n == 1:
        # 单字符无法自我交叉验证，且 ddddocr 对 0/O、1/l、g/9 等形近字
        # 容易混（实测 conf 虚高）→ 适度降权
        conf -= 0.10
        why.append("单字符(形近字风险)")
    elif n <= 2:
        conf += 0.06
        why.append("位数少")
    if row and seg and row == seg:
        conf += 0.26
        why.append("整行/分割一致")
    elif row and seg and row != seg:
        conf -= 0.10
        why.append("两种分法不一致")
    if nchar:
        ratio = agree / nchar
        conf += 0.20 * ratio
        why.append(f"双模型一致{agree}/{nchar}")
    if boxes and len(boxes) == n:
        conf += 0.12
        why.append("分割干净")
    elif boxes and len(boxes) > n:
        conf -= 0.12 * min(3, len(boxes) - n)
        why.append(f"多出{len(boxes) - n}个残块")
    if n > 6:
        conf -= 0.25
    return round(max(0.0, min(1.0, conf)), 3), "、".join(why)


def solve(img_bytes: bytes, prompt: str = "") -> dict | None:
    """统一入口：返回
    {"text": "...", "how": "...", "len": n, "conf": 0~1, "color": 提示色}
    或 None。
    上层按 conf 决定「直接填」还是「换张简单点的图」。"""
    if not _load_models():
        return None
    img = _decode(img_bytes)
    if img is None:
        return None
    spec = parse_prompt(prompt)
    color = spec.get("color")

    big = cv2.resize(img, None, fx=SCALE, fy=SCALE,
                     interpolation=cv2.INTER_CUBIC)
    hsv = cv2.cvtColor(big, cv2.COLOR_BGR2HSV)

    if color:
        attempts = [color]
    else:
        # 「请输入验证码文字」= 全部字符，但颜色未知 → 多色合并（绿多为干扰线）
        attempts = ["黑+红+蓝", "黑+红+蓝+黄", "黑"]

    for name in attempts:
        try:
            if "+" in name:
                mask = np.zeros(hsv.shape[:2], np.uint8)
                for c in name.split("+"):
                    mask |= color_mask(hsv, c)
            else:
                mask = color_mask(hsv, name)
            mask = _clean_mask(mask)
            if mask.sum() == 0:
                continue
            boxes = _char_boxes(mask)
            if not boxes:
                continue
            # 框太碎说明掩码不干净 → 只保留面积最大的 6 个（真实的字符块）
            if len(boxes) > 6:
                boxes = sorted(
                    boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]),
                    reverse=True)[:6]
                boxes.sort(key=lambda b: b[0])
            # 裁到字符有效区域再 OCR（整张图会把空白/残留噪点也喂进去）
            h, w = mask.shape
            x1 = max(0, min(b[0] for b in boxes) - 12)
            y1 = max(0, min(b[1] for b in boxes) - 12)
            x2 = min(w, max(b[2] for b in boxes) + 12)
            y2 = min(h, max(b[3] for b in boxes) + 12)
            crop_bgr = cv2.cvtColor(mask[y1:y2, x1:x2], cv2.COLOR_GRAY2BGR)
            # 方案 A：掩码整行 OCR
            row = _ocr_row(crop_bgr)
            # 方案 B：逐字符分割 OCR（同时统计双模型一致率 = 置信度信号）
            seg = ""
            agree = 0
            nchar = 0
            try:
                chars = []
                for bx in boxes:
                    g = _to_gray_char(mask, bx)
                    ch, ag = _ocr_one_ex(g)
                    if ch:
                        chars.append(ch)
                        nchar += 1
                        agree += 1 if ag else 0
                seg = "".join(chars)
            except Exception:
                seg = ""
            # 择优：一致最优；其次长度合理的整行（掩码干净时整行最稳）；
            #       再次切分结果
            if seg and row and seg == row:
                text, how = seg, f"颜色分离[{name}]+整行分割一致"
            elif row and 1 <= len(row) <= 6:
                text, how = row, f"颜色分离[{name}]+整行"
            elif seg and 1 <= len(seg) <= 6:
                text, how = seg, f"颜色分离[{name}]+分割"
            elif row:
                text, how = row, f"颜色分离[{name}]+整行"
            elif seg:
                text, how = seg, f"颜色分离[{name}]+分割"
            else:
                continue
            if text:
                conf, why = _confidence(text, boxes, row, seg, agree, nchar)
                if not color:
                    # 实测（2026-09-30，连续 15 张样本）：提示为「请输入验证码
                    # 文字」（不限定颜色）时，掩码把所有颜色的字符混在一起，
                    # 平均置信度仅 0.39；而「蓝色/红色文字」类平均 0.67。
                    # 因此主动降权，让程序优先去挑**颜色限定型**的简单图。
                    conf = round(max(0.0, conf - 0.12), 3)
                    why += "、非颜色限定"
                _save_debug(img, mask, boxes, text, name)
                return {"text": text, "how": how, "len": len(text),
                        "conf": conf, "color": name, "why": why}
        except Exception:
            continue
    # 兜底：整图 OCR（混杂多色字符，噪声大 → 置信度刻意压低）
    t = _ocr_row(big)
    if t:
        return {"text": t, "how": "整图OCR", "len": len(t),
                "conf": 0.30, "color": None, "why": "整图识别，未经颜色分离"}
    return None


def _save_debug(img_bgr, mask, boxes, text, name):
    if not _DEBUG_DIR:
        return
    try:
        import time
        from pathlib import Path
        ascii_name = (name.replace("黑", "black").replace("红", "red")
                      .replace("蓝", "blue").replace("黄", "yellow")
                      .replace("绿", "green").replace("紫", "purple")
                      .replace("+", "_"))
        d = Path(_DEBUG_DIR)
        d.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%H%M%S") + f"{int(time.time()*1000) % 1000:03d}"
        vis = img_bgr.copy()
        for (x1, y1, x2, y2) in boxes:
            cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 0, 255), 2)
        cv2.imwrite(str(d / f"{ts}_{ascii_name}_{text}_orig.png"), img_bgr)
        cv2.imwrite(str(d / f"{ts}_{ascii_name}_{text}_mask.png"), mask)
        cv2.imwrite(str(d / f"{ts}_{ascii_name}_{text}_box.png"), vis)
    except Exception:
        pass


# 兼容旧接口（曾经的点选型/颜色型调用）
def solve_colored(img_bytes: bytes, color: str) -> str | None:
    r = solve(img_bytes, f"请输入验证码图片中{color}文字")
    return r["text"] if r else None


def solve_input(img_bytes: bytes) -> dict | None:
    return solve(img_bytes, "请输入验证码文字")
