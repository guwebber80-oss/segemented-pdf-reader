r"""
科研文献 PDF 智能阅读器 —— 主程序（界面层）
================================================
阶段 2：卡片中英文一键切换

文件分工：
    app.py         ← 本文件，只管界面：上传、设置、渲染卡片、诊断面板、语言切换
    pdf_parser.py  ← 解析逻辑：文本块提取、分栏判断、阅读顺序、段落合并、标题识别
    translator.py  ← 翻译逻辑：DeepL / OpenAI / Google 三种后端，带超时与重试

运行命令（本项目约定：不激活虚拟环境，直接用 venv 的 python）：
    .\.venv\Scripts\python.exe -m streamlit run app.py

翻译需要 .env 里有 API Key（复制 .env.example 为 .env 后填写），见 README「阶段 2 说明」。
"""

import os

import streamlit as st

from utils.image_extractor import (
    ASSOC_MAX_GAP,
    associate_with_cards,
    extract_images,
)
from utils.metadata import extract_metadata
from utils.pdf_parser import (
    ROLE_AUTHOR,
    ROLE_COPYRIGHT,
    ROLE_FRONT_MATTER,
    ROLE_HEADER_FOOTER,
    ROLE_LABEL,
    ROLE_NAMES,
    ROLE_REFERENCE,
    count_words,
    escape_markdown,
    parse_pdf,
)
from utils.translator import (
    BACKEND_LABELS,
    TARGET_LABELS,
    TranslationError,
    backend_availability,
    load_config,
    probe_backend,
    translate_many,
)
from ui.cards import (
    LANG_ZH,
    card_label,
    render_card_content,
    render_card_translated,
    render_language_toggle,
    translate_cached,
)
from ui.diagnostics import render_background_panel, render_diagnostics
from ui.metadata_panel import render_grobid_panel, render_metadata_panel
from ui.media import (
    get_full_image,
    is_formula_item,
    is_table_item,
    paragraph_formula_payload,
    render_formula,
    render_table,
)

# ============================================================
# 第 0 部分：页面配置（必须是第一个 Streamlit 调用）
# ============================================================
st.set_page_config(
    page_title="科研文献 PDF 智能阅读器",
    page_icon="📚",
    layout="wide",
)

# ============================================================
# 第 1 部分：会话状态初始化
# ============================================================
# Streamlit 的机制是「任何交互都会把整个脚本从头重跑一遍」，
# 所以【跨交互要保留的东西】必须放进 st.session_state，否则每次点击都会丢。
#   translations     翻译缓存：{(后端, 目标语言, 原文): 译文}，命中就不再调 API（省钱、省时间）
#   translate_stats  计数：用来验证「重复点击不会重复请求」
if "translations" not in st.session_state:
    st.session_state["translations"] = {}
if "translate_stats" not in st.session_state:
    st.session_state["translate_stats"] = {"requests": 0, "hits": 0, "chars": 0, "errors": 0}

# 语言切换控件的两个选项（英文原文 / 中文译文）








st.title("📚 科研文献 PDF 智能阅读器")
st.caption(
    "卡片式阅读（正文按章节聚合成 100~300 词）· 中英一键切换 · 图片浏览 · "
    "元数据提取（标题 / DOI / 摘要）· 背景信息分离"
)

