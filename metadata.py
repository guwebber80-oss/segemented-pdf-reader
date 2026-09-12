"""
文献元数据提取模块 —— 阶段 4.1：标题、DOI、摘要
================================================================
本模块【不依赖 Streamlit】，可以脱离网页单独运行、单独测试。

设计原则（用户明确要求过）：
    **准确性优先于自动化**。提取结果一律只当「候选」，
    界面上所有字段都可手动编辑，并且每个字段都会标明来源，
    让用户一眼看出哪些是可靠的、哪些只是推测。

三个字段各自的策略（都是拿三篇真实论文摸出来的）：

  标题  第一页做字号分析：取「标题区」里字号最大的一行，
        并把它上下紧邻的同字号行并进来（标题常折成 2~3 行）；
        用黑名单排除期刊名、栏目名（Report / Highlights / Authors…）。

  DOI   按可靠性依次尝试：第一页的超链接 → 第一页正文正则 →
        PDF 内嵌元数据 → 全文正则。全文正则很危险（参考文献里有几十个 DOI），
        所以只在「同一个 DOI 出现在前两页」时才采纳——那是页眉页脚，
        基本可以断定是本文自己的 DOI。

  摘要  先找摘要标题（Abstract / SUMMARY / 摘要），再按阅读顺序往下收集，
        遇到 Keywords / Introduction / RESULTS / 版权行等就停；
        没有标题的（例如 ACM 的裸摘要）退化为「第一页第一段长正文」，
        并明确标注这是推测结果。
"""

import re
from dataclasses import dataclass, field

import pymupdf

# ------------------------------------------------------------
# 正则
# ------------------------------------------------------------

# DOI 的标准形式：10.xxxx/后面一串允许的字符
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")

# 标题里不该出现的词：期刊名、栏目名、版权行、页面元素名
_TITLE_BLOCKLIST_RE = re.compile(
    r"\b(report|article|review|letter|editorial|commentary|perspective|"
    r"communication|brief report|research article|original article|"
    r"highlights|authors|correspondence|in brief|abstract|summary|keywords|"
    r"journal|volume|vol\.|issue|issn|isbn|doi|http|www\.|"
    r"copyright|open access|cell press|elsevier|springer|wiley|acm|ieee)\b",
    re.IGNORECASE,
)

# 摘要标题：单独成行的 Abstract / SUMMARY（允许字母间被排版拆开的 A B S T R A C T）
_ABSTRACT_HEADING_RE = re.compile(
    r"^[\s\W]*(abstract|a\s?b\s?s\s?t\s?r\s?a\s?c\s?t|summary)[\s:：.\-—]*$",
    re.IGNORECASE,
)

# 摘要的结束标志
_ABSTRACT_STOP_RE = re.compile(
    r"^[\s\W·•\-–—]*("
    r"keywords?|key\s?words|introduction|background|highlights|graphical abstract|"
    r"results?|discussion|methods?|materials and methods|conclusions?|"
    r"ccs concepts|additional key words|acm reference format|references|"
    r"this work is licensed|creative commons|copyright|acknowledge?ments?|"
    r"funding|author contributions|declaration of interests|data availability"
    r")\b",
    re.IGNORECASE,
)

# 看着像图注 / 版权 / 页眉页脚的行，不能当摘要
_NOT_ABSTRACT_RE = re.compile(
    r"^[\s\W]*(fig(ure)?\.?\s*\d|table\s*\d|scheme\s*\d|"
    r"this (project|work|article) (has been|is)|©|ª|http|www\.|doi)",
    re.IGNORECASE,
)


@dataclass
class Field:
    """一个元数据字段：值 + 来源 + 备注"""
    value: str = ""
    source: str = "未找到"
    note: str = ""
    alternatives: list = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.value.strip())


@dataclass
class Metadata:
    title: Field = field(default_factory=Field)
    doi: Field = field(default_factory=Field)
    abstract: Field = field(default_factory=Field)

    def as_dict(self) -> dict:
        return {"title": self.title, "doi": self.doi, "abstract": self.abstract}


# ------------------------------------------------------------
# 第一页的行级信息
# ------------------------------------------------------------

def _page_lines(page):
    """
    取一页里所有文本行，带上字号、加粗占比、坐标。
    标题识别必须用行级信息：同一行内字号一致，但标题常常折成多行。
    """
    lines = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(span.get("text", "") for span in spans).strip()
            if not text:
                continue
            sizes = [float(s.get("size", 0)) for s in spans if s.get("text", "").strip()]
            total = sum(len(s.get("text", "")) for s in spans if s.get("text", "").strip()) or 1
            bold = sum(len(s.get("text", "")) for s in spans if int(s.get("flags", 0)) & 16)
            bbox = line["bbox"]
            lines.append({
                "text": text,
                "size": max(sizes) if sizes else 0.0,
                "bold": bold / total,
                "x0": bbox[0], "y0": bbox[1], "x1": bbox[2], "y1": bbox[3],
                "height": bbox[3] - bbox[1],
            })
    lines.sort(key=lambda r: (r["y0"], r["x0"]))
    return lines


