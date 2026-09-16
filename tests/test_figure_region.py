r"""阶段 4.6 / 4.7 验收：矢量图页的「图区域」检测与截图（utils/figure_finder.py）

这一套**只用合成数据**（纯函数层），不依赖本机样本，任何机器上取得仓库都能跑；
末尾另有一段「真样本核对」，样本不在本机时自动跳过。

守住五件事：
  ① **页级预筛 + 区域级闸门**（4.7 起分两级）：
     · 页级预筛：矢量路径项 ≥ 20（只决定"去不去定位"，不再看位图大小）；
     · 区域级：区域内位图显示面积的**并集**占比 ≥ 60% → 交给位图提取路径，不截图；
       与同页已确认的表格区域重叠 → 也不截图（避免把表格当图出两遍）。
     旧口径 should_capture_page（矢量 ≥ 20 且无面板级位图）行为逐条不变，留给诊断与单测；
  ② **题注口径**：`Fig. 1 | …` 这种竖线分隔的题注要认出来，
     而 `Fig. 2 suggests that…` / `Figure 1 illustrates…` 这种**引用句不能**被当题注；
  ③ **区域定位**：题注锚点定下界 + 图内小字间隙聚类，且
     **成员必须落在区域内**（成员会被移出文字流、只由截图承载，落在区域外就是内容丢失）；
  ④ **扣除与放弃**：已确认为表格/公式区域的块必须扣除；定位不出可信区域时
     放弃截图并给出中文原因，**绝不退化成截整页**；
  ⑤ **去重**：已被图区域截图吸收的位图（落在区域内 ≥50%）不再单独呈现。
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
print("① 页级预筛（矢量 ≥ 20）+ 旧口径闸门（矢量 ≥ 20 且 面板级位图 < 8000pt²）")
print("-" * 78)
check("矢量 20 + 无大位图 → 过闸门（矢量阈值是「≥」，取等号也算过）",
      ff.should_capture_page({"vector_items": 20, "bitmap_max_area": 0.0}) is True)
check("矢量 19 → 不过闸门（差 1 项也不行）",
      ff.should_capture_page({"vector_items": 19, "bitmap_max_area": 0.0}) is False)
check("位图面积 7999.9 → 过闸门",
      ff.should_capture_page({"vector_items": 100, "bitmap_max_area": 7999.9}) is True)
check("位图面积正好 8000.0 → 不过闸门（阈值是严格小于）",
      ff.should_capture_page({"vector_items": 100, "bitmap_max_area": 8000.0}) is False)
check("旧 NHB 第 19 页的实测值（矢量 338 / 位图 9380.7）→ 旧口径不过闸门（4.7 起另有区域级判定）",
      ff.should_capture_page({"vector_items": 338, "bitmap_max_area": 9380.7}) is False)
check("旧 NHB 第 20 页的实测值（矢量 253 / 位图 7240.4）→ 过闸门（阈值脆，两页跨线）",
      ff.should_capture_page({"vector_items": 253, "bitmap_max_area": 7240.4}) is True)
check("旧 NHB 第 9 页（位图图页：矢量 5477 / 位图 17308.9）→ 旧口径不过闸门",
      ff.should_capture_page({"vector_items": 5477, "bitmap_max_area": 17308.9}) is False)
check("旧 NHB 第 4 页（1352 项矢量 / 无位图）→ 过闸门",
      ff.should_capture_page({"vector_items": 1352, "bitmap_max_area": 0.0}) is True)
check("空探测 / None → 一律不过闸门（解析失败也不能崩）",
      ff.should_capture_page(None) is False and ff.should_capture_page({}) is False)
check("阈值都以模块常量给出（调阈值只改一处）",
      (ff.FIG_VECTOR_MIN, ff.FIG_MAX_BITMAP_AREA) == (20, 8000.0),
      repr((ff.FIG_VECTOR_MIN, ff.FIG_MAX_BITMAP_AREA)))

# ---- 4.7：页级预筛只看矢量（位图大小交给区域级判据）----
check("页级预筛**不看**位图大小：旧 NHB 第 9 页（矢量 5477 / 位图 17309）也要进去定位",
      ff.should_probe_page({"vector_items": 5477, "bitmap_max_area": 17308.9}) is True)
check("页级预筛仍然挡矢量不足的页（正文页的分隔线不算）",
      ff.should_probe_page({"vector_items": 19, "bitmap_max_area": 0.0}) is False
      and ff.should_probe_page(None) is False)
check("「纯矢量图页」判据与旧口径一致（只差页级预筛那一条）",
      ff.is_pure_vector_page({"bitmap_max_area": 7999.9}) is True
      and ff.is_pure_vector_page({"bitmap_max_area": 8000.0}) is False
      and ff.should_capture_page({"vector_items": 100, "bitmap_max_area": 0.0})
      is (ff.should_probe_page({"vector_items": 100, "bitmap_max_area": 0.0})
          and ff.is_pure_vector_page({"bitmap_max_area": 0.0})))

# ------------------------------------------------------------
print()
print("①-B 区域级闸门：区域内位图覆盖率的算法（并集，不是求和）")
print("-" * 78)
check("并集面积：两个相同的矩形只算一次（同一张图重复摆放不能翻倍）",
      ff.union_area([(0, 0, 10, 10), (0, 0, 10, 10)]) == 100.0,
      repr(ff.union_area([(0, 0, 10, 10), (0, 0, 10, 10)])))
check("并集面积：两块并排 → 相加；部分重叠 → 只算一次重叠部分",
      ff.union_area([(0, 0, 10, 10), (10, 0, 20, 10)]) == 200.0
      and ff.union_area([(0, 0, 10, 10), (5, 0, 15, 10)]) == 150.0,
      repr((ff.union_area([(0, 0, 10, 10), (10, 0, 20, 10)]),
            ff.union_area([(0, 0, 10, 10), (5, 0, 15, 10)]))))
check("并集面积：空输入 / 退化矩形 → 0（不抛异常）",
      ff.union_area([]) == 0.0 and ff.union_area([(0, 0, 0, 0)]) == 0.0)
check("覆盖率：位图被裁到区域里（区域外的部分不算数）",
      ff.bitmap_cover_ratio([(0, 0, 20, 10)], (0, 0, 10, 10)) == 1.0
      and abs(ff.bitmap_cover_ratio([(0, 0, 20, 10)], (0, 0, 20, 10)) - 1.0) < 1e-9
      and abs(ff.bitmap_cover_ratio([(0, 0, 5, 10)], (0, 0, 10, 10)) - 0.5) < 1e-9,
      repr(ff.bitmap_cover_ratio([(0, 0, 5, 10)], (0, 0, 10, 10))))
check("覆盖率：重复摆放只算一次（旧 NHB 第 19 页 22 个矩形 / 8 张图，求和会得到 125% 的假象）",
      ff.bitmap_cover_ratio([(0, 0, 10, 10), (0, 0, 10, 10)], (0, 0, 10, 10)) == 1.0)
check("覆盖率：没有位图 → 0；区域退化 → 0",
      ff.bitmap_cover_ratio([], (0, 0, 10, 10)) == 0.0
      and ff.bitmap_cover_ratio([(0, 0, 10, 10)], (5, 5, 5, 5)) == 0.0)
check("区域级阈值是模块常量 0.60",
      ff.FIG_REGION_MAX_BITMAP_COVER == 0.60 and ff.FIG_IMAGE_IN_REGION_RATIO == 0.5
      and ff.FIG_TABLE_OVERLAP_MAX == 0.5,
      repr((ff.FIG_REGION_MAX_BITMAP_COVER, ff.FIG_IMAGE_IN_REGION_RATIO,
            ff.FIG_TABLE_OVERLAP_MAX)))

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
_off_probes[1]["vector_items"] = 3            # 矢量太少 → 页级预筛不过
_regions, _fails = ff.find_figure_regions(_off_blocks, _off_probes, {1: PAGE}, BODY)
check("页级预筛不过的页完全不处理（既无区域也无失败记录）", not _regions and not _fails)

# ---- 4.7 区域级闸门：位图覆盖率 ----
# ⚠️ 合成探测里必须把 bitmap_max_area 抬到 8000 以上：页级预筛之外，
#    「纯矢量图页」（旧口径能过的页）会被无条件放行、不做区域级覆盖判据——
#    这一组要验的是**混合图页**（有大位图 + 大矢量），所以模拟成 12000pt²。
_bit_blocks, _bit_probes = synthetic_page()
_bit_probes[1]["bitmap_max_area"] = 12000.0
_bit_probes[1]["bitmap_rects"] = [(80, 40, 520, 400)]        # 盖住 440×360 ≈ 区域的一大半
_cover = ff.bitmap_cover_ratio(_bit_probes[1]["bitmap_rects"],
                               (80.0, 40.0, 520.0, 400.0))
_regions, _fails = ff.find_figure_regions(_bit_blocks, _bit_probes, {1: PAGE}, BODY)
check("区域内位图覆盖 ≥ 60% → 不截图，如实记为「由位图提取路径呈现」",
      not _regions and len(_fails) == 1 and "位图覆盖" in _fails[0]["reason"],
      f"{len(_regions)} 区域 / {_fails}（合成覆盖率 {_cover:.0%}）")
_mix_blocks, _mix_probes = synthetic_page()
_mix_probes[1]["bitmap_max_area"] = 12000.0
_mix_probes[1]["bitmap_rects"] = [(80, 40, 200, 130)]        # 只盖住一小块 → 混合图页
_regions, _fails = ff.find_figure_regions(_mix_blocks, _mix_probes, {1: PAGE}, BODY)
check("区域内位图覆盖 < 60%（混合图页）→ 照样截图（这是用户报障要的那一类）",
      len(_regions) == 1 and not _fails,
      f"{len(_regions)} 区域 / {_fails}")
_pure_blocks, _pure_probes = synthetic_page()
_pure_probes[1]["bitmap_rects"] = [(80, 40, 520, 400)]       # 位图铺满，但页面本身没有面板级位图
_regions, _fails = ff.find_figure_regions(_pure_blocks, _pure_probes, {1: PAGE}, BODY)
check("「纯矢量图页」（旧口径能过）不被区域级判据吞掉已有的出图行为",
      len(_regions) == 1 and not _fails, f"{len(_regions)} 区域 / {_fails}")

# ---- 4.7 区域级闸门：与已确认的表格区域重叠 ----
_tbl_blocks, _tbl_probes = synthetic_page(extra_blocks=[
    make_block(9, 1, 40, 200, 300, 260, "Table 1 | Summary of the parameters", size=8.3)])
_regions, _fails = ff.find_figure_regions(_tbl_blocks, _tbl_probes, {1: PAGE}, BODY,
                                          avoid_rects=[(1, (40, 195, 300, 265))])
check("与**同页**已确认的表格区域重叠 → 不截图（否则同一张表出两遍）",
      not _regions and len(_fails) == 1 and "表格区域" in _fails[0]["reason"],
      f"{len(_regions)} 区域 / {_fails}")
_regions, _fails = ff.find_figure_regions(_tbl_blocks, _tbl_probes, {1: PAGE}, BODY,
                                          avoid_rects=[(2, (40, 195, 300, 265))])
check("**别页**的表格区域即使坐标相近也不能误伤（avoid_rects 必须带页码）",
      len(_regions) == 1 and not _fails,
      f"{len(_regions)} 区域 / {_fails}")

# ---- 4.7 去重：被区域截图吸收的位图不再单独呈现 ----
class _Img:
    def __init__(self, page, bbox):
        self.page, self.bbox = page, bbox


_regions_for_dedup = [type("R", (), {"page": 1, "rect": (80.0, 40.0, 520.0, 400.0)})()]
_images = [_Img(1, (100, 60, 200, 160)),      # 完全落在区域里 → 吸收
           _Img(1, (500, 380, 560, 440)),     # 只沾一点边（占比 < 50%）→ 保留
           _Img(2, (100, 60, 200, 160))]      # 别的页 → 保留
check("落在图区域里的位图被判为「已被吸收」（下标 [0]）",
      ff.covered_image_indices(_images, _regions_for_dedup) == [0],
      repr(ff.covered_image_indices(_images, _regions_for_dedup)))
check("开关关掉时 figure_regions 为空 → 一张都不吸收（与旧行为逐字段一致）",
      ff.covered_image_indices(_images, []) == [])

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
check("过**旧口径**闸门页只算第 3、4 页（第 9 页位图超限）",
      _report["gate_pages"] == [3, 4], repr(_report["gate_pages"]))
check("过**页级预筛**页 = 第 3、4、9 页（4.7 起位图大小不再挡预筛，三页矢量都够）",
      _report["probe_pages"] == [3, 4, 9], repr(_report["probe_pages"]))
check("诊断里同时给出计数（前端直接显示「本篇 N 个图注页无面板级位图」）",
      _report["caption_pages_no_bitmap_count"] == 2 and _report["caption_page_count"] == 3
      and _report["gate_page_count"] == 2 and _report["probe_page_count"] == 3)

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
    old_regions, old_fails = ff.find_figure_regions(
        plain["blocks"], page_probes, page_sizes, plain["body_size"], excluded=excluded,
        avoid_rects=[(r.page, r.rect) for r in plain["table_regions"]])
    check("旧 NHB：定位出 7 个图区域（4.6 的 3/4/10/23 + 4.7 补上的混合图页 2/9/19）",
          [r.page for r in old_regions] == [2, 3, 4, 9, 10, 19, 23],
          repr([r.page for r in old_regions]))
    check("旧 NHB：第 9/23 页是 vector-only（图内文字是矢量轮廓，没有文字块可移）",
          old_regions[3].source == "vector-only" and old_regions[3].atom_indices == []
          and old_regions[6].source == "vector-only" and old_regions[6].atom_indices == [],
          repr([(r.page, r.source) for r in old_regions]))
    check("旧 NHB：第 2 页 25 块、第 3 页 40 块、第 4 页 134 块、第 19 页 16 块图内小字被收进区域",
          [len(r.atom_indices) for r in old_regions] == [25, 40, 134, 0, 40, 16, 0],
          repr([len(r.atom_indices) for r in old_regions]))
    check("旧 NHB：第 2/9/19 页的位图覆盖率 18% / 39% / 36%（< 60% → 混合图页要截图）",
          [round(ff.bitmap_cover_ratio(page_probes[p].get("bitmap_rects"), r.rect) * 100)
           for p, r in ((2, old_regions[0]), (9, old_regions[3]), (19, old_regions[5]))]
          == [18, 39, 36],
          repr([round(ff.bitmap_cover_ratio(page_probes[p].get("bitmap_rects"), r.rect) * 100)
                for p, r in ((2, old_regions[0]), (9, old_regions[3]), (19, old_regions[5]))]))
    check("旧 NHB：第 20/21 页如实记为定位失败（那两页的插图本身就是「文本示意」）",
          [(f["page"], f["reason"].startswith("区域里框进了正文块")) for f in old_fails[:2]]
          == [(20, True), (21, True)], repr(old_fails))
    check("旧 NHB：第 22 页（区域内位图覆盖 81%）交给位图提取路径，如实记进失败原因",
          old_fails[2]["page"] == 22 and "位图覆盖 81%" in old_fails[2]["reason"],
          repr(old_fails))

    with_figure = pdf_parser.parse_pdf(pdf_bytes, table_mode="image", figure_region=True)
    check("旧 NHB：打开图区域后公式区域数仍是 3、表格区域数仍是 1（互不干扰）",
          len(with_figure["formula_clusters"]) == 3 and len(with_figure["table_regions"]) == 1,
          repr((len(with_figure["formula_clusters"]), len(with_figure["table_regions"]))))
    check("旧 NHB：打开后卡片数 40 → 38、总词数 18538 → 18042（只少图内小字那 496 词）",
          (len(plain["cards"]), sum(pdf_parser.count_words(c.text) for c in plain["cards"]))
          == (40, 18538)
          and (len(with_figure["cards"]),
               sum(pdf_parser.count_words(c.text) for c in with_figure["cards"])) == (38, 18042),
          repr((len(with_figure["cards"]),
                sum(pdf_parser.count_words(c.text) for c in with_figure["cards"]))))
    check("旧 NHB：移出正文的词数 = 图区域成员的词数（一个字都不多不少）",
          sum(pdf_parser.count_words(p.text) for p in with_figure["paragraphs"]
              if p.is_figure)
          == sum(pdf_parser.count_words(with_figure["blocks"][i].text)
                 for r in with_figure["figure_regions"] for i in r.atom_indices)
          == 496,
          repr((sum(pdf_parser.count_words(p.text) for p in with_figure["paragraphs"]
                    if p.is_figure),
                sum(pdf_parser.count_words(with_figure["blocks"][i].text)
                    for r in with_figure["figure_regions"] for i in r.atom_indices))))
    check("旧 NHB：卡片词数差 = 496（18538 → 18042）—— 没有正文被连带删掉",
          sum(pdf_parser.count_words(c.text) for c in with_figure["cards"]) == 18042
          and sum(pdf_parser.count_words(c.text) for c in plain["cards"]) == 18538,
          repr(sum(pdf_parser.count_words(c.text) for c in with_figure["cards"])))
    check("旧 NHB：每个区域在卡片里只出一次截图（不会题注出一次、成员头又出一次）",
          sum(1 for card in with_figure["cards"] for kind, _ in card.segments
              if kind == "figure_region") == 7,
          repr(sum(1 for card in with_figure["cards"] for kind, _ in card.segments
                   if kind == "figure_region")))
    # ---- 4.7 去重：被区域截图吸收的位图不再单独呈现 ----
    from utils import image_extractor                            # noqa: E402
    from webapp import payload as payload_mod                    # noqa: E402
    img_result = image_extractor.extract_images(pdf_bytes)
    image_extractor.associate_with_cards(img_result["images"], with_figure["cards"])
    hidden = ff.covered_image_indices(img_result["images"], with_figure["figure_regions"])
    check("旧 NHB：26 张位图里有 16 张被图区域截图吸收（第 2 页 2 张 + 第 9 页 6 张 + 第 19 页 8 张）",
          len(hidden) == 16
          and sorted(img_result["images"][i].page for i in hidden) == [2, 2] + [9] * 6 + [19] * 8,
          repr(sorted(img_result["images"][i].page for i in hidden)))
    check("旧 NHB：开关关掉（figure_regions 为空）→ 一张都不吸收，与旧行为逐字段一致",
          ff.covered_image_indices(img_result["images"], []) == [])

    # 端到端：payload 里「仍单独呈现」的位图段确实少了 16 张（前端看不到重复的图）
    from utils import metadata as metadata_module                 # noqa: E402
    meta = metadata_module.extract_metadata(pdf_bytes, with_figure["blocks"])
    payload_on = payload_mod.build_paper_payload(
        pdf_bytes, "old.pdf", with_figure, img_result, None, meta,
        settings={"figure_region": True})
    payload_off = payload_mod.build_paper_payload(
        pdf_bytes, "old.pdf", plain, img_result, None, meta, settings={"figure_region": False})
    figures_on = sum(1 for card in payload_on["cards"] for seg in card["segments"]
                     if seg["kind"] == "figure")
    figures_off = sum(1 for card in payload_off["cards"] for seg in card["segments"]
                      if seg["kind"] == "figure")
    check("旧 NHB：payload 里单独呈现的位图段 17 → 9（16 张被吸收，另有 1 张本来就没关联上卡片）",
          figures_on == 9 and figures_off == 17, repr((figures_on, figures_off)))
    check("旧 NHB：概览的插图数跟着变成 10（26 张里 16 张由图区域承载）",
          payload_on["overview"]["images"] == 10
          and payload_off["overview"]["images"] == 26,
          repr((payload_on["overview"]["images"], payload_off["overview"]["images"])))
    check("旧 NHB：payload 的图区域段 = 7，与解析结果一致（每个区域只出一张）",
          sum(1 for card in payload_on["cards"] for seg in card["segments"]
              if seg["kind"] == "figure_region") == 7,
          repr(sum(1 for card in payload_on["cards"] for seg in card["segments"]
                   if seg["kind"] == "figure_region")))

if not os.path.exists(SAMPLE_NEW):
    skip("新 NHB 图区域（第 2/4/7/8 页）")
else:
    from utils import pdf_parser                               # noqa: E402
    pdf_bytes = open(SAMPLE_NEW, "rb").read()
    result = pdf_parser.parse_pdf(pdf_bytes, table_mode="image", figure_region=True)
    check("新 NHB：定位出 4 个图区域（第 2/4/7/8 页）",
          [r.page for r in result["figure_regions"]] == [2, 4, 7, 8],
          repr([r.page for r in result["figure_regions"]]))
    check("新 NHB：第 9 页位图覆盖 62%（≥60%）→ 交给位图路径，如实记进失败原因",
          [(f["page"], f["reason"].startswith("区域内位图覆盖 62%")) for f in result["figure_failures"]]
          == [(9, True)], repr(result["figure_failures"]))
    check("新 NHB：第 8 页图区域仍是 14 块图内小字成员（4.6 的验收值没被改动）",
          len(result["figure_regions"][3].atom_indices) == 14,
          repr(len(result["figure_regions"][3].atom_indices)))
    check("新 NHB：四个区域的成员数 25 / 61 / 19 / 14",
          [len(r.atom_indices) for r in result["figure_regions"]] == [25, 61, 19, 14],
          repr([len(r.atom_indices) for r in result["figure_regions"]]))
    plain_new = pdf_parser.parse_pdf(pdf_bytes, table_mode="image")
    check("新 NHB：打开后卡片 31 → 30、词数 14859 → 14519（只少图内小字那 340 词）",
          (len(plain_new["cards"]),
           sum(pdf_parser.count_words(c.text) for c in plain_new["cards"])) == (31, 14859)
          and (len(result["cards"]),
               sum(pdf_parser.count_words(c.text) for c in result["cards"])) == (30, 14519),
          repr((len(result["cards"]),
                sum(pdf_parser.count_words(c.text) for c in result["cards"]))))
    check("新 NHB：开关关闭时 figure_regions 恒为空、也不带 figure_stats",
          plain_new["figure_regions"] == [] and plain_new["figure_stats"] == {})

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed} · 跳过 {skipped}")
sys.exit(1 if failed else 0)
