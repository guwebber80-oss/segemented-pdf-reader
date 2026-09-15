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
    │ 本地缓存  │ （「显示全部内容」模式在此展开）  │ 背景信息/诊断  │
    │ 章节导航  │                              │              │
    │ 翻译计数  │                              │              │
    └──────────┴─────────────────────────────┴──────────────┘

约定：
  · 卡片内容**居中**、文字内部仍左对齐（英文正文居中排版会串行）；
  · 插图与卡片里的公式/表格截图都可以**点击放大**（Streamlit 原生弹层）；
  · 导入后默认中文视图（离线可用：译文命中缓存就不再请求 API）；
  · **成果落本地**：设置、元数据修正、GROBID 结果、译文、读到第几张都存进 `.cache/`，
    下次打开同一份 PDF 直接接上（见 utils/store.py 与 ui/persist.py）；
  · 解析、图片提取、元数据提取的逻辑与缓存指纹**保持原样**（回归比对守着）。

界面的渲染函数在 `ui/`，解析与元数据的逻辑在 `utils/`，本文件只做流程编排。
"""

import os

import streamlit as st

from utils import store
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
from ui.layout import (
    HEIGHT_KEY,
    HEIGHT_OFFSETS,
    IMMERSIVE_KEY,
    KEY_CARD,
    KEY_LEFT_BOX,
    KEY_RIGHT_BOX,
    SCROLL_KEY,
    columns_spec,
    ensure_layout_state,
    figures_pad_spec,
    layout_css,
    save_prefs,
    text_pad_spec,
)
from ui.metadata_panel import render_grobid_panel, render_metadata_panel
from ui.media import (
    is_formula_item,
    is_table_item,
    paragraph_formula_payload,
    render_figure,
    render_formula,
    render_table,
)
from ui.persist import (
    build_record,
    render_cache_panel,
    render_translation_coverage,
    restore_edits,
    restore_grobid,
    seed_settings,
)
from ui.theme import render_theme_control

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

# 布局偏好（卡内滚动开关 / 卡片高度档位 / 沉浸模式）：会话里没有就读本地 `.cache/ui.json`，
# 这样上次选的档位与沉浸状态，重开浏览器也还在（阶段 6.5）
ensure_layout_state()

# ============================================================
# 第 2 部分：三栏骨架（左：功能控件 / 中：阅读卡片 / 右：背景信息）
# ============================================================
# 阶段 6.5：整页不滚动，左右两栏与卡片各自「卡内滚动」；沉浸模式把左右栏压到极小并隐藏。
# 滚动/隐藏都靠给容器起 key（Streamlit 前端会给带 key 的容器加 `st-key-<key>` 类名），
# 样式由 ui/layout.layout_css() 生成——只用自己的 key，不碰 Streamlit 的内部结构。
immersive = bool(st.session_state.get(IMMERSIVE_KEY, False))
scroll_layout = bool(st.session_state.get(SCROLL_KEY, True))
card_height = st.session_state.get(HEIGHT_KEY, "中")
st.markdown(f"<style>{layout_css(scroll_layout, immersive, card_height)}</style>",
            unsafe_allow_html=True)

col_left, col_mid, col_right = st.columns(
    columns_spec(immersive),
    gap="small" if immersive else "medium",
    vertical_alignment="top",
)

# ------------------------------------------------------------
# 2.1 左栏：上传（唯一的文件入口，不放欢迎页）
# ------------------------------------------------------------
with col_left:
    # 左栏的滚动区：下面所有左栏内容都写进这个容器（阶段 6.5）
    left_box = st.container(key=KEY_LEFT_BOX)

with left_box:
    st.markdown("### 📚 阅读器")
    uploaded_file = st.file_uploader(
        label="上传文献", type=["pdf"], accept_multiple_files=False,
        key="left_uploader",
        help="支持有文字层的单栏 / 双栏 PDF；换文件会自动重新解析。",
    )
    # 明 / 暗模式放在上传框下面：还没选文件时也能先调好再读（阶段 6.2）
    with st.expander("🌗 明 / 暗模式", expanded=False):
        render_theme_control()

    # 阅读布局（阶段 6.5）：卡内滚动可一键退回；卡片高度按显示器挑
    with st.expander("🖥️ 阅读布局", expanded=False):
        st.toggle(
            "卡内滚动（页面固定）", key=SCROLL_KEY,
            help="开启后左右两栏与卡片各自滚动、整页不跟着滚；如果显示不合适，关掉即可"
                 "完全回到改动前的样子（本项的旧行为有回归守着）。",
        )
        st.radio(
            "卡片高度", list(HEIGHT_OFFSETS), key=HEIGHT_KEY, horizontal=True,
            help="按你的显示器挑：卡片矮一点=整页更稳，高一点=一屏看更多正文。"
                 "沉浸模式下这个档位同样生效。",
        )
        st.caption("沉浸模式（隐藏左右两栏）的按钮在卡片区右上角。")

# 还没选文件：中栏显示引导语，两侧留空但不渲染其它控件
if uploaded_file is None:
    with col_mid:
        # 陷阱防守（阶段 6.5）：沉浸模式会把左栏藏起来，而上传入口就在左栏——
        # 上次退出时留在沉浸模式的话，这次打开会看不到上传框，所以这里必须给一条出路。
        if immersive:
            st.warning("当前是**沉浸模式**：左右两栏被隐藏了，上传入口也在里面。")
            if st.button("⤡ 退出沉浸，显示左右两栏", key="exit_immersive_here", width="stretch"):
                st.session_state[IMMERSIVE_KEY] = False
                save_prefs()
                st.rerun()
        st.title("📚 科研文献 PDF 智能阅读器")
        st.caption(
            "卡片式阅读（正文按章节聚合成 100~300 词）· 中英一键切换 · 插图跟随卡片 · "
            "公式与表格原样截图 · 元数据可人工修正"
        )
        st.info(
            "👈 先在左栏选一篇 PDF。上传后：**中间是阅读卡片**（用卡片左右两侧的 ‹ › 翻页，"
            "插图跟在对应卡片后面，点图可放大）· **右栏是文献背景信息**（元数据 / 概览 / 结构）· "
            "卡片区右上角的 **⤢ 沉浸** 可以隐藏左右两栏只看卡片。"
        )
    st.stop()      # 没有文件就停下，避免下面到处写 None 判断

pdf_bytes = uploaded_file.getvalue()

# ------------------------------------------------------------
# 2.2 本地缓存：这篇 PDF 上次读过没有（阶段 6.1）
# ------------------------------------------------------------
# 论文身份取 PDF 字节的 SHA-256（不认文件名），所以改名、换目录、重新上传同一份文件
# 仍然命中；内容变了（期刊给了新版）就当成另一篇，此时还能按 DOI / 标题兜底找回。
paper_key = store.paper_key(pdf_bytes)
cached_record = store.load_paper(paper_key)

# 换文件时一次性恢复「幂等的东西」：解析设置、读到第几张、元数据修正、GROBID 结果。
# ⚠️ 必须在控件被创建之前做——Streamlit 不允许给已经创建过的带 key 控件改 session_state。
if st.session_state.get("cache_key") != paper_key:
    st.session_state["cache_key"] = paper_key
    st.session_state["cache_record"] = cached_record
    st.session_state["cache_source"] = "hash" if cached_record else ""
    seed_settings(cached_record)
    if cached_record:
        # 回到上次读到的那张卡（越界由下面的翻页逻辑夹住）
        st.session_state["card_index"] = int(cached_record.get("card_index") or 0)

# ------------------------------------------------------------
# 2.3 左栏：语言开关与各项设置（解析设置会进解析指纹，所以必须在解析之前）
# ------------------------------------------------------------
with left_box:
    lang = st.radio(
        "语言", [LANG_ZH, LANG_EN], horizontal=True, key="ui_lang",
        help="导入后默认中文视图；切到 EN 看原文。公式、表格与插图始终保留原文截图。",
    )

    # 注意：下面这些控件的初始值由 ui/persist.seed_settings() 预先塞进 session_state，
    # 所以这里**不再传 value= / index=**——默认值与 Session State 同时给会触发 Streamlit 警告。
    with st.expander("⚙️ 解析设置", expanded=False):
        merge_on = st.toggle(
            "段落自动合并", key="set_merge_on",
            help="把被 PDF 拆成多块的同一段落拼回去。若发现两个独立段落被错误粘在一起，关掉它。",
        )
        dehyphenate_on = st.toggle(
            "行尾连字符合并", key="set_dehyphenate_on",
            help="英文排版会把单词在行尾断开（informa- / tion），开启后自动拼成 information。",
        )
        target_words = st.slider(
            "卡片目标词数", min_value=100, max_value=400, step=25, key="set_target_words",
            help="攒到这么多词就收一张卡片；实际落点区间是这个数的 0.5~1.5 倍。",
        )
        table_mode_label = st.radio(
            "表格呈现", ["截图（推荐）", "保留文字"], key="set_table_mode_label",
            help="截图：整张表按原排版截成图片（列对齐不会丢）；保留文字：不做表格识别，"
                 "表格文字照旧线性排在卡片里（4.4-B 之前的行为，用于对照）。",
        )
        show_all = st.toggle(
            "显示全部内容（不过滤非正文）", key="set_show_all",
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

        if st.button("清空翻译缓存", help="会话里的译文与本地保存的译文一起清掉，下次会重新翻译"):
            st.session_state["translations"] = {}
            st.session_state["translate_stats"] = {"requests": 0, "hits": 0, "chars": 0, "errors": 0}
            store.clear_translations()          # 阶段 6.1：磁盘上的译文表也一起清
            st.rerun()
        st.caption("改完 .env 需要点「重读 .env」或重启服务才会生效。")

    st.divider()
    st.markdown("**处理状态**")
    status_slot = st.container()          # 解析完成后往这里填状态
    meta_status_slot = st.empty()

    # 译文覆盖与「翻译整篇」也要等卡片出来才能算，所以同样先占位（阶段 6.4）
    coverage_slot = st.container()

    # 本地缓存面板也先占位，等这次的成果存盘之后再填内容（数字才是最新的）
    cache_slot = st.expander("💾 本地缓存", expanded=False)

# ============================================================
# 第 4 部分：解析（带缓存）
# ============================================================
# 按「文件 + 设置」的指纹判断要不要重新解析，避免每次点击都重算一遍 PDF。
# table_mode 必须进指纹：切换表格呈现方式要重新解析才会生效。
file_key = (f"{uploaded_file.name}|{uploaded_file.size}|{merge_on}|{dehyphenate_on}"
            f"|{target_words}|{table_mode}")

if st.session_state.get("parse_key") != file_key:
    with left_box:
        with st.spinner("正在解析 PDF…"):
            try:
                st.session_state["parse_result"] = parse_pdf(pdf_bytes, merge_on, dehyphenate_on,
                                                             target_words, table_mode)
                st.session_state["parse_key"] = file_key
                st.session_state["parse_error"] = None
                # 换文件/换设置 → 回到第一张；有本地记录则保持上面刚恢复的阅读位置
                if not cached_record:
                    st.session_state["card_index"] = 0
            except Exception as exc:
                # 解析失败不能让页面白屏，要把错误原样显示出来方便排查
                st.session_state["parse_result"] = None
                st.session_state["parse_key"] = None
                st.session_state["parse_error"] = f"{type(exc).__name__}: {exc}"

if st.session_state.get("parse_error"):
    with left_box:
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
    with left_box:
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
    with left_box:
        with st.spinner("正在提取文献元数据…"):
            try:
                st.session_state["metadata"] = extract_metadata(pdf_bytes, blocks)
                st.session_state["meta_error"] = None
            except Exception as exc:
                # 元数据提取失败不影响阅读，降级成空字段让用户手填
                from utils.metadata import Metadata
                st.session_state["metadata"] = Metadata()
                st.session_state["meta_error"] = f"{type(exc).__name__}: {exc}"

        # 换了文件：把「人工修正过的十项」从本地记录里恢复回来（没有记录就清空，
        # 让输入框重新取本次自动提取的值）。这一步必须在右栏渲染之前做完。
        record = st.session_state.get("cache_record")
        if record is None:
            # PDF 字节对不上（重新下载、期刊更新版本），按 DOI / 标题兜底找回
            found = store.find_by_metadata(
                title=st.session_state["metadata"].title.value,
                doi=st.session_state["metadata"].doi.value,
                exclude_key=paper_key,
            )
            if found:
                record = found
                st.session_state["cache_record"] = found
                st.session_state["cache_source"] = "meta"
        restore_edits(record)
        # GROBID 结果一并恢复：命中缓存就不必再等那次 5~10 秒的外部调用
        restore_grobid(record, meta_key)
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

with left_box:
    with status_slot:
        st.caption(
            f"✅ 文本 {len(pages)} 页 / {len(cards)} 张卡 · "
            f"🖼️ 图片 {len(images)} 张 · {'✅' if metadata.title.value else '⚠️'} 元数据 · "
            f"{'✅' if backend else '⚠️'} 翻译后端"
        )
        if st.session_state.get("image_error"):
            st.warning("图片提取失败：" + st.session_state["image_error"])

    # 译文覆盖 + 「翻译整篇」（阶段 6.4）：太长的卡不会自动翻，这里让用户一次补齐，
    # 补齐之后断网重读也是全中文（译文落在 .cache/translations.json 里）
    with coverage_slot:
        render_translation_coverage(cards, backend, target)

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
    # ---- 卡片区顶部：文件信息 + 沉浸开关（这一条永远固定，不随卡片滚动）----
    head_info, head_tool = st.columns([5, 1.35], vertical_alignment="center")
    with head_info:
        if immersive:
            st.caption("**沉浸模式**：只显示卡片，左右两栏已隐藏。")
        else:
            st.caption(f"**{uploaded_file.name}** · {layouts}")
            st.markdown(f"#### {metadata.title.value or '（未识别到标题）'}")
    with head_tool:
        if st.button("⤡ 退出沉浸" if immersive else "⤢ 沉浸",
                     key="toggle_immersive", width="stretch",
                     help="隐藏左右两栏，只留卡片；再点一次恢复（阶段 6.5）"):
            st.session_state[IMMERSIVE_KEY] = not immersive
            save_prefs()
            st.rerun()

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

            # ---- 卡内滚动（阶段 6.5）：正文与插图都在这个滚动区里，
            #      翻页按钮与下面的进度条留在区外固定不动 ----
            with st.container(key=KEY_CARD):
                # 内容居中：两侧留白 + 中间一列（近似 demo 里 760px 的文字列），
                # 文字内部仍是左对齐——英文正文居中排版会串行。
                # 沉浸模式中栏变宽，留白比例随之调整（见 ui/layout.text_pad_spec）。
                pad_left, content_col, pad_right = st.columns(text_pad_spec(immersive))
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
                                    st.caption(
                                        "下面仍然显示英文原文。若这张卡片**本地还没有译文**"
                                        "（左栏「🌐 译文覆盖」能看出来），就需要联网翻译一次；"
                                        "也可以在有网时先点「🌐 翻译整篇」把整篇补齐，之后断网也能看中文。")
                                    render_card_content(card, pdf_bytes)
                                else:
                                    translated_cards += 1
                                    st.markdown(escape_markdown(zh))
                            else:
                                st.error(f"翻译失败：{error}")
                                st.caption(
                                    "下面仍然显示英文原文。若这张卡片**本地还没有译文**"
                                    "（左栏「🌐 译文覆盖」能看出来），就需要联网翻译一次；"
                                    "也可以在有网时先点「🌐 翻译整篇」把整篇补齐，之后断网也能看中文。")
                                render_card_content(card, pdf_bytes)
                    else:
                        render_card_content(card, pdf_bytes)

                # ---- 插图跟在「它对应的那张卡片」后面（比文字列更宽），同样在卡内滚动区里 ----
                card_images = images_by_card.get(card.order, [])
                if card_images:
                    fig_pad_left, figures_col, fig_pad_right = st.columns(figures_pad_spec(immersive))
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
    # 右栏的滚动区（阶段 6.5）：下面所有右栏内容都写进这个容器
    with st.container(key=KEY_RIGHT_BOX):
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
    + (f" · 其中本地缓存 {stats['disk']} 次" if stats.get("disk") else "")
    + (f" · 当前 {translated_cards} 张显示为中文" if translated_cards else "")
)

# ============================================================
# 第 11 部分：把这次的成果落到本地磁盘（阶段 6.1）
# ============================================================
# 每次交互都会走这里：记下「当前设置 + 人工修正后的元数据 + GROBID 结果 + 读到第几张」，
# 下次打开同一份 PDF 就能原样接上。译文是另一张全局表，只在有新译文时才写盘。
# 例外：用户刚点过「清除本篇记录」——本次会话不再把这篇写回来（no_autosave_key 是按论文记的，
# 换个文件就自动恢复保存），否则清除动作会被脚本重跑立刻覆盖掉。
if st.session_state.get("no_autosave_key") == paper_key:
    saved_ok = True
else:
    saved_ok = store.save_paper(build_record(
        paper_key, uploaded_file.name, uploaded_file.size,
        st.session_state.get("card_index", 0),
        grobid_result=st.session_state.get("grobid_result"),
        title=st.session_state.get("in_title", ""),
        doi=st.session_state.get("in_doi", ""),
    ))
store.flush_translations()
# 布局偏好（沉浸 / 卡内滚动 / 卡片高度）也记下来：下次打开还是这个样子（阶段 6.5）
save_prefs()

with cache_slot:
    render_cache_panel(paper_key, st.session_state.get("cache_source", ""), saved_ok)
