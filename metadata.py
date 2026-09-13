"""
文献元数据提取模块 —— 阶段 4.1 / 4.2：标题、DOI、摘要、作者、单位、通讯作者、附件链接
================================================================
本模块【不依赖 Streamlit】，可以脱离网页单独运行、单独测试。

设计原则（用户明确要求过）：
    **准确性优先于自动化**。提取结果一律只当「候选」，
    界面上所有字段都可手动编辑，并且每个字段都会标明来源，
    让用户一眼看出哪些是可靠的、哪些只是推测。

字段各自的策略（都是拿真实论文摸出来的）：

  标题  第一页做字号分析：取「标题区」里字号最大的一行，
        并把它上下紧邻的同字号行并进来（标题常折成 2~3 行）；
        用黑名单排除期刊名、栏目名（Report / Highlights / Authors…）。

  DOI   按可靠性依次尝试：第一页的超链接 → PDF 内嵌元数据（Elsevier 写在 Subject）→
        前两页正文正则。**刻意不做全文正则**（参考文献里有几十个 DOI），
        只在「同一个 DOI 出现在前两页」时才采纳——那是页眉页脚，基本可断定是本文 DOI。

  摘要  先找摘要标题（Abstract / SUMMARY / 摘要），再按阅读顺序往下收集，
        遇到 Keywords / Introduction / RESULTS / 版权行等就停；
        没有标题的（例如 ACM 的裸摘要）退化为「第一页第一段长正文」，并标注为推测。

  作者  第 1 页标题与摘要之间的「作者区」，按版面坐标（先 y 后 x）取候选块，
        拆开逗号 / and / & 分隔的人名，剔除单位、邮箱、日期、栏目名。
        为什么按坐标排序：ACM 的作者是三列网格排版（名字与单位交错），
        解析器的阅读顺序会把第二列的名字排到第一列前面。

  单位  作者区内含单位关键词（University / Institute / Department / School…）的块，
        按编号标记（¹ ² ³ / 1 2 3）切分条目；Nature 那类把单位放在页脚小字里的，
        也会去第一页页脚找。

  通讯  Priority ①：显式的通讯段落（*CORRESPONDENCE / Corresponding Author: /
        Correspondence concerning… addressed to / Corresponding author at: …）；
        ② 由邮箱反查作者（antti.oulasvirta@aalto.fi → Antti Oulasvirta）；
        ③ 都没有就如实报「未找到」，**绝不猜**。

  附件  全页扫描链接注释与正文里印出的 URL，用关键词（supplementary / supporting
        information / data availability / OSF / figshare…）与仓库域名（osf.io /
        github / zenodo…）双重过滤，剔除出版社固定链接（许可协议、crossmark、
        期刊主页、本文 DOI），并保留锚文本与所在块作为证据；
        正文声明「有补充材料」但没有链接时，也如实记一条声明。
"""

import re
import unicodedata
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
    # ---- 阶段 4.2 ----
    authors: Field = field(default_factory=Field)             # 每行一个作者，保持原文顺序
    first_author: Field = field(default_factory=Field)
    corresponding_author: Field = field(default_factory=Field)
    corresponding_email: Field = field(default_factory=Field)
    affiliations: Field = field(default_factory=Field)        # 每行一个单位，保留编号标记
    author_emails: Field = field(default_factory=Field)       # 作者邮箱（ACM 这类逐作者列出）
    supplementary_links: Field = field(default_factory=Field)  # 每行一个链接（或声明）
    supplementary_items: list = field(default_factory=list)    # 证据明细：[{类型, 链接, 页码, 上下文}]

    def as_dict(self) -> dict:
        return {"title": self.title, "doi": self.doi, "abstract": self.abstract,
                "authors": self.authors, "first_author": self.first_author,
                "corresponding_author": self.corresponding_author,
                "affiliations": self.affiliations,
                "supplementary_links": self.supplementary_links}

    def author_list(self) -> list:
        """作者列表（去空行）"""
        return [line.strip() for line in self.authors.value.splitlines() if line.strip()]

    def affiliation_list(self) -> list:
        return [line.strip() for line in self.affiliations.value.splitlines() if line.strip()]


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
# 阶段 4.2：作者 / 单位 / 通讯作者 / 作者邮箱 / 附件链接
# ------------------------------------------------------------

# 作者名后面的单位编号标记：Unicode 上下标 + ^/_ 回退记号 + 星号剑号
_SUPERSCRIPTS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ"
_SUBSCRIPTS = "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"
# 注意 `^1,2` 这种回退记号的数字之间也有逗号——**不能**把逗号后面的作者分隔符一起吃掉，
# 否则 "Yunpeng Bai ^1,2, Xiaofu Jin" 会被粘成一个人名。所以数字标记只吃到
# 「逗号后紧跟数字」为止，逗号后是空格+字母时停下（那是作者之间的分隔符）。
_MARK_RE = re.compile(
    r"[\*†‡§¶]"
    r"|[\^_]\s*\d{1,2}(?:\s*[,–\-]\s*\d{1,2})*"
    r"|[\^_]\s*[A-Za-z]"
    r"|[\^_]"
    r"|[" + _SUPERSCRIPTS + _SUBSCRIPTS + r"]")
