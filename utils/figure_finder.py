"""
figure_finder —— 矢量图页的「图区域」定位（阶段 4.6，**默认关闭**）
================================================================
本模块【不依赖 Streamlit】，可以脱离网页单独运行、单独测试；
除 `probe_page()` 之外全部是纯函数（阈值与聚类都只吃数据、不碰 PDF），
这样 tests/test_figure_region.py 能用合成数据把每条判据单独钉住。

【要解决的问题】
有些期刊的插图是**矢量绘制**的（不嵌位图）。这类页面上按位图提取的管线
一张图都抓不到，图**内部**的文字（坐标轴刻度、子图角标 a/b/c、图例）
却被 PyMuPDF 当成普通文字块提取出来，于是 ≤7pt 的小字混进正文、
进了阅读卡片。实测（旧 NHB，Nature Human Behaviour s41562-026-02534-0）：
    第 2 页 24 块 / 48 词，第 3 页 40 块 / 50 词，第 4 页 130 块 / 253 词
全是图内小字，`page.get_images() == []`（第 3 页）而矢量路径 208 项。

【闸门分两级：页级预筛 + 区域级位图覆盖率】（阶段 4.7 修正，用户报障驱动）
文档级判据不可行（11 篇实测）：两篇 NHB 的元数据相同但真值相反；
矢量密度最高的新 NHB 反而是位图图文献（它的 Reporting Summary 页
把文字转成了矢量轮廓）；同一篇里两种图页还交错（旧 NHB 第 9 页
既有 17308pt² 的位图、又有 5477 项矢量）。

原来只有一条**页级**闸门：「矢量路径项 ≥ 20 且 页面板级位图 < 8000pt²」。
用户实测报障（旧 NHB 第 2/9/18/19 页的图"截取失败或只拿到位图那一块、没有矢量"）：
这些页的插图本来是**位图 + 大量矢量的混合体**，而页级闸门只看"这页有没有大位图"，
于是整页被否掉——位图由 image_extractor 照旧抠出来显示，而坐标轴/面板/示意图
（矢量部分）就永远没人画。实测这四页的区域级位图覆盖率：
    第 2 页 18% · 第 9 页 39% · 第 19 页 36%   → 混合页，**应该截图**
    第 22 页 81%                              → 纯位图图，交给位图提取路径
可见"该不该截图"是**区域**的属性、不是页的属性（第 22 页与第 19 页在同一篇里交错）。
所以闸门改成两级：
    ① **页级预筛**（should_probe_page）：矢量路径项 ≥ FIG_VECTOR_MIN
       —— 只决定"这页值不值得去定位图区域"（正文页的分隔线/框线也算矢量，
          所以真正把关的是下面的区域级判据与 find_figure_regions 的体检）；
    ② **区域级判定**：区域内位图显示面积的**并集** / 区域面积 ≥
       FIG_REGION_MAX_BITMAP_COVER（默认 0.60）→ 这一块本来就由位图提取路径呈现，
       不做区域截图；< 阈值 → 截图（区域截图天然把位图也渲染进去，混合页因此能一张看全）。

⚠️ 覆盖率必须用**并集**、不能把各矩形面积求和：同一页里同一张图会被重复摆放，
旧 NHB 第 19 页有 22 个显示矩形、其实只有 8 张图（求和得到 125% 的假覆盖率，
会把该截图的那一页否掉）。
⚠️ 与**已确认的表格区域**重叠的图区域一律放弃：实测 cn 样本（npj Digital Medicine）
第 6 页"Fig. 6"题注上方那一块其实是 Table 3，与表格区域重叠 92%——不挡的话，
同一张表会以「图区域」的名义再出一遍（而且表内文字会被移出正文流）。
⚠️ FIG_MAX_BITMAP_AREA（8000pt²）与 FIG_VECTOR_MIN 都**保留**为页级常量：
它们不再决定"截不截图"，但仍用于诊断口径（"纯矢量图页"）与回退，
且 should_capture_page 的旧行为逐条不变（tests/test_figure_region.py ① 钉住）。

【区域定位为什么不能按 bbox 截】
矢量 bbox 的并集≈整页（占 75% 面积）—— 但那是**整页**并集，不是图区。
实测把并集限制到「题注上方」、剔除页眉页脚细横线、裁掉落在页面外的
Form XObject 变换框之后，并集就是一块贴合的图区（旧 NHB 第 3 页
(100,54)-(522,387)、第 10 页 (64,65)-(561,271)、第 20 页 (76,47)-(523,342)）。
所以定位用两条腿：**题注锚点定下界 + 图内小字按间隙聚类定成员**，
矢量并集只用来把矩形撑到图形的真实范围（见 find_figure_regions）。

【成员移出正文】
区域内的「图内小字」块会被移出卡片文字流（改由区域截图承载），
并且**不再送进公式聚类**（图页天然产碎片：旧 NHB 第 2/3/4 页有
58 个 fragment/weak 块，不排除的话会在图里冒出一堆公式图）。
**题注块本身不进区域成员**：按项目规则「图注视作正文」，它照旧留在卡片流里。
"""

