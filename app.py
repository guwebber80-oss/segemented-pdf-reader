"""
科研文献 PDF 智能阅读器 —— 主程序
================================================
阶段 0：只做「最小可运行」版本
功能：页面标题 + PDF 上传按钮 + 上传后显示文件名和大小

后续阶段会在此基础上逐步增加：
  阶段 1：PDF 文本提取 + 卡片式分块
  阶段 2：中英文切换翻译
  阶段 3：图片提取与侧边栏展示
  阶段 4：文献元数据提取（标题/DOI/作者/通讯作者/附件链接）
  阶段 5：整合与交互优化、代码拆分为 utils/ 模块

运行命令：
    streamlit run app.py
"""

import streamlit as st  # Streamlit：把 Python 脚本变成网页应用的核心库

# ------------------------------------------------------------
# 1. 页面基础配置
# set_page_config 必须是第一个 Streamlit 调用，否则会报错
# ------------------------------------------------------------
st.set_page_config(
    page_title="科研文献 PDF 智能阅读器",  # 浏览器标签页标题
    page_icon="📚",                        # 标签页小图标
    layout="wide",                         # 宽屏布局，正文区更宽，适合放卡片
)

# ------------------------------------------------------------
# 2. 顶部标题与说明
# ------------------------------------------------------------
st.title("📚 科研文献 PDF 智能阅读器")
st.caption("阶段 0：项目初始化 —— 上传一个 PDF，确认环境跑通即可。")


# ------------------------------------------------------------
# 3. PDF 上传控件
# st.file_uploader 返回的是一个「类文件对象」(UploadedFile)，
# 它不落盘，内容保存在内存里，read() 可以读出原始字节。
# ------------------------------------------------------------
uploaded_file = st.file_uploader(
    label="请上传一篇 PDF 文献",
    type=["pdf"],              # 只允许选 .pdf 文件
    accept_multiple_files=False,  # 阶段 0 先只支持单文件
    help="支持单栏 / 双栏 / 带图表 / 扫描版 PDF，后续阶段逐步适配。",
)

# ------------------------------------------------------------
# 4. 上传后展示结果
# ------------------------------------------------------------
if uploaded_file is not None:
    # ---- 4.1 取出文件名和大小 ----
    file_name = uploaded_file.name                      # 例如 "attention_is_all_you_need.pdf"
    file_size_kb = uploaded_file.size / 1024            # 字节 → KB
    file_size_mb = file_size_kb / 1024                  # KB → MB

    # 大于 1MB 就用 MB 显示，读起来更直观
    if file_size_mb >= 1:
        size_text = f"{file_size_mb:.2f} MB"
    else:
        size_text = f"{file_size_kb:.1f} KB"

    # ---- 4.2 显示「已上传」提示 ----
    st.success(f"已上传：{file_name}")

    # ---- 4.3 用两列布局展示文件基础信息 ----
    col_left, col_right = st.columns(2)
    with col_left:
        st.metric(label="文件名", value=file_name)
    with col_right:
        st.metric(label="文件大小", value=size_text)

    # ---- 4.4 阶段 0 的占位说明：告诉用户下一步会发生什么 ----
    st.info(
        "✅ 阶段 0 验收通过：环境正常、上传链路正常。\n\n"
        "下一步（阶段 1）这里会显示：PDF 每一页按坐标排序后切成的**阅读卡片**。"
    )

    # ---- 4.5 可展开的调试区：方便你排查问题 ----
    with st.expander("🔍 调试信息（阶段 0 用来看上传的文件到底传进来没有）"):
        st.write("文件类型（MIME）：", uploaded_file.type)
        st.write("文件字节数：", uploaded_file.size)
        # 读取前 16 个字节，PDF 文件固定以 b'%PDF' 开头，可用来判断文件是否完整
        head = uploaded_file.read(16)
        st.write("文件头 16 字节：", head)
        st.write("是否为合法 PDF 开头：", head.startswith(b"%PDF"))
        # 读完后把指针拨回开头，避免影响后续阶段读取全文
        uploaded_file.seek(0)
else:
    # ---- 4.6 还没上传时的引导提示 ----
    st.info("👆 请在上方点击「Browse files」选择一篇 PDF 文献。")
