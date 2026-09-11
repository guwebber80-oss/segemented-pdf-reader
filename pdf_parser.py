"""
PDF 解析模块 —— 从 PDF 字节流里提取「按阅读顺序排列的文本卡片」
================================================================
本模块【不依赖 Streamlit】，只做纯数据处理，可以单独运行、单独测试。

主要流程（parse_pdf）：
    PDF 字节流
      → 逐页取出文本块（extract_page_blocks）
      → 判断单栏/双栏（detect_gutter）
      → 按阅读顺序重排（order_blocks）
      → 拼回被拆开的段落（merge_paragraphs）
      → 超长段落切卡片（split_long_block）
      → 识别标题（estimate_body_size + mark_headings）
      → 卡片列表

注：阶段 5 做代码重构时，本文件会搬到 utils/pdf_parser.py，内容不变。
"""

import re
import time
from collections import Counter
from dataclasses import dataclass, replace

import pymupdf      # PDF 解析核心（PyMuPDF；新版推荐 import pymupdf，旧的 fitz 已弃用）


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
    """一个文本块。PDF 里的一「块」通常就对应一个自然段。"""
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
        spans = line.get("spans", [])
        text = "".join(span.get("text", "") for span in spans)

        sizes, bold_chars, total_chars = [], 0, 0
        for span in spans:
            span_text = span.get("text", "")
            if not span_text.strip():
                continue
            sizes.append(float(span.get("size", 0)))
            total_chars += len(span_text)
            # flags 第 4 位（值 16）代表粗体
            if int(span.get("flags", 0)) & 16:
                bold_chars += len(span_text)

        bbox = line.get("bbox") or (0.0, 0.0, 0.0, 0.0)
        records.append({
            "text": text,
            "max_size": max(sizes) if sizes else 0.0,
            "bold_ratio": (bold_chars / total_chars) if total_chars else 0.0,
            "bold_chars": bold_chars,
            "chars": total_chars,
            "bbox": bbox,
            "height": bbox[3] - bbox[1],
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


def _make_block(page_no: int, records, dehyphenate: bool):
    """把若干行记录合成一个 Block（坐标取并集，字号/加粗按内容汇总）"""
    text = join_lines([r["text"] for r in records], dehyphenate).strip()
    if not text:
        return None

    sizes = [r["max_size"] for r in records]
    heights = sorted(r["height"] for r in records)
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
    )


def extract_page_blocks(page, page_no: int, dehyphenate: bool = True):
    """
    取出一页里的所有【文字】块。

    说明：计划里写的是 page.get_text("blocks")，这里改用 page.get_text("dict")。
    原因：blocks 模式不返回字号和加粗信息，而识别标题必须靠字号；
    "dict" 模式同样按 块/行/span 组织，块划分与 blocks 一致，但信息更全。
    """
    data = page.get_text("dict")
    blocks = []

    for raw in data.get("blocks", []):
        if raw.get("type") != 0:        # type 0 = 文字，1 = 图片（图片留到阶段 3 处理）
            continue

        records = _line_records(raw)
        if not records:
            continue

        # 可能需要把块首的标题行拆出来，所以一个 PyMuPDF 块最多产出两个 Block
        for part in _split_leading_heading(records):
            if not part:
                continue
            block = _make_block(page_no, part, dehyphenate)
            if block is not None:
                blocks.append(block)

    return blocks


def detect_gutter(blocks, page_width: float):
    """
    检测这一页是不是双栏排版。

    原理：双栏排版一定存在一条「中缝」——一条竖直的空白带，
    左边一堆块、右边一堆块，几乎没有块横跨它。
    我们在页面宽度 32%~68% 的位置上滑动一条候选中缝，谁的证据最足就用谁。

    返回值：中缝的 x 坐标；判定为单栏时返回 None。
    """
    # 只用较长的块来判断，避开页眉、页码、短标题这些噪声
    candidates = [b for b in blocks if len(b.text) > 40]
    if len(candidates) < 6:
        return None

    best = None
    for gx in range(int(page_width * 0.32), int(page_width * 0.68) + 1, 3):
        left = [b for b in candidates if b.x1 <= gx]        # 完全在中缝左边
        right = [b for b in candidates if b.x0 >= gx]       # 完全在中缝右边
        span = [b for b in candidates if b.x0 < gx < b.x1]  # 横跨中缝

        # 判定门槛：左右各自至少 3 块，且左右块数之和占绝大多数
        if len(left) >= 3 and len(right) >= 3 and (len(left) + len(right)) >= 0.7 * len(candidates):
            # 打分：左右越均衡越好，横跨中缝的块越少越好
            score = min(len(left), len(right)) - 2.0 * len(span)
            if best is None or score > best[0]:
                best = (score, gx)

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
    return cur.text[:1].islower()


def merge_paragraphs(blocks):
    """把同一段的碎块拼回一整段。"""
    merged = []
    for b in blocks:
        if merged and _should_merge(merged[-1], b):
            prev = merged[-1]
            if prev.text.endswith(("-", "‐", "\xad")):
                prev.text = prev.text[:-1] + b.text
            else:
                prev.text = f"{prev.text} {b.text}"
            prev.x0, prev.y0 = min(prev.x0, b.x0), min(prev.y0, b.y0)
            prev.x1, prev.y1 = max(prev.x1, b.x1), max(prev.y1, b.y1)
            prev.max_size = max(prev.max_size, b.max_size)
            prev.line_height = min(prev.line_height, b.line_height)
        else:
            merged.append(b)
    return merged


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
# 七、总入口
# ============================================================

def parse_pdf(pdf_bytes: bytes, merge_on: bool = True, dehyphenate: bool = True,
              max_words: int = 200) -> dict:
    """
    PDF 字节流 → 卡片数据。

    返回字典：
        blocks      : list[Block]  按阅读顺序排好的卡片
        pages       : list[dict]   每页的排版检测结果（诊断用）
        body_size   : float        估算出的正文字号
        total_chars : int          全文可提取字符数（用来识别扫描版）
        elapsed     : float        解析耗时（秒）
    """
    start = time.time()
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")

    try:
        if doc.needs_pass:
            raise ValueError("这个 PDF 有密码保护，无法解析。请先在阅读器里去掉密码再试。")

        pages_meta, blocks, total_chars = [], [], 0

        for i, page in enumerate(doc):
            page_no = i + 1
            chars = len(page.get_text().strip())
            total_chars += chars

            raw_blocks = extract_page_blocks(page, page_no, dehyphenate)
            gutter = detect_gutter(raw_blocks, page.rect.width)
            ordered = order_blocks(raw_blocks, gutter)
            blocks.extend(ordered)

            pages_meta.append({
                "页码": page_no,
                "宽×高": f"{page.rect.width:.0f}×{page.rect.height:.0f}",
                "检测排版": "双栏" if gutter is not None else "单栏",
                "中缝 x": round(gutter, 1) if gutter is not None else None,
                "块数": len(ordered),
                "文字数": chars,
            })

        if merge_on:
            blocks = merge_paragraphs(blocks)

        blocks = [piece for b in blocks for piece in split_long_block(b, max_words)]

        body_size = estimate_body_size(blocks)
        mark_headings(blocks, body_size)

        for idx, b in enumerate(blocks, 1):
            b.order = idx

        return {
            "blocks": blocks,
            "pages": pages_meta,
            "body_size": body_size,
            "total_chars": total_chars,
            "elapsed": round(time.time() - start, 2),
        }
    finally:
        doc.close()      # 无论成功失败都关掉文档，释放文件句柄