def _join_title_lines(lines) -> str:
    """把标题的几行拼起来：行尾断词要连起来，其余用空格"""
    out = ""
    for line in lines:
        text = line["text"].strip()
        if not text:
            continue
        if not out:
            out = text
        elif out[-1] in "-‐\xad" and text[:1].islower():
            out = out[:-1] + text
        else:
            out = f"{out} {text}"
    return re.sub(r"\s+", " ", out).strip()


def extract_title(page, doc_metadata: dict = None) -> Field:
    """
    用字号分析从第一页找标题。

    做法：只看「标题区」（页面上部 55%），挑出字号最大且不在黑名单里的行，
    再把它上下紧邻的同字号行（字号差 < 0.8pt）并进来。
    """
    if page is None:
        return Field()

    lines = [ln for ln in _page_lines(page) if ln["size"] > 0]
    if not lines:
        return Field()

    zone_bottom = page.rect.height * 0.55
    zone = [ln for ln in lines if ln["y0"] <= zone_bottom] or lines

    # 候选行：不在黑名单里、不是纯数字/纯符号、长度合理
    candidates = []
    for line in zone:
        text = line["text"].strip()
        if len(text) < 6 or _TITLE_BLOCKLIST_RE.search(text):
            continue
        if not re.search(r"[A-Za-z]{3,}", text):
            continue
        candidates.append(line)

    if not candidates:
        return _title_from_metadata(doc_metadata)

    biggest = max(ln["size"] for ln in candidates)
    same_size = [ln for ln in candidates if biggest - ln["size"] < 0.8]

    # 按 y 排序后，把彼此紧邻的行连起来（标题折行时行距很小）
    same_size.sort(key=lambda r: r["y0"])
    group = [same_size[0]]
    for line in same_size[1:]:
        gap = line["y0"] - group[-1]["y1"]
        if gap <= 1.6 * max(group[-1]["height"], 1.0):
            group.append(line)
        else:
            break        # 离得太远，说明是另一处同字号文字（作者行之类），不要并

    title = _join_title_lines(group)
    alternatives = []
    if doc_metadata and doc_metadata.get("title"):
        meta_title = str(doc_metadata["title"]).strip()
        if meta_title and meta_title.lower() != title.lower():
            alternatives.append(meta_title)

    if len(title) < 12 or len(title.split()) < 3:
        return _title_from_metadata(doc_metadata, note="第一页字号最大的一行不像标题")

    return Field(value=title, source=f"字号分析（{biggest:.1f}pt，第 1 页）", alternatives=alternatives)


def _title_from_metadata(doc_metadata: dict, note: str = "") -> Field:
    """兜底：用 PDF 内嵌元数据里的标题"""
    meta_title = str((doc_metadata or {}).get("title") or "").strip()
    if meta_title and len(meta_title) > 8:
        return Field(value=meta_title, source="PDF 内嵌元数据", note=note)
    return Field(note=note or "第一页没有找到明显的标题行")


# ------------------------------------------------------------
# DOI
# ------------------------------------------------------------

def _clean_doi(raw: str) -> str:
    """去掉 DOI 尾巴上多出来的标点（参考文献里常见 'doi:10.xxx.' 这种）"""
    doi = raw.strip().rstrip(".,;:)]}>'\"")
    return doi


def _doi_from_links(doc, pages=(1, 2)):
    """从超链接里找 doi.org 链接——这是最可靠的来源"""
    for page_number in pages:
        if page_number > doc.page_count:
            continue
        for link in doc[page_number - 1].get_links():
            uri = str(link.get("uri") or "")
            match = DOI_RE.search(uri)
            if match:
                return _clean_doi(match.group(0)), f"第 {page_number} 页超链接"
    return None, None


def _doi_from_text(doc, pages=(1, 2)):
    """正文正则。要求同一个 DOI 出现在前两页——那基本是页眉页脚里的本文 DOI"""
    hits = {}
    for page_number in pages:
        if page_number > doc.page_count:
            continue
        for match in DOI_RE.findall(doc[page_number - 1].get_text()):
            doi = _clean_doi(match)
            hits.setdefault(doi, []).append(page_number)

    if not hits:
        return None, None

    # 出现在最多页面的那个胜出；并列时取更长的（更完整）
    best = sorted(hits.items(), key=lambda kv: (-len(kv[1]), -len(kv[0])))
    doi, pages_seen = best[0]
    if len(pages_seen) >= 2:
        return doi, f"前两页交叉验证（第 {'、'.join(map(str, pages_seen))} 页都出现）"
    return doi, f"第 {pages_seen[0]} 页正文"


