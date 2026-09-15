"""
科研文献 PDF 智能阅读器 —— 界面层（唯一入口）

界面结构（阶段 5.3 按用户拍板的三栏布局重做，对应 `交互demo.html` 里验证过的设计）：

    ┌──────────┬─────────────────────────────┬──────────────┐
    │ 左栏      │ 中栏                         │ 右栏          │
    │ 功能控件  │ 阅读主体（PPT 式单卡翻页）      │ 背景信息      │
    ├──────────┼─────────────────────────────┼──────────────┤
    │ 上传文献  │ 卡片标题（§章节 · 原文页 · 词数）│ 文献元数据    │
    │ 语言开关  │ ‹  [ 卡片内容，居中 ]  ›       │ GROBID 校验   │
    │ 解析设置  │ 卡片后跟着「该卡片对应的插图」    │ 概览          │
    │ 处理状态  │ 进度条 + 第 i / N 张           │ 文档结构      │
    │ 章节导航  │ （「显示全部内容」模式在此展开）  │ 背景信息/诊断  │
    │ 翻译计数  │                              │              │
    └──────────┴─────────────────────────────┴──────────────┘

约定：
  · 卡片内容**居中**、文字内部仍左对齐（英文正文居中排版会串行）；
  · 插图与卡片里的公式/表格截图都可以**点击放大**（Streamlit 原生弹层）；
  · 导入后默认中文视图（离线可用：译文命中缓存就不再请求 API）；
  · 解析、图片提取、元数据提取的逻辑与缓存指纹**保持原样**（回归比对守着）。

界面的渲染函数在 `ui/`，解析与元数据的逻辑在 `utils/`，本文件只做流程编排。
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
)
from ui.cards import (
    LANG_EN,
    LANG_ZH,
    card_label,
    render_card_content,
    render_card_translated,
    translate_cached,
)
from ui.diagnostics import render_background_panel, render_diagnostics
from ui.metadata_panel import render_grobid_panel, render_metadata_panel
from ui.media import (
    is_formula_item,
    is_table_item,
    paragraph_formula_payload,
    render_figure,
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
#   translations     翻译缓存：{(后端, 目标语言, 原文): 译文}，命中就不再调 API
#   translate_stats  计数：用来验证「重复点击不会重复请求」
#   card_index       当前读到第几张卡（PPT 式翻页）
if "translations" not in st.session_state:
    st.session_state["translations"] = {}
if "translate_stats" not in st.session_state:
    st.session_state["translate_stats"] = {"requests": 0, "hits": 0, "chars": 0, "errors": 0}
if "card_index" not in st.session_state:
    st.session_state["card_index"] = 0

# ============================================================
# 第 2 部分：三栏骨架（左：功能控件 / 中：阅读卡片 / 右：背景信息）
# ============================================================
col_left, col_mid, col_right = st.columns([0.85, 2.7, 1.05], gap="medium")

# ------------------------------------------------------------
# 2.1 左栏：上传（唯一的文件入口，不放欢迎页）
# ------------------------------------------------------------
with col_left:
    st.markdown("### 📚 阅读器")
    uploaded_file = st.file_uploader(
        label="上传文献", type=["pdf"], accept_multiple_files=False,
        key="left_uploader",
        help="支持有文字层的单栏 / 双栏 PDF；换文件会自动重新解析。",
    )

# 还没选文件：中栏显示引导语，两侧留空但不渲染其它控件
if uploaded_file is None:
    with col_mid:
        st.title("📚 科研文献 PDF 智能阅读器")
        st.caption(
            "卡片式阅读（正文按章节聚合成 100~300 词）· 中英一键切换 · 插图跟随卡片 · "
            "公式与表格原样截图 · 元数据可人工修正"
        )
        st.info(
            "👈 先在左栏选一篇 PDF。上传后：**中间是阅读卡片**（用卡片左右两侧的 ‹ › 翻页，"
            "插图跟在对应卡片后面，点图可放大）· **右栏是文献背景信息**（元数据 / 概览 / 结构）。"
        )
    st.stop()      # 没有文件就停下，避免下面到处写 None 判断

pdf_bytes = uploaded_file.getvalue()

# ------------------------------------------------------------
# 2.2 左栏：语言开关与各项设置（解析设置会进解析指纹，所以必须在解析之前）
# ------------------------------------------------------------
with col_left:
    lang = st.radio(
        "语言", [LANG_ZH, LANG_EN], horizontal=True, key="ui_lang",
        help="导入后默认中文视图；切到 EN 看原文。公式、表格与插图始终保留原文截图。",
    )

    with st.expander("⚙️ 解析设置", expanded=False):
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
            help="攒到这么多词就收一张卡片；实际落点区间是这个数的 0.5~1.5 倍。",
        )
        table_mode_label = st.radio(
            "表格呈现", ["截图（推荐）", "保留文字"], index=0,
            help="截图：整张表按原排版截成图片（列对齐不会丢）；保留文字：不做表格识别，"
                 "表格文字照旧线性排在卡片里（4.4-B 之前的行为，用于对照）。",
        )
        show_all = st.toggle(
            "显示全部内容（不过滤非正文）", value=False,
            help="打开后，页眉页脚、作者单位、参考文献等也会照原样列出来，"
                 "方便你核对过滤有没有误杀真正文。该模式保持原文（不做翻译）。",
        )
    table_mode = "text" if table_mode_label == "保留文字" else "image"

    with st.expander("🌐 翻译设置", expanded=False):
        config = load_config()
        availability = backend_availability(config)
        usable_backends = [name for name, ready in availability.items() if ready]
        if usable_backends:
            default_backend = (config["backend"] if config["backend"] in usable_backends
                               else usable_backends[0])
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

    st.divider()
    st.markdown("**处理状态**")
    status_slot = st.container()          # 解析完成后往这里填状态
    meta_status_slot = st.empty()

# ============================================================
# 第 4 部分：解析（带缓存）
# ============================================================
# 按「文件 + 设置」的指纹判断要不要重新解析，避免每次点击都重算一遍 PDF。
# table_mode 必须进指纹：切换表格呈现方式要重新解析才会生效。
file_key = (f"{uploaded_file.name}|{uploaded_file.size}|{merge_on}|{dehyphenate_on}"
            f"|{target_words}|{table_mode}")

if st.session_state.get("parse_key") != file_key:
    with col_left:
        with st.spinner("正在解析 PDF…"):
            try:
                st.session_state["parse_result"] = parse_pdf(pdf_bytes, merge_on, dehyphenate_on,
                                                             target_words, table_mode)
                st.session_state["parse_key"] = file_key
                st.session_state["parse_error"] = None
                st.session_state["card_index"] = 0        # 换文件/换设置 → 回到第一张
            except Exception as exc:
                # 解析失败不能让页面白屏，要把错误原样显示出来方便排查
                st.session_state["parse_result"] = None
                st.session_state["parse_key"] = None
                st.session_state["parse_error"] = f"{type(exc).__name__}: {exc}"

if st.session_state.get("parse_error"):
    with col_left:
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
# 第 5 部分：图片提取（带缓存）与文本卡片关联
# ============================================================
image_key = f"{uploaded_file.name}|{uploaded_file.size}"

if st.session_state.get("image_key") != image_key:
    with col_left:
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

# 卡片 → 图片 的反向索引：插图要跟在「它对应的那张卡片」后面
images_by_card = {}
for img in images:
    if img.card_order:
        images_by_card.setdefault(img.card_order, []).append(img)

# ============================================================
# 第 6 部分：文献元数据（标题 / DOI / 摘要 / 作者 / 单位 / 通讯 / 附件链接）
# ============================================================
meta_key = f"{uploaded_file.name}|{uploaded_file.size}"

if st.session_state.get("meta_key") != meta_key:
    with col_left:
        with st.spinner("正在提取文献元数据…"):
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

# ============================================================
# 第 7 部分：左栏补充 —— 处理状态、章节导航、翻译计数
# ============================================================
total_words = sum(count_words(c.text) for c in cards)
section_count = sum(1 for b in paragraphs if b.role == "heading")
background_blocks = [b for b in paragraphs
                     if b.role in (ROLE_AUTHOR, ROLE_FRONT_MATTER, ROLE_COPYRIGHT,
                                   ROLE_LABEL, ROLE_REFERENCE, ROLE_HEADER_FOOTER)]
layouts = " / ".join(f"第{p['页码']}页 {p['检测排版']}" for p in pages)

with col_left:
    with status_slot:
        st.caption(
            f"✅ 文本 {len(pages)} 页 / {len(cards)} 张卡 · "
            f"🖼️ 图片 {len(images)} 张 · {'✅' if metadata.title.value else '⚠️'} 元数据 · "
            f"{'✅' if backend else '⚠️'} 翻译后端"
        )
        if st.session_state.get("image_error"):
            st.warning("图片提取失败：" + st.session_state["image_error"])

    st.divider()
    st.markdown("**章节导航**")
    if cards:
        sections = []
        for index, card in enumerate(cards):
            label = f"{card.section_number} {card.section_title}".strip() or "（无章节）"
            if not sections or sections[-1][0] != label:
                sections.append((label, index))
        choice = st.selectbox("跳转到章节", [label for label, _ in sections],
                              key="nav_section", label_visibility="collapsed")
        if st.button("跳到这一章", width="stretch"):
            index = next(i for label, i in sections if label == choice)
            st.session_state["card_index"] = index
            st.rerun()
        st.caption(f"共 {len(sections)} 个章节 · 当前第 {st.session_state['card_index'] + 1} / {len(cards)} 张")

    st.divider()
    st.markdown("**翻译计数**")
    # 计数占位符：左栏在页面最前面渲染，而翻译发生在后面的卡片里，
    # 所以先留空位，等卡片渲染完再把真实计数填进来。
    stats_slot = st.empty()

# ============================================================
# 第 8 部分：中栏 —— 阅读主体（PPT 式单卡翻页）
# ============================================================
total_cards = len(cards)
index = int(st.session_state.get("card_index", 0))
index = min(max(index, 0), total_cards - 1) if total_cards else 0
translated_cards = 0

with col_mid:
    st.caption(f"**{uploaded_file.name}** · {layouts}")
    st.markdown(f"#### {metadata.title.value or '（未识别到标题）'}")

    if total_cards == 0:
        st.warning("这份 PDF 没有生成任何阅读卡片（可能是扫描版，或正文极少）。")
    elif show_all:
        st.info("「显示全部内容」模式：按阅读顺序列出**全部**段落（含页眉页脚、作者单位、"
                "参考文献），用于核对过滤有没有误杀；该模式保持原文、不做翻译。")
        for item in paragraphs:
            with st.expander(f"#{item.order} · 第{item.page}页 · "
                             f"{ROLE_NAMES.get(item.role, item.role)} · {count_words(item.text)} 词"
                             + (f" · 公式簇{item.cluster_id}" if item.cluster_id >= 0 else "")
                             + (f" · 表格{item.table_id}" if getattr(item, "table_id", -1) >= 0 else "")):
                render_card_content(item, pdf_bytes)
    else:
        card = cards[index]

        # ---- 翻页按钮在卡片左右两侧，与卡片垂直居中对齐（PPT 的感觉）----
        prev_col, body_col, next_col = st.columns([0.55, 9, 0.55], vertical_alignment="center")

        with prev_col:
            if st.button("‹", key="prev_card", help="上一张（第 1 张时跳到末尾）",
                         width="stretch"):
                st.session_state["card_index"] = (index - 1) % total_cards
                st.rerun()
        with next_col:
            if st.button("›", key="next_card", help="下一张（最后一张时回到开头）",
                         width="stretch"):
                st.session_state["card_index"] = (index + 1) % total_cards
                st.rerun()

        with body_col:
            st.caption(card_label(card))

            # 内容居中：两侧留白 + 中间一列（近似 demo 里 760px 的文字列），
            # 文字内部仍是左对齐——英文正文居中排版会串行。
            pad_left, content_col, pad_right = st.columns([1, 6, 1])
            with content_col:
                if lang == LANG_ZH and backend is None:
                    st.warning("没有可用的翻译后端，先显示英文原文。")
                    render_card_content(card, pdf_bytes)
                elif lang == LANG_ZH:
                    if is_table_item(card) and not card.segments:
                        render_table(paragraph_formula_payload(card), pdf_bytes)
                        translated_cards += 1
                    elif is_formula_item(card) and not card.segments:
                        render_formula(paragraph_formula_payload(card), pdf_bytes)
                        st.caption("公式区域：保留原文截图，不参与翻译。")
                        translated_cards += 1
                    else:
                        status, error = render_card_translated(card, pdf_bytes, backend, target)
                        if status == "ok":
                            translated_cards += 1
                            st.caption("中文视图：正文与标题已翻译；公式与表格保持原文截图。")
                        elif status == "no_segments":
                            zh, batch_error = translate_cached(card.text, backend, target)
                            if batch_error:
                                st.error(f"翻译失败：{batch_error}")
                                st.caption("下面仍然显示英文原文；问题解决后重新切到中文即可。")
                                render_card_content(card, pdf_bytes)
                            else:
                                translated_cards += 1
                                st.markdown(escape_markdown(zh))
                        else:
                            st.error(f"翻译失败：{error}")
                            st.caption("下面仍然显示英文原文；问题解决后重新切到中文即可。")
                            render_card_content(card, pdf_bytes)
                else:
                    render_card_content(card, pdf_bytes)

            # ---- 插图跟在「它对应的那张卡片」后面（比文字列更宽）----
            card_images = images_by_card.get(card.order, [])
            if card_images:
                fig_pad_left, figures_col, fig_pad_right = st.columns([0.4, 8, 0.4])
                with figures_col:
                    for img in card_images:
                        render_figure(img, pdf_bytes, key_prefix=f"card{card.order}")

        # ---- 进度 ----
        st.progress((index + 1) / total_cards if total_cards else 0.0)
        st.caption(f"第 **{index + 1} / {total_cards}** 张 · 用左右的 **‹ ›** 按钮翻页")

# ============================================================
# 第 9 部分：右栏 —— 背景信息（元数据 / GROBID / 概览 / 结构 / 背景信息 / 诊断）
# ============================================================
with col_right:
    render_metadata_panel(metadata)
    if st.session_state.get("meta_error"):
        st.caption(f"⚠️ 元数据提取有异常：{st.session_state['meta_error']}")

    render_grobid_panel(meta_key, metadata, pdf_bytes, uploaded_file)

    st.divider()
    st.markdown("**📊 概览**")
    ov1, ov2 = st.columns(2)
    ov1.metric("页数", len(pages))
    ov2.metric("阅读卡片", total_cards)
    ov3, ov4 = st.columns(2)
    ov3.metric("正文词数", f"{total_words:,}")
    ov4.metric("解析耗时", f"{result['elapsed']} 秒")
    st.caption(f"正文字号基准 {result['body_size']} pt · 章节 {section_count} 个 · "
               f"插图 {len(images)} 张 · 公式区域 {len(clusters)} 个 · 表格区域 {len(table_regions)} 个 · "
               f"已移出正文 {len(background_blocks)} 段")

    # 扫描版（没有文字层）要给出明确提示，不要让它静默变成空白页
    if result["total_chars"] < 200:
        st.warning(
            f"⚠️ 这个 PDF 几乎提取不到文字（全文仅 {result['total_chars']} 个字符），"
            "很可能是**扫描版 / 图片版**。本阶段不做 OCR，请换一篇有文字层的 PDF 测试。"
        )

    st.divider()
    st.markdown("**🧭 文档结构**")
    if cards:
        st.caption(" · ".join(label for label, _ in sections))

    with st.expander("📎 论文背景信息（已移出卡片的内容）", expanded=False):
        render_background_panel(background_blocks)

    with st.expander("🔍 解析诊断（验证阅读顺序、核对公式与表格区域）", expanded=False):
        render_diagnostics(ASSOC_MAX_GAP, association, blocks, clusters, image_result, images,
                           metadata, pages, paragraphs, result, table_regions)

# ============================================================
# 第 10 部分：卡片渲染完之后，把翻译计数填进左栏的占位符
# ============================================================
stats = st.session_state["translate_stats"]
stats_slot.caption(
    f"API 请求 **{stats['requests']}** 次 · 缓存命中 **{stats['hits']}** 次 · "
    f"失败 {stats['errors']} 次 · 累计 {stats['chars']:,} 字符"
    + (f" · 当前 {translated_cards} 张显示为中文" if translated_cards else "")
)
