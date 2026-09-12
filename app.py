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

import streamlit as st

from image_extractor import (
    ASSOC_MAX_GAP,
    associate_with_cards,
    extract_images,
    render_full_image,
    render_region_image,
)
from metadata import extract_metadata
from pdf_parser import (
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
from translator import (
    BACKEND_LABELS,
    TARGET_LABELS,
    TranslationError,
    backend_availability,
    load_config,
    probe_backend,
    translate,
    translate_many,
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
LANG_EN, LANG_ZH = "EN", "中文"


def translate_cached(text: str, backend: str, target: str):
    """
    带缓存的翻译。返回 (译文, 错误消息)，两者必有一个是 None。

    缓存键是「后端 + 目标语言 + 原文」，所以：
      · 同一张卡片来回点 中/EN，第二次起直接读缓存，不会再花额度
      · 换后端或换目标语言会重新翻译（因为缓存键变了）
    """
    if not backend:
        return None, "没有可用的翻译后端：请先在 .env 里配置 API Key。"

    cache = st.session_state["translations"]
    stats = st.session_state["translate_stats"]
    cache_key = (backend, target, text)

    if cache_key in cache:
        stats["hits"] += 1
        return cache[cache_key], None

    try:
        with st.spinner("正在翻译…"):
            result = translate(text, target=target, backend=backend)
    except TranslationError as exc:
        # TranslationError 的文本已经是给用户看的中文提示
        stats["errors"] += 1
        print(f"[translate][失败] {backend} {target} {len(text)}字符: {exc}", flush=True)
        return None, str(exc)
    except Exception as exc:
        stats["errors"] += 1
        print(f"[translate][异常] {type(exc).__name__}: {exc}", flush=True)
        return None, f"未预期的错误 {type(exc).__name__}: {exc}"

    cache[cache_key] = result
    stats["requests"] += 1
    stats["chars"] += len(text)
    # 同时打到终端，方便对照界面上的计数一起验证「有没有重复请求」
    print(f"[translate][成功] {backend} {target} {len(text)}字符 → {len(result)}字符", flush=True)
    return result, None


def render_language_toggle(key: str):
    """
    卡片里的中/EN 切换控件，返回当前选中的语言。

    注意：这里用「先往 session_state 里塞默认值、控件不带 default」的写法，
    而不是给控件传 default=——因为两者同时用会触发 Streamlit 的冲突警告。
    """
    if key not in st.session_state:
        st.session_state[key] = LANG_EN
    return st.segmented_control(
        "显示语言",
        [LANG_EN, LANG_ZH],
        key=key,
        label_visibility="collapsed",
    )


def get_full_image(pdf_bytes: bytes, image_item):
    """
    按需渲染一张图的完整尺寸，并做小缓存。

    只缓存最近 2 张：大图动辄几百 KB 到几 MB，缓存太多会把内存吃满。
    """
    if "full_images" not in st.session_state:
        st.session_state["full_images"] = {}
    cache = st.session_state["full_images"]

    if image_item.key not in cache:
        with st.spinner("正在渲染大图…"):
            cache[image_item.key] = render_full_image(pdf_bytes, image_item.xref, image_item.smask)
        while len(cache) > 2:
            cache.pop(next(iter(cache)))     # 先进先出，只留最近两张

    return cache[image_item.key]


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
file_key = f"{uploaded_file.name}|{uploaded_file.size}|{merge_on}|{dehyphenate_on}|{target_words}"

if st.session_state.get("parse_key") != file_key:
    with st.spinner("正在解析 PDF…"):
        try:
            st.session_state["parse_result"] = parse_pdf(pdf_bytes, merge_on, dehyphenate_on, target_words)
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
# 第 4.6 部分：文献元数据（标题 / DOI / 摘要）
# ============================================================
meta_key = f"{uploaded_file.name}|{uploaded_file.size}"

if st.session_state.get("meta_key") != meta_key:
    with st.spinner("正在提取标题 / DOI / 摘要…"):
        try:
            st.session_state["metadata"] = extract_metadata(pdf_bytes, blocks)
            st.session_state["meta_error"] = None
        except Exception as exc:
            # 元数据提取失败不影响阅读，降级成空字段让用户手填
            from metadata import Metadata
            st.session_state["metadata"] = Metadata()
            st.session_state["meta_error"] = f"{type(exc).__name__}: {exc}"
    # 换了文件：清掉上一个文件留下的编辑内容，让输入框重新取自动提取值
    for key in ("in_title", "in_doi", "in_abstract"):
        st.session_state.pop(key, None)
    st.session_state["meta_key"] = meta_key

metadata = st.session_state["metadata"]


def metadata_input(label: str, widget_key: str, meta_field, is_long: bool = False,
                   height: int = 170):
    """
    一个可编辑的元数据字段 + 它下方的来源说明。

    输入框的值存在 session_state 里，所以用户的修改会跨重跑保留下来，
    不会被 Streamlit 的「每次交互重跑整个脚本」冲掉。
    """
    if widget_key not in st.session_state:
        st.session_state[widget_key] = meta_field.value

    if meta_field.found:
        if is_long:
            st.text_area(label, key=widget_key, height=height)
        else:
            st.text_input(label, key=widget_key)
        caption = f"来源：{meta_field.source}"
        if meta_field.note:
            caption += f"　⚠️ {meta_field.note}"
        st.caption(caption)
    else:
        if is_long:
            st.text_area(f"{label}（未找到，请手动输入）", key=widget_key, height=height)
        else:
            st.text_input(f"{label}（未找到，请手动输入）", key=widget_key)
        st.caption(f"⚠️ {meta_field.note or '自动提取失败，请手动填写'}")


st.subheader("📋 文献元数据")

head_left, head_right = st.columns([4, 1])
with head_left:
    st.caption(
        "自动提取只是**候选值**。科研场景下准确性优先于自动化——"
        "请核对后直接修改，改动立即生效。"
    )
with head_right:
    if st.button("↺ 用自动提取结果覆盖", help="把手动修改过的内容还原成程序提取的值"):
        st.session_state["in_title"] = metadata.title.value
        st.session_state["in_doi"] = metadata.doi.value
        st.session_state["in_abstract"] = metadata.abstract.value
        st.rerun()

if st.session_state.get("meta_error"):
    st.error("元数据提取失败：" + st.session_state["meta_error"])

col_title, col_doi = st.columns([2, 1])
with col_title:
    metadata_input("标题", "in_title", metadata.title)
with col_doi:
    metadata_input("DOI", "in_doi", metadata.doi)

metadata_input("摘要", "in_abstract", metadata.abstract, is_long=True)

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
    "**公式以原文截图呈现**（二维排版不会失真，代价是不可选中）。"
    "展开卡片后可用 **EN / 中文** 切换语言——切到中文时正文按段落翻译，**公式仍以原图呈现**。"
)

_COLUMN_NAME = {-1: "通栏", 0: "左栏", 1: "右栏"}
def card_label(card) -> str:
    """卡片标签：§章节（跨章节时显示范围）· 原文第 X–Y 页 · N 词"""
    start = (card.section_title or "").strip() or "文首"
    if card.section_number:
        start = f"{card.section_number} {start}"
    if len(start) > 44:
        start = start[:42] + "…"
    if card.headings_inside:
        end_number, end_title = card.headings_inside[-1]
        end = f"{end_number} {end_title}".strip()
        if end and end != start:
            start = f"{start} → {end[:28]}"
    page_end = card.page_end or card.page
    pages = f"第 {card.page} 页" if page_end == card.page else f"第 {card.page}–{page_end} 页"
    return f"#{card.card_index} · §{start} · 原文{pages} · {count_words(card.text)} 词"


def get_formula_image(pdf_bytes: bytes, formula: dict):
    """按需把公式区域渲染成图片（带缓存，避免每次重跑都重新渲染）"""
    if "formula_images" not in st.session_state:
        st.session_state["formula_images"] = {}
    cache = st.session_state["formula_images"]

    key = f"p{formula['page']}-{int(formula['rect'][0])}-{int(formula['rect'][1])}"
    if key not in cache:
        cache[key] = render_region_image(pdf_bytes, formula["page"], formula["rect"])
        while len(cache) > 40:          # 公式图很小，可以多留几张
            cache.pop(next(iter(cache)))
    return cache[key]


def translate_cached_batch(texts, backend: str, target: str):
    """
    批量翻译多段文本（带缓存）。返回 (译文列表, 错误消息)。

    为什么要按段翻译：中文视图里**公式要保持图片形式**，而图片没法翻译，
    所以必须逐段决定「翻译 / 原样保留」。DeepL 与 Google 支持一次请求带多段文本，
    所以仍然只发一次 HTTP 请求，速度与整段翻译几乎一样（实测 3 段 1.9s vs 逐段 5.8s）。
    缓存粒度也随之变细：同一段落被别的卡片复用时也能命中。
    """
    if not backend:
        return None, "没有可用的翻译后端：请先在 .env 里配置 API Key。"

    cache = st.session_state["translations"]
    stats = st.session_state["translate_stats"]
    results = ["" for _ in texts]
    pending = []

    for index, text in enumerate(texts):
        key = (backend, target, text)
        if key in cache:
            stats["hits"] += 1
            results[index] = cache[key]
        else:
            pending.append(index)

    if pending:
        try:
            with st.spinner("正在翻译…"):
                fresh = translate_many([texts[i] for i in pending], target=target, backend=backend)
        except TranslationError as exc:
            stats["errors"] += 1
            print(f"[translate][失败] {backend} {target} 批量 {len(pending)} 段: {exc}", flush=True)
            return None, str(exc)
        except Exception as exc:
            stats["errors"] += 1
            print(f"[translate][异常] {type(exc).__name__}: {exc}", flush=True)
            return None, f"未预期的错误 {type(exc).__name__}: {exc}"

        stats["requests"] += 1          # 一次批量请求算 1 次
        for slot, translated in zip(pending, fresh):
            cache[(backend, target, texts[slot])] = translated
            stats["chars"] += len(texts[slot])
            results[slot] = translated
        print(f"[translate][成功] {backend} {target} 批量 {len(pending)} 段 "
              f"{sum(len(texts[i]) for i in pending)}字符", flush=True)

    return results, None


def render_formula(payload: dict, pdf_bytes: bytes) -> None:
    """渲染一个公式段落（失败时降级显示线性文本，不让卡片崩掉）"""
    try:
        st.image(get_formula_image(pdf_bytes, payload), width="content")
    except Exception as exc:
        st.caption(f"公式图片渲染失败（{type(exc).__name__}），下面是线性文本：")
        st.markdown(escape_markdown(payload["text"]))


def paragraph_formula_payload(item) -> dict:
    """
    段落视图里的公式段落 → 渲染载荷。

    段落视图的坐标在同步阶段已经被撑到**整簇外接矩形**，
    所以一个公式区域只会渲染出一张图。
    """
    return {
        "page": item.page,
        "rect": (item.x0, item.y0, item.x1, item.y1),
        "text": item.text,
    }


def is_formula_item(item) -> bool:
    """这个条目是不是公式区域（卡片里的公式段落、或「显示全部内容」里的公式段）"""
    return bool(getattr(item, "is_formula", False))


def render_card_translated(item, pdf_bytes: bytes, backend: str, target: str):
    """
    中文视图的渲染：**按段落翻译，公式段保持原文截图**。

    返回 (状态, 错误消息)，状态取值：
        "ok"           成功渲染
        "no_segments"  这张卡片没有段落结构（「显示全部内容」模式的段落）→ 交给调用方处理
        "error"        翻译失败 → 调用方显示错误并继续显示英文原文
    """
    segments = item.segments
    if not segments:
        return "no_segments", None

    text_indexes = [i for i, (kind, _) in enumerate(segments) if kind in ("heading", "text")]
    if not text_indexes:
        # 整张卡片都是公式：直接显示图片，压根不需要翻译
        for _, payload in segments:
            render_formula(payload, pdf_bytes)
        return "ok", None

    translations, error = translate_cached_batch(
        [segments[i][1] for i in text_indexes], backend, target)
    if error:
        return "error", error

    mapping = dict(zip(text_indexes, translations))
    for index, (kind, payload) in enumerate(segments):
        if kind == "formula":
            render_formula(payload, pdf_bytes)
        elif kind == "heading":
            st.markdown(f"**▍{escape_markdown(mapping.get(index, payload))}**")
        else:
            st.markdown(escape_markdown(mapping.get(index, payload)))
    return "ok", None


def render_card_content(item, pdf_bytes: bytes) -> None:
    """
    渲染卡片内容。

    阅读卡片带 segments（内部结构）：章节标题加粗显示、**公式用图片呈现**、
    其余按文本显示。「显示全部内容」模式下的段落没有 segments，
    但公式段落仍然按区域出图（保持与卡片视图一致的呈现）。
    """
    if not item.segments:
        if is_formula_item(item):
            render_formula(paragraph_formula_payload(item), pdf_bytes)
            return
        st.markdown(escape_markdown(item.text))
        return

    for kind, payload in item.segments:
        if kind == "heading":
            st.markdown(f"**▍{escape_markdown(payload)}**")
        elif kind == "formula":
            try:
                st.image(get_formula_image(pdf_bytes, payload), width="content")
            except Exception as exc:
                # 渲染失败不能让卡片崩掉，退回显示线性文本
                st.caption(f"公式图片渲染失败（{type(exc).__name__}），下面是线性文本：")
                st.markdown(escape_markdown(payload["text"]))
        else:
            st.markdown(escape_markdown(payload))


expanded_used = 0        # 已展开的卡片数，用来实现「默认只展开前 N 张」
translated_cards = 0     # 本次渲染里显示为中文的卡片数（统计用）

if show_all:
    # 不过滤：把派生段落按阅读顺序全列出来（含页眉页脚、作者单位…），便于核对有没有误杀
    display_items = [(f"#{b.order} · 第{b.page}页 · {ROLE_NAMES.get(b.role, b.role)} · "
                      f"{count_words(b.text)} 词"
                      + (f" · 公式簇{b.cluster_id}" if b.cluster_id >= 0 else ""), b)
                     for b in paragraphs]
else:
    display_items = [(card_label(card), card) for card in cards]

for label, item in display_items:
    # 这张卡片附近有几张图（用于在标签上标出来，方便对照阅读）
    image_mark = f" · 🖼️×{len(images_by_card[item.order])}" if item.order in images_by_card else ""

    with st.expander(label + image_mark, expanded=(expanded_used < expand_n)):
        card_lang = render_language_toggle(f"lang_{item.order}")

        if card_lang == LANG_ZH:
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
# 第 7 部分：诊断面板（验证阅读顺序、定位双栏错位）
# ============================================================
# ============================================================
# 第 6.5 部分：论文背景信息（已从阅读卡片里移出的内容）
# ============================================================
BACKGROUND_GROUPS = [
    ("✍️ 作者与单位", (ROLE_AUTHOR, ROLE_FRONT_MATTER),
     "作者名、单位、联系方式，以及封面页上的 Highlights / In Brief 等导读内容"),
    ("📰 期刊信息与栏目", (ROLE_COPYRIGHT, ROLE_LABEL),
     "期刊名与栏目名（Report / Highlights / CCS Concepts…）、版权与许可声明、资助信息、"
     "以及图内的面板标签"),
    ("📚 参考文献", (ROLE_REFERENCE,), "参考文献列表（保持原文顺序）"),
    ("📄 页眉页脚", (ROLE_HEADER_FOOTER,), "每页重复出现的 running head、页码、页脚"),
]

st.subheader("📎 论文背景信息")
st.caption(
    f"下面 {len(background_blocks)} 段**不属于正文**，所以没有进阅读卡片。"
    "如果发现哪一段其实是正文，打开侧边栏的「显示全部内容」就能把它找出来。"
)

for group_title, group_roles, group_hint in BACKGROUND_GROUPS:
    group_items = [b for b in background_blocks if b.role in group_roles]
    if not group_items:
        continue
    with st.expander(f"{group_title}（{len(group_items)} 段）", expanded=False):
        st.caption(group_hint)
        for b in group_items:
            text = b.text.strip().replace("\n", " ")
            if len(text) > 400:
                text = text[:400] + "…"
            st.markdown(f"- 第 {b.page} 页 · {escape_markdown(text)}")

st.divider()

# ============================================================
# 第 7 部分：诊断面板（验证阅读顺序、定位双栏错位）
# ============================================================
with st.expander("🔍 解析诊断（验证阅读顺序、定位双栏错位）"):
    st.markdown("**⓪ 内容角色统计**（正文与图注进阅读卡片，其余归入「论文背景信息」）")
    st.dataframe([{
        "角色": ROLE_NAMES.get(role, role),
        "块数": count,
        "去向": "进卡片流" if role in ("body", "caption", "heading") else "→ 背景信息",
    } for role, count in sorted(result["roles"].items(), key=lambda kv: -kv[1])],
        hide_index=True)

    st.markdown("**① 每页排版检测**")
    st.dataframe(pages, hide_index=True)

    st.markdown("**② 段落明细**（已按程序认定的阅读顺序编号，从上往下就是卡片顺序）")
    rows = [{
        "序号": b.order,
        "页码": b.page,
        "类型": "标题" if b.kind == "heading" else "正文",
        "角色": ROLE_NAMES.get(b.role, b.role),
        "栏": _COLUMN_NAME[b.column],
        "y0": round(b.y0, 1),
        "x0": round(b.x0, 1),
        "字号": round(b.max_size, 1),
        "粗体占比": round(b.bold_ratio, 2),
        "词数": count_words(b.text),
        "拼了块": len(getattr(b, "atom_indices", []) or []),
        "公式": b.formula_tier or ("簇%d" % b.cluster_id if b.cluster_id >= 0 else "-"),
        "开头 40 字": b.text[:40].replace("\n", " "),
    } for b in paragraphs]
    st.dataframe(rows, hide_index=True, height=420)

    # 双栏页里，正常只有标题/摘要/跨栏图注会横跨中缝。
    # 如果这里出现大段正文，说明分栏判断可能出错，需要人工核对。
    spanning = [b for b in paragraphs
                if b.column == -1 and b.kind == "body" and count_words(b.text) > 120]
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

    st.markdown("**④ 图片与卡片的关联情况**")
    if not images:
        st.caption("这个 PDF 没有提取到位图插画（矢量图不算）。")
    else:
        st.write(
            f"共 {len(images)} 张 · 可靠关联 **{association['关联可靠']}** 张 · "
            f"存疑 {association['关联存疑']} 张 · 未关联 {association['未关联']} 张"
        )
        problems = association["未关联列表"] + association["存疑列表"]
        if problems:
            st.markdown(
                f"下面 **{len(problems)}** 张没能可靠关联到文本卡片"
                "（关联方式：取同页距离最近的文本块；距离超过 "
                f"{ASSOC_MAX_GAP:.0f}pt 或同页无文本就算不可靠）："
            )
            st.dataframe([{
                "图片": img.key,
                "页码": img.page,
                "位置": f"({img.bbox[0]:.0f}, {img.bbox[1]:.0f})",
                "与最近卡片距离": f"{img.gap} pt" if img.gap >= 0 else "同页没有文本块",
            } for img in problems], hide_index=True)
        else:
            st.success("所有图片都关联到了文本卡片。")

    if image_result["skipped"]:
        st.markdown(f"**⑤ 被跳过的图片（{len(image_result['skipped'])} 张）**")
        st.dataframe(image_result["skipped"], hide_index=True)

    st.markdown("**⑥ 元数据提取详情**（自动提取的原始值与来源，方便对照你在上面改过的内容）")
    st.dataframe([{
        "字段": name,
        "来源": meta_field.source,
        "自动提取值": (meta_field.value[:70] + "…") if len(meta_field.value) > 70 else meta_field.value,
        "备注": meta_field.note,
    } for name, meta_field in [("标题", metadata.title),
                               ("DOI", metadata.doi),
                               ("摘要", metadata.abstract)]], hide_index=True)
    if metadata.title.alternatives:
        st.caption(f"标题备选（来自 PDF 内嵌元数据，可自行取舍）：{metadata.title.alternatives}")

    st.markdown(f"**⑦ 公式区域明细（{len(clusters)} 个）**"
                "——每个区域渲染成一张图，右边列出被合进来的块，方便核对有没有合错")
    if not clusters:
        st.caption("这个 PDF 没有识别到数学公式区域（正文里的行内统计量仍以文字呈现）。")
    else:
        block_by_order = {b.order: b for b in blocks}
        st.dataframe([{
            "区域": f"#{cluster.cluster_id}",
            "页码": cluster.page,
            "类型": "算法清单" if cluster.kind == "listing" else "公式",
            "成员块数": cluster.member_count,
            "区域范围": f"({cluster.rect[0]:.0f}, {cluster.rect[1]:.0f})"
                        f"–({cluster.rect[2]:.0f}, {cluster.rect[3]:.0f})",
            "成员块（#阅读顺序号:文本）": " | ".join(
                f"#{block_by_order[i].order}:{block_by_order[i].text.strip()[:22]}"
                for i in cluster.atom_indices if i in block_by_order),
        } for cluster in clusters], hide_index=True)
        st.caption(
            "核对方法：区域范围应当刚好框住一处公式；成员块里如果混进了正文句子，"
            "把这一页的截图和一个具体例子发给我，我按这个明细来调。"
        )

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
