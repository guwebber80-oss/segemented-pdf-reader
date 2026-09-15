"""
PDF 解析模块 —— 从 PDF 字节流里提取「按阅读顺序排列的文本卡片」
================================================================
本模块【不依赖 Streamlit】，只做纯数据处理，可以单独运行、单独测试。

主要流程（parse_pdf）：
    PDF 字节流
      → 逐页取出**原子块**（extract_page_blocks，含字符级修复与公式行拆分）
      → 判断单栏/双栏（detect_gutter）
      → 按阅读顺序重排（order_blocks）
      → 估算正文字号 / 识别标题 / 内容角色分类
      → 公式三级标记 + 区域聚类（formula_finder）→ 确认的公式移出文字流
      → 段落分组（build_paragraph_groups，纯派生，不破坏原子块）
      → 超长段落切卡片（split_long_block）
      → 卡片列表

【两层数据模型】
    原子块（Block）：从 PDF 提取的最小文本块，**任何阶段都不得原地改写它的 text**。
        段落拼接、公式剔除都是「派生」——需要段落文本时按分组即时拼接。
    段落（Paragraph）：由若干原子块派生出的自然段视图，是 Block 的子类，
        会写角色（role）等派生属性，最后再回写到原子块上。

    为什么要这么分：以前 merge_paragraphs 直接把后一块的文本塞进前一块，
    一旦公式碎片被并进散文，后面所有基于块的公式判定就再也看不到它了
    （第九页那个求和公式就是这么消失的）。原子不可变之后，这种事故不可能再发生。
"""

import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, fields, replace

import pymupdf      # PDF 解析核心（PyMuPDF；新版推荐 import pymupdf，旧的 fitz 已弃用）

import formula_finder
import table_finder   # 公式锚点表、三级判据、区域聚类（本模块依赖它，它不反向依赖本模块）


# ============================================================
# 一、常量与文本小工具
# ============================================================

# 英文里这些缩写里的句号不是「句末」，切句前先保护起来，
# 否则 "Fig. 3 shows..." 会被错误地切成两句。
_ABBREVIATIONS = [
    "et al.", "e.g.", "i.e.", "cf.", "vs.", "etc.", "approx.", "ca.", "resp.",
    "Figs.", "Fig.", "Eqs.", "Eq.", "Refs.", "Ref.", "Secs.", "Sec.", "Sect.", "Sects.",
    "Tabs.", "Tab.", "Chs.", "Ch.", "Nos.", "No.", "Suppl.", "Supp.", "pp.", "p.",
    "vol.", "Vol.", "eds.", "ed.", "Dr.", "Prof.", "Mr.", "Mrs.", "Ms.", "St.", "Inc.", "Ltd.",
]

# 常见章节标题名：字号不明显时，靠名字也能认出这是标题
_SECTION_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+)?"                     # 允许前缀编号，如 "2.1 " "3. "
    r"(abstract|summary|introduction|background|related works?|"
    r"materials? and methods?|methods?|experimental|results?|discussion|"
    r"conclusions?|references|bibliography|acknowledge?ments?|appendix|"
    r"supplementary|supporting information|data availability)\b",
    re.IGNORECASE,
)

# 带编号的标题，例如 "3 Study" / "3.1 Task"。
# 要求编号后面紧跟【大写字母】开头，否则会把「伪代码行」误判成标题，
# 例如 "7 return "No Gesture";"、"1 𝑦ℎ𝑎𝑛𝑑←𝐾𝑡[right_hand].𝑦;"。
_NUMBERED_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s+[A-Z]")

# 强数学/公式符号：真标题几乎不会含这些符号，而公式、伪代码一定含。
# 用来把公式块挡在标题之外。
_STRONG_MATH_CHARS = set("←→≥≤∑∏∫‖⋅√±×÷≠≈")

# 参与「单栏/双栏」判断的文本块，至少要这么多字符（过滤页眉、页码、编号等噪声）
MIN_COLUMN_CHARS = 30

# ------------------------------------------------------------
# 内容角色：把「正文」和「论文背景信息」分开
# 用户的明确需求——阅读卡片只放正文，其余信息单独呈现
# ------------------------------------------------------------
ROLE_BODY = "body"                    # 正文（会进阅读卡片）
ROLE_HEADING = "heading"              # 章节标题（进阅读卡片，作为分区标题）
ROLE_CAPTION = "caption"              # 图注/表注（按用户要求：视作正文，留在阅读流里）
ROLE_HEADER_FOOTER = "header_footer"  # 页眉、页脚、页码、running head
ROLE_AUTHOR = "author"                # 作者、单位、联系方式
ROLE_COPYRIGHT = "copyright"          # 版权、许可、资助声明
ROLE_REFERENCE = "reference"          # 参考文献条目
ROLE_LABEL = "label"                  # 期刊栏目名/标签（Authors、Highlights、CCS Concepts…）
ROLE_FRONT_MATTER = "front_matter"    # 封面页/前页信息（Highlights、In Brief、作者、单位）

ROLE_NAMES = {
    ROLE_BODY: "正文",
    ROLE_HEADING: "章节标题",
    ROLE_CAPTION: "图注",
    ROLE_HEADER_FOOTER: "页眉页脚",
    ROLE_AUTHOR: "作者与单位",
    ROLE_FRONT_MATTER: "封面页信息",
    ROLE_COPYRIGHT: "版权/许可/资助",
    ROLE_REFERENCE: "参考文献",
    ROLE_LABEL: "栏目名/标签",
}

# 留在阅读卡片里的角色（图注按用户要求视作正文）
READING_ROLES = (ROLE_BODY, ROLE_CAPTION)

# 图注：必须是 "Figure 3." / "Fig. 2:" 这种「编号 + 标点」形式。
# 不能简单用 "Figure 4 illustrates…" 匹配——那是正文在引用图，不是图注。
_CAPTION_RE = re.compile(
    r"^\s*(fig(?:ure)?|tab(?:le)?|scheme|box|supplementary\s+fig(?:ure)?)\s*\.?\s*\d+\s*[:.、]", re.I)

# 版权 / 许可 / 资助声明
_COPYRIGHT_RE = re.compile(
    r"(©|ª\s?\d{4}|copyright|all rights reserved|licensed under|this work is licensed|"
    r"creative commons|publication date|acm isbn|issn|isbn|permission to make|"
    r"this (project|work|study|research) (has been|was) (partially )?(supported|funded)|"
    r"grant (agreement|number)|horizon 20\d\d|national natural science foundation|"
    r"open access article|declaration of interests|competing interests)", re.I)

# 期刊栏目名 / 标签（整块就是这几个词）
_LABEL_RE = re.compile(
    r"^\s*(authors?|highlights?|keywords?|key words|correspondence|in brief|"
    r"ccs concepts|additional key words and phrases|acm reference format|"
    r"abstract|summary|graphical abstract|editor'?s note|research highlights|"
    r"author contributions|data availability|supplementary (information|material)s?|"
    r"report|article|review|letter|editorial|commentary|perspective)\s*[:：]?\s*$", re.I)

# 参考文献标题
_REFERENCES_HEADING_RE = re.compile(
    r"^\s*(references|bibliography|literature cited|works cited|reference list)\s*$", re.I)

# 参考文献条目的样子：[12] 开头，或 "12. Smith, J." 开头
_REFERENCE_ITEM_RE = re.compile(r"^\s*(\[\d{1,3}\]|\d{1,3}\.\s+[A-Z][a-zA-Z’'\-]+,)")

# 单位/联系方式的关键词
_AFFILIATION_RE = re.compile(
    r"(university|universit|institut|department|faculty|school of|college|laborator|"
    r"centre for|center for|academy|hospital|clinic|gmbh|ltd|inc\.|corp\.|"
    r"correspondence|corresponding author|e-?mail|@)", re.I)

# 章节编号："2.1"、"3.1.2"、"4."
_SECTION_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+(.+)$")

# 期刊刊头（不是论文章节）：Report / Current Biology Report / Journal of …
_MASTHEAD_RE = re.compile(
    r"^\s*(report|article|review|letter|editorial|commentary|perspective|communication|"
    r"research article|original article|brief report|"
    r"(current|frontiers in|journal of|proceedings of|ieee|acm|plos|nature|science|cell)\b.*)"
    r"\s*$",
    re.IGNORECASE)

# 图内面板标签："A B C D" / "E F G H"（多子图排版的角标），不是章节
_PANEL_LABEL_RE = re.compile(r"^(?:[A-Z]\s+){1,}[A-Z]$")

# ------------------------------------------------------------
# 公式 / 统计量的字符级修复
# ------------------------------------------------------------

# ① 数学字母符号（𝑇 𝑣 𝑥 这类 U+1D400–U+1D7FF）用 NFKC 归一化成普通字母。
#    不处理的话它们既难读、也没法翻译（翻译 API 会把它们当乱码符号）。
_MATH_ALNUM_MAP = {}
for _codepoint in range(0x1D400, 0x1D800):
    _char = chr(_codepoint)
    _normalized = unicodedata.normalize("NFKC", _char)
    if _normalized != _char:
        _MATH_ALNUM_MAP[_codepoint] = _normalized

# ② 上标 / 下标 → Unicode 上下标字符。很多期刊 PDF 的「上标位」标志位根本没设，
#    只能靠「字号更小 + 基线偏移」来判断：底边高于本行 → 上标，底边低于本行 → 下标。
_SUPER_FROM = "0123456789+-=()abcdefghijklmnoprstuvwxyz"
_SUPER_TO = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ"
_SUPERSCRIPT_MAP = str.maketrans(_SUPER_FROM, _SUPER_TO)

# Unicode 的下标字母不全（缺 b c d f g q w y z），所以下标只在「整段都能转」时才转，
# 否则退回 _ 记号（例如 u_primary、p_torso）——这既保证可读，也不会缺字。
_SUB_FROM = "0123456789+-=()aehijklmnoprstuvx"
_SUB_TO = "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"
_SUBSCRIPT_MAP = str.maketrans(_SUB_FROM, _SUB_TO)


