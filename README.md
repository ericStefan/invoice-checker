# 发票批量查验工具

对国家税务总局「全国增值税发票查验平台」做**批量自动查验**的桌面工具。

导入一批发票（PDF / PNG / JPG），自动完成：读取发票二维码 → 提取号码与金额 →
填入查验平台 → 自动识别并提交验证码 → 逐张截图留痕 → 导出 Excel 结果表与
网页报告。

> 平台地址：https://inv-veri.chinatax.gov.cn/fpcygzfw/national-invoice-check

---

## 快速开始（Windows）

```text
1. 双击 安装依赖.bat      ← 只需做一次，自动建虚拟环境装依赖
2. 双击 启动查验.bat      ← 打开图形界面
3. 界面里「添加发票文件 / 添加文件夹」→「开始查验」
```

首次安装需要联网。装完即可离线使用（浏览器内核在安装时已下载）。

---

## 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11（`.bat` 脚本与Tkinter 界面为 Windows 优先） |
| Python | 3.10 及以上（实测 3.12） |
| 内存 | 建议 4GB 以上（OCR 模型常驻内存） |
| 网络 | 仅安装依赖与实际查验时需要 |

---

## 依赖库（软件正常运行所必需）

全部列在 [`requirements.txt`](requirements.txt) 中，用它安装即可，无需逐个记：

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
```

| 库 | 版本 | 用途 |
|---|---|---|
| `playwright` | 1.63.0 | 驱动浏览器打开查验平台、填单、截图 |
| `pyzbar` | 0.1.9 | 解码发票二维码，取号码/代码/日期/金额 |
| `opencv-python` | 5.0.0.93 | 验证码二值化、字符切分 |
| `Pillow` | 12.3.0 | 图像格式转换与缩略图 |
| `ddddocr` | 1.6.1 | 验证码文字识别（主力） |
| `PyMuPDF` | 1.28.2 | 解析 PDF 版老发票（二维码 + 文本层） |
| `openpyxl` | 3.1.5 | 生成 / 读取 Excel 结果表 |
| `segno` | 1.6.6 | 生成样例发票，仅测试脚本需要 |

**两个必须注意的点：**

1. `playwright` 装完包还要单独下浏览器内核，否则启动时报找不到浏览器：
   ```bash
   .venv\Scripts\python -m playwright install chromium
   ```
2. `pyzbar` 依赖 zbar。Windows 下 pip 包内已内置 `libzbar-64.dll`，开箱可用；
   Linux 需自行 `sudo apt install libzbar0`，否则解码二维码会报 DLL 缺失。

其余（`tkinter`、`numpy`、`onnxruntime`）为标准库或由上述依赖自动带入。

---

## 目录结构

```text
invoice_checker/
├── main.py              图形界面主程序（入口，双击 .bat 就是跑它）
├── checker.py           查验内核：填单、提交、结果判定、风控处理
├── captcha.py           验证码识别：颜色解析 + 置信度评估
├── extract.py           发票解析：二维码/文本提取、重复发票审计
├── report.py            报告输出：Excel、HTML、截图落地与打包
│
├── tests/               离线自测（不消耗查验次数、不碰真实平台）
│   ├── selftest.py              依赖导入 + 二维码解析 + 报告生成
│   ├── test_report.py           报告与截图落地全链路
│   ├── test_dedupe_shots.py     重复发票截图去重
│   ├── test_rate.py             风控暂停与中断兜底
│   ├── test_limit.py            当日次数超限判定
│   ├── test_popup.py            平台提示浮层判定
│   ├── test_gui_notice.py       界面提示条与收尾
│   ├── test_dry.py              干跑（联网，不提交）
│   └── test_e2e.py              端到端（联网，会提交）
│
├── tools/               调试辅助（日常使用不需要）
│   ├── make_samples.py          生成样例发票
│   ├── probe.py                 探测页面选择器（平台改版时校准用）
│   ├── probe_pick.py            实测验证码识别命中率
│   └── diag_once.py             单次提交诊断
│
├── data/                ⚠️ 全部运行产物，已在 .gitignore 中
│   ├── 查验结果.xlsx           本轮结果表
│   ├── 查验结果.html           网页报告（自动弹出）
│   ├── 查验截图.zip            全部截图
│   ├── 查验成功截图.zip        仅「查验一致」的截图
│   ├── 查验截图/               每张唯一发票的结果截图
│   ├── shots/                  原始调试截图
│   ├── logs/                   未识别结果的页面文本快照
│   └── 历史输出/<时间戳>/       上一轮结果自动归档
│
├── 安装依赖.bat          一键装环境
├── 启动查验.bat          一键启动
├── requirements.txt      依赖清单
└── 使用说明.md           面向使用者的操作手册
```

`data/` 里的东西**随时可以整个删掉**，不影响程序运行，下次查验会重新生成。

---

## 使用说明

日常操作细节（验证码怎么填、各结果状态怎么处理、常见问题）见
[`使用说明.md`](使用说明.md)。几条最关键的：

| 用途 | 操作 |
|---|---|
| 日常批量查验 | 「添加文件夹」→「开始查验」 |
| 只查一两张 | 「添加发票文件」多选目标文件 |
| 免重复扫描 | 先「导出识别清单Excel」，下次「导入Excel清单（免扫描）」 |
| 改填错的号码 | 主界面**双击该行**修改 |
| 重跑未完成的 | 导入上一轮结果表，标「平台限流」「未执行」的行会自动重查 |

**验证码**：平台提示填哪种颜色就只填那种颜色；工具会自动挑「简单」的图提交，
复杂的直接刷新换一张（刷新不消耗查验次数），实在不行才弹窗请你人工输入。

---

## 开发者：跑测试

除 `test_dry` / `test_e2e` 需联网外，其余全部离线，**不消耗任何查验次数**：

```bash
.venv\Scripts\python tests\selftest.py
.venv\Scripts\python tests\test_report.py
.venv\Scripts\python tests\test_dedupe_shots.py
.venv\Scripts\python tests\test_rate.py
.venv\Scripts\python tests\test_limit.py
.venv\Scripts\python tests\test_popup.py
.venv\Scripts\python tests\test_gui_notice.py
```

测试产物一律写系统临时目录，不会污染项目。

### 平台改版了怎么办

查验平台的页面结构会变。定位方法：

```bash
.venv\Scripts\python tools\probe.py       # 打印页面元素结构，用于校准 checker.py 里的选择器
.venv\Scripts\python tools\diag_once.py   # 跑一次提交，落盘页面文本与截图
```

排查时看 `data/logs/` 里的页面文本快照，能直接看到平台返回了什么。

### 二次开发要点

| 想改什么 | 改哪里 |
|---|---|
| 验证码识别策略 | `captcha.py` 的 `solve()`、置信度阈值 `CONF_GOOD` / `CONF_OK` |
| 页面元素选择器 | `checker.py` 顶部的 `SEL` 字典 |
| 结果状态与配色 | `report.py` 的 `STATUS_FILL` / `STATUS_COLOR` |
| 界面交互 | `main.py` |
| 输出文件位置 | `main.py` 顶部的 `DATA_DIR` / `SHOTS_DIR` / `RAW_SHOTS` |

---

## 注意事项

- **查验次数有限**：平台限制同一张发票每天最多查 5 次。工具会自动检测清单里的
  重复发票并只提交一次，但把同一张票列很多遍仍会撞上限。
- **别高频重跑**：连续大批量查验会触发「您的访问过于频繁」。工具会自动暂停并
  重试，连续 3 次仍被挡就停批并保留已完成结果，等 10~30 分钟再用「导入Excel清单」
  只重跑「平台限流」「未执行」两类。
- **截图含敏感信息**：结果截图里含发票号码与金额，对外发送前请自行确认。
- 仅供内部对账使用，请遵守平台条款。

---

## 已知问题

- 极端场景（如低分辨率、严重反光、扭曲拍摄的发票）可能无法识别二维码，
  此时该行会标「跳过（未识别为发票）」，需双击手工补录字段。
- 验证码识别为概率性结果，个别复杂图形可能连续识别失败。

## 许可

仅供内部学习与工作使用。
