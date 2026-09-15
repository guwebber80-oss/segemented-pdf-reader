"""
table_finder.py —— 期刊表格区域识别（阶段 4.4-B）

【为什么需要这一步】
期刊表格在 PDF 里**没有任何「表格」标记**，只有一堆带坐标的文字。4.4-B 之前，
它们被当成正文按阅读顺序线性化，于是卡片里出现这样的句子：

    'EV 0.758 0.682 0.735 0.707 0.723 0.746 0.790'
    'Fusion EV-EI-FC 0.960 0.882 0.905 0.949 0.924 0.931 0.977'

列与列的关系全丢了，读者既看不出这是表格，也不知道哪个数字属于哪一列，
还白占了卡片流的大段篇幅。本模块把这些区域找出来，交给截图管线整块呈现。

【判据：① 题注锚点 + ② 对齐列几何校验，缺一不可】
① 题注锚点：`Table 2 | …` / `Table 1: …` / `TABLE 1 …` 这类题注行。
② 几何校验：题注下方的行必须构成「≥3 列 × ≥3 行」的对齐网格。

为什么必须有第②步——实测三篇样本的**正文里都有以 "Table N. " 开头的提及句**：
  · npj 第 2 页  `Table 1. Independent-samples t-tests indicated no group differences in…`
  · SAGE 第 15 页 `Table 7 for cell values including Target Sex). The analysis…`
  · Frontiers 第 6 页 `Table 1. An overview of these loadings is summarized below.`
只认题注的话这三处会全部误判成表格；而它们的下方都是两栏散文
（只有 2 个 x 对齐位置）→ 几何校验一眼就能分开。

【行/单元格的真实形态（实测，决定判据写在哪个层级）】
  · npj Digital Medicine：一行表格 = **同一 y 上的 8 个独立文本行**（每个单元格一个行对象）；
  · Nature Human Behaviour：7 个；ACM CHI：4 个。
也就是说「一行表格」不是「一个文本块」——而 pdf_parser 的 Block 已经把同一行的
多个单元格**合并成一个块**了（列位置信息在合并时就丢了）。因此本模块工作在
**行（line）层级**，行数据由 `pdf_parser.extract_page_lines()` 提供，
判定完再按坐标把行映射回 Block，供管线把整块内容移出文字流。

【不做什么（用户拍板，2026-09-15）】
  · 不跨页合并（跨页表格各自成区域，`TABLE 1 (Continued)` 会各成一块）；
  · 不转 Markdown、不做结构还原（OCR/单元格识别）；
  · 不做「按列聚合数值」之类的语义分析——只保证**看得见原始排版**。
"""

from dataclasses import dataclass, field
import re

# ------------------------------------------------------------
# 判据参数：全部集中在这里，便于按全样本基线调参
# ------------------------------------------------------------
# 题注行：Table 2 | … / Table 1: … / TABLE 1 … / 表 1 …
# 注意结尾要求「后面还有内容」——裸的 "Table" 两个字不算题注。
CAPTION_RE = re.compile(r"^\s*(?:Table|TABLE|Tableau|表)\s*([0-9]{1,2}|[IVX]{1,4})\s*(?:[.:|｜]|\s)\s*\S")
CAPTION_MAX_WORDS = 30        # 题注本身不该是一大段话（挡「提及句」的第一道）
ROW_TOL_PT = 3.0              # 同一行的 y 容差（单元格基线会有零点几点的差）
# 一行至少几个单元格才算表格行 = 3。
# 这条是**区分表格与双栏正文的关键**：双栏排版里左右两栏各有一行文字、
# y 又恰好相同，于是它们会被当成「一行两个单元格」——把正文误当表格。
# 实测的真实表格每行都有 4~11 个单元格，安全余量足够。
MIN_CELLS_IN_ROW = 3
WRAP_IF_FEWER_THAN = 2        # 少于这个数才可能是「单元格内换行」（只有单个单元格的行）
MIN_COLUMNS = 3               # 至少几列——2 列的网格与双栏正文无法区分，故不判
COLUMN_MIN_ROWS = 3           # 一个「列」至少要出现在几行里才算真列
MIN_ROWS = 3                  # 至少几行
ROW_GAP_PT = 16.0             # 相邻行之间的最大垂直间距（超过就当表格结束）
# 题注 → 第一行的最大间距：ACM 的表题注与表体之间隔了一条横线 + 空行，实测 39pt，
# 所以放宽到 48；「是不是真的表格」由下面的几何校验把关，不靠这个间距。
CAPTION_GAP_PT = 48.0
WRAP_GAP_PT = 15.0            # 单元格内的换行（同一格里的第二行）合并间距
WRAP_X_TOL_PT = 10.0          # 换行必须落在同一列的 x 范围里
WRAP_MAX_WORDS = 8            # 能被当成「单元格内换行」的行必须够短（正文行一律 10 词以上）
WRAP_MAX_LINES = 3            # 一行最多并进几个「换行」（防止链式吞并）
COL_TOL_PT = 2.0              # x0 对齐容差
MAX_REGION_HEIGHT_RATIO = 0.62   # 区域高度上限（占页面比例），超了就不判
MAX_ROWS = 80
# 「单元格长得像表格」的兜底：中位单元格够短，或者数字占比够高。
# 用来挡「多栏正文 + 附近恰好有一句 Table N」这种误判（多栏正文每行也够 3 格）。
TABLE_MEDIAN_CELL_WORDS = 6
TABLE_MIN_NUMERIC_RATIO = 0.30

