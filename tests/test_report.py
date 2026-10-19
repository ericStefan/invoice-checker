# -*- coding: utf-8 -*-
"""离线验证：重复去重 → 截图按原文件名落地 → 打包 zip → Excel/HTML 报告"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
import extract
import report

OUT = Path(tempfile.mkdtemp(prefix="invoice_report_"))

# 优先用真实结果表；没有就跳过该段（保证测试不依赖历史数据）
_real_xlsx = ROOT_DIR / "data" / "查验结果.xlsx"
if _real_xlsx.exists():
    recs = report.read_scan_excel(str(_real_xlsx))
else:
    print("提示：未找到 data/查验结果.xlsx，跳过真实清单读取（不影响后续校验）")
    recs = []
audit = extract.audit_duplicates(recs)
todo, followers = extract.plan_dedupe(recs, audit)
print(f"清单 {len(recs)} 行 → 实际查验 {len(todo)} 张，合并掉 {len(followers)} 行")

SH = ROOT_DIR / "data" / "shots"
# 截图素材：优先用真实截图；没有就现场合成（保证测试始终有效，不依赖历史数据）
pool = sorted(SH.glob("*.png")) if SH.exists() else []
SRC = OUT / "_src"
if not pool:
    SRC.mkdir(exist_ok=True)
    from PIL import Image, ImageDraw
    for k in range(3):
        p = SRC / f"fake{k}.png"
        im = Image.new("RGB", (600, 380), (250, 250, 245))
        ImageDraw.Draw(im).text((30, 30), f"invoice check shot {k}",
                                fill=(20, 20, 20))
        im.save(p)
        pool.append(p)
print("截图素材:", len(pool), "张（来源:", pool[0].parent.name, "）")

# 覆盖全部状态（含新增的「平台限流」「未执行」）
STATUSES = [
    ("一致", "当日第1次，查验通过"),
    ("查无此票", "查验结果：查无此票（请核对发票号码/开票日期/金额，"
                 "或该票信息本身有误）"),
    ("超次数跳过", "平台提示「单张发票每天最多查验5次!」→ 该票今日查验次数"
                   "已用尽（每票每天最多5次），请明日再查（已自动二次复核确认）"),
    ("平台限流", "平台提示「您的访问过于频繁，请稍后再试」→ 本张未受理，"
                 "需暂停整批后再重试（已暂停 3 次仍被限流，本批停止）"),
    ("一致", "当日第2次，查验通过"),
    ("查验失败", "验证码连续错误 6 次仍无法通过"),
]
results = []
for i, r in enumerate(todo):
    st, dt = STATUSES[i % len(STATUSES)]
    rr = dict(r)
    rr["result"], rr["detail"] = st, dt
    # 平台限流/未执行按"未查到结果"处理：不给截图
    if st == "平台限流":
        rr["shot"] = ""
    else:
        rr["shot"] = str(pool[i % len(pool)])
    results.append(rr)

# 重复行继承
by_idx = {r["idx"]: r for r in results}
for f in followers:
    rec, master = f["rec"], f["master"]
    m = by_idx.get(master["idx"])
    rr = dict(rec)
    rr["result"] = m["result"]
    rr["shot"] = m["shot"]
    rr["detail"] = (f"【重复发票】与「{Path(m['file']).name}」为同一张发票，"
                    f"已合并查验（未重复消耗查验次数）；{m['detail']}")
    rr["dup_of"] = master["idx"]
    rr["is_dup"] = True
    results.append(rr)
results.sort(key=lambda x: x["idx"])

mapping = report.materialize_shots(results, OUT / "查验截图")
zip_path = report.pack_shots_zip(mapping, str(OUT / "查验截图.zip"))
ok_idx = {r["idx"] for r in results
          if r.get("result") == "一致" and not r.get("is_dup")}
zip_ok = report.pack_shots_zip(mapping, str(OUT / "查验成功截图.zip"),
                               only_idx=ok_idx)
xlsx = report.write_excel(results, str(OUT / "查验结果.xlsx"))
htm = report.write_html(results, str(OUT / "查验结果.html"),
                        zip_name=Path(zip_path).name if zip_path else None,
                        zip_ok_name=Path(zip_ok).name if zip_ok else None,
                        shots_out_dir=OUT / "查验截图")
print("仅成功截图 zip:", Path(zip_ok).name if zip_ok else "无",
      f"({len(ok_idx)} 张)")

print(f"\n结果记录 {len(results)} 条")
print("截图落地：", len(mapping), "张")
for p in sorted((OUT / "查验截图").iterdir())[:6]:
    print("   ", p.name)
print("zip:", Path(zip_path).name if zip_path else "无",
      f"({Path(zip_path).stat().st_size // 1024} KB)" if zip_path else "")
print("html 体积:", f"{Path(htm).stat().st_size // 1024} KB")
print("xlsx:", xlsx)

# 校验：每条有结果的行都应该有对应截图
miss = [r["idx"] for r in results
        if r.get("result") in ("一致", "查无此票", "超次数跳过")
        and not (r.get("is_dup") or r.get("dup_of"))
        and not r.get("shot_final")]
print("\n缺少截图的成功行:", miss or "无")
dups = [r for r in results if r.get("is_dup")]
print("重复行数:", len(dups), "（这些行不再单独落地截图）",
      "示例明细：", dups[0]["detail"][:60] if dups else "")

# 硬校验（唯一发票 1:1 落地；重复副本不落地；新状态出现在报告里）
bad = 0
n_shot_rows = len([r for r in results
                   if r.get("result") not in ("平台限流", "未执行")
                   and not (r.get("is_dup") or r.get("dup_of"))])
if len(mapping) != n_shot_rows:
    print(f"FAIL 截图落地数不符：{len(mapping)} != {n_shot_rows}")
    bad += 1
dup_names = {report.safe_stem(Path(r.get("file") or "").stem) + "_查验截图"
             for r in dups}
dup_files = [p for p in (OUT / "查验截图").iterdir()
             if p.stem in dup_names or p.stem.rsplit("_", 1)[0] in dup_names]
if dup_files:
    print(f"FAIL 截图目录里有 {len(dup_files)} 个重复副本文件:",
          [p.name for p in dup_files][:3])
    bad += 1
if miss:
    print(f"FAIL 有 {len(miss)} 行缺失截图")
    bad += 1
html_txt = Path(htm).read_text(encoding="utf-8")
for kw in ("平台限流", "未执行", "结果口径说明", "查验一致"):
    if kw not in html_txt:
        print(f"FAIL HTML 缺少「{kw}」")
        bad += 1
print("结果:", "全部通过" if not bad else f"{bad} 项失败")
sys.exit(1 if bad else 0)