import re
from collections import Counter
from dataclasses import dataclass, field

# ------------------------------------------------------------
# 阈值（全部集中在这里：调阈值只改这一处）
# ------------------------------------------------------------
FIG_VECTOR_MIN = 20             # 页级预筛：矢量路径项 ≥ 这个数（见模块说明：不再看位图）
FIG_MAX_BITMAP_AREA = 8000.0    # 「纯矢量图页」口径：最大位图显示面积 < 这个值（pt²；仅供诊断与旧口径）
# 区域级闸门（阶段 4.7）：区域内位图显示面积的**并集**占区域面积的比例 ≥ 这个值
# → 这块本来就由位图提取路径呈现，不做区域截图。实测旧 NHB：p2 18% / p9 39% / p19 36%
# （混合页，要截图）· p22 81%（纯位图图，交给位图路径）。改这个值必须重跑这四页的核对。
FIG_REGION_MAX_BITMAP_COVER = 0.60
# 与已确认表格区域的重叠比例上限（占较小者面积）：超过就放弃——那块地方已经有表格图了
FIG_TABLE_OVERLAP_MAX = 0.5
# 「位图已被区域截图吸收」的判据：位图矩形落在图区域里的比例达到这个值就不再单独呈现
# （区域截图天然包含位图，两条都出就是同一张图出现两次）
FIG_IMAGE_IN_REGION_RATIO = 0.5
FIG_GAP_MIN_PT = 8.0            # 聚类间隙下限
FIG_GAP_FACTOR = 1.2            # 聚类间隙 = max(下限, 系数 × 簇内中位字号)
FIG_GAP_MAX_PT = 45.0           # 间隙上限（见 cluster_by_gap：只有空隙里真有矢量图形时才放宽到这里）
FIG_ANCHOR_MAX_GAP = 60.0       # 题注与图区底部之间允许的最大间距
FIG_PAD_PT = 3.0                # 区域矩形外扩（避免切掉图形的描边）
FIG_MIN_WIDTH = 120.0           # 区域最小宽度
FIG_MIN_HEIGHT = 50.0           # 区域最小高度
FIG_MAX_AREA_RATIO = 0.60       # 单簇面积上限（占页面比例）
FIG_OVERLAP_MAX = 0.5           # 同页两个图区重叠超过这个比例就丢掉后一个
FIG_SMALL_SIZE_RATIO = 0.98     # 「图内小字」判据之一：字号 < 正文字号 × 这个比例
FIG_SMALL_MAX_WORDS = 6         # 「图内小字」判据之二：词数 ≤ 这个值
FIG_MEMBER_MAX_WORDS = 12       # 区域成员的最大词数（图内标注都很短，挡住"题注下半截"这类长块）
FIG_PROSE_MIN_WORDS = 8         # 「正文块」判据：词数 ≥ 这个值且字号不缩水 → 区域里不许有它
FIG_RULE_MAX_H = 2.5            # 页眉/页脚分隔线：高度 ≤ 这个值（pt）
FIG_RULE_MIN_W_RATIO = 0.7      # 且宽度 ≥ 页面宽度的这个比例
FIG_BOX_MIN_INSIDE = 0.5        # 矢量项落在页面内的比例下限（挡 Form XObject 的越界变换框）
ANCHOR_MAX_WORDS = 150          # 题注锚点的最大词数（只是防病态的兜底：真题注可以很长，
                                # 实测旧 NHB 第 23 页的 "Extended Data Fig. 5 | …" 有 73 个词，
                                # 卡在 60 会让那一页整个漏掉；「不误伤正文」靠的是分隔符判据）

VECTOR_ITEM_TYPES = ("fill-path", "stroke-path")   # get_bboxlog() 里算「矢量路径」的类型

# 不参与图区判定的角色。**必须与 pdf_parser 的 ROLE_* 常量逐字一致**
# （table_finder.NON_TABLE_ROLES 踩过这个坑：写成 "references" 而实际常量是
# "reference"，结果是整片区域永远不显示）。这里把「标题」也算进来：
# 图页上的章节标题不该被卷进图区。
BACKGROUND_ROLES = ("header_footer", "reference", "copyright", "author",
                    "front_matter", "label", "heading")
# 进阅读流的角色（与 pdf_parser.READING_ROLES 对齐：正文 + 图注）
READING_ROLES = ("body", "caption")

