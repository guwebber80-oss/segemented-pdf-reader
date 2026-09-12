"""
公式识别与区域聚类模块
================================================================
职责：把「公式候选判定」和「把散落的公式碎片聚成一个区域」这两件事，
从 pdf_parser 里独立出来。

为什么需要独立模块：
  1. 公式判定有四层判据 + 三级分类，逻辑量大，混在解析器里会让 pdf_parser 难以维护；
  2. 聚类算法需要独立的参数与测试，独立成模块才能单独验证。

本模块【不依赖 Streamlit】，也不 import pdf_parser（避免循环依赖），
只按鸭子类型读取原子块的这些属性：
    page / x0 / y0 / x1 / y1 / text / column / kind / role
    formula_tier（"strong" / "weak" / "fragment" / ""）/ is_formula
其中 kind == "heading"、role == "body" 这两个字面量与 pdf_parser 里的常量取值一致。

核心概念：
    · **原子块（atom）**：从 PDF 提取出来的最小文本块，不可破坏、不可改写；
    · **强公式**：判据充分，可以单独成图，也可以作为聚类的种子；
    · **弱公式**：有一点数学证据（得分 2~3，或得分够但没过防线），
      只能加入已有簇；满足「相对所在栏有缩进 + 使用数学字体」时才允许当种子；
    · **公式碎片**：短、无句末标点、无散文虚词的数学残片（如单个字母 N、
      求和符号带上限），**只能被区域吸收，绝不单独成图**。
"""

import re
from dataclasses import dataclass, field

# ------------------------------------------------------------
# 一、锚点表（只用符号锚点，不用词锚点）
# ------------------------------------------------------------
# 大运算符：日常散文里几乎不出现，是近乎无噪声的强信号
BIG_OPERATORS = set("∑∏∫∮∬∭⋃⋂√∂∇∞")
# 比较/算术符号：统计量常夹在句子里（"p < 0.05"），信号弱得多。
# 注意 + − × ÷ 都必须在内：真实公式里 "d₀ + κ ΔH" 这种写法极常见，
# 漏了加号就会让整块只剩上下标一个证据而被打成普通文字。
MATH_OPERATORS = set("=≤≥≠<>±+×÷·⋅−^~∕∝≃≅∈⊂⊃∩∪→←↔∣|ℝℕℤ")
MATH_ANCHORS = BIG_OPERATORS | MATH_OPERATORS

# 数学字体：现代论文的公式几乎都用专门的数学字体排版
MATH_FONT_RE = re.compile(r"math|cmsy|cmex|stix|cambria|symbol|sym\b", re.IGNORECASE)

# 上下标记号：Unicode 上下标区 + 多字符上下标的 _ / ^ 回退记号
SCRIPT_RE = re.compile(r"[\u2070-\u209f\u1d2c-\u1d6a]|[_^][A-Za-z0-9]")

# 「比较运算符两边都有东西」才像公式；"<.001" 这种行尾碎片不算
COMPARISON_WITH_OPERANDS_RE = re.compile(r"\S\s*[=≤≥≠<>]\s*\S")

# 英文虚词：出现 ≥3 个就当散文，不参与公式判定（保护正文）
PROSE_WORDS = {
    "the", "and", "of", "to", "in", "is", "was", "were", "that", "with", "for",
    "as", "are", "be", "by", "on", "at", "from", "this", "these", "those",
    "we", "they", "it", "while", "which", "their", "our", "has", "have",
}

# 算法/清单标题：之后连续的行属于同一个清单，由确定性规则整簇处理。
# 必须要求「编号 + 冒号」这类真正的清单标题形式：
#   · 裸词 "function"（表格里的旋转标签）会误触发；
#   · 参考文献条目 "algorithm for categorization from its neural implementation…"
#     也会因为开头是 algorithm 而误触发，然后把整段参考文献吞成一张图。
LISTING_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"(?:algorithm|listing|procedure|pseudocode)\s+(?:\d+|[ivx]+)\s*[:.\-]"
    r"|input\s*:"
    r"|function\s+[A-Za-z_]\w*\s*\("
    r")", re.IGNORECASE)