# ============================================================
# 第 2 部分：侧边栏
# ============================================================
with st.sidebar:
    # ---- 2.1 解析设置 ----
    st.header("⚙️ 解析设置")
    merge_on = st.toggle(
        "段落自动合并", value=True,
        help="把被 PDF 拆成多块的同一段落拼回去。若发现两个独立段落被错误粘在一起，关掉它。",
    )
    dehyphenate_on = st.toggle(
        "行尾连字符合并", value=True,
        help="英文排版会把单词在行尾断开（informa- / tion），开启后自动拼成 information。",
    )
    target_words = st.slider(
        "卡片目标词数", min_value=100, max_value=400, value=200, step=25,
        help="攒到这么多词就收一张卡片；实际落点区间是这个数的 0.5~1.5 倍"
             "（默认 200 → 100~300 词）。卡片按阅读顺序累积，遇到非正文会断开。",
    )
    show_all = st.toggle(
        "显示全部内容（不过滤非正文）", value=False,
        help="打开后，页眉页脚、作者单位、参考文献等也会照原样列出来，"
             "方便你核对过滤有没有误杀真正文。",
    )
    table_mode_label = st.radio(
        "表格呈现", ["截图（推荐）", "保留文字"], index=0,
        help="截图：整张表按原排版截成图片（列对齐不会丢），文字移出卡片流；"
             "保留文字：不做表格识别，表格文字照旧线性排在卡片里"
             "（4.4-B 之前的行为，用于对照）。切换会重新解析，约 1~3 秒。",
    )
    table_mode = "text" if table_mode_label == "保留文字" else "image"
    expand_n = st.slider("默认展开卡片数", min_value=0, max_value=10, value=3)

    st.divider()

    # ---- 2.2 翻译设置 ----
    st.header("🌐 翻译设置")
    config = load_config()
    availability = backend_availability(config)
    usable_backends = [name for name, ready in availability.items() if ready]

    if usable_backends:
        default_backend = config["backend"] if config["backend"] in usable_backends else usable_backends[0]
        backend = st.selectbox(
            "翻译后端", usable_backends,
            index=usable_backends.index(default_backend),
            format_func=lambda name: BACKEND_LABELS.get(name, name),
        )
        target = st.selectbox(
            "目标语言", list(TARGET_LABELS),
            format_func=lambda code: TARGET_LABELS[code],
        )

        col_test, col_reload = st.columns(2)
        if col_test.button("测试连接"):
            try:
                sample = probe_backend(backend, target)
                st.success(f"连接正常 ✅ 示例：{sample}")
            except TranslationError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"未预期的错误 {type(exc).__name__}: {exc}")
        if col_reload.button("重读 .env", help="改完 .env 后点这里，不用重启服务"):
            load_config(force_reload=True)
            st.rerun()
    else:
        backend, target = None, "zh"
        st.warning(
            "没有检测到任何翻译 API Key，中英切换不可用。\n\n"
            "请把项目根目录的 `.env.example` 复制成 `.env`，填入 `DEEPL_API_KEY`，"
            "然后点下面的「重读 .env」。"
        )
        if st.button("重读 .env"):
            load_config(force_reload=True)
            st.rerun()

    if st.button("清空翻译缓存"):
        st.session_state["translations"] = {}
        st.session_state["translate_stats"] = {"requests": 0, "hits": 0, "chars": 0, "errors": 0}
        st.rerun()

    st.caption("改完 .env 需要点「重读 .env」或重启服务才会生效。")

    # 计数占位符：侧边栏在页面最前面渲染，而翻译发生在后面的卡片里，
    # 所以先留一个空位，等所有卡片渲染完再把真实计数填进来（否则显示的永远是上一轮的数字）。
    stats_slot = st.empty()

# ============================================================
# 第 3 部分：上传
# ============================================================
uploaded_file = st.file_uploader(
    label="请上传一篇 PDF 文献",
    type=["pdf"],
    accept_multiple_files=False,
    help="支持有文字层的单栏 / 双栏 PDF。扫描版（纯图片）本阶段不支持。",
)

if uploaded_file is None:
    st.info(
        "👆 请在上方选择一篇 PDF。\n\n"
        "读起来之后，展开任意一张卡片，用里面的 **EN / 中文** 按钮切换语言。"
    )
    st.stop()      # 没有文件就停下，避免下面到处写 None 判断

pdf_bytes = uploaded_file.getvalue()

# ============================================================
# 第 4 部分：解析（带缓存）
# ============================================================
# 按「文件 + 设置」的指纹判断要不要重新解析，避免每次点击都重算一遍 PDF。
# table_mode 必须进指纹：切换表格呈现方式要重新解析才会生效。
file_key = (f"{uploaded_file.name}|{uploaded_file.size}|{merge_on}|{dehyphenate_on}"
            f"|{target_words}|{table_mode}")

if st.session_state.get("parse_key") != file_key:
    with st.spinner("正在解析 PDF…"):
        try:
            st.session_state["parse_result"] = parse_pdf(pdf_bytes, merge_on, dehyphenate_on,
                                                         target_words, table_mode)
            st.session_state["parse_key"] = file_key
            st.session_state["parse_error"] = None
        except Exception as exc:
            # 解析失败不能让页面白屏，要把错误原样显示出来方便排查
            st.session_state["parse_result"] = None
            st.session_state["parse_key"] = None
            st.session_state["parse_error"] = f"{type(exc).__name__}: {exc}"

if st.session_state.get("parse_error"):
    st.error("解析失败：" + st.session_state["parse_error"])
    st.caption("请把上面的完整报错、以及这个 PDF 的特征（单栏 / 双栏 / 扫描版）发给我，我来定位。")
    st.stop()

