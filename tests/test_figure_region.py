r"""阶段 4.6 验收：矢量图页的「图区域」检测与截图（utils/figure_finder.py）

这一套**只用合成数据**（纯函数层），不依赖本机样本，任何机器上取得仓库都能跑；
末尾另有一段「真样本核对」，样本不在本机时自动跳过。

守住四件事：
  ① **页级闸门**：矢量路径项 ≥ 20 且 页面板级位图 < 8000pt² —— 两个条件缺一不可，
     阈值是模块顶部常量（实测旧 NHB 第 19/20 页正好卡在 8000 两侧，这一条要钉住）；
  ② **题注口径**：`Fig. 1 | …` 这种竖线分隔的题注要认出来，
     而 `Fig. 2 suggests that…` / `Figure 1 illustrates…` 这种**引用句不能**被当题注；
  ③ **区域定位**：题注锚点定下界 + 图内小字间隙聚类，且
     **成员必须落在区域内**（成员会被移出文字流、只由截图承载，落在区域外就是内容丢失）；
  ④ **扣除与放弃**：已确认为表格/公式区域的块必须扣除；定位不出可信区域时
     放弃截图并给出中文原因，**绝不退化成截整页**。
"""

import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

from utils import figure_finder as ff          # noqa: E402
from utils.pdf_parser import Block             # noqa: E402

passed, failed, skipped = 0, 0, 0
PAGE = (595.0, 842.0)                          # 一页的尺寸（A4，与 NHB 实测一致）


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK] {label}")
    else:
        failed += 1
        print(f"  [!!] {label}  {detail}")


def skip(label):
    global skipped
    skipped += 1
    print(f"  [跳过] {label}（样本不在本机）")


def make_block(index, page, x0, y0, x1, y1, text, size=6.5, role="body",
               kind="body", column=0):
    """造一个原子块（用真的 Block，顺便钉住字段名）"""
    block = Block(page=page, x0=x0, y0=y0, x1=x1, y1=y1, text=text, max_size=size,
                  bold_ratio=0.0, line_height=size * 1.6, role=role, kind=kind,
                  column=column)
    block.order = index + 1
    return block


# ------------------------------------------------------------
print("=" * 78)
print("① 页级闸门：矢量 ≥ 20 且 面板级位图 < 8000pt²")
print("-" * 78)
check("矢量 20 + 无大位图 → 过闸门（矢量阈值是「≥」，取等号也算过）",
      ff.should_capture_page({"vector_items": 20, "bitmap_max_area": 0.0}) is True)
check("矢量 19 → 不过闸门（差 1 项也不行）",
      ff.should_capture_page({"vector_items": 19, "bitmap_max_area": 0.0}) is False)
check("位图面积 7999.9 → 过闸门",
      ff.should_capture_page({"vector_items": 100, "bitmap_max_area": 7999.9}) is True)
check("位图面积正好 8000.0 → 不过闸门（阈值是严格小于）",
      ff.should_capture_page({"vector_items": 100, "bitmap_max_area": 8000.0}) is False)
check("旧 NHB 第 19 页的实测值（矢量 338 / 位图 9380.7）→ 不过闸门",
      ff.should_capture_page({"vector_items": 338, "bitmap_max_area": 9380.7}) is False)
check("旧 NHB 第 20 页的实测值（矢量 253 / 位图 7240.4）→ 过闸门（阈值脆，两页跨线）",
      ff.should_capture_page({"vector_items": 253, "bitmap_max_area": 7240.4}) is True)
check("旧 NHB 第 9 页（位图图页：矢量 5477 / 位图 17308.9）→ 不过闸门",
      ff.should_capture_page({"vector_items": 5477, "bitmap_max_area": 17308.9}) is False)
check("旧 NHB 第 4 页（1352 项矢量 / 无位图）→ 过闸门",
      ff.should_capture_page({"vector_items": 1352, "bitmap_max_area": 0.0}) is True)
check("空探测 / None → 一律不过闸门（解析失败也不能崩）",
      ff.should_capture_page(None) is False and ff.should_capture_page({}) is False)
check("阈值都以模块常量给出（调阈值只改一处）",
      (ff.FIG_VECTOR_MIN, ff.FIG_MAX_BITMAP_AREA) == (20, 8000.0),
      repr((ff.FIG_VECTOR_MIN, ff.FIG_MAX_BITMAP_AREA)))

