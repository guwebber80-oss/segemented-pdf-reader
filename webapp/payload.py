"""
webapp.payload —— 把引擎的解析结果整理成前端要的 JSON（纯逻辑，不依赖 Streamlit / HTTP）

**为什么单独一层**：新前端（自建 HTML/CSS/JS）与 Streamlit 版共用同一套引擎（`utils/`），
区别只在"谁来画界面"。前端要的东西（卡片数组、图片 id、元数据字段、概览数字）在这里一次算好，
HTTP 层只负责搬运字节，于是这一层可以**完全离线单测**（见 tests/test_webapp_payload.py）。

约定：
  · 图片一律用**id**引用，前端通过 `/api/img/<id>` 取 PNG（而不是把 base64 塞进 JSON——
    demo 0.94MB 就是被内嵌图片撑起来的）；
  · 公式与表格区域沿用"整块截图"的呈现（引擎早已把区域算好，这里只给 id 与说明文字）；
  · 元数据每个字段都带 `value / source / found / note`，前端照样显示来源与警告，允许人工修正。
"""

import re

from utils import store

# 视作「正文/标题」、需要翻译的段落类型
TEXT_KINDS = ("heading", "text")
# 以区域截图呈现的类型
REGION_KINDS = ("formula", "table")

# 人工修正用的存储键：**故意沿用 Streamlit 版那套 `in_xxx`**
# —— 两个前端共用同一份 `.cache/`，键一样才意味着你在任一侧改的元数据，另一侧也看得见。
EDIT_KEYS = {
    "title": "in_title",
    "doi": "in_doi",
    "abstract": "in_abstract",
    "authors": "in_authors",
    "first_author": "in_first_author",
    "corresponding": "in_corresponding",
    "corresponding_email": "in_corresponding_email",
    "affiliations": "in_affiliations",
    "supplementary": "in_supplementary",
    "author_emails": "in_author_emails",
}
# 存储键 → Metadata 对象的字段名（用来判断"值与自动提取相同 ⇒ 不算修正"）
AUTO_FIELDS = {
    "in_title": "title",
    "in_doi": "doi",
    "in_abstract": "abstract",
    "in_authors": "authors",
    "in_first_author": "first_author",
    "in_corresponding": "corresponding_author",
    "in_corresponding_email": "corresponding_email",
    "in_affiliations": "affiliations",
    "in_supplementary": "supplementary_links",
    "in_author_emails": "author_emails",
}


def _round(value, digits=1):
    try:
        return round(float(value), digits)
    except Exception:
        return value


def region_id(page, rect) -> str:
    """区域截图的稳定 id：同页同坐标 → 同一个 id（刷新页面也能复用浏览器缓存）"""
    x0 = int(float(rect[0]))
    y0 = int(float(rect[1]))
    return f"r_{int(page)}_{x0}_{y0}"


def figure_id(index: int, image_item) -> str:
    """插图 id：用序号 + 图片自身 key，保证稳定且不重复"""
    key = re.sub(r"[^0-9A-Za-z]+", "", str(getattr(image_item, "key", "")))[:12]
    return f"fig_{index}_{key}" if key else f"fig_{index}"


def count_words(text: str) -> int:
    """与卡片标签口径一致的词数统计（英文按空白分词，够用）"""
    return len([w for w in re.split(r"\s+", (text or "").strip()) if w])


def settings_payload(settings: dict) -> dict:
    """
    解析设置的规范化 + 回显。

    **字段名与 Streamlit 版完全一致**（`merge_on / dehyphenate_on / target_words /
    table_mode_label / show_all`），因为设置也写进同一份 `.cache/` 记录——两个前端读同一份，
    换前端不会"设置突然全变了"。
    """
    settings = settings or {}
    label = settings.get("table_mode_label")
    if label not in ("截图（推荐）", "保留文字"):
        label = "保留文字" if settings.get("table_mode") == "text" else "截图（推荐）"
    try:
        words = int(settings.get("target_words", 200))
    except (TypeError, ValueError):
        words = 200
    return {
        "merge_on": bool(settings.get("merge_on", True)),
        "dehyphenate_on": bool(settings.get("dehyphenate_on", True)),
        "target_words": min(max(words, 100), 400),
        "table_mode_label": label,
        "table_mode": "text" if label == "保留文字" else "image",
        "show_all": bool(settings.get("show_all", False)),
    }


