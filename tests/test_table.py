r"""4.4-B 验收：表格区域识别与整块截图呈现

背景：期刊表格在 PDF 里没有任何「表格」标记，只有一堆带坐标的文字。4.4-B 之前
它们被当成正文线性化（'EV 0.758 0.682 0.735 0.707 0.723 0.746 0.790'），
列与列的关系全丢。现在按「题注锚点 + ≥3 列 × ≥3 行的对齐网格」把整张表截成图。

本文件验证四件事：
  ① 真样本里的表格被正确识别（npj 4 张、ACM 5 张），且**没有混进正文段**；
  ② 「Table 1. …」这种**正文里的提及句**不会被误判成表格；
  ③ 侧边栏「保留文字」模式回到 4.4-B 之前的线性行为（回退开关）；
  ④ 那次「正文被链式并进表格最后一行」的坑有单元测试守着。

样本在仓库外（测试pdf\...），找不到就跳过对应断言，保证仓库本身可移植。
"""

import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

# ---- 控制台编码兜底（批处理用 chcp 936，GBK 下打印生僻字符会抛异常）----
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

from utils import pdf_parser       # noqa: E402
from utils import table_finder     # noqa: E402

SAMPLES = r"E:\segemented pdf reader\测试pdf"
NPJ = os.path.join(SAMPLES, "4.3测试", "自然互动中孤独症儿童面部表情动态的量化评估.pdf")
ACM = os.path.join(SAMPLES, "双栏.pdf")
CELL = os.path.join(SAMPLES, "1-s2.0-S0960982215012476-main.pdf")

passed, failed, skipped = 0, 0, 0


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


def analyze(path, table_mode="image"):
    return pdf_parser.parse_pdf(open(path, "rb").read(), True, True, 200, table_mode)


def prose_members(result, region, limit=60):
    """区域成员里词数超过 limit 的块（真表格的成员块都是行/单元格，不会这么长）"""
    return [b for b in (result["blocks"][i] for i in region.atom_indices)
            if pdf_parser.count_words(b.text) > limit]


print("=" * 78)
print("① 目标样本：npj 的 4 张表全部识别，且没有混进正文")
npj_image = None
if not os.path.exists(NPJ):
    skip("npj 样本的全部断言")
else:
    result = analyze(NPJ)
    npj_image = result
    regions = result["table_regions"]
    check("识别到 4 处表格区域（T1~T4）", len(regions) == 4, repr(len(regions)))
    check("页码为 3 / 4 / 6 / 7", sorted(r.page for r in regions) == [3, 4, 6, 7],
          repr([r.page for r in regions]))
    table2 = [r for r in regions if r.page == 4]
    if table2:
        check("表 2 = 6 行 × 8 列（人工核对：6 行数据 + 8 列）",
              table2[0].rows == 6 and table2[0].columns == 8,
              f"{table2[0].rows} 行 × {table2[0].columns} 列")
    check("每个区域的成员块都没有长正文段（>60 词）",
          all(not prose_members(result, r) for r in regions),
          repr([[b.text[:40] for b in prose_members(result, r)] for r in regions]))
    table_images = sum(1 for card in result["cards"] for kind, _ in card.segments if kind == "table")
    check("卡片里出现 4 张表格图", table_images == 4, repr(table_images))
    check("第 2 页的「Table 1. Independent-samples…」提及句没有被当成表格",
          all(r.page != 2 for r in regions), repr([r.page for r in regions]))
    linear = " ".join(card.text for card in result["cards"])
    check("表格文字已移出卡片流（截图模式下正文里不再有表格数字串）",
          "0.758 0.682" not in linear, "卡片正文里仍出现表格数字串")

    # 截图管线本身要能跑通：区域矩形换算成像素尺寸应当与页面区域相符
    from utils import image_extractor
    import pymupdf
    pdf_bytes = open(NPJ, "rb").read()
    sizes = []
    for region in regions:
        png = image_extractor.render_region_image(pdf_bytes, region.page, region.rect)
        pixmap = pymupdf.Pixmap(png)
        sizes.append((pixmap.width, pixmap.height))
    check("4 个区域都能渲染成图片，且尺寸与区域矩形相符（200dpi）",
          len(sizes) == 4 and all(
              abs(width - (region.rect[2] - region.rect[0] + 4) * 200 / 72) < 12
              and abs(height - (region.rect[3] - region.rect[1] + 6) * 200 / 72) < 12
              for (width, height), region in zip(sizes, regions)),
          repr(sizes))