# ---- 阈值 ----
FORMULA_MAX_WORDS = 18        # 超过这么多词的不可能是独立公式
STRONG_THRESHOLD = 4          # 得分达到 4 才算「强候选」
WEAK_MIN = 2                  # 得分 2~3 算「弱候选」
FRAGMENT_MAX_WORDS = 6        # 碎片的最长词数
INDENT_MIN_PT = 8.0           # 相对所在栏边界的缩进量（公式通常缩进/居中）
MAX_CLUSTER_BLOCKS = 15       # 单簇块数上限（防止吸进图注与正文）
MAX_CLUSTER_AREA_RATIO = 0.20  # 单簇面积不超过页面的 20%
PROJECTION_OVERLAP_MIN = 0.30  # x/y 投影重叠达到 30% 才算「对齐」
ALIGNED_GAP_FACTOR = 1.0       # 投影对齐时，另一方向允许的间隙上限（× gap_limit）
                               # 这条通道不能比主阈值宽松太多：系数取 2 时实测会把
                               # 一页里上下相隔 22pt 的**三个独立公式**串成一张图。
GAP_MIN_PT = 8.0               # 生长间隙下限
GAP_SIZE_FACTOR = 1.2          # 生长间隙 = max(下限, 系数 × 簇内中位字号)
                               # 字号 10.5pt 时约 12.6pt，与旧版「间距 ≤12pt 就并图」
                               # 的口径相当——不然同一处多行公式会被拆成好几张图。
LISTING_GAP_PT = 26.0         # 清单内相邻行的最大垂直间距
LISTING_HEADING_ATTACH_PT = 60.0  # 清单标题单独成行时，允许下一块接上的最大间距
LISTING_LINE_MAX_WORDS = 30   # 缩进的清单行最多多少词
LISTING_LINE_MAX_WORDS_TIGHT = 12  # 不缩进的清单行最多多少词（用来在正文段落处刹车）


def count_words(text: str) -> int:
    return len(text.split())


# ------------------------------------------------------------
# 二、基础判据
# ------------------------------------------------------------

def looks_like_prose(text: str) -> bool:
    """英文虚词出现 ≥3 次 → 当成散文"""
    words = [w.strip(".,;:()[]\"'").lower() for w in text.split()]
    return sum(1 for w in words if w in PROSE_WORDS) >= 3


def has_balanced_delimiters(text: str) -> bool:
    """括号是否配对。句子碎片往往只剩一个右括号（"… uₜ), where b^′"）"""
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    for ch in text:
        if ch in "([{":
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack.pop() != pairs[ch]:
                return False
    return not stack


def has_alphabetic_variable(text: str, minimum: int = 2) -> bool:
    """公式里总有变量名（字母）；一个字母都没有的多半是数字碎片"""
    return sum(1 for ch in text if ch.isalpha()) >= minimum


def looks_mathy(text: str) -> bool:
    """行级判断：这一行像公式吗（用于把独立成行的公式从段落里拆出来）"""
    compact = text.replace(" ", "")
    if not compact or not (set(text) & MATH_ANCHORS):
        return False
    non_letter = sum(1 for ch in compact if not ch.isalpha())
    return non_letter / len(compact) >= 0.22


def uses_math_font(atom) -> bool:
    return any(MATH_FONT_RE.search(name) for name in getattr(atom, "font_names", ()))


# ------------------------------------------------------------
# 三、打分与三级分类
# ------------------------------------------------------------