result = st.session_state["parse_result"]
cards = result["cards"]           # 阅读卡片：正文按章节聚合到 100~300 词
blocks = result["blocks"]         # 原子块：元数据、诊断、图片关联用
paragraphs = result.get("paragraphs", blocks)   # 派生段落：背景信息、「显示全部内容」用
clusters = result.get("formula_clusters", [])   # 公式区域（整簇渲染成一张图）
table_regions = result.get("table_regions", [])  # 表格区域（整张表渲染成一张图）
pages = result["pages"]

# ============================================================
# 第 4.5 部分：图片提取（带缓存）与文本卡片关联
# ============================================================
image_key = f"{uploaded_file.name}|{uploaded_file.size}"

if st.session_state.get("image_key") != image_key:
    with st.spinner("正在提取图片…"):
        try:
            image_data = extract_images(pdf_bytes)
            association = associate_with_cards(image_data["images"], cards)
            st.session_state["image_result"] = image_data
            st.session_state["image_assoc"] = association
            st.session_state["image_error"] = None
        except Exception as exc:
            # 图片提取失败不影响正文阅读，降级为空列表并把原因记下来
            st.session_state["image_result"] = {"images": [], "skipped": [], "elapsed": 0.0}
            st.session_state["image_assoc"] = {"关联可靠": 0, "关联存疑": 0, "未关联": 0,
                                               "存疑列表": [], "未关联列表": []}
            st.session_state["image_error"] = f"{type(exc).__name__}: {exc}"
    st.session_state["image_key"] = image_key

image_result = st.session_state["image_result"]
images = image_result["images"]
association = st.session_state["image_assoc"]

# 卡片 → 图片 的反向索引，用来在卡片标题上标出「这张卡片旁边有图」
images_by_card = {}
for img in images:
    if img.card_order:
        images_by_card.setdefault(img.card_order, []).append(img)

# ---- 侧边栏：图片缩略图（按页码分组）----
# 放在这里而不是脚本开头的侧边栏里，是因为此时才解析出图片清单。
# Streamlit 允许脚本里出现多个 with st.sidebar 块，它们在侧边栏里按出现顺序排列。
with st.sidebar:
    st.divider()
    st.header(f"🖼️ 文献图片（{len(images)} 张）")
    st.caption(f"提取耗时 {image_result['elapsed']} 秒 · 点击「放大」在主区域查看大图")

    if st.session_state.get("image_error"):
        st.error("图片提取失败：" + st.session_state["image_error"])

    if not images:
        if not st.session_state.get("image_error"):
            st.info(
                "这个 PDF 里没有提取到插画。\n\n"
                "注意：**用线条和文字画出来的矢量图**（比如流程图、伪代码框）不是图片对象，"
                "本阶段只提取真正的位图插图。"
            )
    else:
        grouped = {}
        for img in images:
            grouped.setdefault(img.page, []).append(img)

        for page_no in sorted(grouped):
            page_images = grouped[page_no]
            with st.expander(f"第 {page_no} 页 · {len(page_images)} 张",
                             expanded=(len(images) <= 4)):
                cols = st.columns(2)
                for index, img in enumerate(page_images):
                    with cols[index % 2]:
                        st.image(img.thumb, width="stretch")
                        assoc_text = f"→ 卡片 #{img.card_order}" if img.card_order else "未关联卡片"
                        st.caption(f"{img.pixel_w}×{img.pixel_h}px · {assoc_text}")
                        if st.button("🔍 放大", key=f"zoom_{img.key}", width="stretch"):
                            st.session_state["selected_image"] = img.key

# ============================================================
# 第 4.6 部分：文献元数据（标题 / DOI / 摘要 / 作者 / 单位 / 通讯 / 附件链接）
# ============================================================
meta_key = f"{uploaded_file.name}|{uploaded_file.size}"

if st.session_state.get("meta_key") != meta_key:
    with st.spinner("正在提取文献元数据（标题 / DOI / 摘要 / 作者 / 单位 / 附件链接）…"):
        try:
            st.session_state["metadata"] = extract_metadata(pdf_bytes, blocks)
            st.session_state["meta_error"] = None
        except Exception as exc:
            # 元数据提取失败不影响阅读，降级成空字段让用户手填
            from utils.metadata import Metadata
            st.session_state["metadata"] = Metadata()
            st.session_state["meta_error"] = f"{type(exc).__name__}: {exc}"
    # 换了文件：清掉上一个文件留下的编辑内容，让输入框重新取自动提取值
    for key in ("in_title", "in_doi", "in_abstract", "in_authors", "in_first_author",
                "in_corresponding", "in_corresponding_email", "in_affiliations",
                "in_supplementary", "in_author_emails"):
        st.session_state.pop(key, None)
    st.session_state["meta_key"] = meta_key