# 有些排版把单位编号渲染成普通字号的数字（"Antti Oulasvirta 2,6"），也要去掉
_TRAILING_MARKER_RE = re.compile(r"[\s,;]*(?:\d{1,2}[a-z]?)(?:\s*[,–\-]\s*\d{1,2}[a-z]?)*\s*$")

# 单位关键词（用来认单位、也用来把「名字 + 单位挤在一块」的块截断）
_INSTITUTION_RE = re.compile(
    r"\b(universit(?:y|ies|ät|é|à|ad)|institut\w*|department|dept\.?|school|college|faculty|"
    r"laborator\w*|laboratoire|centre|center|academy|hospital|clinic|hochschule|gmbh|"
    r"ltd\.?|inc\.?|corporation|research group)\b", re.IGNORECASE)

# 人名里不该出现的词（栏目标签、日期、单位词……）
_NON_NAME_WORDS = {
    "the", "and", "of", "for", "from", "with", "in", "on", "at", "to", "by", "an",
    "university", "universities", "institute", "institution", "department", "dept",
    "school", "college", "faculty", "center", "centre", "laboratory", "lab",
    "academy", "hospital", "clinic", "journal", "volume", "vol", "issue", "doi",
    "abstract", "summary", "keywords", "introduction", "received", "accepted",
    "published", "revised", "available", "online", "copyright", "citation",
    "correspondence", "corresponding", "author", "authors", "email", "edited",
    "reviewed", "report", "article", "research", "original", "check", "updates",
    "open", "access", "supplementary", "supplemental", "conflict", "interest",
    "funding", "acknowledgements", "contributions", "data", "code", "materials",
}
# 姓氏前缀（不能被当成名字的首字母缩写）
_NAME_PARTICLES = {"van", "von", "de", "del", "della", "der", "den", "di", "da", "dos",
                   "du", "la", "le", "ter", "ten", "bin", "ibn", "al", "el", "st",
                   "mac", "mc", "i", "y"}
_NAME_TOKEN_RE = re.compile(r"^[A-Z][A-Za-z'’\-]*\.?$")
# 学科 / 院系词：**整串都是**这类词时就不当人名。
# 真实案例：ACM CHI 那篇的作者网格里，"Media Engineering Technology"（院系名）
# 被版面切成单独一块，不含任何单位关键词（University/Institute…），
# 于是被人名规则放行、混进了作者列表。
# 判据是「全部 token 都是学科词」而不是「含一个学科词」——
# 否则像 "Andrew Law" 这类真名会被误杀。
_FIELD_WORDS = {
    "technology", "technologies", "engineering", "media", "science", "sciences",
    "computing", "communications", "communication", "design", "arts", "art",
    "studies", "systems", "informatics", "interaction", "psychology",
    "biology", "medicine", "health", "education", "business", "physics",
    "chemistry", "mathematics", "statistics", "humanities", "architecture",
    "management", "economics", "neuroscience", "linguistics", "anthropology",
    "sociology", "philosophy", "division",
}
# 名字后缀（全大写转正常大小写时要原样保留）
_NAME_SUFFIXES = {"II", "III", "IV", "V", "VI", "JR", "SR"}
# 被排版拆开的重音符号：Cell Press 那篇把 "Angéline" 排成了 "Ange´ lina"
# （尖音符单独成一个字符）。把「字母 + ´/` + 空格」合成组合尖音符 U+0301，
# 再做 NFC 合成就能还原成 é。用 (?<=[A-Za-z]) 保证只在字母后面动手。
_SPLIT_ACCENT_RE = re.compile(r"(?<=[A-Za-z])\s*[´`]\s*")

# 通讯作者线索
_CORRESPONDENCE_RE = re.compile(
    r"(correspond\w*|to whom correspondence|reprint requests?|contact\s+author)",
    re.IGNORECASE)