def paragraph_cards(parse_result, registry) -> list:
    """
    「显示全部内容」核对模式：把**所有段落**（含页眉页脚、作者单位、参考文献）都列成卡片。

    这个模式的用途是核对"过滤有没有误杀真正文"，所以保持原文、不做翻译、也不做区域截图，
    只在标签上标明它的角色（正文/图注/页眉页脚/参考文献…）。
    """
    from utils.pdf_parser import ROLE_NAMES

    items = []
    for index, paragraph in enumerate(parse_result.get("paragraphs", [])):
        role = getattr(paragraph, "role", "body")
        items.append({
            "i": index,
            "number": "",
            "section": ROLE_NAMES.get(role, role),
            "page": int(getattr(paragraph, "page", 0) or 0),
            "page_end": int(getattr(paragraph, "page", 0) or 0),
            "words": count_words(getattr(paragraph, "text", "")),
            "order": int(getattr(paragraph, "order", index) or index),
            "segments": [{"kind": "text", "text": getattr(paragraph, "text", "")}],
            "figures": [],
            "is_formula": False,
            "is_table": False,
        })
    return items


def card_payload(card, images_by_card, region_registry) -> dict:
    """
    一张卡片 → JSON。

    segments 里：heading / text 原样给文本（前端按语言决定显示原文还是译文）；
    formula / table 给区域截图的 id；插图（该卡关联的论文图）作为 kind="figure" 插在正文后面。
    """
    segments = []
    for kind, payload in (card.segments or []):
        if kind in TEXT_KINDS:
            segments.append({"kind": kind, "text": payload})
        elif kind in REGION_KINDS:
            ident = region_id(payload.get("page", 0), payload.get("rect", (0, 0, 0, 0)))
            region_registry[ident] = {"type": "region", "kind": kind, "payload": payload}
            meta = f"原文第 {payload.get('page', '?')} 页 · " + ("表格区域" if kind == "table"
                                                              else "公式区域") + " · 原文截图"
            caption = (payload.get("caption") or "").strip()
            if caption:
                meta += f"（{caption[:60]}）"
            segments.append({"kind": kind, "id": ident, "meta": meta})

    # 该卡片对应的论文插图，统一放在正文之后（与 Streamlit 版的呈现一致）
    figures = []
    for image in images_by_card.get(card.order, []):
        ident = image["id"]
        region_registry[ident] = {"type": "figure", "image": image["item"]}
        segments.append({"kind": "figure", "id": ident, "meta": image["meta"]})
        figures.append(ident)

    if not segments and (card.text or "").strip():
        segments.append({"kind": "text", "text": card.text})

    return {
        "i": int(getattr(card, "card_index", 0)),
        "number": getattr(card, "section_number", "") or "",
        "section": getattr(card, "section_title", "") or "",
        "page": int(getattr(card, "page", 0) or 0),
        "page_end": int(getattr(card, "page_end", getattr(card, "page", 0)) or 0),
        "words": count_words(getattr(card, "text", "")),
        "order": int(getattr(card, "order", 0) or 0),
        "segments": segments,
        "figures": figures,
        "is_formula": bool(getattr(card, "is_formula", False)),
        "is_table": bool(getattr(card, "is_table", False)),
    }


def comparison_payload(rows) -> list:
    """
    把 metadata_compare 的对照结果整理成前端表格。

    只保留前端要用的列，并且**把「会话键」原样带出去**——它就是 `in_xxx` 这套存储键，
    采信时直接回传，服务端据此写入人工修正（两个前端同一套键）。
    """
    out = []
    for row in rows or []:
        out.append({
            "field": row.get("字段", ""),
            "local": str(row.get("本地", ""))[:200],
            "grobid": str(row.get("GROBID", ""))[:200],
            "verdict": row.get("结论", ""),
            "actionable": bool(row.get("可采信")),
            "is_list": row.get("类型") == "list",
            "store_key": row.get("会话键", ""),
            "raw": str(row.get("GROBID原始", row.get("GROBID", "")))[:400],
            "added": row.get("新增", []),
        })
    return out