# 图题注抬头：可选前缀（Extended Data / Supplementary / Supp.）+ Fig./Figure + 编号 + 分隔符。
# 分隔符必须是 `[:.、|｜]` 之一 —— 这就是「不误伤正文」的防线：
#   "Fig. 2 suggests that…" / "Figure 1 illustrates the process…" 编号后面是空格，不匹配 ✓
#   "Fig. 1 | A resource-rational mechanism…"（NHB 两篇的题注体例）匹配 ✓
# 口径与 pdf_parser.is_caption_text 一致（阶段 4.6-B 对齐），只是这里**只认图**、不认表。
_FIGURE_LABEL = r"(?:extended\s+data\s+|supplementary\s+|supp\.?\s+)?fig(?:ure)?"
_FIGURE_CAPTION_RE = re.compile(
    rf"^\s*{_FIGURE_LABEL}\s*\.?\s*(?:\d{{1,3}}|[IVX]{{1,4}})\s*[:.、|｜]", re.I)


@dataclass
class FigureRegion:
    """一处图区域：题注锚点在下方，区域矩形覆盖上方那块矢量插图"""
    region_id: int
    page: int
    rect: tuple                       # (x0, y0, x1, y1) —— 截图就是截这个矩形
    caption: str = ""                 # 触发的题注文字（便于人工核对）
    anchor_index: int = -1            # 题注块的原子块下标（**不进成员**，它留在卡片流里）
    atom_indices: list = field(default_factory=list)   # 区域内的「图内小字」块下标
    text: str = ""                    # 区域内文字（诊断用）
    source: str = "caption+cluster"   # caption+cluster / vector-only（图内文字是矢量轮廓，没文字块）
    cluster_blocks: int = 0           # 参与定位的图内小字块数


# ------------------------------------------------------------
# 一、页级探测与闸门
# ------------------------------------------------------------

def _clip_box(box, page_rect):
    """
    把一个 bbox 裁到页面里；裁完为空、或原本大部分落在页面外 → 返回 None。

    为什么要裁：旧 NHB 第 4 页的矢量并集是 (-4224,-4238)-(562,755) ——
    Form XObject 的变换矩阵把内容画到了页面外，不裁的话并集会糊到页角，
    定位出来的区域会从 (0,0) 开始、把页眉也框进去（实测）。
    """
    x0, y0, x1, y1 = box
    left, top = max(x0, page_rect[0]), max(y0, page_rect[1])
    right, bottom = min(x1, page_rect[2]), min(y1, page_rect[3])
    if right - left <= 0 or bottom - top <= 0:
        return None
    raw = max(x1 - x0, 0.0) * max(y1 - y0, 0.0)
    if raw <= 0 or ((right - left) * (bottom - top)) / raw < FIG_BOX_MIN_INSIDE:
        return None
    return (left, top, right, bottom)


def _is_furniture(box, page_rect) -> bool:
    """
    页眉/页脚的那根细横线是不是"装饰"？—— 又细又横跨整个版心。

    不排除它的话，旧 NHB 第 3 页的矢量并集会从 y=38（页眉分隔线）开始，
    区域顶端被拉到页眉上，截出来的图上面多一条页眉。
    """
    height = box[3] - box[1]
    width = box[2] - box[0]
    return height <= FIG_RULE_MAX_H and width >= (page_rect[2] - page_rect[0]) * FIG_RULE_MIN_W_RATIO


def probe_page(page) -> dict:
    """
    探测一页的「矢量路径项数 / 位图显示面积与矩形 / 矢量路径矩形列表」。

    这是本模块**唯一**碰 PDF 的函数（其余都是纯函数，便于单测）。

    两个易踩的坑：
      · 判位图必须用 `page.get_images(full=True)` + `page.get_image_rects()`，
        **不要用 `page.get_image_info()`**：它会把渐变着色也报成图、还会给出
        xref=0 的假条目（上一轮取证已确认）。
      · `get_bboxlog()` 里区分矢量（fill-path / stroke-path）与位图（fill-image），
        正是页级预筛要的信息。

    返回：
        vector_items    矢量路径项数
        bitmap_max_area 最大位图显示面积（pt²，诊断口径用）
        bitmap_rects    全部位图显示矩形 [(x0, y0, x1, y1), …]（含同一张图的重复摆放，
                        区域级覆盖率自己会取并集，所以这里不去重）
        vector_boxes    裁到页面内、剔除页眉页脚细横线之后的矢量路径矩形
        types           各绘图指令的条数（诊断用）
    """
    page_rect = (page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y1)
    vector_items, boxes, type_counter = 0, [], Counter()

    for item in page.get_bboxlog():
        type_counter[item[0]] += 1
        if item[0] not in VECTOR_ITEM_TYPES:
            continue
        vector_items += 1
        clipped = _clip_box(item[1], page_rect)
        if clipped is None or _is_furniture(clipped, page_rect):
            continue
        boxes.append(clipped)

    areas, bitmap_rects = [], []
    for info in page.get_images(full=True):
        for rect in page.get_image_rects(info[0]):
            areas.append(max(rect.width, 0.0) * max(rect.height, 0.0))
            bitmap_rects.append((rect.x0, rect.y0, rect.x1, rect.y1))

    return {
        "vector_items": vector_items,
        "bitmap_max_area": max(areas) if areas else 0.0,
        "bitmap_rects": bitmap_rects,
        "vector_boxes": boxes,
        "types": dict(type_counter),
    }