metadata = st.session_state["metadata"]

# 面板本身在 ui/metadata_panel.py（阶段 5 拆出），这里只负责准备数据与调用
render_metadata_panel(metadata)
# ============================================================
# 第 4.7 部分：GROBID 交叉校验（阶段 4.3）
# ============================================================
# 思路：本地规则（版面启发式）与 GROBID（CRF 模型）各抽一遍，逐字段对照。
# 两边一致 → 可信度大增；不一致 → 并排显示，由用户决定采信谁（一键写回上面的输入框）。
# GROBID 不提供通讯作者与附件链接，界面上如实标注，不显示成「不一致」误导用户。
render_grobid_panel(meta_key, metadata, pdf_bytes, uploaded_file)
st.divider()

# ============================================================
# 第 5 部分：解析概览
# ============================================================
st.subheader("📊 解析概览")

total_words = sum(count_words(c.text) for c in cards)
section_count = sum(1 for b in paragraphs if b.role == "heading")
background_blocks = [b for b in paragraphs
                     if b.role in (ROLE_AUTHOR, ROLE_FRONT_MATTER, ROLE_COPYRIGHT,
                                   ROLE_LABEL, ROLE_REFERENCE, ROLE_HEADER_FOOTER)]
layouts = " / ".join(f"第{p['页码']}页 {p['检测排版']}" for p in pages)

col1, col2, col3, col4 = st.columns(4)
col1.metric("页数", len(pages))
col2.metric("阅读卡片", len(cards))
col3.metric("正文词数", f"{total_words:,}")
col4.metric("解析耗时", f"{result['elapsed']} 秒")

st.caption(
    f"文件：{uploaded_file.name} · 正文字号基准 {result['body_size']} pt · "
    f"章节 {section_count} 个 · 已移出正文的段落 {len(background_blocks)} 段"
    f"（见下方「论文背景信息」）· 排版检测：{layouts}"
)

# 扫描版（没有文字层）要给出明确提示，不要让它静默变成空白页
if result["total_chars"] < 200:
    st.warning(
        f"⚠️ 这个 PDF 几乎提取不到文字（全文仅 {result['total_chars']} 个字符），"
        "很可能是**扫描版 / 图片版**。本阶段不做 OCR，请换一篇有文字层的 PDF 测试。"
    )

st.divider()

# ============================================================
# 第 5.5 部分：图片详情（点击侧边栏「放大」后在这里显示大图）
# ============================================================
selected_key = st.session_state.get("selected_image")
if selected_key:
    selected = next((img for img in images if img.key == selected_key), None)
    if selected is None:
        # 换了文件，之前选中的图片已经不存在了
        st.session_state.pop("selected_image", None)
    else:
        st.subheader(f"🖼️ 图片详情 · 第 {selected.page} 页")

        col_image, col_meta = st.columns([3, 1])
        with col_image:
            st.image(
                get_full_image(pdf_bytes, selected),
                caption=f"{selected.key} · 原始 {selected.pixel_w}×{selected.pixel_h} px",
                width="stretch",
            )
        with col_meta:
            st.metric("原始像素", f"{selected.pixel_w}×{selected.pixel_h}")
            st.caption(
                f"页面上显示尺寸：{selected.disp_w:.0f} × {selected.disp_h:.0f} pt\n\n"
                f"位置：({selected.bbox[0]:.0f}, {selected.bbox[1]:.0f}) "
                f"→ ({selected.bbox[2]:.0f}, {selected.bbox[3]:.0f})"
            )
            if selected.card_order:
                st.success(f"关联到卡片 #{selected.card_order}（距离 {selected.gap} pt）")
            else:
                st.warning("没有关联到文本卡片")
            if st.button("关闭大图"):
                st.session_state.pop("selected_image", None)
                st.rerun()

        st.divider()