_CORRESPONDENCE_LABEL_RE = re.compile(
    r"^\s*[\*\u2020\u2021\s]*"
    r"(?:correspond(?:ing|ence)s?\s*(?:author|authors)?(?:\s+at)?|"
    r"correspondence concerning[^.]{0,60}?addressed to|"
    r"reprint requests?[^.]{0,40}?to|contact)\s*[:.\-]?\s*",
    re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_EMAIL_LABEL_RE = re.compile(r"e-?mail(?:\s+address)?\s*[:：]\s*", re.IGNORECASE)

# 附件 / 数据链接
_REPOSITORY_RE = re.compile(
    r"(osf\.io|github\.com|gitlab\.com|figshare|zenodo|dryad|icpsr|dataverse|"
    r"aspredicted|clinicaltrials\.gov|10\.15154/|10\.17605/|10\.6084/|10\.5061/)",
    re.IGNORECASE)
_SUPP_KEYWORD_RE = re.compile(
    r"(supplement\w*|supporting\s+(?:information|material|data)|extended\s+data|"
    r"additional\s+file|data\s+availab\w*|open\s+science|preregist\w*|"
    r"materials?\s+availab\w*|code\s+availab\w*|dataset|补充材料|数据可得)",
    re.IGNORECASE)
_SUPP_STATEMENT_RE = re.compile(
    r"(?:supplementary|supporting|supplemental|extended\s+data)\s+"
    r"(?:material|information|data|tables?|figures?)"
    r"[^.]{0,60}?\b(?:available|can be found|is provided|are provided|"
    r"for this (?:article|manuscript|paper))", re.IGNORECASE)
_BOILERPLATE_URL_RE = re.compile(
    r"(creativecommons|crossmark|/permissions|journals?[.-]permissions|sagepub\.com|"
    r"/home/|locate/|reprints|orcid\.org|refhub\.|sciencedirect\.com|elsevier\.com|"
    r"nature\.com|wiley\.com|springer|tandfonline|onlinelibrary|frontiersin\.org/journals|"
    r"editorial-board|/about|/contact|/help|/content/|scispace\.com|/census/|"
    r"/topics/|/en-us/|/journals/)", re.IGNORECASE)
_SELF_DOI_RE = re.compile(r"(?:doi\.org|dx\.doi\.org)/10\.", re.IGNORECASE)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _repair_split_accents(text: str) -> str:
    """
    还原被排版拆开的重音字母："Ange´ lina" → "Angéline"（NFC 合成）。
    不这样做的话，含重音的姓名会整段判不出人名（实测 Cell Press 那篇漏了 Angéline Vernetti）。
    """
    return unicodedata.normalize("NFC", _SPLIT_ACCENT_RE.sub("\u0301", text or ""))


def _strip_markers(text: str) -> str:
    """去掉作者名后面的单位标记（¹ ² ³ / ^1,2 / * / †…）"""
    text = _MARK_RE.sub(" ", _repair_split_accents(text))
    text = _TRAILING_MARKER_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,;.·•")


def _cut_at_institution(text: str) -> str:
    """
    名字后面直接粘着单位（ACM 的三列网格排版：
    "Yanxia Li School of Computer Science Queensland University of Technology"）
    → 在单位关键词处截断，只留名字。
    """
    match = _INSTITUTION_RE.search(text)
    return text[:match.start()].strip(" ,;.") if match else text.strip(" ,;.")


def looks_like_person_name(text: str) -> bool:
    """这段文字像不像一个人名（2~5 个词、首字母大写、不含单位与数字）"""
    text = _cut_at_institution(_strip_markers(text))
    if not text or "@" in text or any(ch.isdigit() for ch in text):
        return False
    tokens = text.split()
    if not 2 <= len(tokens) <= 5:
        return False
    meaningful = [token.strip(".,'’").lower() for token in tokens
                  if token.strip(".,'’").lower() not in _NAME_PARTICLES]
    if meaningful and all(word in _FIELD_WORDS for word in meaningful):
        return False                  # 整串都是学科/院系词（"Media Engineering Technology"）
    for token in tokens:
        clean = token.strip(".,'’")
        if not clean:
            return False
        lowered = clean.lower()
        if lowered in _NAME_PARTICLES:
            continue
        if lowered in _NON_NAME_WORDS:
            return False
        # 首字母必须大写；允许重音字母（Angéline、Émile）
        if not clean[:1].isupper():
            return False
        # 只允许字母（含重音）、撇号、连字符
        if not re.fullmatch(r"[\w'’\-]+", clean):
            return False
        if len(clean) == 1 and not token.endswith("."):
            return False              # 单个字母必须是缩写（带点）
    return True


def _tidy_name_case(text: str) -> str:
    """
    把全大写的作者名转成正常大小写（"LLOGARI CASAS" → "Llogari Casas"）。

    为什么要做：有些期刊（ACM Games 那篇）作者名用小型大写字母排版，
    PDF 取出来整串没有小写字母；和其他样本混在一起显示很扎眼，
    也不方便与 GROBID 的结果对照。**只在整串完全没有小写字母时才转**，
    所以 "Diane Pecher"、"Steve van Pelt" 这类原样保留。
    """
    if not text or any(ch.islower() for ch in text):
        return text
    words = []
    for word in text.split():
        if word.strip(".,") in _NAME_SUFFIXES:
            words.append(word)
        elif any(ch in word for ch in "'’-"):
            words.append(word.title())          # O'BRIEN → O'Brien、JEAN-LUC → Jean-Luc
        else:
            words.append(word.capitalize())
    return " ".join(words)


def split_name_list(text: str) -> list:
    """
    把一行作者列表拆成单个作者名。
    分隔符：逗号 / 分号 / " and " / " & "。
    带前缀的姓（"Steve van Pelt"）不含逗号，不会被切坏。
    """
    text = _strip_markers(_norm(text))
    names = []
    for part in re.split(r"\s*(?:,|;|\band\b|&)\s*", text, flags=re.IGNORECASE):
        candidate = _cut_at_institution(part)
        if looks_like_person_name(candidate):
            names.append(_tidy_name_case(candidate))
    return names


def _title_atom_index(blocks, title_value):
    """
    在第 1 页定位标题所在的原子块。

    取「文字里含标题开头、且字号最大」的那一块：
    单凭包含关系会认错——Frontiers 那篇的 "CITATION Howard AK, …(2026) Multiple dimensions of…"
    引用行里也含完整标题，字号却只有 7pt；标题是页面上字号最大的那块。
    """
    probe = _norm(title_value).lower()
    best = None
    if len(probe) >= 16:
        head = probe[:28]
        for index, block in enumerate(blocks):
            if block.page != 1 or head not in _norm(block.text).lower():
                continue
            if best is None or block.max_size > blocks[best].max_size:
                best = index
    if best is not None:
        return best
    for index, block in enumerate(blocks):
        if block.page == 1 and block.kind == "heading":
            if best is None or block.max_size > blocks[best].max_size:
                best = index
    return best


def _looks_like_affiliation_block(text: str) -> bool:
    """
    这一块像不像「单位列表」：以编号标记开头 + 含单位关键词。

    为什么要单独判它：Frontiers 把 3 个单位写成一个 40 词的小字块，
    如果按「≥40 词的长正文就是摘要起点」处理，这块会被当成摘要边界，
    于是单位 2、3 直接被排除在作者区之外（实测只留下第 1 个单位）。
    """
    if not _INSTITUTION_RE.search(text):
        return False
    return bool(re.match(r"^\s*[¹²³⁴⁵⁶⁷⁸⁹]|^\s*\d{1,2}\s*[A-Z(]", text))


def author_zone(blocks, title_value):
    """
    取第 1 页的作者区：标题下方、摘要（或第一段长正文）上方的所有块。

    按**纵向范围**取，不按阅读顺序取：ACM 的作者是三列网格，
    阅读顺序会把右栏的作者排到左栏正文之后，按顺序切会漏掉他们。
    下界取「摘要标题」或「第一段长正文（≥60 词、且不是单位列表）」里最靠上的那个。
    """
    start = _title_atom_index(blocks, title_value)
    if start is None:
        return [], "没有定位到标题，作者与单位无法自动定位"

    title = blocks[start]
    page_one = [block for block in blocks if block.page == 1]

    bottom = None
    for block in page_one:
        if block.y0 <= title.y1 - 2:
            continue
        text = block.text.strip()
        is_boundary = False
        if _ABSTRACT_HEADING_RE.match(text):
            is_boundary = True
        elif block.kind != "heading" and len(text.split()) >= 60 \
                and not _looks_like_affiliation_block(text):
            is_boundary = True
        if is_boundary:
            bottom = block.y0 if bottom is None else min(bottom, block.y0)
    if bottom is None:
        bottom = title.y1 + 320        # 没找到摘要：只看到标题下方一小段

    zone = [block for block in page_one
            if block is not title and title.y1 - 2 <= block.y0 < bottom]
    return zone, ""


def extract_authors(blocks, title_value):
    """
    提取作者列表。返回 (名字列表, 来源说明, 备注, 带坐标的名字表)。

    带坐标的第三项是给「作者邮箱配对」用的（ACM 那种逐作者列邮箱的排版）。
    排序按**版面坐标**（先 y 后 x）：ACM 的作者是三列网格，
    解析器的阅读顺序会把第二列的名字排到第一列前面。
    """
    zone, note = author_zone(blocks, title_value)
    if not zone:
        return [], "未找到", note, []

    try:
        from pdf_parser import estimate_body_size
        body_size = estimate_body_size(blocks)
    except Exception:
        body_size = 0.0

    candidates = []
    for block in zone:
        text = block.text.strip()
        if not text or "@" in text:
            continue
        if _CORRESPONDENCE_RE.search(text):
            continue
        if _ABSTRACT_HEADING_RE.match(text) or _TITLE_BLOCKLIST_RE.match(text):
            continue
        # 作者行通常比正文大、或者加粗；两条都不满足就不是作者行
        if body_size and block.max_size < body_size * 1.02 and block.bold_ratio < 0.5:
            continue

        if _INSTITUTION_RE.search(text):
            # 名字与单位挤在**同一块**里（ACM Games 那篇：
            # "LLOGARI CASAS, 3FINERY LTD, Biggar, United Kingdom of Great Britain…"）。
            # 早期实现看到单位词就整块丢弃，作者跟着一起没了（实测该样本作者数为 0）。
            # 正确做法：把「第一个逗号之前、单位关键词之前」的那一段切出来当候选人名。
            head = _cut_at_institution(re.split(r"[,;]", text)[0])
            for name in split_name_list(head):
                candidates.append((block.y0, block.x0, name))
            continue

        for name in split_name_list(text):
            candidates.append((block.y0, block.x0, name))

    if not candidates:
        return [], "未找到", "作者区里没有识别出人名，请手动填写", []

    # 版面坐标排序：同一行（y 差 ≤6pt）按 x 从左到右，再按行往下。
    # 同一个块里拆出来的名字必须保持原顺序——它们坐标相同，
    # 只用 (x, 名字) 排序会变成**按字母顺序**（实测把 Diane Pecher 那行打乱了）。
    candidates.sort(key=lambda item: item[0])
    ordered, row, row_y = [], [], None
    for sequence, (y0, x0, name) in enumerate(candidates):
        if row_y is None or abs(y0 - row_y) <= 6.0:
            row.append((x0, sequence, name))
            row_y = y0 if row_y is None else row_y
        else:
            ordered.extend((name, row_y, x) for x, _, name in sorted(row))
            row, row_y = [(x0, sequence, name)], y0
    ordered.extend((name, row_y, x) for x, _, name in sorted(row))

    positioned, unique = [], []
    for name, y0, x0 in ordered:
        if name.lower() in {existing.lower() for existing in unique}:
            continue
        unique.append(name)
        positioned.append((name, y0, x0))

    source = f"第 1 页作者区（{len(zone)} 个块，按版面坐标排序）"
    return unique, source, "", positioned


def _split_affiliation_entries(text: str) -> list:
    """
    把单位块按编号标记切成条目：
    "¹ Department of X, Singapore. ² Department of Y, Finland." → 两条。
    没有编号标记的（Elsevier / xlm 那类单行单位）整行算一条。
    """
    text = _norm(text)
    text = re.sub(r"\s*e-?mail(?:\s+address)?\s*[:：].*$", "", text, flags=re.IGNORECASE)
    marker_re = re.compile(r"(?:^|(?<=\s))([¹²³⁴⁵⁶⁷⁸⁹]|\d{1,2})\s*(?=[A-Z(])")
    matches = list(marker_re.finditer(text))
    if not matches:
        cleaned = text.strip(" ,;.")
        return [cleaned] if cleaned else []

    entries = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip(" ,;.")
        if body:
            entries.append(f"{match.group(1)} {body}")
    return entries


def extract_affiliations(blocks, title_value, doc=None):
    """
    提取单位列表。返回 (单位列表, 来源说明, 备注)。

    三个来源：
      ① 作者区里的单位块（Elsevier / SAGE / Frontiers / ACM / xlm 都在这里）；
      ② **第一页页脚**里以编号开头的小字块（Nature 把 ¹–⁶ 六个单位放在页脚）；
      ③ **第二页**里以编号开头的小字块（Cell Press / Current Biology 把单位挪到第 2 页，
         实测第 1 页一个单位关键词都没有）。
    页脚与第 2 页那两条都加了「必须编号开头 + 含单位关键词」的限制，
    否则会把致谢、基金声明、正文里以编号开头的句子当成单位。
    """
    zone, _ = author_zone(blocks, title_value)
    page_height = doc[0].rect.height if (doc is not None and doc.page_count) else 800.0

    pool = list(zone)
    extra_page_two = False
    for block in blocks:
        if block.page not in (1, 2):
            continue
        if block.page == 1 and block.y0 < page_height * 0.6:
            continue                      # 第 1 页只看页脚区，作者区那条已经在 zone 里了
        text = block.text.strip()
        if not _looks_like_affiliation_block(text):
            continue
        pool.append(block)
        if block.page == 2:
            extra_page_two = True

    entries, source_note = [], ""
    for block in pool:
        text = block.text.strip()
        if not text or len(text) > 1200:
            continue
        if not _INSTITUTION_RE.search(text):
            continue
        # 上限放宽到 150 词：Nature 把 6 个单位挤在一个小字块里，
        # 卡在 60 词会把整块单位都丢掉（实测只留下了最后一条 ELLIS Institute）。
        if len(text.split()) > 150:
            continue
        if _ABSTRACT_HEADING_RE.match(text) or _TITLE_BLOCKLIST_RE.match(text):
            continue
        for item in _split_affiliation_entries(text):
            if len(item) < 8 or not _INSTITUTION_RE.search(item):
                continue
            if item.lower() in {existing.lower() for existing in entries}:
                continue
            entries.append(item)
        if block not in zone:
            source_note = "（含页脚小字里的单位）"

    if not entries:
        return [], "未找到", "没有找到单位信息，请手动填写"
    source = "第 1 页作者区" + (" / 页脚" if source_note else "")
    if extra_page_two:
        source = "第 1 页作者区 / 第 2 页脚注" + ("（也含页脚）" if source_note else "")
    return entries, source + source_note, ""


def _normalize_name(text: str) -> str:
    """归一化人名用于比对：去重音符、只留小写字母与空格"""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z ]", " ", text.lower()).strip()