def metadata_payload(metadata, edits=None) -> dict:
    """
    元数据十个字段：**人工修正优先**，同时把自动提取的原值也带上。

    前端据此可以：① 显示你改过的值；② 标出哪些是被你改过的（`edited`）；③ 一键"还原成
    自动提取结果"（`auto` 字段，不需要再问服务端）。
    """
    edits = edits or {}
    specs = (
        ("title", "标题", metadata.title),
        ("doi", "DOI", metadata.doi),
        ("abstract", "摘要", metadata.abstract),
        ("authors", "作者列表", metadata.authors),
        ("first_author", "第一作者", metadata.first_author),
        ("corresponding", "通讯作者", metadata.corresponding_author),
        ("corresponding_email", "通讯邮箱", metadata.corresponding_email),
        ("affiliations", "作者单位", metadata.affiliations),
        ("supplementary", "附件链接", metadata.supplementary_links),
        ("author_emails", "作者邮箱", metadata.author_emails),
    )
    out = {}
    for key, label, field in specs:
        store_key = EDIT_KEYS[key]
        auto_value = field.value or ""
        edited = store_key in edits
        out[key] = {
            "label": label,
            "value": edits.get(store_key, auto_value),
            "auto": auto_value,
            "edited": edited,
            "store_key": store_key,
            "source": getattr(field, "source", "") or "",
            "found": bool(getattr(field, "found", False)),
            "note": getattr(field, "note", "") or "",
        }
    links = getattr(metadata, "supplementary_items", None) or []
    out["supplementary_items"] = [
        {"type": item.get("类型", ""), "page": item.get("页码", ""), "url": item.get("链接", "")}
        for item in links
    ]
    return out


def _card_text(card) -> str:
    """卡片可能是引擎对象，也可能是已经整理好的 dict（「显示全部内容」模式）"""
    if isinstance(card, dict):
        # 注意要用空格连接：直接拼会造出 "IntroductionHello" 这种假词，词数就少算了（实测差 1 个词）
        return card.get("text") or " ".join(seg.get("text", "") for seg in card.get("segments", []))
    return getattr(card, "text", "")


def overview_payload(result, cards, images, table_regions) -> dict:
    """右栏「概览」用的数字（口径与 Streamlit 版一致）"""
    pages = result.get("pages", [])
    return {
        "pages": len(pages),
        "cards": len(cards),
        "words": sum(count_words(_card_text(card)) for card in cards),
        "images": len(images),
        "formula_regions": len(result.get("formula_clusters", [])),
        "table_regions": len(table_regions),
        "elapsed": result.get("elapsed", 0.0),
        "body_size": result.get("body_size", 0.0),
        "total_chars": result.get("total_chars", 0),
        "layouts": " / ".join(f"第{p['页码']}页 {p['检测排版']}" for p in pages),
    }


def build_paper_payload(pdf_bytes, file_name, parse_result, image_result, association,
                        metadata, position=0, cache_hit=False, backend=None, target="zh",
                        edits=None, settings=None) -> dict:
    """
    汇总成一次 `/api/open` 的响应。

    返回的 `registry`（id → 图片来源）**不进 JSON**，由调用方留在服务端，
    供 `/api/img/<id>` 取图时查表用。
    """
    settings = settings_payload(settings)
    # 「显示全部内容」核对模式：把全部段落列成卡片（含页眉页脚/参考文献），保持原文不翻译
    cards = (paragraph_cards(parse_result, {}) if settings["show_all"]
             else parse_result["cards"])
    images = image_result.get("images", [])
    table_regions = parse_result.get("table_regions", [])

    registry = {}
    images_by_card = {}
    for index, image in enumerate(images):
        ident = figure_id(index, image)
        card_order = getattr(image, "card_order", None)
        meta = (f"第 {image.page} 页 · {image.pixel_w}×{image.pixel_h} px"
                + (f" · 关联卡片 #{card_order}" if card_order else " · 未关联卡片"))
        images_by_card.setdefault(card_order, []).append({"id": ident, "item": image, "meta": meta})

    # 「显示全部内容」模式下，卡片直接由段落整理而成（已经就是前端要的形状）；
    # 正常模式才走 card_payload 组装（含公式/表格区域 id 与插图）
    show_all = bool(settings.get("show_all"))
    if show_all:
        payload_cards = paragraph_cards(parse_result, registry)
    else:
        payload_cards = [card_payload(card, images_by_card, registry)
                         for card in parse_result["cards"]]

    key = store.paper_key(pdf_bytes)
    stats = store.cache_stats()
    return {
        "file": {"name": file_name, "size": len(pdf_bytes)},
        "key": key,
        "cache_hit": bool(cache_hit),
        "position": int(position or 0),
        "settings": settings_payload(settings),
        "show_all": show_all,
        "overview": overview_payload(parse_result, payload_cards, images, table_regions),
        "metadata": metadata_payload(metadata, edits),
        "cards": payload_cards,
        "backend": backend,
        "target": target,
        "cache": {"papers": stats.get("papers", 0),
                  "translations": stats.get("translations", 0),
                  "kb": round((stats.get("bytes", 0) or 0) / 1024, 1),
                  "root": stats.get("root", "")},
        "_registry": registry,       # 给服务端用，序列化前会去掉
    }