def formula_score(atom) -> int:
    """
    给一个原子块打「像不像独立公式」的分：

      · 出现大运算符（∑ ∏ ∫ ∮ √ ∂ ∇ ∞…）  +3   ← 近乎无噪声的强信号，
                                                 仅凭它就能把块抬成强候选
      · 出现 = 或比较运算符                 +2
      · 出现上下标                          +2
      · 其它数学运算符每出现一种             +1（最多 +2）
      · 词数 ≤10                            +1
      · 像散文（虚词 ≥3）                    直接 0 分
      · 以句号结尾且词数 >10                  −3
    """
    if getattr(atom, "kind", "body") == "heading":
        return 0

    text = atom.text.strip()
    if not text or len(text) > 220:
        return 0

    words = count_words(text)
    if words > FORMULA_MAX_WORDS or looks_like_prose(text):
        return 0

    chars = set(text)
    if not (chars & MATH_ANCHORS):
        return 0

    score = 0
    if chars & BIG_OPERATORS:
        score += 3
    if chars & set("=≤≥≠<>"):
        score += 2
    if SCRIPT_RE.search(text):
        score += 2
    score += min(len(chars & (MATH_OPERATORS - set("=≤≥≠<>"))), 2)
    if words <= 10:
        score += 1
    if text.endswith(".") and words > 10:
        score -= 3
    return score


def passes_standalone_guards(atom, margin) -> bool:
    """
    「能否单独成图」的防线。注意：**防线只管这件事**，
    没通过防线的块不会被打回普通文字，而是降级为弱候选（可被聚类吸收）。

      · 必须含变量名（字母 ≥2）
      · 比较运算符两边必须有操作数（挡掉 "<.001" 这类表格碎片）
      · 括号必须配对（挡掉 "… uₜ), where b^′" 这类句子碎片）
      · 必须相对所在栏有缩进（段落正文一律从左边界开始，天然排除）
    """
    text = atom.text.strip()
    if not has_alphabetic_variable(text):
        return False
    if (set(text) & set("=≤≥≠<>")) and not COMPARISON_WITH_OPERANDS_RE.search(text):
        return False
    if not has_balanced_delimiters(text):
        return False
    if margin is not None and atom.x0 < margin + INDENT_MIN_PT:
        return False
    return True


def is_fragment(atom) -> bool:
    """
    公式碎片判据（宽松，因为碎片只能被区域吸收，单独成图是被禁止的）：
      · 词数 ≤6
      · 没有句末标点
      · 不是散文（虚词 <3）
      · 含数学锚点 / 上下标 / 数学字体 / 单字母变量
    注意：**不使用括号配对、变量数量这些「单独成图」的防线**——
    否则 "d_saccade)" 这种真正属于公式的残片会被挡在簇外。
    """
    text = atom.text.strip()
    if not text or count_words(text) > FRAGMENT_MAX_WORDS:
        return False
    # 「句末标点」只算紧跟在单词后面的那种（"eyes."）；
    # 公式里的句点是独立成点的（"d_saccade) ."），不能当成一句话的结束
    if re.search(r"[A-Za-z\u00c0-\u024f]\.\s*$", text) and count_words(text) > 2:
        return False
    if looks_like_prose(text):
        return False

    has_single_letter = any(len(w.strip("()[],.;:=+-")) == 1 and w.strip("()[],.;:=+-").isalpha()
                            for w in text.split())
    return bool(
        (set(text) & MATH_ANCHORS)
        or SCRIPT_RE.search(text)
        or uses_math_font(atom)
        or has_single_letter
    )


def is_seedable(atom, margin) -> bool:
    """
    弱候选能不能当种子？门槛有三条：不是散文、括号配对、相对所在栏有缩进 + 数学字体。

    括号配对这条是必须的：从段落里拆出来的「句子碎片」也带公式等级
    （例如 Nature 那篇的 "ₜₑₓₜ, φₖ, uₜ), where b^′"——它其实是被截断的半句话），
    如果不拦，它一生长就会把周围正文吸进来、生成一张半句话的图片。
    """
    text = atom.text.strip()
    if looks_like_prose(text):
        return False
    if not has_balanced_delimiters(text):
        return False
    if margin is not None and atom.x0 < margin + INDENT_MIN_PT:
        return False
    return uses_math_font(atom)


