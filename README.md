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
├── app.py                 # 主程序（Streamlit 入口）
├── requirements.txt       # 依赖清单
├── .env.example           # 环境变量模板（复制成 .env 后填 Key）
├── .gitignore             # 排除 .env / __pycache__ / *.pdf 等
├── README.md              # 本文件
└── utils/                 # （阶段 5 代码重构时创建）
    ├── pdf_parser.py      # PDF 文本/图片/链接提取
    ├── translator.py      # 翻译接口封装
    └── metadata.py        # 元数据提取与回退策略
```

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

## 协作约定

- 一次只推进一个阶段，通过验收测试并提交后再进入下一阶段。
- 元数据提取遵循「准确性优先于自动化」，任何字段都可人工编辑覆盖。
- 密钥只写在 `.env` 中，`.env` 永不提交。