# 无题注的兜底（几何路径）额外要满足的条件——比题注路径严格得多：
# 必须「单元格基本是数字/统计量」且每格都很短，否则双栏、三栏正文会被当成表格。
GEOMETRY_MIN_ROWS = 4
GEOMETRY_MIN_NUMERIC_RATIO = 0.45
GEOMETRY_MAX_CELL_WORDS = 6
_NUMERIC_CELL_RE = re.compile(r"^[<>=≤≥~±+\-−–]?\s*[.\d][\d.,%]*\s*(?:%|‰|pt|px|ms|s|Hz|mm|cm)?$")
_STAT_CELL_RE = re.compile(r"^(?:[<>]\s*)?[.\d]+(?:\s*[±]\s*[.\d]+)?$")

# 这些角色的行不参与表格判定：页眉页脚里的 "Table 1 | …" 是版式噪声，
# 参考文献里的 "Table 1" 是引用标题，都不是本文的表格。
# 名字必须与 pdf_parser 的 ROLE_* 常量**逐字一致**——这里写错过一次
# （写成 "references"，而实际常量是 "reference"），结果是参考文献区里的一张
# 表格被识别出来、却因为角色不进卡片流而永远不显示（区域与出图数不一致）。
NON_TABLE_ROLES = ("header_footer", "reference", "copyright", "author", "front_matter", "label")


@dataclass
class TableRegion:
    """一处表格区域（题注 + 对齐的表格行）"""
    region_id: int
    page: int
    rect: tuple                     # (x0, y0, x1, y1) —— 截图就是截这个矩形
    caption: str = ""
    columns: int = 0                # 校验通过的列数
    rows: int = 0                   # 校验通过的行数
    line_count: int = 0             # 区域里一共多少行（含单元格行）
    kind: str = "captioned"         # captioned（有题注）/ geometry（无题注兜底）
    atom_indices: list = field(default_factory=list)   # 区域覆盖的原子块下标
    text: str = ""                  # 区域内的文字（「保留文字」模式与诊断用）


def _is_caption(line) -> bool:
    """这一行像不像表格题注（形状判据，不含几何校验）"""
    text = line["text"].strip()
    if not CAPTION_RE.match(text):
        return False
    return len(text.split()) <= CAPTION_MAX_WORDS


def _group_rows(lines):
    """
    把文本行按 y 归组成「表格行」：同一 y 上的多个行对象 = 同一行的多个单元格。

    认不出来的一律单独成行（后面 _merge_wrapped 会把「单元格内换行」并回上一行）。
    """
    rows = []
    for line in sorted(lines, key=lambda item: (item["y0"], item["x0"])):
        if rows and abs(line["y0"] - rows[-1]["y"]) <= ROW_TOL_PT:
            rows[-1]["cells"].append(line)
            rows[-1]["bottom"] = max(rows[-1]["bottom"], line["y1"])
        else:
            rows.append({"y": line["y0"], "bottom": line["y1"], "cells": [line]})
    return rows


