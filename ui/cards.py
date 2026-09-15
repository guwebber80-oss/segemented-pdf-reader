"""
ui.cards —— 阅读卡片的标签、翻译与渲染（阶段 5 从 app.py 拆出）

约定：卡片的中文视图**按段落翻译**，公式段与表格段保持原文截图、不参与翻译；
翻译缓存键是 (后端, 目标语言, 文本)，命中即不请求（侧边栏有计数可核对）。
"""

import streamlit as st

from utils.pdf_parser import ROLE_NAMES, count_words, escape_markdown
from utils.translator import TranslationError, translate_many

from .media import (
    get_formula_image,
    is_formula_item,
    is_table_item,
    paragraph_formula_payload,
    render_formula,
    render_table,
)

LANG_EN, LANG_ZH = "EN", "中文"

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
        # 整张卡片都是公式 / 表格：直接显示图片，压根不需要翻译
        for kind, payload in segments:
            if kind == "table":
                render_table(payload, pdf_bytes)
            else:
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
        elif kind == "table":
            render_table(payload, pdf_bytes)      # 表格同样保持原文截图
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
        if is_table_item(item):
            # 「显示全部内容」模式是给人**核对分类**用的，所以表格在这里显示原文字，
            # 而不是截图——这样你能看清它到底框住了哪些行，也能复制里面的数字。
            st.caption("表格区域（本模式按原文字列出，卡片视图里是整张截图）：")
            st.markdown(escape_markdown(item.text))
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
        elif kind == "table":
            render_table(payload, pdf_bytes)
        else:
            st.markdown(escape_markdown(payload))