def match_author(query: str, authors):
    """
    把一段文字（人名片段或邮箱）匹配到作者列表里的某个人。
    匹配顺序：整体包含 → 姓氏命中 → 邮箱本地部分的名+姓命中。
    """
    query = _norm(query)
    if not query:
        return None
    normalized_query = _normalize_name(query)
    if not normalized_query:
        return None

    for name in authors:
        if _normalize_name(name) == normalized_query:
            return name
    for name in authors:
        full = _normalize_name(name)
        if full and (full in normalized_query or normalized_query in full):
            return name
    words = set(normalized_query.replace(".", " ").replace("_", " ").split())
    for name in authors:
        tokens = _normalize_name(name).split()
        if tokens and tokens[-1] in words and tokens[0][:1] and \
                any(word[:1] == tokens[0][:1] for word in words):
            return name
    for name in authors:
        surname = _normalize_name(name).split()
        if surname and surname[-1] in words:
            return name
    return None


def _email_pool(doc, blocks):
    """
    收集「像是通讯邮箱」的地址：通讯段落里的、第一页 mailto 链接里的、
    带 e-mail 标签的。ACM 那种逐作者列出的邮箱**不算**（它没有标注通讯作者）。
    """
    pool = []
    if doc is not None and doc.page_count:
        for link in doc[0].get_links():
            uri = (link.get("uri") or "")
            if uri.lower().startswith("mailto:"):
                pool.append((uri[7:].strip(), "第 1 页邮件链接", 0))
    for block in blocks:
        if block.page > 2:
            continue
        text = block.text.strip()
        if not _EMAIL_RE.search(text):
            continue
        has_label = bool(_EMAIL_LABEL_RE.search(text)) or bool(_CORRESPONDENCE_RE.search(text))
        if not has_label:
            continue
        for email in _EMAIL_RE.findall(text):
            pool.append((email, f"第 {block.page} 页通讯栏目", block.page))
    unique, seen = [], set()
    for email, source, page in pool:
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append((email, source, page))
    return unique