def should_probe_page(probe) -> bool:
    """
    页级**预筛**：这一页值不值得去定位图区域？—— 只看「矢量路径项 ≥ FIG_VECTOR_MIN」。

    阶段 4.7 起页级不再看位图大小：混合图页（位图 + 大量矢量）本来就该截图，
    而"该不该截图"改由**区域级**判据（bitmap_cover_ratio）决定，见模块说明。
    这一条仍然必要：正文页的分隔线与表格框线也会产生矢量项，
    完全没有矢量的页面（纯文字/纯位图）连定位都不用试。
    """
    if not probe:
        return False
    return int(probe.get("vector_items", 0)) >= FIG_VECTOR_MIN


def is_pure_vector_page(probe) -> bool:
    """这页是不是「纯矢量图页」（旧口径的第二条：没有面板级位图）。诊断与旧口径沿用"""
    if not probe:
        return False
    return float(probe.get("bitmap_max_area", 0.0)) < FIG_MAX_BITMAP_AREA


def should_capture_page(probe) -> bool:
    """
    **旧口径**（阶段 4.6 的页级闸门）：矢量路径项 ≥ FIG_VECTOR_MIN **且** 没有面板级位图。

    阶段 4.7 之后它**不再决定截不截图**——真正的判定在区域级
    （见 find_figure_regions 里的 FIG_REGION_MAX_BITMAP_COVER）。
    保留它有两个用处：
      · 诊断面板上的「纯矢量图页」计数（figure_report.gate_pages）；
      · 作为"这一类页肯定要走图区域"的**无条件通行证**：旧口径能过的页，
        区域级判据不必再拦（否则一块被位图铺满的小区域会被新判据否掉，
        而它本来就在旧行为里出过图）。
    两条判据的意义与阈值都没变，tests/test_figure_region.py ① 逐条钉住。
    """
    return should_probe_page(probe) and is_pure_vector_page(probe)


def union_area(rects, clip=None) -> float:
    """
    一组矩形（先裁到 clip）的**并集**面积 —— 精确值，不用栅格近似。

    为什么要并集：同一页里同一张图会被重复摆放（旧 NHB 第 19 页 22 个显示矩形
    其实只有 8 张图），把各矩形面积直接相加会得到 125% 这种假覆盖率。
    做法：取全部 x 边界切片，每片内把 y 区间合并后累加。
    """
    shapes = []
    for rect in rects or ():
        x0, y0, x1, y1 = rect
        if clip is not None:
            x0, y0 = max(x0, clip[0]), max(y0, clip[1])
            x1, y1 = min(x1, clip[2]), min(y1, clip[3])
        if x1 > x0 and y1 > y0:
            shapes.append((x0, y0, x1, y1))
    if not shapes:
        return 0.0
    xs = sorted({shape[0] for shape in shapes} | {shape[2] for shape in shapes})
    total = 0.0
    for i in range(len(xs) - 1):
        left, right = xs[i], xs[i + 1]
        if right <= left:
            continue
        middle = (left + right) / 2
        spans = sorted((shape[1], shape[3]) for shape in shapes
                       if shape[0] <= middle <= shape[2])
        if not spans:
            continue
        covered, cur_top, cur_bottom = 0.0, spans[0][0], spans[0][1]
        for top, bottom in spans[1:]:
            if top <= cur_bottom:
                cur_bottom = max(cur_bottom, bottom)
            else:
                covered += cur_bottom - cur_top
                cur_top, cur_bottom = top, bottom
        covered += cur_bottom - cur_top
        total += covered * (right - left)
    return total


def bitmap_cover_ratio(bitmap_rects, rect) -> float:
    """
    区域里被**位图**盖住的面积比例（0~1）—— 区域级闸门的核心数字。

    取并集（不是求和），并裁到区域矩形里：只有真正落在区域内的那部分位图才作数。
    """
    area = _area(rect)
    if area <= 0:
        return 0.0
    return union_area(bitmap_rects, rect) / area


# ------------------------------------------------------------
# 二、题注锚点与「图内小字」候选池
# ------------------------------------------------------------

def count_words(text: str) -> int:
    """与卡片口径一致的词数（英文按空白分词，够用）"""
    return len([w for w in (text or "").split() if w])


def is_figure_caption(text: str) -> bool:
    """这块文字是不是「图题注」抬头（形状判据，不含几何校验）"""
    return bool(_FIGURE_CAPTION_RE.match(text or ""))