def classify(atom, margin) -> str:
    """
    三级分类，返回 "strong" / "weak" / "fragment" / ""（空串＝普通文字）。

    优先顺序：强 > 弱 > 碎片。
    「得分够但没过防线」的块降级为弱——**绝不能落回普通文字**，
    否则它又会被段落合并吞掉（这正是要根治的病）。
    """
    score = formula_score(atom)
    if score >= STRONG_THRESHOLD:
        return "strong" if passes_standalone_guards(atom, margin) else "weak"
    if score >= WEAK_MIN:
        return "weak"
    if is_fragment(atom):
        return "fragment"
    return ""


def is_formula_ish(atom) -> bool:
    """有没有资格被聚类吸收"""
    return getattr(atom, "formula_tier", "") in ("strong", "weak", "fragment")


# ------------------------------------------------------------
# 四、区域聚类
# ------------------------------------------------------------

@dataclass
class FormulaCluster:
    """一个确认的公式区域：整簇共用一个外接矩形，渲染成一张图"""
    cluster_id: int
    page: int
    rect: tuple                      # (x0, y0, x1, y1)
    atom_indices: list = field(default_factory=list)
    text: str = ""
    kind: str = "formula"            # "formula" | "listing"

    @property
    def member_count(self) -> int:
        return len(self.atom_indices)


def _union_rect(atoms) -> tuple:
    return (min(a.x0 for a in atoms), min(a.y0 for a in atoms),
            max(a.x1 for a in atoms), max(a.y1 for a in atoms))