# ------------------------------------------------------------
print()
print("② 图题注口径：认竖线分隔的题注，不认正文里的引用句")
print("-" * 78)
check("NHB 体例 'Fig. 1 | A resource-rational mechanism…' → 是图题注",
      ff.is_figure_caption("Fig. 1 | A resource-rational mechanism for reading") is True)
check("'Figure 3. Results of the simulation' → 是图题注",
      ff.is_figure_caption("Figure 3. Results of the simulation") is True)
check("'Extended Data Fig. 2 | Illustration of sentence-level…' → 是图题注",
      ff.is_figure_caption("Extended Data Fig. 2 | Illustration of dynamics") is True)
check("'Supplementary Fig. 1: Stimulus presentation' → 是图题注",
      ff.is_figure_caption("Supplementary Fig. 1: Stimulus presentation") is True)
check("全角竖线 'Fig. 2｜模型示意' → 是图题注（与表格那一支的 [.:|｜] 对齐）",
      ff.is_figure_caption("Fig. 2｜模型示意") is True)
check("正文引用句 'Fig. 2 suggests that the model…' → **不是**题注（编号后是空格）",
      ff.is_figure_caption("Fig. 2 suggests that the model learns") is False)
check("正文引用句 'Figure 1 illustrates the process' → **不是**题注",
      ff.is_figure_caption("Figure 1 illustrates the process") is False)
check("没有分隔符的 'Fig. 1' → 不是题注",
      ff.is_figure_caption("Fig. 1") is False)
check("表题注 'Table 1 | Summary of inferential statistics' → 不是**图**题注（表由 table_finder 管）",
      ff.is_figure_caption("Table 1 | Summary of inferential statistics") is False)

# ------------------------------------------------------------
print()
print("③ 图内小字 / 正文块的判据")
print("-" * 78)
BODY = 8.3                                   # 旧 NHB 的实测正文字号
label = make_block(0, 1, 100, 60, 160, 70, "A1", size=6.5)
check("6.5pt 的角标 → 图内小字（字号 < 0.98×正文）",
      ff.is_figureish(label, BODY) is True and ff.is_prose(label, BODY) is False)
panel = make_block(1, 1, 46, 50, 92, 64, "a    Word-level", size=9.3)
check("9.3pt 但只有 2 个词的图内面板标签 → 仍是图内小字（词数 ≤ 6 也算）",
      ff.is_figureish(panel, BODY) is True)
prose = make_block(2, 1, 40, 400, 300, 420,
                   "Eye-movement control is organized into three nested levels of control", size=8.3)
check("8.3pt、11 词的正文块 → 不是图内小字，是正文",
      ff.is_figureish(prose, BODY) is False and ff.is_prose(prose, BODY) is True)
ticks = make_block(3, 4, 269, 145, 348, 161,
                   "−2.4 −1.8 −1.2 −0.6 0 0.6 Logit predictability", size=6.0)
check("6.0pt 的坐标轴刻度组 → 算图内小字（该移出正文），但**不是正文块**",
      ff.is_figureish(ticks, BODY) is True and ff.is_prose(ticks, BODY) is False,
      f"{ff.is_figureish(ticks, BODY)} / {ff.is_prose(ticks, BODY)}")
long_label = make_block(4, 1, 40, 500, 300, 520,
                        "the resource-rational POMDP model reads the sentence word by word "
                        "and decides where to move next in the text",
                        size=7.0)
check("7.0pt、19 词的长块 → 超出成员词数上限，不是图内小字（挡住题注下半截）",
      ff.is_figureish(long_label, BODY) is False,
      f"{ff.count_words(long_label.text)} 词")

# ------------------------------------------------------------
print()
print("④ 间隙聚类：近邻直接并；稍远但空隙里有图形也并；空白就断开")
print("-" * 78)
blocks = [
    make_block(0, 1, 100, 100, 160, 110, "L1"),      # 相邻（间隙 2pt）
    make_block(1, 1, 100, 112, 160, 122, "L2"),
    make_block(2, 1, 100, 152, 160, 162, "L3"),      # 与 L2 间隙 30pt
    make_block(3, 1, 100, 240, 160, 250, "L4"),      # 与 L3 间隙 78pt（> 上限）
]
bridged = [(100, 122, 160, 152)]   # 落在 (122, 152) 这段空隙里的一个矢量框 ← 桥
check("间隙 2pt → 并进同一簇",
      len(ff.cluster_by_gap([0, 1], blocks, [])) == 1)