def _merge_wrapped(rows):
    """
    合并「单元格内换行」：某行只有一个单元格、又紧跟在上一行下面、
    且 x 落在上一行某一列的范围内 → 它其实是上一行某个单元格的第二行文字。
    （ACM 的表头 'Right Eye' / 'or Equal Viewing' 就是这种折行。）

    三道闸门，缺一不可——**这一步是整份实现里最容易出错的地方**：
    实测（npj 第 3、4 页）表格下方第一行正文的左边界与表格第一列几乎重合，
    只按「x 接近 + y 相邻」合并的话，会把整段正文一路链式并进最后一行，
    区域矩形随即吃进正文、把 123 词和 101 词的正文段整块标成「表格」。
      · ① 行必须够短（≤ WRAP_MAX_WORDS 词）——正文行一般 10 词以上；
      · ② 距离按**该行自己的 y** 算，不按合并后不断下移的 bottom 算，
           否则相邻的正文行会一个接一个地续上来；
      · ③ 以句末标点结尾的行不并（那是句子，不是单元格续行）。
    """
    merged = []
    for row in rows:
        if merged and len(row["cells"]) < WRAP_IF_FEWER_THAN:
            previous = merged[-1]
            cell = row["cells"][0]
            short_enough = len(cell["text"].split()) <= WRAP_MAX_WORDS
            no_period = not cell["text"].rstrip().endswith((".", "。", "?", "！", "!", "?", ";"))
            close_y = row["y"] - previous["y"] <= WRAP_GAP_PT
            close_x = any(abs(cell["x0"] - other["x0"]) <= WRAP_X_TOL_PT
                          for other in previous["cells"])
            if short_enough and no_period and close_y and close_x \
                    and previous.get("wrapped", 0) < WRAP_MAX_LINES:
                previous["cells"].extend(row["cells"])
                previous["bottom"] = max(previous["bottom"], row["bottom"])
                previous["wrapped"] = previous.get("wrapped", 0) + 1
                continue
        merged.append(row)
    return merged


def _columns(rows):
    """
    统计「列」：把各行单元格的 x0 聚类，只保留出现在 ≥COLUMN_MIN_ROWS 行里的列。

    返回 [(x0, 命中行数), …]。
    """
    buckets = []
    for row in rows:
        key = round(row["y"], 1)
        for cell in row["cells"]:
            for bucket in buckets:
                if abs(bucket["x"] - cell["x0"]) <= COL_TOL_PT:
                    bucket["ys"].add(key)
                    break
            else:
                buckets.append({"x": cell["x0"], "ys": {key}})
    return [(b["x"], len(b["ys"])) for b in buckets if len(b["ys"]) >= COLUMN_MIN_ROWS]


def _rows_under_caption(lines, caption):
    """
    从题注往下扫出候选表格行：题注下方到「间距突然变大」为止。
    这里只做「连续块」的切分，够不够成表格由 _columns 判定。
    """
    rows, cursor = [], caption["y1"]
    for line in sorted(lines, key=lambda item: (item["y0"], item["x0"])):
        if line["y0"] <= caption["y1"] - 1:          # 必须在题注下方
            continue
        if rows:
            if line["y0"] - cursor > ROW_GAP_PT:
                break
        elif line["y0"] - caption["y1"] > CAPTION_GAP_PT:
            break                                     # 题注下方空太多，后面不是这张表
        if rows and abs(line["y0"] - rows[-1]["y"]) <= ROW_TOL_PT:
            rows[-1]["cells"].append(line)
            rows[-1]["bottom"] = max(rows[-1]["bottom"], line["y1"])
        else:
            rows.append({"y": line["y0"], "bottom": line["y1"], "cells": [line]})
        cursor = max(cursor, line["y1"])
        if len(rows) > MAX_ROWS:
            break
    return rows


