r"""
科研文献 PDF 智能阅读器 —— 主程序（界面层）
================================================
阶段 1：PDF 文本提取与卡片式分块

文件分工：
    app.py         ← 本文件，只管界面：上传、设置、渲染卡片、诊断面板
    pdf_parser.py  ← 解析逻辑：文本块提取、分栏判断、阅读顺序、段落合并、标题识别

为什么拆成两个文件：解析逻辑不依赖 Streamlit，可以脱离网页单独测试和调试；
以后要调算法时不用碰界面代码。（阶段 5 会把它搬进 utils/ 目录。）

运行命令（本项目约定：不激活虚拟环境，直接用 venv 的 python）：
    .\.venv\Scripts\python.exe -m streamlit run app.py

测试样本要求：1 篇单栏 + 1 篇双栏英文文献，必须有文字层（非扫描版）。
"""

import streamlit as st

from pdf_parser import count_words, escape_markdown, parse_pdf

# ============================================================
# 第 0 部分：页面配置（必须是第一个 Streamlit 调用）
# ============================================================
st.set_page_config(
    page_title="科研文献 PDF 智能阅读器",
    page_icon="📚",
    layout="wide",
)

st.title("📚 科研文献 PDF 智能阅读器")
st.caption("阶段 1：把 PDF 变成按阅读顺序排列的卡片流（默认展开前 3 张正文卡片）")

# ============================================================
# 第 1 部分：侧边栏 —— 解析设置
# ============================================================
with st.sidebar:
    st.header("⚙️ 解析设置")
    merge_on = st.toggle(
        "段落自动合并", value=True,
        help="把被 PDF 拆成多块的同一段落拼回去。若发现两个独立段落被错误粘在一起，关掉它。",
    )
    dehyphenate_on = st.toggle(
        "行尾连字符合并", value=True,
        help="英文排版会把单词在行尾断开（informa- / tion），开启后自动拼成 information。",
    )
    max_words = st.slider(
        "单张卡片最长词数", min_value=80, max_value=400, value=200, step=20,
        help="只有超过 300 词的超长段落才会被切开；切开后每张卡片控制在这么多词左右。",
    )
    expand_n = st.slider("默认展开卡片数", min_value=0, max_value=10, value=3)

# ============================================================
# 第 2 部分：上传
# ============================================================
uploaded_file = st.file_uploader(
    label="请上传一篇 PDF 文献",
    type=["pdf"],
    accept_multiple_files=False,
    help="阶段 1 支持有文字层的单栏 / 双栏 PDF。扫描版（纯图片）本阶段不支持。",
)

if uploaded_file is None:
    st.info("👆 请在上方选择一篇 PDF。建议先用一篇**双栏**英文文献测试阅读顺序。")
    st.stop()      # 没有文件就停下，避免下面到处写 None 判断

pdf_bytes = uploaded_file.getvalue()

# ============================================================
# 第 3 部分：解析（带缓存）
# ============================================================
# 为什么要缓存：Streamlit 的机制是「任何交互都会把整个脚本从头重跑一遍」——
# 你每点一次展开/收起、每动一次滑块，脚本都会重新执行。
# 不缓存的话，每次点击都要重新解析一遍 PDF，大文件会明显卡顿。
# 这里用 session_state 按「文件 + 设置」的指纹来判断要不要重新解析。
file_key = f"{uploaded_file.name}|{uploaded_file.size}|{merge_on}|{dehyphenate_on}|{max_words}"

if st.session_state.get("parse_key") != file_key:
    with st.spinner("正在解析 PDF…"):
        try:
            st.session_state["parse_result"] = parse_pdf(pdf_bytes, merge_on, dehyphenate_on, max_words)
            st.session_state["parse_key"] = file_key
            st.session_state["parse_error"] = None
        except Exception as exc:
            # 解析失败不能让页面白屏或抛栈，要把错误原样显示出来方便排查
            st.session_state["parse_result"] = None
            st.session_state["parse_key"] = None
            st.session_state["parse_error"] = f"{type(exc).__name__}: {exc}"

if st.session_state.get("parse_error"):
    st.error("解析失败：" + st.session_state["parse_error"])
    st.caption("请把上面的完整报错、以及这个 PDF 的特征（单栏 / 双栏 / 扫描版）发给我，我来定位。")
    st.stop()