check("间隙 30pt、空隙里**有**矢量图形 → 仍并进同一簇",
      len(ff.cluster_by_gap([1, 2], blocks, bridged)) == 1)
check("同样的 30pt 间隙、空隙里**没有**图形 → 断开（版面留白不是图）",
      len(ff.cluster_by_gap([1, 2], blocks, [])) == 2)
check("间隙 78pt 超过上限 → 即便有空隙图形也断开",
      len(ff.cluster_by_gap([2, 3], blocks, [(162, 240)])) == 2)
check("乱序输入也能按 y 排出正确的簇",
      ff.cluster_by_gap([3, 0, 2, 1], blocks, bridged) == [[0, 1, 2], [3]],
      repr(ff.cluster_by_gap([3, 0, 2, 1], blocks, bridged)))
check("空输入 → 空结果（不抛异常）", ff.cluster_by_gap([], blocks, []) == [])
check("上限常量可调：把 gap_max 调到 10 之后那段 30pt 间隙就断开了",
      len(ff.cluster_by_gap([1, 2], blocks, bridged, gap_max=10.0)) == 2)
check("簇的间隙上限确实是模块常量 FIG_GAP_MAX_PT = 45",
      ff.FIG_GAP_MAX_PT == 45.0 and ff.FIG_GAP_MIN_PT == 8.0)

# ------------------------------------------------------------
print()
print("⑤ 区域定位：题注锚点定下界 + 图内小字聚类 + 矢量并集撑矩形")
print("-" * 78)


def synthetic_page(extra_blocks=(), anchor_y0=400.0, vector_boxes=((90, 50, 500, 390),),
                   caption="Fig. 1 | A synthetic figure caption with bars"):
    page_blocks = [
        make_block(0, 1, 100, 120, 160, 130, "L1"),
        make_block(1, 1, 100, 328, 160, 338, "L2"),
        make_block(2, 1, 100, 340, 160, 350, "L3"),
        make_block(3, 1, 40, anchor_y0, 300, anchor_y0 + 8, caption, size=7.0, role="caption"),
    ]
    page_blocks.extend(extra_blocks)
    return page_blocks, {1: {"vector_items": 40, "bitmap_max_area": 0.0,
                             "vector_boxes": list(vector_boxes)}}


page_blocks, probes = synthetic_page()
regions, failures = ff.find_figure_regions(page_blocks, probes, {1: PAGE}, BODY)
check("合成图页定位出 1 个图区域", len(regions) == 1 and not failures,
      f"{len(regions)} 区域 / {failures}")
check("区域矩形不越过题注（下界夹在题注上方）",
      regions and regions[0].rect[3] <= page_blocks[3].y0, repr(regions[0].rect if regions else None))
check("区域矩形被矢量并集撑开（不是只框住那几个小字块）",
      regions and regions[0].rect[0] <= 90 and regions[0].rect[2] >= 500,
      repr(regions[0].rect if regions else None))
check("三个图内小字块都被收成成员",
      regions and sorted(regions[0].atom_indices) == [0, 1, 2],
      repr(regions[0].atom_indices if regions else None))
check("**题注块本身不在成员里**（图注要留在卡片流，项目规则：图注视作正文）",
      regions and 3 not in regions[0].atom_indices)
check("成员全部落在矩形里（成员会被移出文字流，落在区域外就是内容丢失）",
      regions and all(ff._inside_ratio(page_blocks[i], regions[0].rect) >= 0.5
                      for i in regions[0].atom_indices))
check("定位依据记为 caption+cluster（便于人工核对）",
      regions and regions[0].source == "caption+cluster")

# 表格 / 公式区域的块必须扣除
page_blocks2, probes2 = synthetic_page()
regions2, _ = ff.find_figure_regions(page_blocks2, probes2, {1: PAGE}, BODY, excluded={1})
check("被 excluded（表格/公式区域）的块不再成为区域成员",
      regions2 and sorted(regions2[0].atom_indices) == [0, 2],
      repr(regions2[0].atom_indices if regions2 else None))
regions3, failures3 = ff.find_figure_regions(page_blocks2, probes2, {1: PAGE}, BODY,
                                             excluded={0, 1, 2})
check("候选池被表格/公式区域占满时 → 退成 vector-only：只出图、不动文字",
      len(regions3) == 1 and regions3[0].atom_indices == []
      and regions3[0].source == "vector-only" and not failures3,
      f"{len(regions3)} 区域 / {[r.source for r in regions3]} / {failures3}")