def _region_rect(caption, rows):
    """区域外接矩形：题注 + 表格行，左右外扩 1pt（截图时还会再扩 2~3pt）"""
    left = min([caption["x0"]] + [c["x0"] for row in rows for c in row["cells"]])
    right = max([caption["x1"]] + [c["x1"] for row in rows for c in row["cells"]])
    top = caption["y0"]
    bottom = max(row["bottom"] for row in rows)
    return (left - 1.0, top - 1.0, right + 1.0, bottom + 1.0)


def _numeric_ratio(rows):
    """单元格里「数字/统计量」的占比——无题注兜底路径用它挡散文"""
    total = numeric = 0
    for row in rows:
        for cell in row["cells"]:
            total += 1
            text = cell["text"].strip()
            if _NUMERIC_CELL_RE.match(text) or _STAT_CELL_RE.match(text):
                numeric += 1
    return (numeric / total) if total else 0.0


def _short_cells(rows) -> bool:
    """每格都很短（表格单元格的特征；散文列里的句子不会这么短）"""
    for row in rows:
        for cell in row["cells"]:
            if len(cell["text"].split()) > GEOMETRY_MAX_CELL_WORDS:
                return False
    return True


def _block_index(blocks, page_no):
    """这一页的 (原子块下标, bbox)，用来把「行」映射回「块」"""
    return [(index, (b.x0, b.y0, b.x1, b.y1))
            for index, b in enumerate(blocks) if b.page == page_no]


def _line_owner(line, index):
    """行落在哪个原子块里（按行中心点判断）"""
    cx = (line["x0"] + line["x1"]) / 2
    cy = (line["y0"] + line["y1"]) / 2
    for block_index, (x0, y0, x1, y1) in index:
        if x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1:
            return block_index
    return None


def _eligible_lines(lines, index, blocks, excluded_roles):
    """
    筛掉不该参与表格判定的行（页眉页脚 / 参考文献 / 已确认的公式区域）。

    顺带把「这一行属于哪个块」缓存进行记录（line["owner"]）：这一步是
    行 × 块的嵌套扫描，一页几百行 × 上百块，如果每个题注都重算一遍，
    多表样本的解析耗时实测会多出 1.8 秒（双栏.pdf 2.24s → 4.07s）。
    """
    out = []
    for line in lines:
        owner = _line_owner(line, index)
        if owner is None:
            continue
        block = blocks[owner]
        if block.role in excluded_roles or block.cluster_id >= 0:
            continue
        line["owner"] = owner
        out.append(line)
    return out