def extract_corresponding(blocks, authors, doc=None):
    """
    提取通讯作者与通讯邮箱。返回 (Field 作者, Field 邮箱)。

    只在有证据时才给结果：
      ① 显式通讯段落（*CORRESPONDENCE / Corresponding Author: / …addressed to…）；
      ② 由通讯邮箱反查作者（antti.oulasvirta@aalto.fi → Antti Oulasvirta）。
    都没有就如实报「未找到」——**绝不猜**（用户要求准确性优先）。
    """
    emails = _email_pool(doc, blocks)
    email_field = Field()
    if emails:
        email = emails[0][0]
        email_field = Field(value=email, source=emails[0][1],
                            note="" if len(emails) == 1 else
                                 f"PDF 里有 {len(emails)} 个通讯类邮箱，这里取第一个："
                                 + "、".join(item[0] for item in emails[1:4]))

    # ---- ① 显式通讯段落 ----
    for index, block in enumerate(blocks):
        if block.page > 2:
            continue
        text = block.text.strip()
        if not _CORRESPONDENCE_RE.search(text):
            continue

        # 标签后面直接跟名字（Frontiers / SAGE / xlm）
        remainder = _CORRESPONDENCE_LABEL_RE.sub("", text).strip()
        if not remainder and index + 1 < len(blocks):        # 标签单独成行 → 看下一块
            remainder = blocks[index + 1].text.strip()
        head = _cut_at_institution(re.split(r"[,;]", remainder)[0])
        matched = match_author(head, authors) if looks_like_person_name(head) else None
        # 括号里的缩写名（Elsevier："pecher@essb.eur.nl (D. Pecher)"）
        if matched is None:
            for candidate in re.findall(r"\(([A-Z]\.?\s?[A-Z][A-Za-z’'\-]+)\)", text):
                matched = match_author(candidate, authors)
                if matched:
                    break
        # 最后：整段文字里直接找作者姓氏
        if matched is None:
            for name in authors:
                surname = _normalize_name(name).split()
                if surname and surname[-1] and surname[-1] in _normalize_name(text):
                    matched = name
                    break
        if matched:
            return (Field(value=matched, source=f"通讯标注段落（第 {block.page} 页）",
                          note=_norm(remainder)[:80] if remainder else ""),
                    email_field)

    # ---- ② 由通讯邮箱反查作者 ----
    for email, source, _page in emails:
        local = email.split("@")[0]
        matched = match_author(local, authors)
        if matched:
            return (Field(value=matched, source=f"通讯邮箱反查（{email}）",
                          note="本文没有显式的「通讯作者」标注，这是按通讯邮箱匹配到的作者，请核对"),
                    email_field)

    note = "本文没有标注通讯作者"
    if emails:
        note += f"；PDF 里有一个通讯邮箱：{emails[0][0]}"
    else:
        note += "，也没有找到通讯邮箱，请手动填写"
    return Field(note=note), email_field