def _to_superscript(text: str) -> str:
    """转成 Unicode 上标；有字符没有上标形式就退回 ^ 记号"""
    stripped = text.strip()
    if stripped and all(ch in _SUPER_FROM for ch in stripped):
        return text.translate(_SUPERSCRIPT_MAP)
    return "^" + stripped


def _to_subscript(text: str) -> str:
    """转成 Unicode 下标；有字符没有下标形式就退回 _ 记号（Unicode 下标字母不全）"""
    stripped = text.strip()
    if stripped and all(ch in _SUB_FROM for ch in stripped):
        return text.translate(_SUBSCRIPT_MAP)
    return "_" + stripped


def _convert_script(text: str, converter) -> str:
    """
    转换上下标，但把尾部标点留在外面：
    PDF 里 "𝑖," 这种「下标字母 + 逗号」很常见，直接转会得到 "ᵢ," 看着还行，
    但多字符下标退回 _ 记号时会变成 "_i,"，标点留在外面更干净。
    """
    core, trailing = text, ""
    while core and core[-1] in ",.;:":
        trailing = core[-1] + trailing
        core = core[:-1]
    if not core:
        return text
    return converter(core) + trailing


# 数学排版里 "idᵢto"、"x²y" 这种「上下标后面直接接字母」是常态，
# 压成一行文字后会连成一坨，这里补一个空格方便阅读。
_SCRIPT_CHARS = re.escape(_SUPER_TO + _SUB_TO)
_SCRIPT_SPACING_RE = re.compile(f"([{_SCRIPT_CHARS}]+)([A-Za-z])")


def add_script_spacing(text: str) -> str:
    """上下标字符后面紧跟拉丁字母时补一个空格（纯排版可读性）"""
    return _SCRIPT_SPACING_RE.sub(r"\1 \2", text)


# ------------------------------------------------------------
# 公式块判定：决定哪些块「渲染成图片」而不是塞进一行文字
# ------------------------------------------------------------
# 判定逻辑（锚点表 / 三级判据 / 区域聚类）已经独立到 formula_finder.py。
# 这里只保留提取阶段要用的别名，以及「按栏位算左边界」这类解析器自己的活。
_FORMULA_SCRIPT_RE = formula_finder.SCRIPT_RE        # 上下标记号
_MATH_FONT_RE = formula_finder.MATH_FONT_RE          # 数学字体
_MATH_ANCHORS = formula_finder.MATH_ANCHORS          # 数学锚点（符号）


def _looks_mathy(text: str) -> bool:
    """
    行级判断：这一行是不是「像公式」？
    用于把独立成行的公式从段落里拆出来（比块级判定宽松一点，因为拆错的代价小）。
    """
    return formula_finder.looks_mathy(text)


def looks_like_formula(block) -> bool:
    """
    单个块是否达到「强公式」的分数门槛。
    保留这个接口是为了诊断脚本兼容；正式管线用的是三级标记 _mark_formula_tiers。
    """
    return formula_finder.formula_score(block) >= formula_finder.STRONG_THRESHOLD


def column_margins(blocks) -> dict:
    """
    每个「页 × 栏」的正文左边界（用来判断公式有没有缩进）。

    取 x0 里**出现 ≥2 次的最左位置**（没有任何重复值时退回最小值）。

    为什么不能用中位数：公式碎片多的页面上，碎片块的数量会超过正文段落，
    中位数会被推到碎片位置上——实测单栏.pdf 第 9 页的「左边界」被算成 192.8pt
    （真实边界是 46pt），于是页面上真正的公式全被判成「没有缩进」而降级。

    为什么也不能用众数：算法清单页里，缩进的清单行（x≈48）比正文段落还多，
    众数会选到清单的缩进位置，同样把真正的左边距（46）顶掉。

    「出现 ≥2 次的最左位置」两头都能防住：正文左边界在同一栏里一定会重复出现，
    而它总是最靠左的那一批。
    """
    collected = {}
    for block in blocks:
        if block.role == ROLE_BODY:
            collected.setdefault((block.page, block.column), []).append(block.x0)

    margins = {}
    for key, values in collected.items():
        counter = Counter(round(value) for value in values)
        repeated = [value for value, count in counter.items() if count >= 2]
        margins[key] = min(repeated) if repeated else min(values)
    return margins


def mark_formula_tiers(blocks, margins: dict, excluded=frozenset()) -> None:
    """
    给每个块标出公式等级（就地写 formula_tier）：

        "strong"    判据充分，可以单独成图，也可以当区域聚类的种子；
        "weak"      有数学证据但不充分（分数 2~3，或分数够但没过防线）——
                    只能加入已有区域；只有「相对所在栏有缩进 + 数学字体」时才允许当种子；
        "fragment"  公式碎片（短、无句末标点、无散文虚词）——
                    **只能被区域吸收，绝不单独成图**，这是「不把半句话变成图片」的关键；
        ""          普通文字。

    这里只管「谁有资格参与公式判定」：
      · 只有进阅读流的角色（正文、图注）参与，标题与背景信息一律不判定；
      · 提取阶段已经拆出来的公式行有结构证据（居中 + 数学字体 + 符号密度），
        直接算强候选，但仍然要过一遍「单独成图」的防线，没过就降为弱候选。
    """
    for index, block in enumerate(blocks):
        if index in excluded:
            # 「保留文字」模式下的表格区域：表格文字照旧留在卡片里，
            # 但不参与公式判定——否则表格里的 "β = 13.85" 这类单元格会被
            # 碎片堆规则聚成一张公式图，在表格文字中间冒出一张图。
            block.formula_tier = ""
            block.is_formula = False
            continue
        if block.role not in READING_ROLES or block.kind == "heading":
            block.formula_tier = ""
            block.is_formula = False
            continue

        # 表格区域里的块不参与公式判定（顺序上表格先判）：表格单元格里的
        # "β = 13.85" 这类内容如果被公式聚类吸走，表格图里就会缺一块、还多出一张公式图。
        if block.is_table:
            block.formula_tier = ""
            block.is_formula = False
            continue

        margin = margins.get((block.page, block.column))
        if block.is_formula:                    # 提取阶段的结构证据
            block.formula_tier = (
                "strong" if formula_finder.passes_standalone_guards(block, margin) else "weak")
        else:
            block.formula_tier = formula_finder.classify(block, margin)

    # ---- 碎片堆 → 提升为强候选 ----
    # 被排成上下叠放多块的显示公式（分数、分式方程组）常常**一块强候选都没有**，
    # 聚类因此从不启动，这些公式既不出图、又作为乱码文字留在卡片里
    # （用户验收时报的 npj 第 8、9 页正是这种情况）。这里按「同一处公式的碎片堆」
    # 补一个种子，让聚类能正常启动。
    for index in formula_finder.find_fragment_stacks(blocks):
        blocks[index].formula_tier = "strong"


def apply_clusters(blocks, clusters, reading_roles=READING_ROLES) -> None:
    """
    把聚类结果写回原子块：簇内成员标上 cluster_id，并统一 is_formula。

    这里把 is_formula 的语义**收紧成一个**：属于某个已确认的公式区域。
    提取阶段曾经把「拆出来的公式行」也标成 is_formula，但那只是候选——
    如果没进簇就出图，就会出现一张「半句话」的图片
    （实测 Nature 那篇的 "ₜₑₓₜ, φₖ, uₜ), where b^′" 就是这么冒出来的）。

    **只有进阅读流的角色**才会被标成公式：标题与背景信息（作者、参考文献、表格页脚…）
    里的块如果被吸进簇，会出现「文字从正文里消失了、却没有任何图片替代」的内容丢失。
    """
    for cluster in clusters:
        for index in cluster.atom_indices:
            if blocks[index].role not in reading_roles:
                continue
            blocks[index].cluster_id = cluster.cluster_id

    for block in blocks:
        block.is_formula = block.cluster_id >= 0

# ③ 子集字体把希腊字母错映射成拉丁字母的修复表。
#    真实案例：Cell Press 用「单字形子集字体」画 η，提取出来变成 "h"，
#    于是 "ηp² = 0.16" 成了 "hp2 = 0.16"。字体里的信息已经彻底丢失
#    （glyph 名就被改写成 "LATIN SMALL LETTER H"），只能在统计量上下文里按模式修。
#    后面必须紧跟 = < >，避免误伤普通文字里的 h。
_STATS_SYMBOL_FIXES = (
    (re.compile(r"\bh\s*p\s*(?:²|2)(?=\s*[=<>])"), "ηp²"),
    (re.compile(r"\bh\s*(?:²|2)(?=\s*[=<>])"), "η²"),
)


def apply_table_regions(blocks, regions) -> None:
    """
    把表格区域写回原子块：区域覆盖到的块标上 table_id 与 is_table。

    与公式一样，is_table 的语义只有一条——**属于某个已确认的表格区域**。
    被标上的块会以整区域截图的形式呈现，并从文字流里移出（见 build_paragraph_groups）。
    """
    for region in regions:
        for index in region.atom_indices:
            blocks[index].table_id = region.region_id
    for block in blocks:
        block.is_table = block.table_id >= 0


def repair_stats_symbols(text: str) -> str:
    """在统计量上下文里修回被字体错映射的希腊字母（ηp² / η²）"""
    for pattern, replacement in _STATS_SYMBOL_FIXES:
        text = pattern.sub(replacement, text)
    return text


