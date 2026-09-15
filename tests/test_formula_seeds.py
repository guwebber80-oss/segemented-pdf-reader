r"""验收修复（4.4-B 期间用户报的两个问题）的回归断言

问题①「表格图最底部缺一截」：表注（缩写说明 / "Bold values…" / "Note: …"）是单行、
    只有 1 个单元格，被「表格行必须 ≥3 格」的规则排除 → 截图缺底部。
问题②「比较多的公式没有被截图」：npj 第 8、9 页的显示公式被排成上下叠放的碎片，
    全是 weak/fragment 等级、**没有强候选当种子**，聚类从不启动；而且这些公式的
    等号被字体错映射成 `¼`（`Cði; jÞ` 的 ð Þ 是括号），线性文本本身也是乱码。

本文件同时守住这两条修复的**边界**：不能为了多认公式，把图的子图标签
（A₁ O₂ B₁ B₂）、图例（Human, high K）、坐标轴刻度（30 s 60 s 90 s）当成公式
——这些都在 Nature Human Behaviour 那篇里实测出现过。
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

import pdf_parser       # noqa: E402

SAMPLES = r"E:\segemented pdf reader\测试pdf"
NPJ = os.path.join(SAMPLES, "4.3测试", "自然互动中孤独症儿童面部表情动态的量化评估.pdf")
NATURE = os.path.join(SAMPLES, "for 4.2", "s41562-026-02534-0.pdf")
ONE_COL = os.path.join(SAMPLES, "单栏.pdf")

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


def analyze(path):
    return pdf_parser.parse_pdf(open(path, "rb").read(), True, True, 200, "image")


print("=" * 78)
print("① 字形错映射修复：¼ → =、ð Þ → 括号（只在数学上下文里替换）")
check("'GEV ¼ n' → 'GEV = n'",
      pdf_parser.repair_obfuscated_math("GEVₑₘₒₜᵢₒₙ ¼ nᵉᵐᵒᵗⁱᵒⁿ") == "GEVₑₘₒₜᵢₒₙ = nᵉᵐᵒᵗⁱᵒⁿ",
      repr(pdf_parser.repair_obfuscated_math("GEVₑₘₒₜᵢₒₙ ¼ nᵉᵐᵒᵗⁱᵒⁿ")))
check("'Cði; jÞ' → 'C(i; j)'",
      pdf_parser.repair_obfuscated_math("Cði; jÞ") == "C(i; j)",
      repr(pdf_parser.repair_obfuscated_math("Cði; jÞ")))
check("单独的 ¼（四分之一）不被改（正文语境）",
      pdf_parser.repair_obfuscated_math("about ¼ of the trials") == "about ¼ of the trials",
      repr(pdf_parser.repair_obfuscated_math("about ¼ of the trials")))
check("单个 ð（不成对）不被改",
      pdf_parser.repair_obfuscated_math("the ð sound") == "the ð sound")
# 用户报的第三处：等号被排到块首，旧版正则要求「¼ 前面有字符」→ 一直不匹配
check("块首的 ¼ 也能修（'¼ Cði; jÞ P' → '= C(i; j) P'）",
      pdf_parser.repair_obfuscated_math("¼ Cði; jÞ P").strip() == "= C(i; j) P",
      repr(pdf_parser.repair_obfuscated_math("¼ Cði; jÞ P")))
check("错映射括号授权的替换仍然安全（含 ð 与 ¼ 但没有括号对）",
      pdf_parser.repair_obfuscated_math("the ð sound is ¼ of it") == "the ð sound is ¼ of it",
      repr(pdf_parser.repair_obfuscated_math("the ð sound is ¼ of it")))

print()
print("② 目标样本：npj 第 8、9 页的显示公式现在被截成图")
if not os.path.exists(NPJ):
    skip("npj 样本的断言")
else:
    result = analyze(NPJ)
    clusters = result["formula_clusters"]
    texts = " ".join(cluster.text for cluster in clusters)
    check("公式区域共 5 处（含用户第二次报的那处分数）", len(clusters) == 5, repr(len(clusters)))
    check("5 处都在第 8、9 页", sorted({c.page for c in clusters}) == [8, 9],
          repr(sorted({c.page for c in clusters})))
    check("含 'GEV…= n…'（此前完全没被识别）", "GEV" in texts)
    check("含 'Spearman' 那一处（大公式 Σ + 相关系数）", "Spearman" in texts)
    check("含用户第二次报的 'C(i; j)' / 'C(i; k)' 那一处",
          "C(i; j)" in texts and "C(i; k)" in texts,
          texts[:120])
    check("第 8 页那处分数（= C(i; j) P / P i; j / ₖ C(i; k)）在同一个区域里",
          any("C(i; j)" in c.text and "C(i; k)" in c.text for c in clusters))
    check("卡片正文里不再残留被错映射成控制字符的乱码块",
          not any("\x01" in card.text or "\x06" in card.text for card in result["cards"]),
          repr([card.text[:40] for card in result["cards"] if "\x01" in card.text][:2]))
    check("公式图真的进了卡片",
          sum(1 for card in result["cards"] for kind, _ in card.segments if kind == "formula")
          == len(clusters),
          repr(sum(1 for card in result["cards"] for kind, _ in card.segments
                   if kind == "formula")))
    check("没有把散文吸进公式区域（成员块都 ≤40 词）",
          all(pdf_parser.count_words(result["blocks"][i].text) <= 40
              for cluster in clusters for i in cluster.atom_indices),
          repr([[pdf_parser.count_words(result["blocks"][i].text)
                 for i in c.atom_indices] for c in clusters]))

print()
print("③ 边界：图的子图标签 / 图例 / 坐标轴刻度不得被当成公式")
if not os.path.exists(NATURE):
    skip("Nature 样本的断言")
else:
    nature = analyze(NATURE)
    check("Nature 公式区域仍为 3 处（收紧前后一致，没被噪声顶上去）",
          len(nature["formula_clusters"]) == 3, repr(len(nature["formula_clusters"])))
    check("没有以 'Policy π' 这类图注为成员的公式区域",
          all("Policy" not in nature["blocks"][i].text
              for cluster in nature["formula_clusters"] for i in cluster.atom_indices))
    check("没有以图例（'Human, high K'）为成员的公式区域",
          all("high K" not in nature["blocks"][i].text
              for cluster in nature["formula_clusters"] for i in cluster.atom_indices))
    check("没有以坐标轴刻度（'30 s 60 s 90 s'）为成员的公式区域",
          all("60 s" not in nature["blocks"][i].text
              for cluster in nature["formula_clusters"] for i in cluster.atom_indices))

print()
print("④ 边界：公式丰富的样本数量不变（改动只针对「碎片堆」这一类）")
if not os.path.exists(ONE_COL):
    skip("单栏.pdf 的断言")
else:
    one = analyze(ONE_COL)
    check("单栏.pdf 公式区域仍为 32 处", len(one["formula_clusters"]) == 32,
          repr(len(one["formula_clusters"])))

print()
print("⑤ 表注并进表格截图（问题①：底部不能缺一截）")
if not os.path.exists(NPJ):
    skip("表注的断言")
else:
    result = analyze(NPJ)
    by_page = {region.page: region for region in result["table_regions"]}
    check("表 1（第 3 页）的截图含表注「Continuous variables are presented」",
          "Continuous variables" in by_page[3].text, by_page[3].text[-80:])
    check("表 2（第 4 页）的截图含表注「Emotion variation, EI Expression Intensity」",
          "Emotion variation" in by_page[4].text, by_page[4].text[-80:])
    check("表 3（第 6 页）的截图含「Bold values indicate the best performance」",
          "Bold values" in by_page[6].text, by_page[6].text[-80:])
    check("表注行数已记入区域信息（诊断面板可核对）",
          all(region.notes >= 1 for region in result["table_regions"]),
          repr([(r.page, r.notes) for r in result["table_regions"]]))
    check("区域底部确实延伸到了表注下方（不再停在最后一行）",
          by_page[6].rect[3] > 540, repr(round(by_page[6].rect[3], 1)))
    check("表注没有被并进正文（词数相应下降）",
          all(region.notes <= 4 for region in result["table_regions"]),
          repr([(r.page, r.notes) for r in result["table_regions"]]))

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed} · 跳过 {skipped}")
sys.exit(1 if failed else 0)