def extract_author_emails(zone, authors):
    """
    作者区里逐作者列出的邮箱（ACM 那类），按「最近的上方作者」配对。
    这只是辅助信息：通讯作者字段**不会**拿它来顶替。
    """
    pairs = []
    for block in zone:
        if "@" not in block.text:
            continue
        for email in _EMAIL_RE.findall(block.text):
            owner, best = "", None
            for name, y0, x0 in authors:
                if y0 > block.y0 + 2:
                    continue
                distance = abs(x0 - block.x0) + (block.y0 - y0)
                if best is None or distance < best:
                    best, owner = distance, name
            pairs.append(f"{owner} <{email}>" if owner else email)
    unique = []
    for pair in pairs:
        if pair.lower() not in {item.lower() for item in unique}:
            unique.append(pair)
    return unique


def _overlap_text(lines, rect):
    """取与某个矩形重叠最多的一行文本（链接的锚文本）"""
    if not rect:
        return ""
    best, best_area = "", 0.0
    for line in lines:
        ox = max(0.0, min(line["x1"], rect[2]) - max(line["x0"], rect[0]))
        oy = max(0.0, min(line["y1"], rect[3]) - max(line["y0"], rect[1]))
        area = ox * oy
        if area > best_area:
            best, best_area = line["text"], area
    return best