# 定位失败的各种情形：一律放弃截图 + 中文原因
_thin_blocks, _thin_probes = synthetic_page(vector_boxes=((90, 380, 500, 390),))
_regions, _fails = ff.find_figure_regions(_thin_blocks, _thin_probes, {1: PAGE}, BODY,
                                          excluded={0, 1, 2})
check("矢量并集太扁（高 16pt < 50pt）→ 放弃截图并说明原因",
      not _regions and len(_fails) == 1 and "太扁" in _fails[0]["reason"],
      repr(_fails))

_no_vec_blocks, _no_vec_probes = synthetic_page(vector_boxes=())
_regions, _fails = ff.find_figure_regions(_no_vec_blocks, _no_vec_probes, {1: PAGE}, BODY)
check("题注上方没有矢量路径 → 放弃截图（这不是矢量图页）",
      not _regions and "没有矢量路径" in _fails[0]["reason"], repr(_fails))

_far_blocks, _far_probes = synthetic_page(anchor_y0=800.0)
_regions, _fails = ff.find_figure_regions(_far_blocks, _far_probes, {1: PAGE}, BODY)
check("图内小字离题注太远（>60pt）且矢量并集也够不着 → 放弃截图",
      not _regions and "太远" in _fails[0]["reason"], repr(_fails))

_prose_blocks, _prose_probes = synthetic_page(extra_blocks=[
    make_block(9, 1, 60, 200, 300, 216,
               "the model reads sentence by sentence and decides where to move next", size=8.3)])
_regions, _fails = ff.find_figure_regions(_prose_blocks, _prose_probes, {1: PAGE}, BODY)
check("区域里框进了正文块（≥8 词且字号没缩水）→ 放弃截图，**不退化截整页**",
      not _regions and "正文块" in _fails[0]["reason"], repr(_fails))

# 面积上限：用一页 595×500pt 的小页面来演示（同样一块区域在那页上就超过 60% 了）
_big_blocks, _big_probes = synthetic_page(vector_boxes=((5, 5, 590, 390),))
_regions, _fails = ff.find_figure_regions(_big_blocks, _big_probes, {1: (595.0, 500.0)}, BODY)
check("区域面积超过页面 60% → 放弃截图（挡住「截整页」）",
      not _regions and "面积超限" in _fails[0]["reason"], repr(_fails))

_two_blocks, _two_probes = synthetic_page(extra_blocks=[
    make_block(9, 1, 40, 410, 300, 418, "Fig. 2 | Another caption right below", size=7.0,
               role="caption")])
_regions, _fails = ff.find_figure_regions(_two_blocks, _two_probes, {1: PAGE}, BODY)
check("同一张图被两条题注各判一次 → 后一条按「重叠」丢弃（同一区域只出一张图）",
      len(_regions) == 1 and len(_fails) == 1 and "重叠" in _fails[0]["reason"],
      f"{len(_regions)} 区域 / {_fails}")
check("第二条题注（字号 7.0pt、形状上像图内小字）**没有**被当成成员移出文字流",
      all(9 not in r.atom_indices for r in _regions))

_off_blocks, _off_probes = synthetic_page()
_off_probes[1]["vector_items"] = 3            # 矢量太少 → 闸门不过
_regions, _fails = ff.find_figure_regions(_off_blocks, _off_probes, {1: PAGE}, BODY)
check("闸门不过的页完全不处理（既无区域也无失败记录）", not _regions and not _fails)

_no_anchor_blocks = [b for b in _off_blocks if b.role != "caption"]
_regions, _fails = ff.find_figure_regions(_no_anchor_blocks, probes, {1: PAGE}, BODY)
check("过闸门但没有图题注的页 → 什么都不做（不算定位失败）",
      not _regions and not _fails)

# ------------------------------------------------------------
print()
print("⑥ 段落分组：图内小字必须是一堵「硬墙」（粘进正文就是内容丢失）")
print("-" * 78)
from utils import pdf_parser as parser_mod                     # noqa: E402

_wall_blocks = [
    make_block(0, 1, 40, 100, 300, 112, "the model reads sentence by sentence and moves on", size=8.3),
    make_block(1, 1, 100, 114, 160, 124, "A1", size=6.5),        # 图内小字
    make_block(2, 1, 40, 126, 300, 138, "next words of the same paragraph continue here", size=8.3),
]
parser_mod.apply_figure_regions(_wall_blocks, [type("R", (), {
    "region_id": 0, "page": 1, "rect": (90, 95, 200, 125), "caption": "Fig. 1 | x",
    "anchor_index": -1, "atom_indices": [1], "text": "A1", "source": "caption+cluster",
    "cluster_blocks": 1})()])