def extract_doi(doc) -> Field:
    if doc is None or doc.page_count == 0:
        return Field()

    # ① 超链接
    doi, source = _doi_from_links(doc)
    if doi:
        return Field(value=doi, source=source)

    # ② PDF 内嵌元数据（Elsevier 常把 doi 写在 Subject 字段里）
    metadata = doc.metadata or {}
    for key in ("subject", "keywords", "title"):
        match = DOI_RE.search(str(metadata.get(key) or ""))
        if match:
            return Field(value=_clean_doi(match.group(0)), source=f"PDF 元数据的 {key} 字段")

    # ③ 前两页正文
    doi, source = _doi_from_text(doc)
    if doi:
        field_value = Field(value=doi, source=source)
        if "交叉验证" not in source:
            field_value.note = "只在一页里找到，请核对是否确实是本文 DOI"
        return field_value

    return Field(note="没有找到 DOI，请手动填写")


# ------------------------------------------------------------
# 摘要
# ------------------------------------------------------------

MAX_ABSTRACT_WORDS = 450
MAX_ABSTRACT_BLOCKS = 8


def _fallback_abstract(blocks):
    """
    没有摘要标题时的退路：取第一页第一段「够长的正文」。
    排除图注、版权行、作者单位行、页眉页脚。
    """
    for block in blocks:
        if block.page != 1 or block.kind == "heading":
            continue
        text = block.text.strip()
        words = len(text.split())
        if words < 40:
            continue
        if _NOT_ABSTRACT_RE.match(text) or _TITLE_BLOCKLIST_RE.match(text):
            continue
        if "@" in text or text.count(",") > words / 6:
            continue                      # 邮箱、作者列表
        if words > 400:
            continue
        return text, block
    return None, None


def extract_abstract(blocks) -> Field:
    """
    提取摘要。blocks 是 pdf_parser.parse_pdf 出来的、已按阅读顺序排好的卡片列表。
    """
    if not blocks:
        return Field()

    heading_index = None
    for index, block in enumerate(blocks):
        text = block.text.strip()
        if len(text) > 60:
            continue
        if _ABSTRACT_HEADING_RE.match(text):
            heading_index = index
            break

    if heading_index is None:
        # 有可能是「标题和正文粘在一个块里」的情况（SUMMARY in which…）
        for index, block in enumerate(blocks):
            head = block.text.strip()[:80]
            match = _ABSTRACT_HEADING_RE.match(head.split(".")[0] + ".")
            if match and len(block.text.split()) > 8:
                heading_index = index
                break

    if heading_index is not None:
        pieces, words, used = [], 0, 0
        for block in blocks[heading_index + 1:]:
            text = block.text.strip()
            if not text:
                continue
            if _ABSTRACT_STOP_RE.match(text) or block.kind == "heading":
                break
            if _NOT_ABSTRACT_RE.match(text):
                break
            pieces.append(text)
            words += len(text.split())
            used += 1
            if words >= MAX_ABSTRACT_WORDS or used >= MAX_ABSTRACT_BLOCKS:
                break

        if pieces:
            note = ""
            if used >= MAX_ABSTRACT_BLOCKS or words >= MAX_ABSTRACT_WORDS:
                note = "内容较多，可能多收了后面的段落，请核对"
            return Field(value=" ".join(pieces),
                         source=f"摘要标题定位（{'/'.join([b.text.strip()[:12] for b in blocks[heading_index:heading_index+1]])}）",
                         note=note)

    # 退路：没有摘要标题
    text, block = _fallback_abstract(blocks)
    if text:
        return Field(
            value=text,
            source="推测：第一页第一段长正文（本文没有摘要标题）",
            note="这篇 PDF 的摘要没有标题，上面是程序推测的结果，请务必核对",
        )

    return Field(note="没有找到摘要，请手动填写")


# ------------------------------------------------------------
# 总入口
# ------------------------------------------------------------

def extract_metadata(pdf_bytes: bytes, blocks=None) -> Metadata:
    """
    提取标题 / DOI / 摘要。

    blocks 传入 parse_pdf 的结果可以省一次解析；不传就自己解析一遍。
    """
    if blocks is None:
        from pdf_parser import parse_pdf
        blocks = parse_pdf(pdf_bytes, merge_on=True, dehyphenate=True)["blocks"]

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        if doc.needs_pass:
            return Metadata(title=Field(note="PDF 有密码保护"),
                            doi=Field(note="PDF 有密码保护"),
                            abstract=Field(note="PDF 有密码保护"))
        first_page = doc[0] if doc.page_count else None
        return Metadata(
            title=extract_title(first_page, doc.metadata),
            doi=extract_doi(doc),
            abstract=extract_abstract(blocks),
        )
    finally:
        doc.close()