def _overlap_block_text(parsed_blocks, rect):
    """取与某个矩形重叠最多的文本块内容（比锚文本宽一点，便于判类型）"""
    if not rect:
        return ""
    best, best_area = "", 0.0
    for block in parsed_blocks:
        if block.get("type") != 0:
            continue
        bbox = block.get("bbox") or (0, 0, 0, 0)
        ox = max(0.0, min(bbox[2], rect[2]) - max(bbox[0], rect[0]))
        oy = max(0.0, min(bbox[3], rect[3]) - max(bbox[1], rect[1]))
        area = ox * oy
        if area > best_area:
            text = " ".join("".join(s["text"] for s in line.get("spans", []))
                            for line in block.get("lines", []))
            best, best_area = _norm(text), area
    return best[:300]


def _is_supplementary(uri: str, context: str) -> bool:
    """这个链接算不算「附件 / 数据 / 预注册」类链接"""
    if _REPOSITORY_RE.search(uri) or _SUPP_KEYWORD_RE.search(uri):
        return True
    if _SELF_DOI_RE.search(uri):
        return False                     # 本文自己的 DOI
    if _BOILERPLATE_URL_RE.search(uri):
        return False                     # 出版社固定链接
    return bool(_SUPP_KEYWORD_RE.search(context))


def _link_type(uri: str, context: str) -> str:
    blob = f"{uri} {context}"
    if re.search(r"preregist|aspredicted|clinicaltrials", blob, re.IGNORECASE):
        return "预注册"
    if _REPOSITORY_RE.search(uri) or re.search(
            r"data\s+availab|open\s+science|dataset|code\s+availab|材料|数据", blob, re.IGNORECASE):
        return "数据与代码"
    if _SUPP_KEYWORD_RE.search(blob):
        return "补充材料"
    return "其他材料"


def _url_host(uri: str) -> str:
    match = re.match(r"https?://([^/]+)", uri or "")
    return match.group(1).lower() if match else ""


def _drop_truncated(items) -> list:
    """
    去掉「换行被截断」的 URL。

    PDF 里长 URL 换行时会印成两截（"https://osf.io/" + "q2dm6/"），
    正文扫描会抓到一个前缀版；同一个链接又常常同时是链接注释（完整版）。
    规则：若某个 URL 去掉尾部 -/ 之后是另一个**同域名** URL 的前缀，就丢弃它。
    """
    urls = [item["链接"] for item in items]
    kept = []
    for item in items:
        uri = item["链接"]
        host = _url_host(uri)
        stem = uri.rstrip("-/")
        redundant = False
        for other in urls:
            if other == uri or _url_host(other) != host or len(other) <= len(uri):
                continue      # 只跟「更长」的那个比，否则两个变体会互相把对方删掉
            if len(stem) >= 12 and (other.startswith(stem) or (len(uri) < 44 and stem in other)):
                redundant = True
                break
        if not redundant:
            kept.append(item)
    return kept


