# -*- coding: utf-8 -*-
"""
发票批量查验桌面工具（Tkinter）

用法：
  ① 添加发票文件 / 添加文件夹 / 导入Excel清单  → ② 开始查验
  → 自动填单、自动挑「简单验证码」提交 → 结束后生成 Excel + HTML 报告，
    并把截图按「原发票文件名_查验截图.png」整理到「查验截图」目录。

设计要点（2026-09-30 版）：
  · 验证码**择优提交**：只填识别清晰的图，复杂图直接刷新换一张。
  · 超次数**二次复核**：命中次数上限后自动再确认一次，避免误判。
  · 重复发票**自动去重**：同一张发票只提交查验一次，其余行继承结果与截图
    （这是"超次数率高"的主因——同一张票被重复计入清单）。
  · 查无此票**独立标注**：不再与"查验通过"混淆。
"""
import queue
import shutil
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

import extract
from checker import Checker, ManualSolver, detect_engine
import report

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"               # 所有运行产物集中在此（已 gitignore）
SHOTS_DIR = DATA_DIR / "查验截图"      # 与发票一一对应的截图
RAW_SHOTS = DATA_DIR / "shots"         # 原始调试截图
DATA_DIR.mkdir(parents=True, exist_ok=True)


class ManualCaptchaDialog(tk.Toplevel):
    """人工输入验证码弹窗：显示验证码图 + **平台提示原文与操作指引**，输入文字
    （预填自动识别建议），或按提示顺序在图上点选"""
    def __init__(self, master, img_bytes, prompt, mode="click", scale=3.0,
                 guess=None):
        super().__init__(master)
        self.title("请输入验证码")
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.grab_set()
        self.lift()
        try:
            self.bell()                    # 提示音：忙别的事时也能察觉
        except Exception:
            pass
        self.res = None
        self.scale = scale
        self.points = []
        import io
        from PIL import Image, ImageTk
        self.pil = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        w, h = self.pil.size
        self.disp = self.pil.resize((int(w * scale), int(h * scale)),
                                    Image.LANCZOS)
        self.tkimg = ImageTk.PhotoImage(self.disp)

        # ---- 提示区：平台原文 + 人话指引（关键：告诉用户填哪种颜色）----
        box = ttk.Frame(self, padding=(10, 8, 10, 0))
        box.pack(fill="x")
        ttk.Label(box, text=f"平台提示：{prompt or '（未读到提示，请按图片内容填写）'}",
                  font=("微软雅黑", 12, "bold"),
                  foreground="#b91c1c", wraplength=380,
                  justify="left").pack(anchor="w")
        ttk.Label(box, text=self._human_hint(prompt),
                  font=("微软雅黑", 11), foreground="#1a4f8b",
                  wraplength=380, justify="left").pack(anchor="w", pady=(4, 0))

        self.canvas = tk.Canvas(self, width=self.disp.width,
                                height=self.disp.height, cursor="crosshair")
        self.canvas.pack(padx=10, pady=8)
        self.canvas.create_image(0, 0, anchor="nw", image=self.tkimg)
        if mode == "click":
            self.canvas.bind("<Button-1>", self._click)
            ttk.Button(self, text="清除重选", command=self._clear).pack(
                side="left", padx=10, pady=8)
            ttk.Button(self, text="完成", command=self._done).pack(
                side="right", padx=10, pady=8)
        else:
            e = ttk.Entry(self, font=("微软雅黑", 16))
            e.pack(padx=10, pady=8, fill="x")
            if guess:
                e.insert(0, guess)
                e.select_range(0, "end")
            e.focus()
            e.bind("<Return>", lambda _e: self._done_text(e.get()))
            ttk.Button(self, text="确定（回车）",
                       command=lambda: self._done_text(e.get())
                       ).pack(pady=(0, 8))
        # 窗口居中显示在屏幕中部偏上，避免被浏览器挡住
        self.update_idletasks()
        try:
            sw = self.winfo_screenwidth()
            ww = max(420, self.winfo_reqwidth())
            self.geometry(f"+{(sw - ww) // 2}+120")
        except Exception:
            pass
        self.protocol("WM_DELETE_WINDOW", lambda: self._finish({"skip": True}))

    @staticmethod
    def _human_hint(prompt: str) -> str:
        """把平台提示翻译成人话 —— 重点是「只填指定颜色的字」"""
        p = prompt or ""
        for c in ("红", "蓝", "绿", "黄", "紫", "橙", "黑"):
            if c in p:
                return (f"→ 只输入图中【{c}色】的字符，其他颜色的不用输！\n"
                        f"（已预填自动识别结果，核对无误后直接按回车）")
        if "文字" in p or "输入" in p:
            return ("→ 输入图片中的全部字符（不分颜色）。\n"
                    "（已预填自动识别结果，核对无误后直接按回车）")
        return "→ 按图片内容输入验证码（已预填自动识别结果）"

    def _click(self, ev):
        self.points.append((ev.x / self.scale, ev.y / self.scale))
        r = 7
        self.canvas.create_oval(ev.x - r, ev.y - r, ev.x + r, ev.y + r,
                                outline="red", width=2)
        self.canvas.create_text(ev.x + r + 2, ev.y,
                                text=str(len(self.points)), fill="red",
                                font=("Arial", 10, "bold"))

    def _clear(self):
        self.points = []
        self.canvas.create_image(0, 0, anchor="nw", image=self.tkimg)

    def _done(self):
        self._finish({"points": self.points})

    def _done_text(self, text):
        self._finish({"text": text})

    def _finish(self, res):
        self.res = res
        self.destroy()