_wall_groups = parser_mod.build_paragraph_groups(_wall_blocks)
check("图内小字块后面的正文**不会**被粘进同一段（否则整段文字会跟着消失）",
      _wall_groups == [[0], [1], [2]], repr(_wall_groups))
_wall_groups2 = parser_mod.build_paragraph_groups([
    make_block(0, 1, 40, 100, 300, 112, "the model reads sentence by sentence and moves on", size=8.3),
    make_block(1, 1, 100, 114, 160, 124, "A1", size=6.5),
])
parser_mod.apply_figure_regions(_wall_blocks, [])              # 清掉上一轮的标记
check("段落分组不会把图内小字并进前一段（第二条硬墙）",
      len(_wall_groups2) == 2 or _wall_groups2 == [[0], [1]], repr(_wall_groups2))

# ------------------------------------------------------------
print()
print("⑦ 诊断数字：题注页 / 无面板级位图页 / 过闸门页")
print("-" * 78)
_report_blocks = [
    make_block(0, 3, 40, 392, 294, 401, "Fig. 2 | Hierarchical control", size=7.0, role="caption"),
    make_block(1, 4, 40, 488, 265, 497, "Fig. 3 | Model reproduces effects", size=7.0, role="caption"),
    make_block(2, 9, 40, 300, 265, 309, "Fig. 4 | Reading behaviour", size=7.0, role="caption"),
    make_block(3, 9, 40, 20, 133, 34, "Nature Human Behaviour", size=8.0, role="header_footer"),
]
_report_probes = {
    3: {"vector_items": 208, "bitmap_max_area": 0.0},
    4: {"vector_items": 1352, "bitmap_max_area": 0.0},
    9: {"vector_items": 5477, "bitmap_max_area": 17308.9},
}
_report = ff.figure_report(_report_blocks, _report_probes)
check("题注页 = 有图题注的页（页眉页脚里的 'Fig.' 不算）",
      _report["caption_pages"] == [3, 4, 9], repr(_report["caption_pages"]))
check("「无面板级位图」的图注页 = 第 3、4 页（第 9 页有位图 17308.9pt²）",
      _report["caption_pages_no_bitmap"] == [3, 4], repr(_report["caption_pages_no_bitmap"]))
check("过闸门页只算第 3、4 页（第 9 页位图超限）",
      _report["gate_pages"] == [3, 4], repr(_report["gate_pages"]))
check("诊断里同时给出计数（前端直接显示「本篇 N 个图注页无面板级位图」）",
      _report["caption_pages_no_bitmap_count"] == 2 and _report["caption_page_count"] == 3
      and _report["gate_page_count"] == 2)

# ------------------------------------------------------------
print()
print("⑧ 真样本核对（两篇 NHB，样本不在本机就跳过）")
print("-" * 78)
SAMPLE_OLD = r"E:\segemented pdf reader\测试pdf\补例\s41562-026-02534-0.pdf"
SAMPLE_NEW = r"E:\segemented pdf reader\测试pdf\补例\s41562-025-02379-z.pdf"
if not os.path.exists(SAMPLE_OLD):
    skip("旧 NHB 图区域（第 3/4/10/23 页）")