# ④ 子集字体把**数学符号**错映射成别的字符的修复（第二种形态，2026-09-15 用户验收时发现）。
#    真实案例：npj Digital Medicine 第 8 页的显示公式排成
#        'GEVₑₘₒₜᵢₒₙ ¼ nᵉᵐᵒᵗⁱᵒⁿ'      ← ¼ 其实是 "="
#        'Cði; jÞ'                      ← ð Þ 其实是括号
#    这批 Adv* 子集字体（AdvMacMthSyN / AdvOT7d6df7ab.I）与 η→h 是同一类问题：
#    字形到 Unicode 的映射被改写，原始信息丢失，只能按上下文修。
#    为什么必须修：① 不修的话线性文本是乱码；② 公式判定的防线（括号配对、
#    比较符两边要有操作数）全都过不了，显示公式因此**既没被截图、又留成乱码**。
#    修复条件收紧到「同一行里出现上下标」：真公式必有上下标（实测这批公式全都有，
#    例如 'Σ_{t=1}^{N}' 里的 'ₜ_¼₁' —— 那正是 t=1，反过来印证了 ¼ 就是等号），
#    而正文里的 ¼（四分之一）不会跟上下标同现。第一版没加这条，实测把
#    'about ¼ of the trials' 改成了 'about = of the trials'，所以必须卡住。
#    注意正则**不能**要求 ¼ 前面有字符：实测有一处公式因为阅读顺序把等号排到了块首
#    （'¼ C(i; j) P'），带 lookbehind 的版本直接不匹配，这处公式因此一直没出图。
#    防误伤改由「同一行必须有上下标或错映射括号」这道语境门槛负责。
_OBFUSCATED_EQUALITY_RE = re.compile(r"\s*¼\s*(?=[\w(])")
_SCRIPT_CHARS_RE = re.compile("[\u2070-\u209f\u1d2c-\u1d6a]")   # 上下标字符（含上标字母 ᵃᵉ 段）


def repair_obfuscated_math(text: str) -> str:
    """修回被 Adv* 子集字体错映射的数学符号（¼ → =、ð Þ → 括号）"""
    # 证据必须在替换前判断：错映射的括号本身就是「这批字体」的直接证据
    # （正常文字里不会成对出现 ð Þ），所以它和上下标一样能授权 ¼ → = 的替换。
    # 实测踩过：'¼ Cði; jÞ P' 这行没有上下标、只有括号，旧版因此没修，
    # 这处公式就少了等号证据、聚类起不来（用户报的第三个漏检公式）。
    has_corrupt_parens = "ð" in text and text.count("ð") == text.count("Þ")
    has_scripts = bool(_SCRIPT_CHARS_RE.search(text))
    if has_corrupt_parens:
        text = text.replace("ð", "(").replace("Þ", ")")
    if "¼" in text and (has_scripts or has_corrupt_parens):
        text = _OBFUSCATED_EQUALITY_RE.sub(" = ", text)
    return text


def parse_section(text: str):
    """把章节标题拆成（编号, 标题）。没有编号的（Abstract/References）编号返回空串。"""
    stripped = text.strip()
    match = _SECTION_NUMBER_RE.match(stripped)
    if match:
        return match.group(1), match.group(2).strip()
    return "", stripped


def count_words(text: str) -> int:
    """英文词数：按空白切分。中文下这个数字没有意义，但阶段 1 只处理英文文献。"""
    return len(text.split())


def join_lines(lines, dehyphenate: bool = True) -> str:
    """
    把 PDF 里同一个块的多行文本拼成一段话。

    要点：英文排版会在行尾用连字符把一个单词断开，例如
        informa-
        tion
    拼回去时必须【删掉连字符直接连】；若换成空格就会得到 "informa tion"。
    判断依据：前一行以连字符结尾，且下一行以小写字母开头。
    """
    out = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not out:
            out = line
            continue
        # "-" 普通连字符；"‐" U+2010；"\xad" 软连字符
        if dehyphenate and out[-1] in "-‐\xad" and line[:1].islower():
            out = out[:-1] + line
        else:
            out = f"{out} {line}"
    return out


def split_sentences(text: str):
    """
    把一段话切成句子：先把缩写换成占位符 → 按「句末标点 + 空格」切 → 再还原占位符。
    """
    protected = text
    for i, abbr in enumerate(_ABBREVIATIONS):
        protected = protected.replace(abbr, f"\x00{i}\x00")

    parts = re.split(r"(?<=[.!?])\s+", protected)

    def restore(s: str) -> str:
        return re.sub(r"\x00(\d+)\x00", lambda m: _ABBREVIATIONS[int(m.group(1))], s)

    return [restore(p).strip() for p in parts if p.strip()]


def escape_markdown(text: str) -> str:
    """
    转义 Markdown 特殊符号，保证【屏幕上显示的就是 PDF 里的原文】。
    少了这一步，正文里的 "_"、"*"、"#"、"$" 会被当成排版标记，
    例如 "p53_KO"、"p > 0.05" 就会显示错。
    （这个函数给界面用，放在这里是为了让文本处理的工具都集中在一处。）
    """
    out = re.sub(r"([\\`*_{}\[\]<>|~$])", r"\\\1", text)
    # 行首的 # > - + * 和 "1." 也会触发排版，一并转义
    out = re.sub(r"^(\s*)([#>\-+*]|\d+\.)", r"\1\\\2", out)
    return out


# ============================================================
# 二、数据结构
# ============================================================

@dataclass
class Block:
    """
    一个**原子块**：从 PDF 提取出来的最小文本块（PDF 里的一「块」通常是一个自然段，
    也可能是被拆出来的标题行、公式行）。

    【重要】原子块是不可变的：任何阶段都不许改写它的 text。
    段落拼接、公式剔除、角色标注都是「派生」——需要段落文本时按分组即时拼接
    （见 build_paragraph_groups / Paragraph）。以前直接改 text 的做法会把公式碎片
    并进散文，之后再也找不回来。
    """
    page: int           # 页码，从 1 开始
    x0: float           # 左边界坐标
    y0: float           # 上边界坐标（PDF 坐标系原点在左上角，y 向下增大）
    x1: float           # 右边界
    y1: float           # 下边界
    text: str           # 文本内容
    max_size: float     # 块内最大字号（用来判断标题）
    bold_ratio: float   # 加粗字符占比 0~1（用来判断标题）
    line_height: float  # 中位数行高（用来判断两个块是不是同一段）
    column: int = -1    # -1 通栏（跨两栏）/ 0 左栏 / 1 右栏
    kind: str = "body"  # body 正文 / heading 标题
    level: int = 0      # 标题级别：1 最大，2 次之，3 最小
    order: int = 0      # 全局阅读顺序号
    role: str = ROLE_BODY       # 内容角色：正文 / 页眉页脚 / 作者单位 / 参考文献…
    section_number: str = ""    # 所属章节编号，如 "2.1"
    section_title: str = ""     # 所属章节标题
    page_end: int = 0           # 卡片跨页时的结束页（普通块等于 page）
    card_index: int = 0         # 阅读卡片编号（聚合后才有意义）
    segments: list = field(default_factory=list)        # 卡片内部结构：[("heading"|"text", 文本)]
    headings_inside: list = field(default_factory=list)  # 卡片内部包含的章节 [(编号, 标题), …]
    is_formula: bool = False    # 是否属于某个已确认的公式区域（渲染成图片、移出文字流）
    formula_tier: str = ""      # 公式等级：strong / weak / fragment / ""（见 mark_formula_tiers）
    cluster_id: int = -1        # 所属公式区域编号（-1 = 不属于任何公式区域）
    is_table: bool = False      # 是否属于某个已确认的表格区域（同样是截图呈现、移出文字流）
    table_id: int = -1          # 所属表格区域编号（-1 = 不属于任何表格区域）
    table_caption: str = ""     # 表格题注（界面在图下方标出来，便于核对）
    font_names: tuple = ()      # 块内出现的字体名（数学字体是公式的强证据）
    origin_baseline: float = 0.0  # 块内行的中位基线 y（上下标判定的基准）


@dataclass
class Paragraph(Block):
    """
    由若干原子块**派生**出的自然段视图（Block 的子类，字段全部继承）。

    它只是一个视图：写它的 role/kind 不会影响原子块，等分类结束后再由
    propagate_paragraph_roles 把角色回写到原子块上。卡片构建、内容分类、
    背景信息面板读的都是这一层——因为它们要的是「读出完整的一段话」。
    """
    atom_indices: list = field(default_factory=list)   # 组成这个段落的原子块下标


# ============================================================
# 三、逐页提取 + 排序
# ============================================================

def _line_records(raw_block):
    """
    把一个 PyMuPDF 的「块」拆成「行记录」列表。
    每行记录含：文本、最大字号、加粗字符占比、加粗字符数、总字符数、bbox、行高。
    有了行级信息，才能把混在正文块里的标题行拆出来。
    """
    records = []
    for line in raw_block.get("lines", []):
        # 注意：纯空白的 span（词与词之间的空格）也必须参与拼接，
        # 否则文本会变成 "Atotalof28participants" 这种挤在一起的样子
        visible = [span for span in line.get("spans", []) if span.get("text", "").strip()]
        if not visible:
            continue

        # 行内基准：取「字号最大的 span」当主文本，用它的字号 + **基线**（origin[1]）当基准。
        # 为什么用基线而不是 bbox：PyMuPDF 的 span bbox 包含字体升降部，
        # 「同基线的小字号文字」和「真正的下标」在 bbox 上几乎分不开，而基线一测就准。
        main_span = max(visible, key=lambda s: float(s.get("size", 0)))
        base_size = float(main_span.get("size", 0))
        base_baseline = main_span["origin"][1]

        parts, sizes, bold_chars, total_chars = [], [], 0, 0
        for span in line.get("spans", []):
            span_text = span.get("text", "")
            if not span_text:
                continue

            # ① 数学字母符号归一化成普通字母（𝑇 → T、𝑣 → v）
            span_text = span_text.translate(_MATH_ALNUM_MAP)

            if span_text.strip():
                size = float(span.get("size", 0))
                baseline = span["origin"][1]

                # ② 字号更小 + 基线抬高 → 上标；③ 字号更小 + 基线压低 → 下标
                #    再加「长度 ≤15 字符」的限制，避免把作者单位那类小字号文字误判进去
                smaller = base_size > 0 and size <= base_size * 0.85 \
                    and len(span_text.strip()) <= 15
                raised = (base_baseline - baseline) >= max(0.8, 0.25 * size)
                lowered = (baseline - base_baseline) >= max(0.6, 0.20 * size)

                if smaller and raised:
                    span_text = _convert_script(span_text, _to_superscript)
                elif smaller and lowered:
                    span_text = _convert_script(span_text, _to_subscript)

                sizes.append(size)
                total_chars += len(span_text)
                # flags 第 4 位（值 16）代表粗体
                if int(span.get("flags", 0)) & 16:
                    bold_chars += len(span_text)

            parts.append(span_text)

        text = "".join(parts)
        bbox = line.get("bbox") or (0.0, 0.0, 0.0, 0.0)
        records.append({
            "text": text,
            "max_size": max(sizes) if sizes else 0.0,
            "bold_ratio": (bold_chars / total_chars) if total_chars else 0.0,
            "bold_chars": bold_chars,
            "chars": total_chars,
            "bbox": bbox,
            "height": bbox[3] - bbox[1],
            # 这一行是否用了数学字体（公式的强证据）
            "math_font": any(_MATH_FONT_RE.search(str(span.get("font", "")))
                             for span in visible),
            # 这一行的基准基线（上下标判定的基准，也是公式片段的证据之一）
            "baseline": base_baseline,
            # 这一行用到的字体名（公式判定里「有没有用数学字体」）
            "fonts": tuple(str(span.get("font", "")) for span in visible if span.get("font")),
        })
    return records