def _looks_like_table_cells(rows) -> bool:
    """
    单元格长得像不像表格：**中位单元格长度够短**，或者数字占比够高。

    挡的是「多栏正文 + 附近恰好有一句 Table N」这种误判——多栏正文同样满足
    「每行 ≥3 格、≥3 列」，但格子里是一整句话（中位 11~15 词），
    而真实表格的中位单元格只有 2~4 词。

    注意用**中位数**而不是「每一格都必须短」：Nature Human Behaviour 的表里
    有 'P value, effect size (95% CI)' 这种长单元格，逐格卡长度会把真表格一起挡掉
    （实测踩过：加上逐格判定后，Nature 的表和 ACM 的 Table 4 全部消失）。
    """
    words = sorted(len(cell["text"].split()) for row in rows for cell in row["cells"])
    if not words:
        return False
    median = words[len(words) // 2]
    return median <= TABLE_MEDIAN_CELL_WORDS or _numeric_ratio(rows) >= TABLE_MIN_NUMERIC_RATIO


def _near_column(x, columns_x, tol=COL_TOL_PT) -> bool:
    """这个 x 是不是落在某个「校验通过的列」上"""
    return any(abs(x - column_x) <= tol for column_x in columns_x)


def _trim_to_columns(rows, columns_x):
    """
    切掉「最后一个确实对齐的行」之后的内容。

    为什么需要：表格下方的正文段（双栏时左右各一行、三栏时三行）在行层也满足
    「一行多个单元格」，如果它们恰好落在校验出的列上，区域矩形就会被一路拉到页尾
    （实测 npj 第 3、4 页把 123 词和 101 词的正文段整块框进了表格图）。

    注意**不能**从第一行开始严格匹配就停：ACM 的表头是居中对齐、数据是右对齐，
    表头单元格与数据列根本对不上（实测 4 个表头格只有 1 个落在列上）。
    所以规则是「保留到最后一个对齐行为止」，表头自然被保留在图片里。
    """
    aligned = [position for position, row in enumerate(rows)
               if sum(1 for cell in row["cells"] if _near_column(cell["x0"], columns_x)) >= 2]
    if not aligned:
        return rows
    return rows[:aligned[-1] + 1]


def find_table_regions(blocks, page_lines, page_sizes, excluded_roles=NON_TABLE_ROLES):
    """
    找出全部表格区域。返回 [TableRegion, …]，按 (页码, y) 排序。

    参数：
        blocks      : 原子块列表（要读 role / cluster_id，最后写回 atom_indices）
        page_lines  : {页码: [行, …]}，由 pdf_parser.extract_page_lines 产出
        page_sizes  : {页码: (宽, 高)}
        excluded_roles : 不参与判定的角色

    调用时机很重要：必须在**角色分类之后**（角色用来排除页眉页脚/参考文献）、
    在**公式聚类之前**（表格块先被认定，公式聚类就不会把它们吸进公式图里）。
    """
    regions = []
    for page_no in sorted(page_lines):
        lines = [line for line in page_lines[page_no] if line["text"].strip()]
        if not lines:
            continue
        page_height = page_sizes.get(page_no, (0.0, 0.0))[1]
        index = _block_index(blocks, page_no)
        eligible = _eligible_lines(lines, index, blocks, excluded_roles)
        if len(eligible) < MIN_ROWS:
            continue

        taken = []          # 已占用的纵向区间，避免同一个表被两个题注各判一次
        for caption in sorted(eligible, key=lambda item: item["y0"]):
            if not _is_caption(caption):
                continue
            if any(top <= caption["y0"] <= bottom for top, bottom in taken):
                continue

            # 只看与题注**同一栏**的行。
            # 为什么必须按栏过滤：双栏排版里右栏正文行与左栏表格的 y 恰好相同，
            # 于是它们会被当成表格的额外单元格；更糟的是——因为多行都有它，
            # 它还会被 _columns 投票成一个「合法列」（实测 npj 第 3 页的 x≈306
            # 拿到 5 票），靠列过滤根本挡不住，只能从栏位下手。
            # 通栏（-1）的行一律允许：表格本身经常跨栏，题注却排在左栏。
            caption_owner = caption.get("owner")
            if caption_owner is None:
                continue
            caption_column = blocks[caption_owner].column
            same_column = [line for line in eligible
                           if blocks[line["owner"]].column == caption_column
                           or blocks[line["owner"]].column == -1]

            rows = _merge_wrapped(_rows_under_caption(same_column, caption))
            data_rows = [row for row in rows if len(row["cells"]) >= MIN_CELLS_IN_ROW]
            if len(data_rows) < MIN_ROWS:
                continue
            columns = _columns(data_rows)
            if len(columns) < MIN_COLUMNS:
                continue
            # 单元格还得「像表格」：否则三栏正文 + 一句 Table N 会被整块框进来
            if not _looks_like_table_cells(data_rows):
                continue
            # 裁剪：只留落在校验列上的连续行（切掉被误并进来的正文行）
            table_rows = _trim_to_columns(data_rows, [x for x, _n in columns])
            if len(table_rows) < MIN_ROWS:
                continue
            # 再按「校验出的列」筛一遍单元格：双栏排版里与表格同 y 的**右栏正文行**
            # 会被当成表格的额外单元格（左栏表格 + 右栏正文并排就是这种情况），
            # 它们的 x 不在校验列上，一律丢掉。
            column_xs = [x for x, _n in columns]
            grid_rows = []
            for row in table_rows:
                cells = [cell for cell in row["cells"] if _near_column(cell["x0"], column_xs)]
                if len(cells) >= 2:
                    grid_rows.append({"y": row["y"],
                                      "bottom": max(cell["y1"] for cell in cells),
                                      "cells": cells})
            if len(grid_rows) < MIN_ROWS:
                continue

            rect = _region_rect(caption, grid_rows)
            if page_height and (rect[3] - rect[1]) > page_height * MAX_REGION_HEIGHT_RATIO:
                continue                                  # 区域高得离谱，多半是误判

            cells = [caption] + [cell for row in grid_rows for cell in row["cells"]]
            atoms = sorted({owner for owner in (_line_owner(line, index) for line in cells)
                            if owner is not None})
            regions.append(TableRegion(
                region_id=len(regions), page=page_no, rect=rect,
                caption=caption["text"].strip(), columns=len(columns), rows=len(grid_rows),
                line_count=len(cells), kind="captioned", atom_indices=atoms,
                text=" ".join(line["text"].strip() for line in cells),
            ))
            taken.append((rect[1], rect[3]))

        regions.extend(_geometry_regions(eligible, index, page_no, page_height,
                                         len(regions), taken))

    regions.sort(key=lambda item: (item.page, item.rect[1]))
    for number, region in enumerate(regions):
        region.region_id = number
    return regions


def _regular_pitch(rows, tolerance: float = 0.25, min_ratio: float = 0.7) -> bool:
    """
    行距是否规整——只用在「无题注兜底」这条路上。

    为什么需要（实测 Nature Human Behaviour 第 4 页）：那是一张**六面板图**，
    坐标轴刻度（'400 480 0.5 0.25'、'Average gaze duration (ms)'…）在行层看
    同样是「一行好几个短单元格、x 还对齐」，于是被兜底路径整块框成了表格
    （66 个块、129 行 ✗）。区别在于：真表格的行距是均匀的（实测 npj 13pt、
    ACM 10pt、Nature 14.6pt），而图的刻度标签疏密不均。
    """
    ys = [row["y"] for row in rows]
    gaps = [b - a for a, b in zip(ys, ys[1:]) if b - a > 0.5]
    if len(gaps) < 2:
        return True
    gaps.sort()
    median = gaps[len(gaps) // 2]
    if median <= 0:
        return False
    close = sum(1 for gap in gaps if abs(gap - median) <= median * tolerance)
    return (close / len(gaps)) >= min_ratio


def _geometry_regions(lines, index, page_no, page_height, next_id, taken):
    """
    无题注兜底：找「没有题注但确实是对齐网格」的区域。

    比题注路径严格得多——必须 ≥4 行、每行 ≥3 格、每格都很短、且**多数格子是数字**。
    这样双栏/三栏正文（每格都是整句话）不会被误判成表格。
    """
    found = []
    rows = _group_rows(lines)
    start = 0
    while start < len(rows):
        end = start + 1
        while end < len(rows) and rows[end]["y"] - rows[end - 1]["bottom"] <= ROW_GAP_PT:
            end += 1
        window = _merge_wrapped(rows[start:end])
        data_rows = [row for row in window if len(row["cells"]) >= MIN_CELLS_IN_ROW]
        if len(data_rows) >= GEOMETRY_MIN_ROWS and _short_cells(data_rows) \
                and _looks_like_table_cells(data_rows) and _regular_pitch(data_rows) \
                and _numeric_ratio(data_rows) >= GEOMETRY_MIN_NUMERIC_RATIO:
            columns = _columns(data_rows)
            if len(columns) >= MIN_COLUMNS:
                top = min(row["y"] for row in data_rows)
                bottom = max(row["bottom"] for row in data_rows)
                inside = any(top < taken_bottom and bottom > taken_top
                             for taken_top, taken_bottom in taken)
                if not inside and page_height \
                        and (bottom - top) <= page_height * MAX_REGION_HEIGHT_RATIO:
                    cells = [cell for row in data_rows for cell in row["cells"]]
                    rect = _region_rect({"x0": min(c["x0"] for c in cells),
                                         "x1": max(c["x1"] for c in cells),
                                         "y0": top, "y1": top}, data_rows)
                    atoms = sorted({owner for owner in (_line_owner(line, index) for line in cells)
                                    if owner is not None})
                    found.append(TableRegion(
                        region_id=next_id + len(found), page=page_no, rect=rect,
                        caption="（没有题注，按对齐网格判定）", columns=len(columns),
                        rows=len(data_rows), line_count=len(cells), kind="geometry",
                        atom_indices=atoms,
                        text=" ".join(line["text"].strip() for line in cells),
                    ))
        start = end if end > start else start + 1
    return found