else:
    import pymupdf                                             # noqa: E402
    from utils import pdf_parser                               # noqa: E402

    pdf_bytes = open(SAMPLE_OLD, "rb").read()
    plain = pdf_parser.parse_pdf(pdf_bytes, table_mode="image")
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page_probes, page_sizes = {}, {}
    for i, page in enumerate(doc):
        page_probes[i + 1] = ff.probe_page(page)
        page_sizes[i + 1] = (page.rect.width, page.rect.height)
    doc.close()
    excluded = set()
    for region in plain["table_regions"]:
        excluded.update(region.atom_indices)
    for cluster in plain["formula_clusters"]:
        excluded.update(cluster.atom_indices)
    old_regions, old_fails = ff.find_figure_regions(plain["blocks"], page_probes, page_sizes,
                                                   plain["body_size"], excluded=excluded)
    check("旧 NHB：定位出 4 个图区域，页号 3 / 4 / 10 / 23",
          [r.page for r in old_regions] == [3, 4, 10, 23], repr([r.page for r in old_regions]))
    check("旧 NHB：第 23 页是 vector-only（图内文字是矢量轮廓，没有文字块可移）",
          old_regions[3].source == "vector-only" and old_regions[3].atom_indices == [])
    check("旧 NHB：第 3 页 40 块、第 4 页 134 块图内小字被收进区域",
          len(old_regions[0].atom_indices) == 40 and len(old_regions[1].atom_indices) == 134,
          f"{len(old_regions[0].atom_indices)} / {len(old_regions[1].atom_indices)}")
    check("旧 NHB：第 20/21 页如实记为定位失败（那两页的插图本身就是「文本示意」）",
          [(f["page"], f["reason"].startswith("区域里框进了正文块")) for f in old_fails]
          == [(20, True), (21, True)], repr(old_fails))

    with_figure = pdf_parser.parse_pdf(pdf_bytes, table_mode="image", figure_region=True)
    check("旧 NHB：打开图区域后公式区域数仍是 3、表格区域数仍是 1（互不干扰）",
          len(with_figure["formula_clusters"]) == 3 and len(with_figure["table_regions"]) == 1,
          repr((len(with_figure["formula_clusters"]), len(with_figure["table_regions"]))))
    check("旧 NHB：打开后卡片数 40 → 38、总词数 18538 → 18146（只少图内小字那 392 词）",
          (len(plain["cards"]), sum(pdf_parser.count_words(c.text) for c in plain["cards"]))
          == (40, 18538)
          and (len(with_figure["cards"]),
               sum(pdf_parser.count_words(c.text) for c in with_figure["cards"])) == (38, 18146),
          repr((len(with_figure["cards"]),
                sum(pdf_parser.count_words(c.text) for c in with_figure["cards"]))))
    check("旧 NHB：移出正文的词数 = 图区域成员的词数（一个字都不多不少）",
          sum(pdf_parser.count_words(p.text) for p in with_figure["paragraphs"]
              if p.is_figure)
          == sum(pdf_parser.count_words(with_figure["blocks"][i].text)
                 for r in with_figure["figure_regions"] for i in r.atom_indices)
          == 392,
          repr((sum(pdf_parser.count_words(p.text) for p in with_figure["paragraphs"]
                    if p.is_figure),
                sum(pdf_parser.count_words(with_figure["blocks"][i].text)
                    for r in with_figure["figure_regions"] for i in r.atom_indices))))
    check("旧 NHB：卡片词数差 = 392（18538 → 18146）—— 没有正文被连带删掉",
          sum(pdf_parser.count_words(c.text) for c in with_figure["cards"]) == 18146
          and sum(pdf_parser.count_words(c.text) for c in plain["cards"]) == 18538,
          repr(sum(pdf_parser.count_words(c.text) for c in with_figure["cards"])))
    check("旧 NHB：每个区域在卡片里只出一次截图（不会题注出一次、成员头又出一次）",
          sum(1 for card in with_figure["cards"] for kind, _ in card.segments
              if kind == "figure_region") == 4,
          repr(sum(1 for card in with_figure["cards"] for kind, _ in card.segments
                   if kind == "figure_region")))

if not os.path.exists(SAMPLE_NEW):
    skip("新 NHB 图区域（第 8 页）")
else:
    from utils import pdf_parser                               # noqa: E402
    pdf_bytes = open(SAMPLE_NEW, "rb").read()
    result = pdf_parser.parse_pdf(pdf_bytes, table_mode="image", figure_region=True)
    check("新 NHB：定位出 1 个图区域（第 8 页），且没有定位失败的图题注",
          [r.page for r in result["figure_regions"]] == [8] and not result["figure_failures"],
          repr([(r.page, f) for r in result["figure_regions"] for f in result["figure_failures"]]))
    check("新 NHB：图区域带 14 块图内小字成员",
          len(result["figure_regions"][0].atom_indices) == 14,
          repr(len(result["figure_regions"][0].atom_indices)))
    check("新 NHB：开关关闭时 figure_regions 恒为空、也不带 figure_stats",
          pdf_parser.parse_pdf(pdf_bytes, table_mode="image")["figure_regions"] == []
          and pdf_parser.parse_pdf(pdf_bytes, table_mode="image")["figure_stats"] == {})

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed} · 跳过 {skipped}")
sys.exit(1 if failed else 0)