def is_figureish(block, body_size: float) -> bool:
    """
    这个块像不像「图内小字」（图里的标注、刻度、角标、图例）？

    三个条件**同时**成立才算：
      · 字号比正文小（< FIG_SMALL_SIZE_RATIO × 正文字号）**或**词数 ≤ FIG_SMALL_MAX_WORDS；
      · 词数 ≤ FIG_MEMBER_MAX_WORDS（图内标注都很短：挡住"题注下半截"这种长块）；
      · 文本非空。
    两个尺寸判据是"或"的关系：有的图用与正文同字号的短标注（实测新 NHB 第 8 页），
    还有的图整页都是 5~7pt 的小字（旧 NHB 第 4 页字号有 5.0/5.1/5.5/5.6/6.0/7.0 六种）。
    """
    text = (block.text or "").strip()
    words = count_words(text)
    if not text or words > FIG_MEMBER_MAX_WORDS:
        return False
    small = bool(body_size) and block.max_size < body_size * FIG_SMALL_SIZE_RATIO
    return small or words <= FIG_SMALL_MAX_WORDS


def is_prose(block, body_size: float) -> bool:
    """
    这个块像不像「真正的正文」？（区域里只要出现它，区域就不可信）

    判据：词数 ≥ FIG_PROSE_MIN_WORDS **且** 字号没有缩水（≥ 0.98 × 正文字号）。
    字号这一条不能省：旧 NHB 第 4 页的坐标轴刻度组
    `−2.4 −1.8 −1.2 −0.6 0 0.6 Logit predictability` 有 9 个词，
    只按词数判就会把它当正文，于是那一页的图区永远被否掉。
    """
    if count_words(block.text) < FIG_PROSE_MIN_WORDS:
        return False
    if not body_size:
        return True
    return block.max_size >= body_size * FIG_SMALL_SIZE_RATIO


# ------------------------------------------------------------
# 三、间隙聚类（同构于 formula_finder.grow_clusters 的思路，但**互不影响**：
#     这里另写一份，绝不改 formula_finder 的现有行为）
# ------------------------------------------------------------

def _median(values) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else 0.0