def extract_supplementary(doc, max_items: int = 15):
    """
    扫描全部页面，找出「补充材料 / 数据 / 代码 / 预注册」链接。

    三重过滤：① 仓库域名或关键词直接命中；② 剔除出版社固定链接（许可、crossmark、
    期刊主页、本文 DOI）；③ 其余要求**上下文**里出现补充材料/数据可得之类的词
    （这样参考文献里的普通网址不会被当成附件链接）。
    正文声明「有补充材料」但没有链接时，也如实记一条声明。
    """
    items, seen = [], set()
    statements = []

    for page_no in range(doc.page_count):
        page = doc[page_no]
        parsed_blocks = page.get_text("dict").get("blocks", [])
        lines = _page_lines(page)

        for link in page.get_links():
            uri = _norm(link.get("uri") or "")
            if not uri or "@" in uri or uri in seen:
                continue
            anchor = _overlap_text(lines, link.get("from"))
            context = _overlap_block_text(parsed_blocks, link.get("from"))
            if not _is_supplementary(uri, f"{anchor} {context}"):
                continue
            seen.add(uri)
            items.append({"类型": _link_type(uri, f"{anchor} {context}"),
                          "链接": uri, "页码": page_no + 1,
                          "上下文": _norm(anchor or context)[:110]})

        # 正文里印出来、但没有做成链接的 URL
        for line in lines:
            for uri in re.findall(r"https?://[^\s)\]<>，。；]+", line["text"]):
                uri = uri.rstrip(".,;:\"'”’")
                if uri in seen or len(uri) < 12:
                    continue
                if not _is_supplementary(uri, line["text"]):
                    continue
                seen.add(uri)
                items.append({"类型": _link_type(uri, line["text"]),
                              "链接": uri, "页码": page_no + 1,
                              "上下文": _norm(line["text"])[:110]})

        for line in lines:
            if not _SUPP_STATEMENT_RE.search(line["text"]):
                continue
            if re.search(r"https?://", line["text"]) and any(
                    uri in line["text"] for uri in seen):
                continue                     # 链接已经收录，不必再记声明
            statements.append({"页码": page_no + 1, "上下文": _norm(line["text"])[:110]})

    items = _drop_truncated(items)[:max_items]
    note = ""
    if statements:
        unique_statements = []
        for statement in statements:
            if statement["上下文"][:50] not in {item["上下文"][:50] for item in unique_statements}:
                unique_statements.append(statement)
        note = "正文声明了补充材料但没有可直接点击的链接：" + "；".join(
            f"第 {item['页码']} 页「{item['上下文'][:60]}」" for item in unique_statements[:3])

    if not items:
        return Field(note=note or "没有找到补充材料 / 数据链接"), [], note
    value = "\n".join(item["链接"] for item in items)
    source = f"全页扫描链接注释与正文 URL（{len(items)} 条）"
    return Field(value=value, source=source, note=note), items, note


# ------------------------------------------------------------
# 总入口
# ------------------------------------------------------------

def extract_metadata(pdf_bytes: bytes, blocks=None) -> Metadata:
    """
    提取标题 / DOI / 摘要 / 作者 / 第一作者 / 通讯作者 / 单位 / 附件链接。

    blocks 传入 parse_pdf 的结果（原子块）可以省一次解析；不传就自己解析一遍。
    """
    if blocks is None:
        from pdf_parser import parse_pdf
        blocks = parse_pdf(pdf_bytes, merge_on=True, dehyphenate=True)["blocks"]

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        if doc.needs_pass:
            message = "PDF 有密码保护"
            return Metadata(title=Field(note=message), doi=Field(note=message),
                            abstract=Field(note=message), authors=Field(note=message),
                            first_author=Field(note=message),
                            corresponding_author=Field(note=message),
                            affiliations=Field(note=message),
                            supplementary_links=Field(note=message))

        first_page = doc[0] if doc.page_count else None
        title = extract_title(first_page, doc.metadata)

        # ---- 阶段 4.2 ----
        names, author_source, author_note, positioned = extract_authors(blocks, title.value)
        affiliations, affiliation_source, affiliation_note = extract_affiliations(
            blocks, title.value, doc)
        corresponding, corresponding_email = extract_corresponding(blocks, names, doc)
        author_emails = extract_author_emails(
            author_zone(blocks, title.value)[0], positioned)
        supplementary, supplementary_items, supplementary_note = extract_supplementary(doc)

        if names:
            first_author = Field(value=names[0], source="作者列表的第一个（按原文顺序）",
                                 note="" if len(names) > 1 else "只识别到一个作者，请核对")
            authors_field = Field(value="\n".join(names), source=author_source,
                                  note=author_note)
        else:
            first_author = Field(note=author_note or "没有识别到作者，请手动填写")
            authors_field = Field(note=author_note or "没有识别到作者，请手动填写")

        # ACM 那类没有通讯标注、但每个作者都列了邮箱 → 把邮箱放进备注里提示
        if author_emails:
            extra = "作者区逐作者列出的邮箱：" + "；".join(author_emails[:6])
            for target in (corresponding, corresponding_email):
                target.note = (target.note + " · " + extra) if target.note else extra

        return Metadata(
            title=title,
            doi=extract_doi(doc),
            abstract=extract_abstract(blocks),
            authors=authors_field,
            first_author=first_author,
            corresponding_author=corresponding,
            corresponding_email=corresponding_email,
            affiliations=Field(value="\n".join(affiliations), source=affiliation_source,
                               note=affiliation_note),
            author_emails=Field(value="\n".join(author_emails),
                                source="第 1 页作者区的邮箱（与其上方最近的作者配对）"
                                if author_emails else "未找到"),
            supplementary_links=supplementary,
            supplementary_items=supplementary_items,
        )
    finally:
        doc.close()
