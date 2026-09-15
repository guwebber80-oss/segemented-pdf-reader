"""
ui.media —— 图片、公式、表格的渲染与缓存（阶段 5 从 app.py 拆出）

这里的函数都跟 Streamlit 渲染有关（用 st.image / st.caption），但不参与脚本流程编排。
公式与表格的区域截图**共用同一套缓存**：键是页码 + 区域左上角坐标。
"""

import streamlit as st

from utils.image_extractor import render_full_image, render_region_image
from utils.pdf_parser import escape_markdown


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


def _zoom_key(prefix: str, payload: dict) -> str:
    """给「放大」按钮生成稳定的 key（同一区域只渲染一张图，key 必须唯一）"""
    rect = payload.get("rect") or (0, 0, 0, 0)
    return f"zoom_{prefix}_{payload.get('page', 0)}_{int(rect[0])}_{int(rect[1])}"


@st.dialog("区域截图", width="large")
def region_dialog(pdf_bytes: bytes, payload: dict) -> None:
    """
    点「放大」后弹出的原尺寸截图（Streamlit 原生弹层，对应 demo 里的灯箱）。

    为什么需要放大：公式的上下标、表格里的小字号，在卡片宽度里往往看不清；
    截图是 200dpi 渲染的，放大后仍然清晰。
    """
    try:
        st.image(get_formula_image(pdf_bytes, payload), width="content")
    except Exception as exc:                      # pragma: no cover
        st.error(f"图片渲染失败（{type(exc).__name__}）：{exc}")
    caption = (payload.get("caption") or "").strip()
    st.caption(("表格区域" if caption else "公式区域")
               + f" · 原文第 {payload.get('page', '?')} 页 · 原文截图"
               + (f"（{caption[:80]}）" if caption else ""))


@st.dialog("文献图片", width="large")
def figure_dialog(pdf_bytes: bytes, image_item) -> None:
    """文献插图的原尺寸弹层（对应卡片后插图上的「放大」按钮）"""
    try:
        st.image(get_full_image(pdf_bytes, image_item), width="content")
    except Exception as exc:                      # pragma: no cover
        st.error(f"图片渲染失败（{type(exc).__name__}）：{exc}")
    assoc = f"关联卡片 #{image_item.card_order}" if image_item.card_order else "未关联卡片"
    st.caption(f"第 {image_item.page} 页 · 原始 {image_item.pixel_w}×{image_item.pixel_h} px · "
               f"页面上显示尺寸 {image_item.disp_w:.0f}×{image_item.disp_h:.0f} pt · {assoc}")


def render_figure(image_item, pdf_bytes: bytes, key_prefix: str = "fig") -> None:
    """
    渲染一张**文献插图**（卡片后面那一张）+ 「放大」按钮。

    用户要求「图片放在对应位置的文本卡片后」，所以插图跟着卡片走，
    不再只放在侧边栏画廊里；点「放大」弹原生弹层看原尺寸。
    """
    st.image(image_item.thumb, width="stretch")
    assoc = f"关联卡片 #{image_item.card_order}" if image_item.card_order else "未关联卡片"
    st.caption(f"第 {image_item.page} 页 · {image_item.pixel_w}×{image_item.pixel_h} px · {assoc}")
    if st.button("🔍 点击放大", key=f"{key_prefix}_{image_item.key}", width="stretch"):
        figure_dialog(pdf_bytes, image_item)


def render_formula(payload: dict, pdf_bytes: bytes) -> None:
    """渲染一个公式段落（失败时降级显示线性文本，不让卡片崩掉）+ 放大按钮"""
    try:
        st.image(get_formula_image(pdf_bytes, payload), width="content")
        if st.button("🔍 放大", key=_zoom_key("formula", payload)):
            region_dialog(pdf_bytes, payload)
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


def render_table(payload: dict, pdf_bytes: bytes) -> None:
    """
    渲染一个表格区域（与公式**共用同一套截图管线与缓存**：都是「按区域裁原页」）。

    表格与公式的处境完全一样：PDF 里没有任何「表格」标记，文字线性化之后
    列与列的关系全丢（'EV 0.758 0.682 0.735 …' 读不出哪一列是什么），
    所以同样整区域截图呈现；代价也一样——不可选中、不可翻译。
    """
    try:
        st.image(get_formula_image(pdf_bytes, payload), width="content")
        caption = (payload.get("caption") or "").strip()
        st.caption("表格区域：原文截图，不参与翻译"
                   + (f"（{caption[:70]}）" if caption else "") + "。")
        if st.button("🔍 放大", key=_zoom_key("table", payload)):
            region_dialog(pdf_bytes, payload)
    except Exception as exc:
        st.caption(f"表格图片渲染失败（{type(exc).__name__}），下面是原文字：")
        st.markdown(escape_markdown(payload["text"]))


def is_formula_item(item) -> bool:
    """这个条目是不是公式区域（卡片里的公式段落、或「显示全部内容」里的公式段）"""
    return bool(getattr(item, "is_formula", False))


def is_table_item(item) -> bool:
    """这个条目是不是表格区域（卡片里的表格段落、或「显示全部内容」里的表格段）"""
    return bool(getattr(item, "is_table", False))