result = st.session_state["parse_result"]
blocks = result["blocks"]
pages = result["pages"]

# ============================================================
# 第 4 部分：解析概览
# ============================================================
st.subheader("📊 解析概览")

total_words = sum(count_words(b.text) for b in blocks)
heading_count = sum(1 for b in blocks if b.kind == "heading")
layouts = " / ".join(f"第{p['页码']}页 {p['检测排版']}" for p in pages)

col1, col2, col3, col4 = st.columns(4)
col1.metric("页数", len(pages))
col2.metric("卡片数", len(blocks))
col3.metric("总词数", f"{total_words:,}")
col4.metric("解析耗时", f"{result['elapsed']} 秒")

st.caption(
    f"文件：{uploaded_file.name} · 正文字号基准 {result['body_size']} pt · "
    f"其中标题 {heading_count} 张 · 排版检测：{layouts}"
)

# 扫描版（没有文字层）要给出明确提示，不要让它静默变成空白页
if result["total_chars"] < 200:
    st.warning(
        f"⚠️ 这个 PDF 几乎提取不到文字（全文仅 {result['total_chars']} 个字符），"
        "很可能是**扫描版 / 图片版**。阶段 1 不做 OCR，请换一篇有文字层的 PDF 测试。"
    )

st.divider()

# ============================================================
# 第 5 部分：卡片流
# ============================================================
st.subheader("📖 阅读卡片")

_COLUMN_NAME = {-1: "通栏", 0: "左栏", 1: "右栏"}
expanded_used = 0        # 已展开的正文卡片数，用来实现「默认只展开前 N 张」

for b in blocks:
    if b.kind == "heading":
        # 标题单独成卡片：用 Markdown 标题级别对应它的层级
        level = min(max(b.level, 1), 4)
        st.markdown(f"{'#' * (level + 1)} {escape_markdown(b.text)}")
        st.caption(
            f"#{b.order} · 第{b.page}页 · 标题 L{b.level} · "
            f"{count_words(b.text)} 词 · {b.max_size:.1f}pt"
        )
    else:
        label = (
            f"#{b.order} · 第{b.page}页 · {_COLUMN_NAME[b.column]} · "
            f"{count_words(b.text)} 词 · {len(b.text)} 字符"
        )
        with st.expander(label, expanded=(expanded_used < expand_n)):
            st.markdown(escape_markdown(b.text))
        expanded_used += 1

st.divider()

# ============================================================
# 第 6 部分：诊断面板（验证阅读顺序、定位双栏错位）
# ============================================================
with st.expander("🔍 解析诊断（验证阅读顺序、定位双栏错位）"):
    st.markdown("**① 每页排版检测**")
    st.dataframe(pages, hide_index=True)

    st.markdown("**② 文本块明细**（已按程序认定的阅读顺序编号，从上往下就是卡片顺序）")
    rows = [{
        "序号": b.order,
        "页码": b.page,
        "类型": "标题" if b.kind == "heading" else "正文",
        "栏": _COLUMN_NAME[b.column],
        "y0": round(b.y0, 1),
        "x0": round(b.x0, 1),
        "字号": round(b.max_size, 1),
        "粗体占比": round(b.bold_ratio, 2),
        "词数": count_words(b.text),
        "开头 40 字": b.text[:40].replace("\n", " "),
    } for b in blocks]
    st.dataframe(rows, hide_index=True, height=420)

    # 双栏页里，正常只有标题/摘要/跨栏图注会横跨中缝。
    # 如果这里出现大段正文，说明分栏判断可能出错，需要人工核对。
    spanning = [b for b in blocks if b.column == -1 and b.kind == "body" and count_words(b.text) > 120]
    if spanning:
        st.markdown("**③ 疑似问题：跨栏的长正文块**（双栏页里出现这些，说明分栏可能有误）")
        for b in spanning:
            st.write(f"#{b.order} 第{b.page}页 · {count_words(b.text)} 词 · {b.text[:60]}…")
    else:
        st.success("③ 未发现异常：没有「跨栏的长正文块」。")

    st.caption(
        "自查方法：看 ① 表的「检测排版」是否与你的 PDF 相符，"
        "再对照片子里的正文，看 ② 表的顺序是不是「先把左栏读完，再读右栏」。"
    )