def _median_size(atoms) -> float:
    sizes = sorted(getattr(a, "max_size", 0.0) for a in atoms)
    return sizes[len(sizes) // 2] if sizes else 10.0


def _median(values) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else 0.0


def _gap(rect: tuple, atom) -> float:
    """原点到矩形的最短距离（重叠时为 0）"""
    dx = max(rect[0] - atom.x1, atom.x0 - rect[2], 0.0)
    dy = max(rect[1] - atom.y1, atom.y0 - rect[3], 0.0)
    return max(dx, dy)


def _same_line(rect: tuple, atom) -> bool:
    """
    候选块与当前簇在**同一行**上吗（y 范围重叠超过较矮者高度的一半）。

    为什么需要单独一条：二维公式常常在同一行左右分开画
    （"\u2009Arm Spanᵤₛₑᵣ\u2009\u2009s_scale = …" 这种），横向间距可能上百点，
    但纵向完全重叠。只按「间隙」判断会把这些碎片漏在外面，
    于是同一处公式被渲染成好几张零碎的图片。
    """
    overlap = max(0.0, min(rect[3], atom.y1) - max(rect[1], atom.y0))
    shorter = min(max(rect[3] - rect[1], 0.1), max(atom.y1 - atom.y0, 0.1))
    return overlap >= 0.5 * shorter


def _aligned_and_near(rect: tuple, atom, gap_limit: float) -> bool:
    """
    x/y 投影重叠 ≥30% **且** 在另一方向上确实相邻（间隙 ≤ 2×gap_limit）。

    为什么必须补上「确实相邻」这一条：只看向 x 投影重叠的话，同一栏里上下相距
    几百点的两个公式也会被判成「横向对齐」而吸进同一个簇——实测这样会把整页的
    公式连成一张巨大的图（第九页那个簇一度长到 15 块、纵跨 460pt）。
    投影规则的用途只是抓住「横向错位但纵向紧邻」的二维公式碎片。
    """
    ox = max(0.0, min(rect[2], atom.x1) - max(rect[0], atom.x0))
    oy = max(0.0, min(rect[3], atom.y1) - max(rect[1], atom.y0))
    width = max(atom.x1 - atom.x0, 1e-6)
    height = max(atom.y1 - atom.y0, 1e-6)
    dx = max(rect[0] - atom.x1, atom.x0 - rect[2], 0.0)
    dy = max(rect[1] - atom.y1, atom.y0 - rect[3], 0.0)

    if ox / width >= PROJECTION_OVERLAP_MIN and dy <= ALIGNED_GAP_FACTOR * gap_limit:
        return True
    if oy / height >= PROJECTION_OVERLAP_MIN and dx <= ALIGNED_GAP_FACTOR * gap_limit:
        return True
    return False


def _area(rect: tuple) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def detect_listing_clusters(atoms, margins: dict, start_id: int = 0):
    """
    清单上下文：从 ALGORITHM / Listing / Procedure / Input: 标题出发，
    把之后连续的行整体作为一个簇。

    这条规则是**确定性的**，不靠近邻生长，所以它产生的簇不受块数上限约束
    （一个 20 行的伪代码清单必须成一整张图，不能被上限截成两张）。

    标题行有时会和清单正文隔开（排版上标题跟着上方的图），所以标题单独一行时
    先「挂起」，只要下一块同页且间距 ≤60pt 就并进来——否则会多出一张只有
    "ALGORITHM 1: Swipe" 几个字的图片。
    """
    clusters, current, pending = [], [], []
    page, last_y = None, None

    def close(group):
        if not group:
            return
        members = [atoms[i] for i in group]
        clusters.append(FormulaCluster(
            cluster_id=-1, page=members[0].page, rect=_union_rect(members),
            atom_indices=list(group),
            text=" ".join(a.text.strip() for a in members), kind="listing"))

    for index, atom in enumerate(atoms):
        if getattr(atom, "kind", "body") == "heading":
            close(current)
            close(pending)
            current, pending = [], []
            continue

        # 注意这里只排除标题，**不**要求 role == body：
        # 伪代码清单里的行号（单独的 "5"、"6"）会被跨页重复计数误判成页脚，
        # 以前按 role 过滤就会把清单的最后两行漏在图片外面
        # （实测 ALGORITHM 1 只截到 "return "Swipe Detected";" 就断了）。

        # 什么块可以并进「正在攒的清单」？——两条限制：
        #   ① 非缩进行最多 12 词，缩进行最多 30 词
        #   ② 非正文角色（清单行号 "5"、"6" 会被跨页重复计数误判成页脚）必须缩进
        # 不加这两条，清单会一路吞到页面底部的正文段和页脚上
        # （实测把 ALGORITHM 1 的图片从 447~562pt 撑到 447~677pt，整页都被吞掉）。
        margin = margins.get((atom.page, getattr(atom, "column", -1)))
        indented = margin is None or atom.x0 > margin + 1.0
        word_limit = LISTING_LINE_MAX_WORDS if indented else LISTING_LINE_MAX_WORDS_TIGHT
        if count_words(atom.text) > word_limit:
            close(current)
            close(pending)
            current, pending = [], []
            continue
        if getattr(atom, "role", "body") != "body" and not indented:
            close(current)
            close(pending)
            current, pending = [], []
            continue

        if LISTING_HEADING_RE.match(atom.text.strip()):
            # "Input: / Output:" 这类行本身也匹配清单标题，但它们就在 ALGORITHM 标题
            # 下面一行，属于同一个清单——必须先判断「能不能接上正在攒的清单」，
            # 否则会把清单标题孤立成一张只有标题的图片。
            if current and atom.page == page and (atom.y0 - last_y) <= LISTING_GAP_PT:
                current.append(index)
                last_y = atom.y1
                continue

            close(current)
            # 上一行「挂起的清单标题」能接上就直接并进这个清单：
            # 单栏.pdf 里 "ALGORITHM 1: Swipe…" 的下一块正是 "Input: …"，
            # 两块各自都是一个清单标题，不特判就会多出一张只有标题的图片。
            if pending and atom.page == page \
                    and (atom.y0 - last_y) <= LISTING_HEADING_ATTACH_PT:
                current, pending = pending + [index], []
            else:
                close(pending)
                current, pending = [index], []
            page, last_y = atom.page, atom.y1
            continue

        if current:
            if atom.page == page and (atom.y0 - last_y) <= LISTING_GAP_PT:
                current.append(index)
                last_y = atom.y1
                continue
            # 间距过大 → 清单结束。整簇只有标题一行时先挂起，看下一块能不能接上
            if len(current) == 1:
                pending, current = current, []
            else:
                close(current)
                current = []

        if pending and not current:
            if atom.page == page and (atom.y0 - last_y) <= LISTING_HEADING_ATTACH_PT:
                current, pending = pending + [index], []
                last_y = atom.y1
                continue
            close(pending)
            pending = []

    close(current)
    close(pending)

    for offset, cluster in enumerate(clusters):
        cluster.cluster_id = start_id + offset
    return clusters


def grow_clusters(atoms, page_sizes: dict, margins: dict, claimed: set, start_id: int):
    """
    每页区域生长聚类：从强种子（或符合条件的弱种子）出发，向外吸收空间相邻的公式块。

    吸收条件（满足其一）：
      · 与当前簇外接矩形的间隙 ≤ max(6pt, 0.8 × 簇内中位字号)
      · x 或 y 投影重叠 ≥ 30%，且另一方向上间隙不超过 2× 上述阈值

    被吸收的块必须是「公式候选」（强/弱/碎片），散文永远不会被吸进来。
    每次吸收后更新外接矩形并继续迭代；单簇块数 ≤15、面积 ≤ 页面 20%。
    """
    clusters = []
    next_id = start_id
    by_page = {}
    for index, atom in enumerate(atoms):
        if index in claimed:
            continue
        if getattr(atom, "kind", "body") == "heading" or getattr(atom, "role", "body") != "body":
            continue
        by_page.setdefault(atom.page, []).append(index)

    for page in sorted(by_page):
        indices = by_page[page]
        width, height = page_sizes.get(page, (600.0, 800.0))
        page_area = max(width * height, 1.0)
        used = set()

        for seed_index in indices:
            if seed_index in used:
                continue
            seed = atoms[seed_index]
            tier = getattr(seed, "formula_tier", "")
            margin = margins.get((page, getattr(seed, "column", -1)))
            if tier == "strong":
                pass
            elif tier == "weak" and is_seedable(seed, margin):
                pass
            else:
                continue

            members = [seed_index]
            used.add(seed_index)
            changed = True

            while changed and len(members) < MAX_CLUSTER_BLOCKS:
                changed = False
                rect = _union_rect([atoms[i] for i in members])
                gap_limit = max(GAP_MIN_PT, GAP_SIZE_FACTOR * _median_size([atoms[i] for i in members]))

                for candidate_index in indices:
                    if candidate_index in used:
                        continue
                    candidate = atoms[candidate_index]
                    if not is_formula_ish(candidate):
                        continue          # 只有公式候选能被吸收，散文一律排除
                    same_line = _same_line(rect, candidate) and (
                        candidate.column == seed.column or -1 in (candidate.column, seed.column))
                    if same_line \
                            or _gap(rect, candidate) <= gap_limit \
                            or _aligned_and_near(rect, candidate, gap_limit):
                        members.append(candidate_index)
                        used.add(candidate_index)
                        changed = True
                        if len(members) >= MAX_CLUSTER_BLOCKS:
                            break

                grown = _union_rect([atoms[i] for i in members])
                if _area(grown) > page_area * MAX_CLUSTER_AREA_RATIO:
                    # 面积超限：退掉最后吸收的那一块，停止生长
                    if len(members) > 1:
                        used.discard(members.pop())
                    break

            member_atoms = [atoms[i] for i in members]
            clusters.append(FormulaCluster(
                cluster_id=next_id, page=page, rect=_union_rect(member_atoms),
                atom_indices=list(members),
                text=" ".join(a.text.strip() for a in member_atoms), kind="formula"))
            next_id += 1

    return clusters


def find_formula_clusters(atoms, page_sizes: dict, margins: dict):
    """
    总入口：先做确定性的清单聚类，再做近邻生长聚类，返回全部簇。
    （清单的块不再参与生长，避免被重复吸收。）
    """
    listing_clusters = detect_listing_clusters(atoms, margins)
    claimed = {i for cluster in listing_clusters for i in cluster.atom_indices}
    grown = grow_clusters(atoms, page_sizes, margins, claimed, len(listing_clusters))
    return listing_clusters + grown