def _split_leading_heading(records):
    """
    把「块首的标题行」从正文块里拆出来。

    为什么需要这一步：不少期刊排版里，小标题会和它下面那段正文被 PyMuPDF 归进同一个块，
    例如 "2.2 Gesture Recognition and Interaction" + "Gesture recognition approaches…"。
    如果不拆，标题就会被当成正文的头部，白白丢掉一个标题卡片。

    判断依据（只检查开头最多 3 行）：
      · 该行字号 ≥ 正文行字号 × 1.12，或者
      · 该行基本全加粗，而后续正文行不加粗
    返回值：(标题行列表, 正文行列表)
    """
    if len(records) < 2:
        return [], records

    body = records[1:]
    body_sizes = sorted(r["max_size"] for r in body)
    body_size = body_sizes[len(body_sizes) // 2]
    body_bold = sum(r["bold_ratio"] for r in body) / len(body)

    k = 0
    for record in records[:3]:
        if count_words(record["text"]) > 20 or len(record["text"]) > 160:
            break                                     # 太长，不像标题
        bigger = body_size > 0 and record["max_size"] >= body_size * 1.12
        bolder = record["bold_ratio"] >= 0.6 and body_bold <= 0.3
        if bigger or bolder:
            k += 1
        else:
            break

    if k == 0 or k == len(records):
        return [], records
    return records[:k], records[k:]


def _split_formula_lines(records):
    """
    把「独立成行的公式」从段落里拆出来，返回 [(行记录列表, 是否公式), …]。

    为什么必须拆：很多论文的公式是**单独一行、居中或缩进**的（比正文行短），
    但 PDF 的块划分常把它和前后散文归进同一个块；不拆的话它会被当成正文
    粘进段落里，后面所有基于「块」的公式判定都看不到它，永远转不成图片。

    判据（错拆的代价很小，所以可以宽松些）：
      · 行宽明显小于本块最宽行（<0.8 倍）——公式行通常短且居中
      · 词数 ≤20
      · 符号密度够高（_looks_mathy）
    """
    if len(records) < 2:
        return [(records, False)]

    widths = [r["bbox"][2] - r["bbox"][0] for r in records]
    widest = max(widths) or 1.0
    left_margin = min(r["bbox"][0] for r in records)

    groups, current = [], []
    for record, width in zip(records, widths):
        text = record["text"].strip()
        # 判据（三道都要满足）：
        #   ① 明显比最宽行短，**且左右两边都留白**（真正的居中公式）——
        #      只用「比最宽行短」是不行的：段落的最后一行本来就短，
        #      实测把 "group, F(1, 31) = 5.92, p = 0.021, hₚ" 这种句子碎片拆成了公式图；
        #   ② 用了数学字体（公式的强证据，段落散文不会用）；
        #   ③ 符号密度够高。
        left_slack = record["bbox"][0] - left_margin
        right_slack = (left_margin + widest) - record["bbox"][2]
        is_display_formula = (
            width < widest * 0.8
            and left_slack >= 12.0
            and right_slack >= 12.0
            and record.get("math_font", False)
            and count_words(text) <= 20
            and _looks_mathy(text)
            # 还要有「公式结构」：比较运算符或上下标。
            # 只靠箭头符号（图里的 −−−→ 之类）不算，那是指示线不是公式。
            and (bool(set(text) & set("=≤≥≠<>")) or bool(_FORMULA_SCRIPT_RE.search(text)))
        )
        if is_display_formula:
            if current:
                groups.append((current, False))
                current = []
            groups.append(([record], True))
        else:
            current.append(record)
    if current:
        groups.append((current, False))
    return [group for group in groups if group[0]]


def _is_standalone_formula_line(record, page_width: float) -> bool:
    """
    单行块是不是一条独立公式？

    为什么需要单独一套判据：_split_formula_lines 靠「比块内最宽行短」判断，
    那需要块内有多行做参照；而**很多公式在 PDF 里本来就是一个独立的单行块**
    （实测 `v_threshold = k⋅v̄ᵢ` 就是这种）。这类块如果不在这里判出来，
    后面会被段落合并粘进上一行散文（因为它的首字符是数学斜体小写字母），
    再因为整块词数超限而被公式判定拒掉——永远转不成图片。

    判据：明显窄于页面 + 用了数学字体 + 符号密度高 + 有比较符或上下标。
    """
    text = record.get("text", "").strip()
    if not text or count_words(text) > 14:
        return False
    if not record.get("math_font", False):
        return False

    width = record["bbox"][2] - record["bbox"][0]
    if width > page_width * 0.6:            # 公式行不会占满版面宽度
        return False
    if not _looks_mathy(text):
        return False
    return bool(set(text) & set("=≤≥≠<>")) or bool(_FORMULA_SCRIPT_RE.search(text))


def _make_block(page_no: int, records, dehyphenate: bool):
    """把若干行记录合成一个 Block（坐标取并集，字号/加粗按内容汇总）"""
    text = join_lines([r["text"] for r in records], dehyphenate).strip()
    text = add_script_spacing(text)
    text = repair_stats_symbols(text)
    # 字形错映射的修复放在**块**这一层：门槛要看「整块里有没有上下标/错映射括号」，
    # 只按行判断会漏——实测那处分数公式的 '¼' 与 'ð Þ' 分属两行，
    # 逐行看谁都不满足条件，于是等号一直没修回来。
    text = repair_obfuscated_math(text).strip()
    if not text:
        return None

    sizes = [r["max_size"] for r in records]
    heights = sorted(r["height"] for r in records)
    baselines = sorted(r.get("baseline", 0.0) for r in records)
    fonts = tuple(dict.fromkeys(f for r in records for f in r.get("fonts", ())))
    chars = sum(r["chars"] for r in records)
    bold_chars = sum(r["bold_chars"] for r in records)

    return Block(
        page=page_no,
        x0=min(r["bbox"][0] for r in records),
        y0=min(r["bbox"][1] for r in records),
        x1=max(r["bbox"][2] for r in records),
        y1=max(r["bbox"][3] for r in records),
        text=text,
        max_size=max(sizes) if sizes else 0.0,
        bold_ratio=(bold_chars / chars) if chars else 0.0,
        line_height=heights[len(heights) // 2] if heights else 10.0,
        font_names=fonts,
        origin_baseline=baselines[len(baselines) // 2] if baselines else 0.0,
    )


def extract_page(page, page_no: int, dehyphenate: bool = True):
    """
    一次 `page.get_text("dict")` 同时产出这一页的【文字块】与【文字行】。

    为什么要一起产出：表格判定（阶段 4.4-B）需要**行级**数据，而块级数据本来
    就要解析一遍页面；分成两个函数各解析一次的话，实测多表样本的解析耗时
    会多出 1.7 秒（双栏.pdf 2.27s → 3.98s），纯粹是白跑的。

    返回 (blocks, lines)：
        blocks  文字块（与旧版 extract_page_blocks 完全一致）
        lines   文字行（不合并成块，表格判据用，见 table_finder 的说明）
    """
    data = page.get_text("dict")
    blocks, lines = [], []

    for raw in data.get("blocks", []):
        if raw.get("type") != 0:        # type 0 = 文字，1 = 图片（图片留到阶段 3 处理）
            continue

        records = _line_records(raw)
        if not records:
            continue

        # ---- 行级数据（表格判定用）----
        for record in records:
            text = record["text"].strip()
            if not text:
                continue
            bbox = record["bbox"]
            lines.append({
                "page": page_no,
                "text": text,
                "x0": bbox[0], "y0": bbox[1], "x1": bbox[2], "y1": bbox[3],
                "size": record["max_size"],
            })

        # ---- 块级数据 ----
        # 一个 PyMuPDF 块可能产出多个 Block：
        #   · 块首的标题行要拆出来（_split_leading_heading）
        #   · 独立成行的公式也要拆出来（_split_formula_lines）
        for part in _split_leading_heading(records):
            if not part:
                continue
            for piece, is_formula in _split_formula_lines(part):
                block = _make_block(page_no, piece, dehyphenate)
                if block is None:
                    continue
                # 单行块用另一套判据（拆行判据需要块内有多行做宽度参照）
                if not is_formula and len(piece) == 1:
                    is_formula = _is_standalone_formula_line(piece[0], page.rect.width)
                block.is_formula = is_formula
                blocks.append(block)

    return blocks, lines


def extract_page_blocks(page, page_no: int, dehyphenate: bool = True):
    """
    取出一页里的所有【文字】块（兼容入口：个别脚本与单测直接调用它）。

    说明：计划里写的是 page.get_text("blocks")，这里改用 page.get_text("dict")。
    原因：blocks 模式不返回字号和加粗信息，而识别标题必须靠字号；
    "dict" 模式同样按 块/行/span 组织，块划分与 blocks 一致，但信息更全。
    """
    return extract_page(page, page_no, dehyphenate)[0]


def extract_page_lines(page, page_no: int):
    """
    取出一页里的所有【文字行】——**不合并成块**，专供表格判定使用（阶段 4.4-B）。

    为什么必须单独来一趟：表格一行里的每个单元格在 PDF 里都是**独立的行对象**
    （实测 npj 一行 8 个、Nature 7 个、ACM 4 个），而 extract_page_blocks 会把
    同一行的单元格合并成一个 Block——合并之后「哪几列对齐」这个信息就没了，
    表格判据正是靠它区分「表格」和「两栏散文」。

    实现上直接复用 extract_page（一次 get_text 同时产出块与行），不额外解析页面。
    """
    return extract_page(page, page_no)[1]


def detect_gutter(blocks, page_width: float):
    """
    检测这一页是不是双栏排版。返回中缝的 x 坐标；判定为单栏时返回 None。

    思路（比「数块数」稳得多）：
      把每个文本块的横向范围投影到 x 轴上，按【字符数】累加覆盖量。
      双栏排版会在页面中部出现一条覆盖量明显偏低的竖直带，那就是中缝。

    为什么不用块数：短标题、页码、公式编号会让块数统计严重失真。真实翻过的车——
      Cell Press 某页左栏只有 2 个长块、右栏有 6 个，用「左右各 ≥3 块」的门槛一卡就
      漏判成单栏，于是左右栏文字被按 y 坐标混排，摘要和正文串成了
      "SUMMARY in which actresses remained still…" 这种读不通的句子。
    """
    # 只用较长文本参与判断，避开页眉、页码、编号这类噪声
    candidates = [b for b in blocks if len(b.text) >= MIN_COLUMN_CHARS]
    total = sum(len(b.text) for b in candidates)
    if len(candidates) < 4 or total < 400:
        return None

    lo, hi = int(page_width * 0.25), int(page_width * 0.75)
    if hi - lo < 20:
        return None

    # 覆盖量投影：cover[i] = 覆盖住该 x 位置的所有文本块的字符数之和
    cover = [0] * (hi - lo + 1)
    for b in candidates:
        a, z = max(int(b.x0), lo), min(int(b.x1), hi)
        if z < a:
            continue
        chars = len(b.text)
        for index in range(a - lo, z - lo + 1):
            cover[index] += chars

    peak = max(cover)
    if peak <= 0:
        return None

    threshold = 0.45 * peak        # 覆盖量低于峰值的 45% 才算「空白带」
    best = None
    index, width = 0, len(cover)

    while index < width:
        if cover[index] > threshold:
            index += 1
            continue

        start = index
        while index < width and cover[index] <= threshold:
            index += 1
        end = index - 1

        if end - start + 1 >= 6:                      # 中缝至少要 6pt 宽
            center = lo + (start + end) // 2
            left_chars = sum(len(b.text) for b in candidates if b.x1 <= center)
            right_chars = sum(len(b.text) for b in candidates if b.x0 >= center)
            span_chars = sum(len(b.text) for b in candidates if b.x0 < center < b.x1)

            # 两侧都要有足够文字（否则那只是「一侧是图」的页面，不是双栏）
            if left_chars >= 0.15 * total and right_chars >= 0.15 * total:
                score = min(left_chars, right_chars) - 2 * span_chars
                if best is None or score > best[0]:
                    best = (score, center)

    return best[1] if best else None


def order_blocks(blocks, gutter):
    """
    按阅读顺序重排一页里的块。

    单栏：直接按 y（从上到下）。
    双栏：难点在于「通栏块」（大标题、摘要、跨栏图注）会打断两栏。做法是把通栏块当分段点：
        - 通栏块自己排在它所在的位置
        - 通栏块【下方】的两栏内容，按「先读完左栏、再读右栏」排
    排序键 = (上方最近通栏块的 y0, 栏目, y0)，栏目 -1 排在 0/1 之前。
    """
    for b in blocks:
        if gutter is None:
            b.column = -1                       # 单栏：全部当通栏处理
        elif b.x1 <= gutter:
            b.column = 0                        # 左栏
        elif b.x0 >= gutter:
            b.column = 1                        # 右栏
        else:
            b.column = -1                       # 横跨中缝 → 通栏

    if gutter is None:
        return sorted(blocks, key=lambda b: (b.y0, b.x0))

    full_width = sorted([b for b in blocks if b.column == -1], key=lambda b: b.y0)

    def anchor_y(block) -> float:
        """找出 block 上方最近那个通栏块的 y0；没有则用 -1 表示「在最上面」"""
        a = -1.0
        for f in full_width:
            if f.y0 <= block.y0 + 1.0:
                a = f.y0
            else:
                break
        return a

    keyed = []
    for b in blocks:
        if b.column == -1:
            keyed.append(((b.y0, -1, b.y0), b))
        else:
            keyed.append(((anchor_y(b), b.column, b.y0), b))
    keyed.sort(key=lambda item: item[0])
    return [b for _, b in keyed]


# ============================================================
# 四、段落合并
# ============================================================

def _should_merge(prev: Block, cur: Block) -> bool:
    """
    判断相邻两块是不是「同一个自然段被 PDF 拆开了」。
    采取保守策略：宁可少合并，也不要错合并（错合并会让两张卡片粘成一张）。
    """
    # 公式块不参与段落合并：否则拆出来的公式行又会被粘回散文里
    # 表格块同理：表格行一旦和上下文的散文粘在一起，就再也切不干净了
    if prev.is_formula or cur.is_formula or prev.is_table or cur.is_table:
        return False

    if cur.page == prev.page + 1:
        gap = 0.0                     # 跨页：y 坐标会重置，无法比较间距，视为紧邻
    elif cur.page != prev.page:
        return False                  # 隔了多页，肯定不是同一段
    else:
        gap = cur.y0 - prev.y1

    # 后一块字号明显更大 → 多半是标题，不能并进正文
    if prev.max_size > 0 and cur.max_size > prev.max_size * 1.15:
        return False

    # 同页时才检查栏位；跨页时「上页右栏 → 下页左栏」是正常的续写
    if cur.page == prev.page and prev.column != cur.column:
        return False

    if gap > 0.9 * max(prev.line_height, 1.0):
        return False                  # 垂直间距够大 → 本来就是两个段落

    text = prev.text.rstrip()
    if not text:
        return False

    if text.endswith(("-", "‐", "\xad")):
        return True                   # 行尾断词，必须接上

    if text.endswith((".", "!", "?", "。", "！", "？", ":", ";", "”", '"')):
        return False                  # 正常段落结尾 → 到此为止

    # 剩下情况：前块没写完（缺句末标点），后块以小写字母开头 → 认为是同段续写
    first_char = cur.text[:1]
    if first_char.islower():
        return True

    # 上标被拆到下一块开头也是同一段的续写：
    # 例如 "...p = 0.021, hp" | "² = 0.16), demonstrating…"
    return bool(re.match(r"[\d²³¹⁰⁴⁵⁶⁷⁸⁹₀-₉)\]},;%·]", first_char))


def build_paragraph_groups(blocks) -> list:
    """
    把原子块按「同一自然段」分组，返回索引分组，例如 [[0], [1, 2], [3], …]。

    **纯派生**：不修改任何原子块，随时可以重算或回退——这正是本轮修复的核心。
    旧版 merge_paragraphs 直接把后一块的文本塞进前一块，公式碎片一旦被并进散文，
    后面所有基于块的公式判定就再也看不到它了。

    三条规则：
      · 已经带公式等级的块（强 / 弱 / 碎片）**各自单独成组**，
        否则碎片会被当成段落续写吞掉；
      · 属于表格区域的块也各自单独成组（表格要整块出图，不能与散文粘在一起）；
      · 其余按 _should_merge 的保守规则判断「后一块是不是前一块的续写」。
    """
    groups = []
    for index, block in enumerate(blocks):
        if block.is_formula or block.formula_tier or block.is_table:
            groups.append([index])
            continue
        if groups and _should_merge(blocks[groups[-1][-1]], block):
            groups[-1].append(index)
            continue
        groups.append([index])
    return groups


def build_paragraphs(blocks, groups, dehyphenate: bool = True) -> list:
    """
    按索引分组拼出段落视图（Paragraph）。

    视图是派生对象：写它的 role / kind 不会影响原子块，
    需要时再由 propagate_paragraph_roles 把结论回写到原子块。
    """
    paragraphs = []
    for group in groups:
        members = [blocks[index] for index in group]
        first = members[0]
        total_chars = max(sum(len(b.text) for b in members), 1)

        kwargs = {f.name: getattr(first, f.name) for f in fields(Block)}
        kwargs.update(
            text=_join_pieces(members, dehyphenate),
            page=min(b.page for b in members),
            page_end=max(b.page_end or b.page for b in members),
            x0=min(b.x0 for b in members),
            y0=min(b.y0 for b in members),
            x1=max(b.x1 for b in members),
            y1=max(b.y1 for b in members),
            max_size=max(b.max_size for b in members),
            line_height=min(b.line_height for b in members),
            bold_ratio=sum(b.bold_ratio * len(b.text) for b in members) / total_chars,
            # 聚合出来的视图不继承原子块的卡片字段（避免共享可变对象）
            segments=[], headings_inside=[], card_index=0,
            atom_indices=list(group),
        )
        paragraphs.append(Paragraph(**kwargs))
    return paragraphs


def build_paragraph_layer(blocks, merge_on: bool = True, dehyphenate: bool = True):
    """一次算好「分组索引 + 段落视图」，返回 (groups, paragraphs)。

    分组是纯派生，可以在管线里算多次（公式判定前后各算一次）：
    第二次算出来的分组会把公式块单独摘出来，不再并进散文。
    """
    if merge_on:
        groups = build_paragraph_groups(blocks)
    else:
        groups = [[index] for index in range(len(blocks))]
    return groups, build_paragraphs(blocks, groups, dehyphenate)


def propagate_paragraph_roles(blocks, paragraphs) -> None:
    """
    把段落层判定的角色回写到原子块。

    角色本身是段落级判断（要读完整的一段话才能定），但界面、角色统计、
    背景信息面板都是按块查询的，所以结论要落回原子块。
    """
    for paragraph in paragraphs:
        for index in paragraph.atom_indices:
            blocks[index].role = paragraph.role
            blocks[index].kind = paragraph.kind
            blocks[index].level = paragraph.level


def sync_paragraph_formulas(blocks, paragraphs, clusters) -> None:
    """
    把原子块的公式状态同步到段落视图（卡片构建、界面渲染读的是段落层）。

    簇内段落的坐标会被**撑到整簇的外接矩形**：公式图渲染的就是这个矩形，
    一个区域一张图。不撑开的话，簇里每块碎片都会按自己的小矩形各出一张图，
    等于又回到了「公式被拆成好几张图」的老问题。
    """
    cluster_by_id = {cluster.cluster_id: cluster for cluster in clusters}
    for paragraph in paragraphs:
        head = blocks[paragraph.atom_indices[0]]
        paragraph.is_formula = head.is_formula
        paragraph.cluster_id = head.cluster_id
        paragraph.formula_tier = head.formula_tier

        cluster = cluster_by_id.get(paragraph.cluster_id)
        if cluster is not None:
            paragraph.x0, paragraph.y0, paragraph.x1, paragraph.y1 = cluster.rect
            paragraph.page = cluster.page


def sync_paragraph_tables(blocks, paragraphs, regions) -> None:
    """
    把原子块的表格状态同步到段落视图（卡片构建、界面渲染读的是段落层）。

    与公式同理：区域内的段落坐标被**撑到整个表格区域的外接矩形**——
    表格图渲染的就是这个矩形，一张表只出一张图，不会每个单元格各出一张。
    """
    region_by_id = {region.region_id: region for region in regions}
    for paragraph in paragraphs:
        head = blocks[paragraph.atom_indices[0]]
        paragraph.is_table = head.is_table
        paragraph.table_id = head.table_id

        region = region_by_id.get(paragraph.table_id)
        if region is not None:
            paragraph.x0, paragraph.y0, paragraph.x1, paragraph.y1 = region.rect
            paragraph.page = region.page
            paragraph.table_caption = region.caption


# ============================================================
# 五、切卡片
# ============================================================

def split_long_block(block: Block, max_words: int, hard_limit: int = 300):
    """
    超长段落切成多张卡片。
    规则：超过 hard_limit（默认 300 词）才切；切的时候落在【句子边界】上，
          每张卡片控制在 max_words 词左右，绝不把一句话劈成两半。
    """
    if count_words(block.text) <= hard_limit:
        return [block]

    sentences = split_sentences(block.text)
    if len(sentences) <= 1:
        return [block]                # 只有一句话（超长），切不了就别切

    chunks, current, current_words = [], [], 0
    for sentence in sentences:
        words = count_words(sentence)
        if current and current_words + words > max_words:
            chunks.append((current, current_words))
            current, current_words = [], 0
        current.append(sentence)
        current_words += words
    if current:
        chunks.append((current, current_words))

    if len(chunks) <= 1:
        return [block]

    # 把原 y 范围均分给各张卡片，让诊断表里的坐标看着合理
    height = (block.y1 - block.y0) / len(chunks)
    return [
        replace(block, text=" ".join(sents), y0=block.y0 + i * height, y1=block.y0 + (i + 1) * height)
        for i, (sents, _) in enumerate(chunks)
    ]


# ============================================================
# 六、标题识别
# ============================================================

def estimate_body_size(blocks) -> float:
    """
    估算「正文字号」：按字符数加权，取出现最多的字号。
    正文字数远多于标题，所以这个值非常稳，是识别标题的基准。
    """
    counter = Counter()
    for b in blocks:
        if b.text:
            counter[round(b.max_size, 1)] += len(b.text)
    if not counter:
        return 10.0
    return counter.most_common(1)[0][0]


def mark_headings(blocks, body_size: float) -> None:
    """
    识别标题并标记级别（就地修改 blocks）。

    判定分三种「证据」，组合使用（阈值都是拿真实论文调出来的）：
      · 字号：比正文大 ≥1.45 倍 → 一级标题（论文大标题）；≥1.12 倍 → 候选二级
      · 加粗：加粗字符占比 ≥0.6，且很短（≤14 词），且字号不小于正文的 95%
      · 形式：像章节名（Abstract/References…），或带编号（"3 Study"、"3.1 Task"）

    为什么要组合：期刊的图内标签、表格表头也常常又大又粗
    （例如坐标轴上的 "Cursor C"、表头的 "M (SD) [°]"）。
    单看字号会误判，所以「字号大」必须再配上「加粗」或「像章节名/带编号」才算数。
    """
    for b in blocks:
        text = b.text.strip()
        words = count_words(text)
        b.kind, b.level = "body", 0

        if not text or words > 25 or len(text) > 200:
            continue                                   # 太长，不可能是标题

        ratio = (b.max_size / body_size) if body_size else 1.0

        is_bold = b.bold_ratio >= 0.6
        is_section = bool(_SECTION_RE.match(text)) and words <= 8
        # 带编号的标题："3 Study" / "3.1 Task" / "2.4.1 Something"
        is_numbered = bool(_NUMBERED_RE.match(text)) and words <= 12
        has_math = bool(set(text) & _STRONG_MATH_CHARS)

        # 完整句子（以句号结尾且较长）一般不是标题，例外是章节名
        if text.endswith((".", "。")) and words > 6 and not is_section:
            continue

        if ratio >= 1.45:
            b.kind, b.level = "heading", 1
        elif has_math:
            continue                                   # 公式/伪代码，不是标题
        elif ratio >= 1.12 and (is_bold or is_section or is_numbered):
            b.kind, b.level = "heading", 2
        elif is_section:
            b.kind, b.level = "heading", 2
        elif (is_bold and ratio >= 0.95) or is_numbered:
            b.kind, b.level = "heading", 3


# ============================================================
# 七、内容角色分类：把「正文」和「论文背景信息」分开
# ============================================================

def _repeat_key(text: str) -> str:
    """跨页重复判断用的键：去掉数字、统一空白，避免页码差异导致认不出 running head"""
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text.strip().lower()))[:70]


def classify_roles(blocks, page_heights: dict, total_pages: int, body_size: float) -> None:
    """
    给每个块标注内容角色（就地修改 role）。

    判定顺序按「证据强度」从强到弱：
      1. 已被识别成标题的 → 章节标题
      2. 跨页反复出现的短文本 → 页眉页脚（最可靠的信号）
      3. "Figure 3." / "Table 2:" 这种编号开头的 → 图注
      4. 版权 / 许可 / 资助关键词 → 版权声明
      5. References 标题之后的所有块，或条目模式 → 参考文献
      6. 整块就是栏目名（Authors / Highlights / CCS Concepts…）→ 栏目名
      7. 第一页：没有长正文 → 整页是封面页；有长正文 → 标题与摘要之间是作者单位
      8. 剩下的 → 正文
    """
    # ---- 跨页重复的短文本（页眉页脚）----
    # 关键：必须要求「出现在 ≥3 个**不同页**」。
    # 只看出现次数会误伤作者区——ACM 那篇有 3 位作者共用同一行单位文字
    # （"School of Computer Science Queensland University of Technology" 在同一页出现 3 次），
    # 按次数判定就会把单位行标成页眉页脚，阶段 4.2 就取不到单位了。
    pages_by_key = {}
    for block in blocks:
        if count_words(block.text) <= 20:
            pages_by_key.setdefault(_repeat_key(block.text), set()).add(block.page)
    repeated = {key for key, pages in pages_by_key.items() if len(pages) >= 3}

    # ---- 参考文献的起始位置 ----
    reference_start = None
    for index, b in enumerate(blocks):
        text = b.text.strip()
        if count_words(text) <= 5 and _REFERENCES_HEADING_RE.match(text):
            reference_start = index
            break

    for index, b in enumerate(blocks):
        text = b.text.strip()
        words = count_words(text)

        # ① 参考文献区间：References 标题本身及其后的全部内容都算参考文献
        if reference_start is not None and index >= reference_start:
            b.role = ROLE_REFERENCE
            continue

        # ② 跨页反复出现的短文本 → 页眉页脚。
        #    这条必须排在标题判定之前：期刊的 running head / 页脚有时会被当成标题，
        #    例如 Cell Press 每页底部的 "3086 Current Biology 25, 3086–3091…"
        if words <= 20 and _repeat_key(text) in repeated:
            b.role = ROLE_HEADER_FOOTER
            continue

        # ③ 标题：但期刊刊头（Report / Current Biology Report）和
        #    图内面板标签（"A B C D"）都不是论文章节
        if b.kind == "heading":
            if _MASTHEAD_RE.match(text) or _PANEL_LABEL_RE.match(text):
                b.role = ROLE_LABEL
            else:
                b.role = ROLE_HEADING
            continue

        if _CAPTION_RE.match(text):
            b.role = ROLE_CAPTION
            continue

        if _COPYRIGHT_RE.search(text):
            b.role = ROLE_COPYRIGHT
            continue

        if _REFERENCE_ITEM_RE.match(text) and b.page > total_pages * 0.6:
            b.role = ROLE_REFERENCE
            continue

        if _LABEL_RE.match(text):
            b.role = ROLE_LABEL
            continue

        b.role = ROLE_BODY

    # ---- 第一、二页的作者区（不少期刊在第 2 页重复标题和作者，如 Cell Press）----
    document_title = ""
    for page_no in (1, 2):
        page_blocks = [b for b in blocks if b.page == page_no]
        if not page_blocks:
            continue

        # 先找「像论文标题」的大字号块（字号明显大于正文）。要先做这一步：
        # 封面页也要靠它记下论文标题，供后面识别「后续页重复的 running title」
        title_block = None
        for b in page_blocks:
            if b.role != ROLE_HEADING or b.max_size < 1.3 * body_size:
                continue
            if title_block is None or b.order < title_block.order:
                title_block = b
        if page_no == 1 and title_block is not None:
            document_title = _repeat_key(title_block.text)

        long_body = [b for b in page_blocks
                     if b.role in READING_ROLES and count_words(b.text) >= 60]

        if not long_body:
            # 这页没有任何长正文 → 是「封面页/前页」（Cell Press 那种
            # Highlights + Authors + Correspondence + In Brief 版面），整页算背景
            if page_no == 1:
                for b in page_blocks:
                    if b.role == ROLE_BODY:
                        b.role = ROLE_FRONT_MATTER
            continue

        if title_block is None:
            continue

        first_long = min(long_body, key=lambda b: b.order)
        for b in page_blocks:
            if b.role != ROLE_BODY:
                continue
            if b.order <= title_block.order or b.order >= first_long.order:
                continue
            b.role = ROLE_AUTHOR        # 标题与摘要之间 = 作者、单位、联系方式

    # 后续页面重复出现的论文标题（running title）不是章节，归入背景
    if document_title:
        for b in blocks:
            if b.page >= 2 and b.role == ROLE_HEADING and _repeat_key(b.text) == document_title:
                b.role = ROLE_LABEL


# ============================================================
# 八、阅读卡片：正文按章节聚合到目标词数
# ============================================================

def _join_pieces(pieces, dehyphenate: bool = True) -> str:
    """把多个块的文本拼成一整段（处理行尾断词）"""
    out = ""
    for piece in pieces:
        text = piece.text.strip()
        if not text:
            continue
        if not out:
            out = text
        elif dehyphenate and out[-1] in "-‐\xad" and text[:1].islower():
            out = out[:-1] + text
        else:
            out = f"{out} {text}"
    return out


def _text_entries(buffer):
    """缓冲里的**文字**内容（公式除外——公式不参与翻译，也不计入词数）"""
    return [paragraph for kind, paragraph in buffer if kind in ("heading", "text")]


def _formula_payload(paragraph) -> dict:
    """公式段落 → 渲染载荷（页码 + 区域矩形 + 线性文本兜底）"""
    return {
        "page": paragraph.page,
        "rect": (paragraph.x0, paragraph.y0, paragraph.x1, paragraph.y1),
        "text": paragraph.text,
    }


def _table_payload(paragraph) -> dict:
    """表格段落 → 渲染载荷（同一套截图管线，只是换了区域矩形与标题）"""
    return {
        "page": paragraph.page,
        "rect": (paragraph.x0, paragraph.y0, paragraph.x1, paragraph.y1),
        "text": paragraph.text,
        "caption": getattr(paragraph, "table_caption", ""),
    }


def _build_segments(buffer):
    """
    把缓冲里的段落转成卡片段落。

    段落类型：
        ("heading", 文本)   章节标题（卡片内加粗显示）
        ("text",    文本)   正文
        ("formula", {...})  公式——**渲染成图片**，因为二维公式没法用一行文字表达
        ("table",   {...})  表格——同样渲染成图片，因为线性化后列关系全丢

    公式段落的范围直接取「公式区域」（formula_finder 聚类得到的整簇外接矩形）：
    一个区域只出一张图，区域内的其余块在 build_reading_cards 里已经跳过。
    不再需要「按垂直间距把相邻公式粘起来」那种补丁式逻辑——聚类已经把
    「同一处公式的上下游碎片」合并好了。表格段落同理，范围取整张表的外接矩形。
    """
    segments = []
    for kind, paragraph in buffer:
        text = paragraph.text.strip()
        if not text:
            continue
        if kind == "formula":
            segments.append(("formula", _formula_payload(paragraph)))
        elif kind == "table":
            segments.append(("table", _table_payload(paragraph)))
        elif kind == "heading":
            segments.append(("heading", text))
        else:
            segments.append(("text", text))
    return segments


def _append_to_card(card, buffer, dehyphenate: bool = True):
    """把缓冲里的内容并进已有卡片（用于收掉章节末尾的零头，避免出现过短的尾卡）"""
    card.segments.extend(_build_segments(buffer))
    for kind, paragraph in buffer:
        if kind == "heading" and paragraph.text.strip():
            card.headings_inside.append(parse_section(paragraph.text.strip()))

    card.text = _join_pieces([card] + _text_entries(buffer), dehyphenate)
    card.page_end = max(card.page_end or card.page,
                        max((p.page_end or p.page) for _, p in buffer))
    card.y1 = max(card.y1, max(p.y1 for _, p in buffer))


def _make_card(buffer, section_number: str, section_title: str, dehyphenate: bool = True):
    """
    把缓冲里的段落合成一张阅读卡片。

    buffer 的元素是 (kind, Paragraph)：kind="heading" 章节标题、"text" 正文、
    "formula" 公式区域。卡片的 text 是**纯文字**拼平后的全文（翻译、词数统计用），
    公式的线性文本不入内——它已经由公式图片承载，混进来只会污染翻译与计数。
    segments 保留内部结构，界面据此加粗标题、把公式渲染成图。
    """
    items = [paragraph for _, paragraph in buffer]
    texts = _text_entries(buffer)
    first = texts[0] if texts else items[0]
    total_chars = sum(len(p.text) for p in texts) or 1
    weighted_bold = sum(p.bold_ratio * len(p.text) for p in texts) / total_chars

    segments = _build_segments(buffer)
    headings_inside = [parse_section(p.text.strip()) for kind, p in buffer
                       if kind == "heading" and p.text.strip()]

    return Block(
        page=min(p.page for p in items),
        page_end=max((p.page_end or p.page) for p in items),
        x0=min(p.x0 for p in items),
        y0=min(p.y0 for p in items),
        x1=max(p.x1 for p in items),
        y1=max(p.y1 for p in items),
        text=_join_pieces(texts, dehyphenate),
        max_size=max(p.max_size for p in items),
        bold_ratio=weighted_bold,
        line_height=first.line_height,
        column=first.column,
        kind="body",
        level=0,
        role=ROLE_BODY,
        section_number=section_number,
        section_title=section_title,
        segments=segments,
        headings_inside=headings_inside,
    )


def build_reading_cards(paragraphs, target_words: int = 400, dehyphenate: bool = True):
    """
    把段落聚合成「阅读卡片」。

    规则（对应用户要求：每张 100~300 词、语义连贯）：
      · 只收 READING_ROLES 的段落（正文 + 图注；图注按用户要求视作正文）
      · 正文按阅读顺序连续累加，凑到目标下限就收一张卡
      · 章节标题**进卡片内部**、保持原位并加粗显示，不再单独占一张卡——
        否则像 CHI 这种小节特别多的论文，每张卡都只有一两百词，凑不到目标长度
      · 卡片不跨「非正文」区域（页眉页脚、参考文献等会被跳过）
      · 公式区域整块出图（一个区域只出一张图），**不计入词数、不进翻译**
      · 末尾零头能并进上一张就并；单个超长段落先按句子边界切开
    """
    # 「目标词数」是攒到多少就收一张卡的阈值（默认 200），
    # 实际落点区间是它的 0.5~1.5 倍（默认 100~300 词）。
    flush_at = max(60, target_words)
    target_min = max(40, round(target_words * 0.5))    # 低于这个值的尾卡会并进上一张
    target_max = round(target_words * 1.5)             # 单块超过这个值按句子边界切开
    slack = round(target_words * 0.25)                 # 尾卡并入后允许超出的余量

    # 先给每个段落标上所属章节：标题之后的内容都属于该标题开启的章节
    current_number, current_title = "", ""
    for paragraph in paragraphs:
        if paragraph.role == ROLE_HEADING:
            current_number, current_title = parse_section(paragraph.text)
        paragraph.section_number = current_number
        paragraph.section_title = current_title

    # 公式区域只出一张图：簇内第一个成员负责出图，同簇其余成员直接跳过。
    # （不这样做的话，一个跨卡片边界的区域会在两张卡里各渲染一次。）
    cluster_head = {}
    for paragraph in paragraphs:
        if paragraph.cluster_id >= 0:
            cluster_head.setdefault(paragraph.cluster_id, paragraph.atom_indices[0])

    # 表格区域同理：整张表只出一张图，由区域里第一个成员负责
    table_head = {}
    for paragraph in paragraphs:
        if paragraph.table_id >= 0:
            table_head.setdefault(paragraph.table_id, paragraph.atom_indices[0])

    cards = []
    buffer = []                     # [(kind, Paragraph)]，kind = "heading"/"text"/"formula"
    buffer_section = ("", "")

    def buffered_words() -> int:
        return sum(count_words(p.text) for kind, p in buffer if kind == "text")

    def close_buffer():
        """收卡。若末尾只是零头（不足下限），尽量并进上一张，避免出现过短的尾卡。"""
        nonlocal buffer
        if not buffer:
            return

        words = buffered_words()
        previous = cards[-1] if cards else None
        can_merge = (
            previous is not None
            and words < target_min
            and count_words(previous.text) + words <= target_max + slack
        )

        if can_merge:
            _append_to_card(previous, buffer, dehyphenate)
        else:
            cards.append(_make_card(buffer, buffer_section[0], buffer_section[1], dehyphenate))

        buffer = []

    for paragraph in paragraphs:
        # 同一公式区域的非头部成员：图已经出过，这里直接跳过
        if paragraph.cluster_id >= 0 \
                and cluster_head.get(paragraph.cluster_id) != paragraph.atom_indices[0]:
            continue
        # 同一表格区域的非头部成员：同理（表格区域可能横跨多个块）
        if paragraph.table_id >= 0 \
                and table_head.get(paragraph.table_id) != paragraph.atom_indices[0]:
            continue

        if paragraph.role == ROLE_HEADING and not paragraph.is_table:
            # 章节标题进卡片内部（保持原位、加粗显示），不再单独占一张卡
            if not buffer:
                buffer_section = (paragraph.section_number, paragraph.section_title)
            buffer.append(("heading", paragraph))
            continue

        if paragraph.is_table:
            # 表格段落**必须排在「标题」分支之前**：表题注通常加粗、字号略大，
            # 角色分类常把它判成 heading——先走标题分支的话，表格只会变成一行
            # 标题文字、永远出不了图（实测 Nature Human Behaviour 第 6 页就是这样：
            # 区域识别出来了，卡片里却一张表格图都没有）。
            if not buffer:
                buffer_section = (paragraph.section_number, paragraph.section_title)
            words = count_words(paragraph.text)
            if buffer and buffered_words() >= target_min \
                    and buffered_words() + words > target_max:
                close_buffer()
            buffer.append(("table", paragraph))
            if buffered_words() >= flush_at:
                close_buffer()
            continue

        if paragraph.role not in READING_ROLES:
            continue                          # 非正文：不进卡片流，它们去背景信息面板

        if not buffer:
            buffer_section = (paragraph.section_number, paragraph.section_title)

        if paragraph.is_formula:
            # 公式区域：整簇出图，不切分；词数只为「卡片多长」服务，不参与翻译
            words = count_words(paragraph.text)
            if buffer and buffered_words() >= target_min \
                    and buffered_words() + words > target_max:
                close_buffer()
            buffer.append(("formula", paragraph))
        else:
            for piece in split_long_block(paragraph, max_words=round(target_words),
                                          hard_limit=target_max):
                piece_words = count_words(piece.text)
                # 已经够了下限、再加这一段就会超上限 → 先收卡，保证卡片不超上限
                if buffer and buffered_words() >= target_min \
                        and buffered_words() + piece_words > target_max:
                    close_buffer()
                buffer.append(("text", piece))

        # 攒到目标词数就收卡；但不允许卡片以标题结尾（否则标题会悬在末尾没有正文）
        if buffered_words() >= flush_at:
            close_buffer()

    close_buffer()
    return cards


# ============================================================
# 九、总入口
# ============================================================

def parse_pdf(pdf_bytes: bytes, merge_on: bool = True, dehyphenate: bool = True,
              target_words: int = 400, table_mode: str = "image") -> dict:
    """
    PDF 字节流 → 阅读数据。

    返回字典：
        cards            : list[Block]  阅读卡片（正文按章节聚合）——界面主体读它
        blocks           : list[Block]  原子块（带 role/section/formula_tier/cluster_id）
        paragraphs       : list[Paragraph] 派生段落（背景信息、显示全部内容读它）
        groups           : list[list[int]] 段落分组（原子块下标），纯派生、可解释
        formula_clusters : list[FormulaCluster] 公式区域（含成员块下标，便于核对）
        table_regions  : list[TableRegion] 表格区域（题注 + 对齐列几何校验通过）
        roles            : dict         各角色的块数统计（正文/页眉页脚/作者单位/参考文献…）
        pages            : list[dict]   每页的排版检测结果（诊断用）
        body_size        : float        估算出的正文字号
        total_chars      : int          全文可提取字符数（用来识别扫描版）
        elapsed          : float        解析耗时（秒）

    table_mode：表格的呈现方式（侧边栏可切）
        "image"（默认）表格识别出来、整区域截图呈现，文字移出卡片流；
        "text"         不做表格识别，表格文字照旧线性留在卡片流里
                       —— 这条路必须与 4.4-B 之前**逐字节一致**，是回退开关。
    """
    start = time.time()
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")

    try:
        if doc.needs_pass:
            raise ValueError("这个 PDF 有密码保护，无法解析。请先在阅读器里去掉密码再试。")

        pages_meta, blocks, total_chars = [], [], 0
        page_heights, page_sizes = {}, {}
        page_lines = {}                 # 行级数据（表格判定用，只在 image 模式下采集）

        for i, page in enumerate(doc):
            page_no = i + 1
            chars = len(page.get_text().strip())
            total_chars += chars
            page_heights[page_no] = page.rect.height
            page_sizes[page_no] = (page.rect.width, page.rect.height)

            raw_blocks, raw_lines = extract_page(page, page_no, dehyphenate)
            gutter = detect_gutter(raw_blocks, page.rect.width)
            ordered = order_blocks(raw_blocks, gutter)
            blocks.extend(ordered)
            # 行级数据两种模式都要（「保留文字」模式也要靠它把表格区域排除在公式判定之外）
            page_lines[page_no] = raw_lines

            pages_meta.append({
                "页码": page_no,
                "宽×高": f"{page.rect.width:.0f}×{page.rect.height:.0f}",
                "检测排版": "双栏" if gutter is not None else "单栏",
                "中缝 x": round(gutter, 1) if gutter is not None else None,
                "块数": len(ordered),
                "文字数": chars,
            })

        # ---- 一、原子层：只做字符级修复与标题识别，绝不改写原子块结构 ----
        body_size = estimate_body_size(blocks)
        mark_headings(blocks, body_size)

        for idx, b in enumerate(blocks, 1):
            b.order = idx

        # ---- 二、段落层（第一次）：纯派生，供角色分类读「一整段话」----
        groups, paragraphs = build_paragraph_layer(blocks, merge_on, dehyphenate)
        for paragraph in paragraphs:
            paragraph.text = repair_stats_symbols(paragraph.text)

        # ---- 三、内容角色：段落级判断（要读完整一段才能定），结论回写到原子块 ----
        # 注意：order 必须先于 classify_roles 赋值——分类里要用它比较「在标题/摘要之前还是之后」
        classify_roles(paragraphs, page_heights, doc.page_count, body_size)
        propagate_paragraph_roles(blocks, paragraphs)

        # ---- 四、表格：题注锚点 + 对齐列几何校验 → 整区域截图 ----
        # 顺序：角色分类之后（要靠角色排除页眉页脚/参考文献里的 "Table N"），
        #      公式聚类之前（表格块先被认定，公式聚类就不会把单元格吸进公式图）。
        # 表格判定在**行层**做：Block 已经把同一行的多个单元格合并了，列对齐信息只在行层还在。
        margins = column_margins(blocks)
        found_regions = table_finder.find_table_regions(blocks, page_lines, page_sizes,
                                                        body_size=body_size)
        if table_mode == "image":
            apply_table_regions(blocks, found_regions)
            # 一行都没标上的区域不呈现（例如被排除的角色占满了），避免出现空图
            table_regions = [region for region in found_regions
                             if any(blocks[i].is_table for i in region.atom_indices)]
            table_excluded = frozenset()
        else:
            # 「保留文字」模式：表格文字照旧线性留在卡片里（与 4.4-B 之前一致），
            # 但表格区域**不参与公式判定**——否则表格里的 "β = 13.85" 这类单元格
            # 会被碎片堆规则聚成公式图，在表格文字中间冒出一张图（实测 npj 那篇
            # 会比截图模式多出 3 个公式区域）。
            table_regions = []
            table_excluded = frozenset(i for region in found_regions
                                       for i in region.atom_indices)

        # ---- 五、公式：三级标记 → 区域聚类 → 确认的公式移出文字流 ----
        # 表格块已经在 mark_formula_tiers 里被排除，不会与表格图打架。
        mark_formula_tiers(blocks, margins, excluded=table_excluded)
        clusters = formula_finder.find_formula_clusters(blocks, page_sizes, margins)
        apply_clusters(blocks, clusters)
        # 一个成员都没标上的簇（判定出来的块全在背景信息里）不呈现，避免出现空区域
        clusters = [cluster for cluster in clusters
                    if any(blocks[i].cluster_id == cluster.cluster_id
                           for i in cluster.atom_indices)]

        # ---- 六、段落层（第二次）：把公式块与表格块从段落里摘出来 ----
        # 必须重建一次：第一次分组时还不知道谁是公式，公式块会被当成段落续写并进去，
        # 于是它的文字就永远留在了散文里（第九页的
        # "…The average velocity over Nframes is k ∑ k=k−N+1" 正是这么来的）。
        # 角色已经写在原子块上，重建视图时会自动继承；表格块同理（表格要整块出图）。
        groups, paragraphs = build_paragraph_layer(blocks, merge_on, dehyphenate)
        for paragraph in paragraphs:
            paragraph.text = repair_stats_symbols(paragraph.text)
        sync_paragraph_formulas(blocks, paragraphs, clusters)
        sync_paragraph_tables(blocks, paragraphs, table_regions)

        # ---- 七、卡片：正文按章节聚合，公式 / 表格区域整块出图 ----
        cards = build_reading_cards(paragraphs, target_words, dehyphenate)

        for idx, card in enumerate(cards, 1):
            card.card_index = idx
            card.order = idx          # 卡片自己的编号（图片关联等按卡片记账）

        return {
            "cards": cards,
            "blocks": blocks,               # 原子块：元数据、诊断、图片关联用
            "paragraphs": paragraphs,       # 派生段落：背景信息、显示全部内容用
            "groups": groups,               # 段落分组（原子块下标），可解释、可回退
            "formula_clusters": clusters,   # 公式区域：诊断面板据此核对「合了哪几块」
            "table_regions": table_regions,  # 表格区域：诊断面板据此核对「框住了哪几行」
            "roles": dict(Counter(b.role for b in blocks)),
            "pages": pages_meta,
            "body_size": body_size,
            "total_chars": total_chars,
            "elapsed": round(time.time() - start, 2),
        }
    finally:
        doc.close()      # 无论成功失败都关掉文档，释放文件句柄