class App:
    def __init__(self, master: tk.Tk):
        self.master = master
        master.title("发票批量查验工具 ｜ 全国增值税发票查验平台")
        master.geometry("1180x820")
        self.q = queue.Queue()
        self.records = []
        self.results = []
        self.followers = []          # 被去重合并的重复行
        self.audit = None
        self.worker = None
        self.ck = None
        self.manual = ManualSolver()
        self._build_ui()
        master.after(150, self._poll_queue)

    # ---------- UI ----------
    def _build_ui(self):
        top = ttk.Frame(self.master, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="① 添加发票文件", command=self.add_files
                   ).pack(side="left")
        ttk.Button(top, text="添加文件夹", command=self.add_folder
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="导入Excel清单（免扫描）",
                   command=self.import_excel).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="清空列表", command=self.clear_records
                   ).pack(side="left", padx=(6, 0))
        self.var_folder = tk.StringVar(value="（尚未添加发票）")
        ttk.Label(top, textvariable=self.var_folder, foreground="#555"
                  ).pack(side="left", padx=8, fill="x", expand=True)

        bar = ttk.Frame(self.master, padding=(8, 0, 8, 4))
        bar.pack(fill="x")
        self.btn_start = ttk.Button(bar, text="② 开始查验（自动跳过未识别项）",
                                    command=self.start, state="disabled")
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="停止", command=self.stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_scanxlsx = ttk.Button(bar, text="导出识别清单Excel",
                                       command=self.export_scan,
                                       state="disabled")
        self.btn_scanxlsx.pack(side="left", padx=6)
        ttk.Button(bar, text="打开HTML报告", command=lambda: self.export("html")
                   ).pack(side="left", padx=6)
        ttk.Button(bar, text="导出结果Excel", command=lambda: self.export("excel")
                   ).pack(side="left")
        ttk.Button(bar, text="打开截图文件夹", command=self.open_shots
                   ).pack(side="left", padx=6)

        opts = ttk.Frame(self.master, padding=(8, 0, 8, 4))
        opts.pack(fill="x")
        self.var_dedupe = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="重复发票只查验一次（推荐：同一张票每天限5次）",
                        variable=self.var_dedupe).pack(side="left")
        self.engine = detect_engine()
        self.var_use_chrome = tk.BooleanVar(value=bool(self.engine))
        ttk.Checkbutton(opts, text="用本机Chrome/Edge内核",
                        variable=self.var_use_chrome).pack(side="right")
        self.var_headless = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="隐藏浏览器(不建议)",
                        variable=self.var_headless).pack(side="right")
        self.var_manual_cap = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="验证码人工输入(默认全自动)",
                        variable=self.var_manual_cap).pack(side="right")

        mid = ttk.Frame(self.master, padding=(8, 0))
        mid.pack(fill="both", expand=True)
        self.mid_frame = mid
        # 限流/暂停提示条（非阻塞，避免被误认为"卡死"）
        self.notice_lbl = tk.Label(self.master, text="", anchor="w",
                                   justify="left", wraplength=1120,
                                   font=("微软雅黑", 10, "bold"),
                                   fg="#ffffff", bg="#c2410c",
                                   padx=10, pady=6)
        self._notice_shown = False
        cols = ("idx", "file", "kind", "code", "number", "date",
                "amount", "result", "detail")
        heads = ("序号", "文件名", "票种", "发票代码", "发票号码", "开票日期",
                 "金额", "查验结果", "明细")
        widths = (50, 200, 66, 110, 160, 90, 74, 90, 300)
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=17)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        ysb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        for st, color in (("一致", "#1a7f37"), ("冲红", "#b45309"),
                          ("作废", "#b45309"), ("不一致", "#b91c1c"),
                          ("查无此票", "#b45309"), ("超次数跳过", "#c2410c"),
                          ("票面信息有误", "#b91c1c"),
                          ("平台限流", "#c2410c"), ("未执行", "#6b7280"),
                          ("重复发票", "#5b4bb7"), ("dup", "#5b4bb7"),
                          ("跳过（未识别为发票）", "#6b7280")):
            self.tree.tag_configure(st, foreground=color)
        self.tree.bind("<Double-1>", self.edit_record)

        logframe = ttk.LabelFrame(self.master, text="运行日志（关键步骤）",
                                  padding=4)
        logframe.pack(fill="both", expand=False, padx=8, pady=(4, 0))
        self.logbox = tk.Text(logframe, height=8, font=("Consolas", 9),
                              state="disabled", wrap="word")
        lysb = ttk.Scrollbar(logframe, orient="vertical",
                             command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=lysb.set)
        self.logbox.pack(side="left", fill="both", expand=True)
        lysb.pack(side="right", fill="y")

        bottom = ttk.Frame(self.master, padding=8)
        bottom.pack(fill="x")
        self.var_status = tk.StringVar(value="就绪。双击行可修改发票字段后重新查验。")
        ttk.Label(bottom, textvariable=self.var_status).pack(side="left")

    def _log(self, msg: str):
        """日志区显示 + 状态栏同步"""
        import time as _t
        line = f"[{_t.strftime('%H:%M:%S')}] {msg}"
        self.logbox.configure(state="normal")
        self.logbox.insert("end", line + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    # ---------- 提示条（非阻塞，用于限流暂停等"需要用户知情"的状态） ----------
    def _show_notice(self, text, bg="#c2410c", fg="#ffffff"):
        self.notice_lbl.configure(text=text, bg=bg, fg=fg)
        if not self._notice_shown:
            self.notice_lbl.pack(fill="x", padx=8, pady=(4, 0),
                                 before=self.mid_frame)
            self._notice_shown = True
        else:
            self.notice_lbl.pack_configure(fill="x", padx=8, pady=(4, 0))

    def _hide_notice(self):
        if self._notice_shown:
            self.notice_lbl.pack_forget()
            self._notice_shown = False

    def open_shots(self):
        import os
        d = SHOTS_DIR if SHOTS_DIR.exists() else RAW_SHOTS
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))
        except Exception:
            messagebox.showinfo("截图目录", str(d))

    # ---------- 输出归档（防止上一轮结果被静默覆盖） ----------
    def _rotate_prev_outputs(self):
        """新一轮开始前，把上一次的结果表/报告/截图包归档到
        「历史输出/<时间戳>/」。

        ⚠ 由来（2026-09-30）：这些文件名是固定的，新一跑会直接覆盖旧文件，
        一旦流程中断或误操作，用户已经查好的结果就没了。归档后历史永远可查。
        """
        names = ("查验结果.xlsx", "查验结果.html",
                 "查验截图.zip", "查验成功截图.zip")
        prev = [DATA_DIR / n for n in names if (DATA_DIR / n).exists()]
        # 上一轮的截图也一并归档：否则「查验截图」目录会一轮轮堆叠，混着新旧批次
        shots = [p for p in SHOTS_DIR.glob("*") if p.is_file()] \
            if SHOTS_DIR.exists() else []
        if not prev and not shots:
            return None
        dst = DATA_DIR / "历史输出" / time.strftime("%Y%m%d_%H%M%S")
        try:
            dst.mkdir(parents=True, exist_ok=True)
            for p in prev:
                shutil.move(str(p), str(dst / p.name))
            if shots:
                sd = dst / "查验截图_上一轮"
                sd.mkdir(parents=True, exist_ok=True)
                for p in shots:
                    shutil.move(str(p), str(sd / p.name))
        except Exception as e:
            self._log(f"历史结果归档失败（不影响本次查验）：{str(e)[:60]}")
            return None
        self._log(f"上一次的结果已归档到：{dst}")
        return dst

    def clear_records(self):
        self.records, self.results, self.followers = [], [], []
        self.audit = None
        self.tree.delete(*self.tree.get_children())
        self.var_folder.set("（尚未添加发票）")
        self.btn_start["state"] = "disabled"
        self.btn_scanxlsx["state"] = "disabled"
        self.var_status.set("已清空列表。")

    # ---------- 添加发票 ----------
    def add_files(self):
        """添加单个或多个发票文件（少量查验需求）"""
        paths = filedialog.askopenfilenames(
            title="选择发票文件（可多选）",
            filetypes=[("发票文件", "*.pdf *.jpg *.jpeg *.png *.bmp *.webp"),
                       ("PDF", "*.pdf"), ("图片", "*.jpg *.jpeg *.png *.bmp"),
                       ("全部文件", "*.*")])
        if not paths:
            return
        recs = []
        for p in paths:
            self.var_status.set(f"正在识别 {Path(p).name} …")
            self.master.update_idletasks()
            try:
                recs.append(extract.extract_file(p))
            except Exception as e:
                recs.append({"file": p, "number": "", "note": f"解析异常: {e}"})
        self._apply_records(self.records + recs,
                            f"已添加 {len(paths)} 个文件（累计 {len(self.records) + len(paths)} 个）")

    def add_folder(self):
        d = filedialog.askdirectory(title="选择发票所在文件夹")
        if not d:
            return
        self.var_status.set("正在扫描文件夹…")
        self.master.update_idletasks()
        recs = extract.scan_folder(d)
        self._apply_records(self.records + recs,
                            f"[{Path(d).name}] 已扫描 {len(recs)} 个文件")

    def import_excel(self):
        """导入识别清单 Excel，免扫描直接查验"""
        p = filedialog.askopenfilename(
            title="选择识别清单/查验结果 Excel",
            filetypes=[("Excel 文件", "*.xlsx")])
        if not p:
            return
        try:
            recs = report.read_scan_excel(p)
        except Exception as e:
            messagebox.showerror("导入失败", f"读取 Excel 出错：\n{e}")
            return
        if not recs:
            messagebox.showwarning("导入失败", "表格里没有有效数据行。")
            return
        self._apply_records(recs, f"[Excel导入] {Path(p).name}")

    def _apply_records(self, recs, label):
        """统一装载记录列表：重排序号 → 审计重复 → 刷新表格 → 提示"""
        for i, r in enumerate(recs, 1):
            r["idx"] = i
            r.setdefault("result", "")
            r.setdefault("detail", "")
            r.setdefault("shot", "")
            r.pop("is_dup", None)
            r.pop("dup_of", None)
        self.records = recs
        self.followers = []
        self.var_folder.set(label)
        self.audit = extract.audit_duplicates(recs)
        self._refresh_tree()

        ok = sum(1 for r in recs if r.get("number"))
        extra = self.audit["extra_rows"] if self.var_dedupe.get() else 0
        msg = (f"共 {len(recs)} 条记录，识别出发票 {ok} 张"
               f"（未识别 {len(recs) - ok} 条将自动跳过）")
        if self.audit["groups"]:
            msg += (f"；检测到 {len(self.audit['groups'])} 组重复发票号，"
                    f"去重后可少查 {extra} 次")
        self.var_status.set(msg + "。双击行可人工修改字段。")
        self.btn_start["state"] = "normal" if ok else "disabled"
        self.btn_scanxlsx["state"] = "normal"
        self._show_audit_dialog()

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        dup_nums = {}
        if self.audit:
            for g in self.audit["groups"]:
                dup_nums[g["number"]] = g
        for r in self.records:
            num = (r.get("number") or "").strip()
            mark = r.get("note", "") if not num else ""
            g = dup_nums.get(num)
            tag = ()
            if g:
                if not g["consistent"]:
                    mark = (f"⚠ 疑似提取错误：号码重复但字段不一致"
                            f"（日期{g['dates']}／金额{g['amounts']}），请人工核对")
                    tag = ("票面信息有误",)
                else:
                    mark = f"↺ 与另外 {g['count'] - 1} 个文件为同一张发票"
                    tag = ("dup",)
            self.tree.insert("", "end", iid=str(r["idx"]), values=(
                r["idx"], Path(r["file"]).name, r.get("kind", ""),
                r.get("code", ""), num, r.get("date", ""),
                r.get("amount", ""), "", mark or r.get("detail", "")),
                tags=tag)

    def _show_audit_dialog(self):
        """重复发票审计提示 —— 直接回答"是识别错还是发票本身重复" """
        a = self.audit
        if not a or not a["groups"]:
            return
        lines = [f"检测到 {len(a['groups'])} 组发票号码重复，"
                 f"共 {a['dup_rows']} 条记录（去重后可少查验 {a['extra_rows']} 次）。",
                 ""]
        incon = [g for g in a["groups"] if not g["consistent"]]
        same = [g for g in a["groups"] if g["consistent"]]
        if same:
            lines.append(f"【同一张发票被多次收录】{len(same)} 组")
            for g in same[:6]:
                lines.append(f"  · {g['number']}：{g['count']} 个文件，"
                             f"日期/金额完全一致（{g['dates'][0]}／"
                             f"{g['amounts'][0]}）")
            lines.append("  → 说明发票本身重复（同一张票附在多份单据里），"
                         "不是识别错误。默认只查验一次。")
        if incon:
            lines.append("")
            lines.append(f"【⚠ 疑似提取错误】{len(incon)} 组：号码相同但日期/金额不同")
            for g in incon[:6]:
                lines.append(f"  · {g['number']}：日期{g['dates']} 金额{g['amounts']}")
            lines.append("  → 这些行**不会自动合并**，请双击核对原始发票后再查验。")
        lines.append("")
        lines.append("可在主界面取消勾选「重复发票只查验一次」改为逐张查验"
                     "（注意：同一张票每天只能查 5 次）。")
        messagebox.showinfo("发票号重复检查", "\n".join(lines))

    def export_scan(self):
        """导出识别清单（扫描后即可用，无需先查验）"""
        if not self.records:
            messagebox.showinfo("提示", "请先添加发票。")
            return
        p = filedialog.asksaveasfilename(defaultextension=".xlsx",
                                         initialfile="发票识别清单.xlsx")
        if not p:
            return
        recs = []
        for r in self.records:
            rr = dict(r)
            if not rr.get("number"):
                rr["result"] = "跳过（未识别为发票）"
                rr["detail"] = rr.get("note", "")
            recs.append(rr)
        report.write_excel(recs, p)
        self.var_status.set(f"识别清单已导出：{p}")
        messagebox.showinfo("导出成功",
                            f"识别清单已保存：\n{p}\n\n下次点「导入Excel清单」"
                            f"选择此文件即可免扫描直接查验。")

    def edit_record(self, event):
        """双击编辑该行字段（弹小窗）"""
        iid = self.tree.focus()
        if not iid:
            return
        rec = next((r for r in self.records if str(r["idx"]) == iid), None)
        if rec is None:
            return
        win = tk.Toplevel(self.master)
        win.title("编辑发票字段")
        win.grab_set()
        vals = {}
        for j, (label, key) in enumerate([("票种(kind)", "kind"),
                                          ("发票代码", "code"),
                                          ("发票号码", "number"),
                                          ("开票日期(YYYYMMDD)", "date"),
                                          ("金额", "amount"),
                                          ("校验码后6位", "checkcode")]):
            ttk.Label(win, text=label).grid(row=j, column=0, padx=8, pady=4,
                                            sticky="e")
            v = tk.StringVar(value=rec.get(key, ""))
            ttk.Entry(win, textvariable=v, width=28).grid(row=j, column=1,
                                                          padx=8, pady=4)
            vals[key] = v

        def save():
            for k, v in vals.items():
                rec[k] = v.get().strip()
            win.destroy()
            self.audit = extract.audit_duplicates(self.records)
            self._refresh_tree()
            self.var_status.set("字段已修改，重复检查已重新计算。")
        ttk.Button(win, text="保存", command=save).grid(row=6, column=1,
                                                        pady=8, sticky="e")

    # ---------- 执行 ----------
    def start(self):
        if self.var_dedupe.get():
            todo, self.followers = extract.plan_dedupe(self.records, self.audit)
        else:
            todo, self.followers = self.records, []
        todo = [r for r in todo if r.get("number")]
        skipped = [r for r in self.records
                   if not r.get("number") and not r.get("is_dup")]
        if not todo:
            messagebox.showwarning("提示", "没有可查验的发票，请先补录发票号码。")
            return
        plan = (f"将查验 {len(todo)} 张发票"
                f"（自动跳过 {len(skipped)} 个未识别项）。\n")
        if self.followers:
            plan += (f"其中 {len(self.followers)} 条重复发票已合并，"
                     f"结果与截图会一并复制给对应文件。\n")
        plan += ("浏览器会自动填单，只挑识别清晰的验证码提交，"
                 "遇到识别不出时才弹窗。\n"
                 if not self.var_manual_cap.get() else
                 "验证码弹窗会显示平台提示（注意看要填哪种颜色的字）。\n")
        plan += "\n注意：同一张发票每天最多查验 5 次。确定开始？"
        if not messagebox.askyesno("确认开始查验", plan):
            return
        self.results = []
        for r in skipped:
            rr = dict(r)
            rr["result"] = "跳过（未识别为发票）"
            rr["detail"] = r.get("note", "") or "未识别出发票号码"
            self.results.append(rr)
            self.tree.item(str(r["idx"]), values=(
                r["idx"], Path(r["file"]).name, r.get("kind", ""),
                r.get("code", ""), "", r.get("date", ""),
                r.get("amount", ""), "跳过（未识别为发票）", rr["detail"]))
        self.btn_start["state"] = "disabled"
        self.btn_stop["state"] = "normal"
        self._hide_notice()
        self._rotate_prev_outputs()
        self.var_status.set("正在启动浏览器…")
        self._log(f"开始查验：共 {len(todo)} 张，跳过 {len(skipped)} 个未识别项，"
                  f"合并重复 {len(self.followers)} 条")
        eng = self.engine if self.var_use_chrome.get() else None
        self._log(f"正在启动本机 {eng} 内核浏览器…" if eng else
                  "正在启动内置 Chromium 内核"
                  "（若页面加载慢，请勾选「用本机Chrome/Edge内核」）")
        self._log("浏览器工作期间请勿手动操作它，验证码弹窗会在工具里弹出。")

        if self.var_headless.get() and self.var_use_chrome.get():
            messagebox.showwarning(
                "提示", "「隐藏浏览器」与「用本机Chrome/Edge内核」同时勾选"
                       "可能导致页面加载失败，现自动取消隐藏。")
            self.var_headless.set(False)

        def worker():
            manual_only = self.var_manual_cap.get()
            ck = Checker(shots_dir=str(RAW_SHOTS),
                         headless=self.var_headless.get(),
                         manual=self.manual, log=lambda m: self.q.put(("log", m)),
                         # 全自动：自动挑图；纯人工：直接弹窗
                         auto_captcha_tries=0 if manual_only else 3,
                         manual_captcha_tries=6,
                         channel=self.engine if self.var_use_chrome.get() else None,
                         # 限流暂停/倒计时/恢复 → 主线程更新界面提示条
                         notice=lambda kind, sec, msg:
                         self.q.put(("notice", kind, sec, msg)))
            self.ck = ck
            try:
                res = ck.run_batch(todo, on_done=lambda r, i, n:
                                   self.q.put(("done", r, i, n)))
                self.q.put(("finish", res))
            except Exception as e:
                self.q.put(("error", str(e)))
        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def stop(self):
        if not messagebox.askyesno("确认停止", "停止后需重新点「开始查验」继续。\n"
                                                "确定停止吗？"):
            return
        self.var_status.set("正在停止（当前发票完成后退出）…")
        self._log("收到停止指令，将在当前发票完成后退出…")
        if self.ck:
            self.ck.stop()
        self.btn_stop["state"] = "disabled"

    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.var_status.set(msg[1])
                    self._log(msg[1])
                elif kind == "done":
                    r, i, n = msg[1], msg[2], msg[3]
                    iid = str(r["idx"])
                    if self.tree.exists(iid):
                        self.tree.item(iid, values=(
                            r["idx"], Path(r["file"]).name, r.get("kind", ""),
                            r.get("code", ""), r.get("number", ""),
                            r.get("date", ""), r.get("amount", ""),
                            r.get("result", ""), r.get("detail", "")))
                        self.tree.item(iid, tags=(r.get("result", ""),))
                        self.tree.see(iid)
                    self.results.append(r)
                    self.var_status.set(f"进度 {i}/{n}")
                    self._log(f"第{i}/{n}张 {r.get('number','')} → "
                              f"{r.get('result','')} {r.get('detail','')[:40]}")
                elif kind == "notice":
                    act, sec, msg = msg[1], msg[2], msg[3]
                    if act == "pause":
                        self.var_status.set(
                            f"平台风控：已暂停 {sec} 秒后自动重试本张…")
                        self._show_notice(
                            f"⏸ 平台弹出「您的访问过于频繁，请稍后再试」——"
                            f"程序已自动关掉该弹框并暂停整批，"
                            f"{sec} 秒后自动重试本张（剩余 {sec} 秒）。\n"
                            f"可点「停止」中止；若连续多次仍被限流，"
                            f"程序会停止本批并保留已完成的结果。")
                        self._log(f"⏸ 平台风控：暂停 {sec} 秒（界面有倒计时）")
                    elif act == "countdown":
                        self._show_notice(
                            f"⏸ 平台风控暂停中…剩余 {sec} 秒后自动重试本张"
                            f"（点「停止」可中止）")
                    elif act == "resume":
                        self._show_notice("▶ 已恢复查验，正在重试刚才被限流的那张…",
                                          bg="#1a7f37")
                        self._log("▶ 暂停结束，恢复查验")
                        self.master.after(4000, self._hide_notice)
                elif kind == "finish":
                    self.results = msg[1]
                    self._finished()
                elif kind == "error":
                    self._hide_notice()
                    self._log("出错：" + msg[1])
                    self.var_status.set(f"出错：{msg[1]}")
                    messagebox.showerror(
                        "查验中断",
                        f"流程异常，已停止：\n\n{msg[1]}\n\n"
                        f"常见问题：网络不通/平台改版/浏览器未安装。\n"
                        f"可先重跑一次；若仍失败请截图发给管理员。")
                    self.btn_start["state"] = "normal"
                    self.btn_stop["state"] = "disabled"
        except queue.Empty:
            pass
        # 人工输入验证码请求
        try:
            while True:
                req = self.manual.q_req.get_nowait()
                dlg = ManualCaptchaDialog(self.master, req["img"],
                                          req["prompt"], req["mode"],
                                          guess=req.get("guess"))
                self.master.wait_window(dlg)
                self.manual.q_res.put(dlg.res or {"skip": True})
        except queue.Empty:
            pass
        self.master.after(150, self._poll_queue)

    # ---------- 收尾 ----------
    def _merge_followers(self, results):
        """把"重复发票"行补进结果表：继承主记录的结果与截图"""
        if not self.followers:
            return results
        by_idx = {r.get("idx"): r for r in results}
        out = list(results)
        for f in self.followers:
            rec, master = f["rec"], f["master"]
            m = by_idx.get(master.get("idx"))
            rr = dict(rec)
            if not m or not m.get("result"):
                rr["result"] = "跳过（未识别为发票）"
                rr["detail"] = "所属重复组的主记录未被查验"
                out.append(rr)
                continue
            name = Path(m.get("file") or "").name
            rr["result"] = m.get("result", "")
            rr["shot"] = m.get("shot", "")
            rr["kind"] = rr.get("kind") or m.get("kind", "")
            rr["detail"] = (f"【重复发票】与「{name}」为同一张发票，已合并查验"
                            f"（未重复消耗查验次数）；{m.get('detail', '')}")
            rr["dup_of"] = master.get("idx")
            rr["is_dup"] = True
            out.append(rr)
        out.sort(key=lambda x: x.get("idx") or 0)
        return out

    def _finished(self):
        self.btn_start["state"] = "normal"
        self.btn_stop["state"] = "disabled"
        self._hide_notice()
        self.results = self._merge_followers(self.results)
        reason = (self.ck.stop_reason if self.ck else "") or ""

        # 被中断时：把「平台限流 / 未执行」两类同步到表格，便于一眼看出待重跑
        for r in self.results:
            if r.get("result") not in ("平台限流", "未执行"):
                continue
            iid = str(r.get("idx"))
            if self.tree.exists(iid):
                self.tree.item(iid, values=(
                    r["idx"], Path(r["file"]).name, r.get("kind", ""),
                    r.get("code", ""), r.get("number", ""), r.get("date", ""),
                    r.get("amount", ""), r.get("result", ""),
                    r.get("detail", "")))
                self.tree.item(iid, tags=(r.get("result", ""),))

        # 截图落地：原发票文件名_查验截图.png（重复发票只留一份，不产生副本）
        mapping, zip_path, zip_ok = {}, "", ""
        try:
            mapping = report.materialize_shots(self.results, SHOTS_DIR)
            # 把历史遗留的重复副本（同一张票的其它文件名副本）移走，目录保持唯一
            moved = report.prune_dup_shots(self.results, SHOTS_DIR)
            if moved:
                self._log(f"已把 {len(moved)} 张重复发票的截图副本移出"
                          f"（内容相同，仅保留第一张不重复的文件名）")
            if mapping:
                zip_path = report.pack_shots_zip(
                    mapping, str(DATA_DIR / "查验截图.zip"))
                ok_idx = {r.get("idx") for r in self.results
                          if r.get("result") == "一致" and not r.get("is_dup")}
                zip_ok = report.pack_shots_zip(
                    mapping, str(DATA_DIR / "查验成功截图.zip"), only_idx=ok_idx)
        except Exception as e:
            self._log(f"截图整理失败：{str(e)[:80]}")

        xlsx = report.write_excel(self.results, str(DATA_DIR / "查验结果.xlsx"))
        htm = report.write_html(
            self.results, str(DATA_DIR / "查验结果.html"),
            zip_name=Path(zip_path).name if zip_path else None,
            zip_ok_name=Path(zip_ok).name if zip_ok else None,
            shots_out_dir=SHOTS_DIR if mapping else None)
        n_ok = sum(1 for r in self.results if r.get("result") == "一致")
        n_none = sum(1 for r in self.results if r.get("result") == "查无此票")
        n_rate = sum(1 for r in self.results if r.get("result") == "平台限流")
        n_left = sum(1 for r in self.results if r.get("result") == "未执行")
        tail = ""
        if reason == "平台限流":
            tail = (f"⚠ 平台风控中断：{n_rate} 张被限流、{n_left} 张未执行，"
                    f"建议等 10~30 分钟后用「导入Excel清单」只重跑这两类。")
        elif reason == "用户停止":
            tail = f"已手动停止：{n_left} 张未执行（可重跑）。"
        elif reason.startswith("异常中断"):
            tail = (f"⚠ 运行中断：{n_left} 张未执行，"
                    f"已完成的结果已全部保存（可重跑未执行的）。")
        self.var_status.set(
            f"完成：{len(self.results)} 条记录，查验一致 {n_ok} 条，"
            f"查无此票 {n_none} 条（需核对）；截图已整理到「查验截图」目录。"
            + (f"  {tail}" if tail else ""))
        self._log(f"=== 结束：{len(self.results)} 条，一致 {n_ok}，"
                  f"查无此票 {n_none}，唯一发票截图 {len(mapping)} 张"
                  f"（重复副本已剔除）"
                  f"{'，已打包 查验截图.zip' if zip_path else ''}")
        if tail:
            self._log(tail)
        import webbrowser
        webbrowser.open("file://" + htm.replace("\\", "/"))
        extra = ""
        if reason == "平台限流":
            extra = ("\n\n⚠ 本次被平台风控中断（提示「您的访问过于频繁」）。\n"
                     f"程序已自动关闭该弹框、暂停并重试多次仍无法继续，故停止本批：\n"
                     f"• 被限流 {n_rate} 张、未执行 {n_left} 张\n"
                     f"• 其余发票的结果与截图已全部保存，不会丢\n"
                     f"• 请等 10~30 分钟后，用「导入Excel清单」导入本次的"
                     f"结果 Excel，直接对这两类重跑即可\n"
                     f"（这两类在表里标为「平台限流」「未执行」）")
        elif reason.startswith("异常中断"):
            err = (self.ck.last_error if self.ck else "") or ""
            extra = (f"\n\n⚠ 本次运行意外中断：\n{err[:300]}\n\n"
                     f"• 已完成的 {len(self.results) - n_left} 张结果与截图已保存\n"
                     f"• {n_left} 张未执行，可稍后重跑未执行的")
        messagebox.showinfo(
            "完成",
            f"查验结束，报告已生成：\n\n"
            f"• 结果表：{xlsx}\n"
            f"• 网页报告：{htm}\n"
            + (f"• 截图目录：{SHOTS_DIR}（{len(mapping)} 张，"
               f"已去掉重复发票的副本，与唯一发票一一对应）\n"
               if mapping else "")
            + (f"• 截图压缩包：查验截图.zip"
               + (f"、查验成功截图.zip（仅「一致」的 {n_ok} 张）" if zip_ok else "")
               + "\n" if zip_path else "")
            + extra)

    def export(self, which):
        if not self.results:
            messagebox.showinfo("提示", "还没有查验结果。")
            return
        if which == "excel":
            p = filedialog.asksaveasfilename(defaultextension=".xlsx",
                                             initialfile="查验结果.xlsx")
            if p:
                report.write_excel(self.results, p)
        else:
            p = filedialog.asksaveasfilename(defaultextension=".html",
                                             initialfile="查验结果.html")
            if p:
                report.write_html(self.results, p,
                                  shots_out_dir=SHOTS_DIR if SHOTS_DIR.exists()
                                  else None)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
