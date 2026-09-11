# 科研文献 PDF 智能阅读器

帮助科研人员降低英文文献阅读认知负荷的 Web 应用：上传 PDF → 自动提取文本与图片 → 切成可渐进展开的阅读卡片 → 一键中英切换 → 自动抽取文献元数据（标题 / DOI / 作者 / 通讯作者 / 单位 / 补充材料链接），且**所有元数据字段都允许人工修正**。

## 技术栈

| 层 | 选型 |
| --- | --- |
| 前后端 | Python + Streamlit |
| PDF 解析 | PyMuPDF (fitz)，复杂排版用 GROBID 兜底 |
| 翻译 | DeepL 优先，OpenAI / Google 备选（可配置） |
| 元数据 | GROBID（本地 Docker）+ 正则回退 |
| 依赖管理 | requirements.txt |
| 密钥管理 | .env + python-dotenv |

## 项目结构

```
segemented-pdf-reader/
├── app.py                 # 主程序（Streamlit 界面层）
├── pdf_parser.py          # PDF 解析逻辑（不依赖 Streamlit，可单独测试）
├── requirements.txt       # 依赖清单
├── .env.example           # 环境变量模板（复制成 .env 后填 Key）
├── .gitignore             # 排除 .env / .venv / __pycache__ / *.pdf 等
├── README.md              # 本文件
└── utils/                 # （阶段 5 代码重构时创建）
    ├── pdf_parser.py      # 由根目录的 pdf_parser.py 搬入
    ├── translator.py      # 翻译接口封装
    └── metadata.py        # 元数据提取与回退策略
```

**为什么解析逻辑单独一个文件**：它不依赖 Streamlit，可以脱离网页直接跑、直接调试；
调算法时不用碰界面代码。阶段 5 会把它搬进 `utils/`。

## 环境准备

需要 **Python 3.10+**（本项目已在 Python 3.14 上验证通过）。

```powershell
# 1. 创建虚拟环境（只需一次）
python -m venv .venv

# 2. 安装依赖（无需激活虚拟环境，直接用 venv 里的 python 调用 pip）
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 运行

```powershell
# 推荐：不激活虚拟环境，直接用 venv 里的 python 运行（不受脚本执行策略影响）
.\.venv\Scripts\python.exe -m streamlit run app.py
```

浏览器会自动打开 http://localhost:8501 。停止服务按 `Ctrl + C`。

如果你想用「先激活、再输 streamlit」的写法，见下面 FAQ 第 1 条。

## 常见问题（FAQ）

### 1. PowerShell 报「因为在此系统上禁止运行脚本」，无法加载 `Activate.ps1`

**原因**：Windows 默认执行策略是 `Restricted`（禁止运行任何 `.ps1` 脚本），与本项目无关，也不是装错了东西。
用 `Get-ExecutionPolicy` 可确认；本项目机器的所有作用域都是 `Undefined`，即默认的 `Restricted`。

四种解法任选其一：

| 方案 | 命令 | 说明 |
| --- | --- | --- |
| **A. 不激活（推荐）** | `.\.venv\Scripts\python.exe -m streamlit run app.py` | 一个系统设置都不改 |
| B. 临时放行 | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` 后再 `.\.venv\Scripts\Activate.ps1` | 只对当前这个终端窗口生效，关掉即恢复原样 |
| C. 永久放行当前用户 | `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` | 微软推荐的开发者设置；本地脚本可直接运行，从网上下载的脚本仍需数字签名；**不需要管理员权限** |
| D. 改用 cmd | 输入 `cmd` 回车 → `.venv\Scripts\activate.bat` | cmd 没有执行策略限制 |

### 2. 8501 端口被占用

换一个端口启动：

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py --server.port 8502
```

### 3. 项目统一约定

本项目所有命令统一写成 `.\.venv\Scripts\python.exe -m <模块>` 的形式，**不依赖虚拟环境激活**，从根上避开执行策略问题。

## 开发路线图

- [x] **阶段 0** 环境准备与项目初始化（上传页面跑通）
- [ ] **阶段 1** PDF 文本提取与卡片式分块
- [ ] **阶段 2** 接入翻译 API，卡片中英切换
- [ ] **阶段 3** 图片提取与侧边栏展示
- [ ] **阶段 4.1** 标题、DOI、摘要提取
- [ ] **阶段 4.2** 作者、通讯作者、单位、附件链接提取
- [ ] **阶段 4.3** 接入 GROBID 提升准确率
- [ ] **阶段 5** 整合全部功能、优化交互与代码结构
- [ ] **阶段 6** 部署上线（Streamlit Cloud）

## 阶段 1 说明：卡片是怎么切出来的

解析流程（都在 `pdf_parser.py`）：

1. **取文本块** —— 逐页用 `page.get_text("dict")` 取块，块基本对应自然段。用 `dict` 而不是
   `blocks`，是因为需要字号和加粗信息来判断标题。
2. **拆出块首标题** —— 有些排版把小标题和它下面的正文归进同一个块，会把标题行单独拆出来。
3. **判断单栏 / 双栏** —— 在页面宽度 32%~68% 范围滑动一条候选「中缝」，
   左右各有 ≥3 个块且几乎没有块横跨它，就认定为双栏。
4. **重排阅读顺序** —— 双栏时以「通栏块」（大标题、摘要、跨栏图注）为分段点：
   通栏块之下先读完左栏，再读右栏。
5. **合并段落** —— 把被 PDF 拆成多块的同一段拼回去；保守策略，宁可少合并也不错合并。
6. **切卡片** —— 超过 300 词的超长段落在句子边界切开（连字符断词会自动拼回）。
7. **识别标题** —— 综合字号（≥1.45 倍 → 一级）、加粗、章节名、编号四种证据。

**侧边栏开关**：段落自动合并、行尾连字符合并、单张卡片最长词数、默认展开卡片数。
改动任意一项都会自动重新解析（结果按「文件 + 设置」缓存，重复点击不会重算）。

**自查方法**：展开页面底部的「🔍 解析诊断」，对照 ① 每页排版检测、② 文本块明细
（已按程序认定的阅读顺序编号）和原 PDF，看顺序是不是「先把左栏读完，再读右栏」。

### 已知限制（阶段 1）

| 现象 | 说明 | 计划 |
| --- | --- | --- |
| 图内文字标签偶尔被当成小标题 | 例如散点图里的 "Cursor C"。矢量图内的文字目前无法与正文区分 | 阶段 3 提取图片区域后一并处理 |
| 与正文同行的「行内小标题」不会单独成卡片 | 例如 "3.1.1 Task 1: Random Saccades. This task explored…" 标题与正文在同一行 | 暂不处理，保持行内阅读更自然 |
| 跨页的同一段落会分成两张卡片 | 分页处本身就存在语义断点 | 阶段 5 可考虑合并 |
| 整页大图的页面会显示「单栏」 | 页面上只有一侧有文字（另一侧是图），但阅读顺序不受影响 | 诊断标签措辞优化 |
| 扫描版 PDF 提不出文字 | 没有文字层，需要 OCR | 不在本项目范围内，会给出明确提示 |

## 协作约定

- 一次只推进一个阶段，通过验收测试并提交后再进入下一阶段。
- 元数据提取遵循「准确性优先于自动化」，任何字段都可人工编辑覆盖。
- 密钥只写在 `.env` 中，`.env` 永不提交。