print()
print("② 负例：没有表格的样本必须一处都不判")
if not os.path.exists(CELL):
    skip("Cell Press 样本的断言")
else:
    cell_result = analyze(CELL)
    check("Cell Press 样本：表格 0 处", cell_result["table_regions"] == [],
          repr([(r.page, r.caption[:30]) for r in cell_result["table_regions"]]))

print()
print("③ 「保留文字」模式：回到 4.4-B 之前的线性行为（回退开关）")
if npj_image is None:
    skip("回退开关的断言")
else:
    linear_result = analyze(NPJ, "text")
    check("text 模式下不识别表格", linear_result["table_regions"] == [],
          repr(len(linear_result["table_regions"])))
    linear_text = " ".join(card.text for card in linear_result["cards"])
    check("text 模式下表格数字串仍在正文里（与改动前一致）",
          "0.758 0.682" in linear_text, "正文里找不到表格数字串")
    check("text 模式下卡片里没有表格图",
          not any(kind == "table" for card in linear_result["cards"] for kind, _ in card.segments))
    check("两种模式的原子块数与角色分布完全一致（改动只影响呈现，不影响分类）",
          len(linear_result["blocks"]) == len(npj_image["blocks"])
          and linear_result["roles"] == npj_image["roles"],
          f"块 {len(linear_result['blocks'])} vs {len(npj_image['blocks'])}")

print()
print("④ 单元测试：正文行不得被并进表格最后一行（4.4-B 开发中踩过的坑）")


def fake_line(text, x0, y0, x1=None, y1=None):
    return {"text": text, "x0": x0, "y0": y0,
            "x1": x1 if x1 is not None else x0 + 60.0,
            "y1": y1 if y1 is not None else y0 + 8.0, "size": 8.0}


rows = [
    {"y": 100.0, "bottom": 108.0, "cells": [fake_line("Method", 66.0, 100.0)]},
    # 单元格内换行：短、且 x 与上一行的单元格对齐（真实案例 ACM 表头 'Right Eye' 折行）
    {"y": 110.0, "bottom": 118.0, "cells": [fake_line("or Equal Viewing", 66.0, 110.0)]},
    {"y": 125.0, "bottom": 133.0,
     "cells": [fake_line("facial coordination enhanced group separation and the clearest", 42.0, 125.0)]},
]
merged = table_finder._merge_wrapped([dict(row) for row in rows])
check("单元格内换行（短行、x 对齐）被并进上一行", len(merged) == 2, repr(len(merged)))
check("表格下方的正文行**不**被并入（它的词数远超单元格上限）",
      all(len(row["cells"]) == 1 for row in merged[1:]), repr([len(r["cells"]) for r in merged]))

long_rows = [dict(row) for row in rows]
long_rows[1] = {"y": 110.0, "bottom": 118.0,
                "cells": [fake_line("this is a full sentence, and.", 66.0, 110.0)]}
merged2 = table_finder._merge_wrapped(long_rows)
check("以句末标点结尾的行不并（那是句子，不是单元格续行）",
      len(merged2) == 3, repr(len(merged2)))

far_rows = [dict(row) for row in rows]
far_rows[1] = {"y": 130.0, "bottom": 138.0, "cells": [fake_line("or Equal", 66.0, 130.0)]}
merged3 = table_finder._merge_wrapped(far_rows)
check("距离按该行自己的 y 算：隔得远的短行不会被链式并进来（这是最坑的那个 bug）",
      len(merged3) == 3, repr(len(merged3)))

check("题注正则认得 'Table 1 | …' / 'Table 2: …' / 'TABLE 3 …'",
      table_finder._is_caption(fake_line("Table 1 | Demographic characteristics", 40, 10))
      and table_finder._is_caption(fake_line("Table 2: Standard Eye Dominance", 40, 10))
      and table_finder._is_caption(fake_line("TABLE 3 Comparison between groups", 40, 10)))
check("裸「Table」两个字不算题注",
      not table_finder._is_caption(fake_line("Table", 40, 10)))

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed} · 跳过 {skipped}")
sys.exit(1 if failed else 0)