def _union_rect(boxes) -> tuple:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _area(rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _band_has_vector(vector_boxes, top: float, bottom: float) -> bool:
    """
    [top, bottom] 这段纵向空隙里有没有矢量图形？

    这一条是「间隙聚类」在图形排版下的关键补丁：图里大部分面积是图形本体
    （箭头、方框、曲线），标签之间动辄隔 26~41pt —— 旧 NHB 第 3 页 40 个标签的
    纵向间距就是 26/30/36/41pt。只按「间隙 ≤ max(8pt, 1.2×中位字号)=8pt」聚类，
    40 个标签会碎成 40 个单块簇，图区永远定位不出来（实测）。
    而**版面留白**（段落之间、图与正文之间）里没有矢量路径：
    于是「空隙里有图形 → 属于同一张图；空隙是空白 → 断开」这条判据，
    既能把图内的稀疏标签连起来，又不会把正文段落吸进来。
    """
    for box in vector_boxes or ():
        if box[3] > box[1] and box[1] >= top and box[3] <= bottom:
            return True
    return False


def cluster_by_gap(indices, blocks, vector_boxes=(), gap_min: float = FIG_GAP_MIN_PT,
                   gap_factor: float = FIG_GAP_FACTOR, gap_max: float = FIG_GAP_MAX_PT):
    """
    把候选块按纵向间隙聚成簇，返回 [ [下标, …], … ]（每簇按 y 排序）。

    合并规则（三档，逐条与实测数据对应）：
      · 间隙 ≤ max(gap_min, gap_factor × 簇内中位字号) → 并（近邻；公式聚类用的就是这一档）；
      · 间隙 ≤ gap_max **且**这段空隙里有矢量图形 → 并（图形排版里的稀疏标签）；
      · 其余 → 断开（版面留白，或一段没有图形的空白）。
    """
    ordered = sorted(indices, key=lambda i: (blocks[i].y0, blocks[i].x0))
    clusters = []
    for index in ordered:
        block = blocks[index]
        if not clusters:
            clusters.append([index])
            continue
        current = clusters[-1]
        bottom = max(blocks[i].y1 for i in current)
        gap = block.y0 - bottom
        limit = max(gap_min, gap_factor * _median([blocks[i].max_size for i in current]))
        if gap <= limit:
            current.append(index)
        elif 0 <= gap <= gap_max and _band_has_vector(vector_boxes, bottom, block.y0):
            current.append(index)
        else:
            clusters.append([index])
    return clusters


# ------------------------------------------------------------
# 四、区域定位
# ------------------------------------------------------------

def _overlap_ratio(a, b) -> float:
    """两个矩形的交叠面积 / 较小者面积（0~1）"""
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    smaller = min(_area(a), _area(b))
    return (width * height) / smaller if smaller > 0 else 0.0


def _inside_ratio(block, rect) -> float:
    """这个块有多大比例落在矩形里"""
    return _overlap_ratio((block.x0, block.y0, block.x1, block.y1), rect)


def _pad_rect(rect, anchor_y0: float, page_size) -> tuple:
    """外扩 FIG_PAD_PT、下界夹在题注上方 1pt、并且夹进页面内"""
    padded = (rect[0] - FIG_PAD_PT, rect[1] - FIG_PAD_PT,
              rect[2] + FIG_PAD_PT, min(rect[3] + FIG_PAD_PT, anchor_y0 - 1))
    return (max(padded[0], 0.0), max(padded[1], 0.0),
            min(padded[2], page_size[0]), min(padded[3], page_size[1]))


def _reject_reason(rect, page, page_size, blocks, members, excluded, body_size) -> str:
    """区域可信度体检：返回空串表示通过，否则返回中文原因"""
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    if width < FIG_MIN_WIDTH:
        return f"区域太窄（{width:.0f}pt < {FIG_MIN_WIDTH:.0f}pt）"
    if height < FIG_MIN_HEIGHT:
        return f"区域太扁（{height:.0f}pt < {FIG_MIN_HEIGHT:.0f}pt）"
    page_area = max(page_size[0] * page_size[1], 1.0)
    if _area(rect) > page_area * FIG_MAX_AREA_RATIO:
        return f"区域面积超限（占页面 {_area(rect) / page_area:.0%}）"

    member_set = set(members)
    for index, block in enumerate(blocks):
        if block.page != page or index in excluded or index in member_set:
            continue
        if getattr(block, "kind", "") == "heading":
            continue                       # 标题夹在图里是排版常态，不算"框进正文"
        if _inside_ratio(block, rect) >= 0.5 and is_prose(block, body_size):
            return f"区域里框进了正文块（{count_words(block.text)} 词）"
    return ""


def _is_anchor(block, body_size=None) -> bool:
    """
    这块是不是可用的「图题注锚点」：角色不是背景信息 + 词数没到病态 + 抬头形状对。

    ⚠️ 这个函数必须被 find_figure_regions 与 figure_report **共用**：
    早先两边各写一套（一边还多一个 60 词的卡口），于是第 23 页出现了
    「诊断里算它一个图注页、定位时又不认它的题注」的自相矛盾（实测）。
    """
    return (count_words(block.text) <= ANCHOR_MAX_WORDS and is_figure_caption(block.text))


def find_figure_regions(blocks, page_probes, page_sizes, body_size,
                        excluded=frozenset(), excluded_roles=BACKGROUND_ROLES,
                        avoid_rects=()):
    """
    找出全部图区域。返回 (regions, failures)：
        regions   [FigureRegion, …]，按 (页码, y) 排序
        failures  [{"page", "caption", "reason"}, …] —— 过了闸门但**定位失败**的题注，
                  诊断面板要如实列出（稳定优先：宁可不截图，也不截整页正文）

    参数：
        blocks       : 原子块列表（要读 page/role/坐标/字号，最后写回 atom_indices）
        page_probes  : {页码: probe_page() 的结果}
        page_sizes   : {页码: (宽, 高)}
        body_size    : 正文字号（判「图内小字」与「正文块」的基准）
        excluded     : **已确认为表格/公式区域的原子块下标**（必须扣除，不能重复出图）
        excluded_roles : 不参与判定的角色（背景信息与标题）
        avoid_rects  : **已确认的表格区域** `[(页码, (x0, y0, x1, y1)), …]`：同页上与之重叠
                       的区域放弃（阶段 4.7；默认空 = 旧行为）。必须带页码——只比矩形会
                       把不同页上坐标相近的地方误判成重叠。

    定位三步（顺序不能换）：
      ① **题注锚点定下界**：只认「图题注」抬头（Fig./Figure/Extended Data Fig.…）；
      ② **图内小字按间隙聚类**：候选池 = 题注上方、非正文、短小的块；
         取与题注相邻（间距 ≤ FIG_ANCHOR_MAX_GAP）的那一簇作为区域成员；
      ③ **矢量并集撑矩形**：把矩形撑到「题注上方全部矢量路径」的范围，
         这样图形的描边与边距不会被切掉；再外扩 FIG_PAD_PT、下界夹到题注上方。
    没通过体检（太窄/太扁/面积超限/框进正文/与前一个图区重叠）就**放弃截图**并记一条 failure。

    区域级闸门（阶段 4.7，用户报障驱动）——体检之后、出图之前再看两条：
      · `avoid_rects`（已确认的表格区域）里有谁与它重叠超过 FIG_TABLE_OVERLAP_MAX
        → 放弃（那块地方已经有表格图，再截一次就是把表格当图出两遍）；
      · 区域内位图显示面积的并集占比 ≥ FIG_REGION_MAX_BITMAP_COVER
        → 放弃（这一块本来就由位图提取路径呈现）。
      例外：过**旧页级闸门**（should_capture_page = 纯矢量图页）的区域无条件放行
      —— 那一类页在 4.6 里就出图，不能因为新判据把已有行为吞掉。
    """
    regions, failures = [], []
    for page in sorted(page_probes):
        probe = page_probes[page]
        if not should_probe_page(probe):
            continue                          # 页级预筛：矢量太少（纯文字/纯位图）不必试
        pure_vector_page = should_capture_page(probe)   # 旧口径：这一类页无条件放行
        page_blocks = [i for i, b in enumerate(blocks) if b.page == page]
        if not page_blocks:
            continue
        anchors = [i for i in page_blocks
                   if i not in excluded
                   and blocks[i].role not in excluded_roles
                   and _is_anchor(blocks[i])]
        if not anchors:
            continue                          # 过了预筛但这一页没有图题注：无事可做
        page_size = page_sizes.get(page, (595.0, 842.0))
        claimed = []

        for anchor_index in sorted(anchors, key=lambda i: blocks[i].y0):
            anchor = blocks[anchor_index]
            caption = anchor.text.strip()

            # ① 题注上方必须有矢量图形（否则这块题注上方不是矢量图）
            above = [box for box in probe.get("vector_boxes", ()) if box[3] <= anchor.y0 + 1]
            if not above:
                failures.append({"page": page, "caption": caption[:40],
                                 "reason": "题注上方没有矢量路径（不是矢量图页）"})
                continue
            vector_rect = _union_rect(above)

            # ② 图内小字候选池 + 间隙聚类，取与题注相邻的那一簇
            # ⚠️ 候选池要排除**本页全部题注块**（不只是当前这一条）：题注字号通常比正文小
            # （旧 NHB 是 7.0pt vs 8.3pt），形状上完全符合「图内小字」，一旦落进区域矩形
            # 就会被移出卡片流——那就违反了项目规则「图注视作正文」。
            pool = [i for i in page_blocks
                    if i not in anchors and i not in excluded
                    and blocks[i].role not in excluded_roles
                    and blocks[i].y1 <= anchor.y0 + 1
                    and is_figureish(blocks[i], body_size)]
            clusters = cluster_by_gap(pool, blocks, probe.get("vector_boxes", ()))
            adjacent, best_gap = None, None
            for cluster in clusters:
                gap = anchor.y0 - max(blocks[i].y1 for i in cluster)
                if gap < -1 or gap > FIG_ANCHOR_MAX_GAP:
                    continue
                if best_gap is None or gap < best_gap:
                    adjacent, best_gap = cluster, gap

            cluster_rect = None
            if adjacent:
                cluster_rect = _union_rect([(blocks[i].x0, blocks[i].y0,
                                             blocks[i].x1, blocks[i].y1) for i in adjacent])
            elif vector_rect[3] < anchor.y0 - FIG_ANCHOR_MAX_GAP:
                failures.append({"page": page, "caption": caption[:40],
                                 "reason": "题注上方没有相邻的图内文字，矢量并集也离题注太远"})
                continue

            # ③ 矩形 = 矢量并集 ∪ 文字簇并集，外扩后夹在「页面内、题注上方」
            rect = vector_rect if cluster_rect is None else _union_rect([vector_rect, cluster_rect])
            rect = _pad_rect(rect, anchor.y0, page_size)

            if cluster_rect is None:
                # 图内文字本身是矢量轮廓时（新 NHB 的 Reporting Summary 就是这么排的），
                # 候选池是空的：没有文字会漏进卡片，但图还是该出 —— 只在
                # 「矢量并集确实贴着题注」时才这么做，source 记为 vector-only 以便核对。
                members = []
            else:
                # 成员 = **落在区域里的**图内小字（不是"相邻簇的成员"）。
                # 为什么改成按矩形收：相邻簇只是"这一页确实有图"的定位证据，
                # 而图里可能还有间距太大、连不成一簇的孤立标注（实测旧 NHB 第 3 页
                # 左侧的 "Text-level" 就是单独一块）。按矩形收能把它一起移出正文。
                # ⚠️ 成员必须**保证落在矩形里**（容差 50%）：成员会被移出卡片流、
                # 只由截图承载，块在矩形外就意味着"文字既不在卡片里、也不在图里"=内容丢失。
                members = [i for i in pool if _inside_ratio(blocks[i], rect) >= 0.5]
                if members:
                    # 把矩形撑到**完整包住**这些成员：不然成员块会被截去一半
                    # （实测第 3 页 "Text-level" 被截成 "t-level"，人工核对时一眼就看出来了）
                    rect = _union_rect([rect] + [(blocks[i].x0, blocks[i].y0,
                                                  blocks[i].x1, blocks[i].y1) for i in members])
                    rect = _pad_rect(rect, anchor.y0, page_size)

            reason = _reject_reason(rect, page, page_size, blocks, members, excluded, body_size)
            if not reason and any(_overlap_ratio(rect, other) > FIG_OVERLAP_MAX for other in claimed):
                reason = "与前一个图区重叠（同一张图被两条题注各判一次）"
            if not reason:
                # 区域级闸门之一：与已确认的表格区域重叠 → 那块地方已经有表格图了
                # ⚠️ avoid_rects 必须带页码：(页码, 矩形) —— 只比矩形的话，
                # 不同页上"坐标恰好相近"的地方会被误判成重叠（实测：旧 NHB 第 6 页的表格
                # 把第 3/4/10/23 页本来正常的图区域全否掉了）。
                for avoid_page, avoid in avoid_rects or ():
                    if int(avoid_page) != page:
                        continue
                    if _overlap_ratio(rect, tuple(avoid)) > FIG_TABLE_OVERLAP_MAX:
                        reason = "与已确认的表格区域重叠（表格已单独出图，不再当图截一遍）"
                        break
            if not reason and not pure_vector_page:
                # 区域级闸门之二：区域内位图覆盖率过高 → 交给位图提取路径
                # （过旧页级闸门的「纯矢量图页」不做这一条：那类页在 4.6 里就出图，
                #   不能因为一块被位图铺满的小区域被新判据吞掉已有的出图行为）
                cover = bitmap_cover_ratio(probe.get("bitmap_rects", ()), rect)
                if cover >= FIG_REGION_MAX_BITMAP_COVER:
                    reason = (f"区域内位图覆盖 {cover:.0%}（≥ {FIG_REGION_MAX_BITMAP_COVER:.0%}）"
                              f"——这一块由位图提取路径呈现，不做区域截图")
            if reason:
                failures.append({"page": page, "caption": caption[:40], "reason": reason})
                continue

            claimed.append(rect)
            regions.append(FigureRegion(
                region_id=len(regions), page=page, rect=rect, caption=caption,
                anchor_index=anchor_index, atom_indices=members,
                text=" ".join(blocks[i].text.strip() for i in members),
                source="caption+cluster" if members else "vector-only",
                cluster_blocks=len(members)))

    regions.sort(key=lambda item: (item.page, item.rect[1]))
    for number, region in enumerate(regions):
        region.region_id = number
    return regions, failures


def covered_image_indices(images, regions, min_ratio: float = FIG_IMAGE_IN_REGION_RATIO) -> list:
    """
    哪些**已提取的位图**已经被图区域截图吸收了（阶段 4.7 的去重）—— 返回下标列表。

    区域截图是把整块矩形渲染出来，位图本来就在里面；如果位图再单独出一遍，
    用户会看到同一张图出现两次。判据：位图矩形落在某个图区域里的比例 ≥ min_ratio
    （默认 0.5）。

    纯函数、不碰 PDF：吃的是 image_extractor.ImageItem（只读 page/bbox）与 FigureRegion。
    ⚠️ 返回**下标**而不是对象：ImageItem 是非 frozen 的 dataclass，`==` 会逐字段比较
    （包含 thumb 那串 PNG 字节），用对象做 `in`/集合既慢又容易踩"两张一模一样的图"
    这种语义陷阱；下标没有这个问题。

    开关关掉时 figure_regions 恒为空列表 → 这里恒返回空 → 与旧行为逐字段一致。
    """
    hidden = []
    by_page = {}
    for region in regions or ():
        by_page.setdefault(int(region.page), []).append(tuple(region.rect))
    for index, image in enumerate(images or ()):
        rects = by_page.get(int(getattr(image, "page", 0) or 0))
        if not rects:
            continue
        box = tuple(getattr(image, "bbox", ()) or ())
        if len(box) != 4:
            continue
        for rect in rects:
            if _overlap_ratio(box, rect) >= min_ratio:
                hidden.append(index)
                break
    return hidden


def figure_report(blocks, page_probes, excluded_roles=BACKGROUND_ROLES) -> dict:
    """
    诊断数字（右栏「解析诊断」显示用）：

        caption_pages            有图题注锚点的页
        caption_pages_no_bitmap  其中**没有面板级位图**的页 —— 就是
                                 「本篇 N 个图注页无面板级位图」里的 N
        gate_pages               过**旧页级闸门**的页（矢量够 + 没有面板级位图）
        probe_pages              过**页级预筛**的页（只要矢量够；阶段 4.7 起
                                 真正去定位图区域的就是这一组，混合图页也在里面）

    注意 caption_pages_no_bitmap 只看位图面积这一条：它是"值得关注的图注页"的口径，
    与页级预筛（还要求矢量 ≥ 20）不同 —— 两者的差集就是"图注页但矢量也不足"的那些页。
    """
    caption_pages = sorted({b.page for b in blocks
                            if b.role not in excluded_roles and _is_anchor(b)})
    no_bitmap = [page for page in caption_pages
                 if float((page_probes.get(page) or {}).get("bitmap_max_area", 0.0))
                 < FIG_MAX_BITMAP_AREA]
    gate_pages = [page for page, probe in sorted(page_probes.items())
                  if should_capture_page(probe)]
    probe_pages = [page for page, probe in sorted(page_probes.items())
                   if should_probe_page(probe)]
    return {"caption_pages": caption_pages,
            "caption_pages_no_bitmap": no_bitmap,
            "gate_pages": gate_pages,
            "probe_pages": probe_pages,
            "caption_page_count": len(caption_pages),
            "caption_pages_no_bitmap_count": len(no_bitmap),
            "gate_page_count": len(gate_pages),
            "probe_page_count": len(probe_pages)}