# ============================================================
# 第 6 部分：卡片流（含中英切换）
# ============================================================
st.subheader("📖 阅读卡片")
st.caption(
    f"正文已按阅读顺序聚合成约 {round(target_words * 0.5)}~{round(target_words * 1.5)} 词一张的卡片，"
    "不跨「非正文」区域；卡片里的**加粗行**是原文的章节标题，"
    "**公式与表格都以原文截图呈现**（排版不会失真，代价是不可选中、不可翻译）。"
    "展开卡片后可用 **EN / 中文** 切换语言——切到中文时正文按段落翻译，"
    "**公式与表格仍以原图呈现**。表格也可以用侧边栏「表格呈现 → 保留文字」切回线性文字。"
)





















expanded_used = 0        # 已展开的卡片数，用来实现「默认只展开前 N 张」
translated_cards = 0     # 本次渲染里显示为中文的卡片数（统计用）

if show_all:
    # 不过滤：把派生段落按阅读顺序全列出来（含页眉页脚、作者单位…），便于核对有没有误杀
    display_items = [(f"#{b.order} · 第{b.page}页 · {ROLE_NAMES.get(b.role, b.role)} · "
                      f"{count_words(b.text)} 词"
                      + (f" · 公式簇{b.cluster_id}" if b.cluster_id >= 0 else "")
                      + (f" · 表格{b.table_id}" if getattr(b, "table_id", -1) >= 0 else ""), b)
                     for b in paragraphs]
else:
    display_items = [(card_label(card), card) for card in cards]

for label, item in display_items:
    # 这张卡片附近有几张图（用于在标签上标出来，方便对照阅读）
    image_mark = f" · 🖼️×{len(images_by_card[item.order])}" if item.order in images_by_card else ""

    with st.expander(label + image_mark, expanded=(expanded_used < expand_n)):
        card_lang = render_language_toggle(f"lang_{item.order}")

        if card_lang == LANG_ZH:
            if is_table_item(item) and not item.segments:
                # 表格区域：整张表原文截图，**不参与翻译**（与公式同策略）
                render_table(paragraph_formula_payload(item), pdf_bytes)
                translated_cards += 1
                expanded_used += 1
                continue

            if is_formula_item(item) and not item.segments:
                # 公式区域：整簇原文截图，**不参与翻译**（用户要求公式保持原样）
                render_formula(paragraph_formula_payload(item), pdf_bytes)
                st.caption("公式区域：保留原文截图，不参与翻译。")
                translated_cards += 1
                expanded_used += 1
                continue

            status, error = render_card_translated(item, pdf_bytes, backend, target)

            if status == "ok":
                translated_cards += 1
                st.caption("中文视图：正文与标题已翻译，**公式保持原文截图**（不参与翻译）。")
            elif status == "no_segments":
                # 「显示全部内容」模式的段落没有内部结构 → 整段翻译
                zh, batch_error = translate_cached(item.text, backend, target)
                if batch_error:
                    st.error(f"翻译失败：{batch_error}")
                    st.caption("下面仍然显示英文原文；问题解决后重新点「中文」即可。")
                    render_card_content(item, pdf_bytes)
                else:
                    translated_cards += 1
                    st.markdown(escape_markdown(zh))
                    st.caption("中文视图显示整段译文，原文的章节标题加粗与公式图片不会保留。")
            else:
                st.error(f"翻译失败：{error}")
                st.caption("下面仍然显示英文原文；问题解决后重新点「中文」即可。")
                render_card_content(item, pdf_bytes)
        else:
            render_card_content(item, pdf_bytes)

    expanded_used += 1

st.divider()

# ============================================================
# 第 6.5 部分：论文背景信息（已从阅读卡片里移出的内容）
# ============================================================
# 面板在 ui/diagnostics.py；这里只负责筛出「非正文」段落并调用
render_background_panel(background_blocks)
st.divider()

# ============================================================
# 第 7 部分：解析诊断面板（验证阅读顺序、定位双栏错位、核对公式/表格区域）
# ============================================================
render_diagnostics(ASSOC_MAX_GAP, association, blocks, clusters, image_result, images,
                   metadata, pages, paragraphs, result, table_regions)

# ============================================================
# 第 8 部分：卡片全部渲染完之后，把翻译计数填进侧边栏的占位符
# ============================================================
stats = st.session_state["translate_stats"]
stats_slot.caption(
    f"**翻译计数**（用来验证不重复请求）\n\n"
    f"API 请求 **{stats['requests']}** 次 · 缓存命中 **{stats['hits']}** 次 · "
    f"失败 {stats['errors']} 次\n\n"
    f"累计翻译 {stats['chars']:,} 字符"
    + (f" · 当前 {translated_cards} 张卡片显示为中文" if translated_cards else "")
)
