r"""验收修复：DOI / URL 行不得被当成卡片小标题（用户报的缺陷）

用户报的现象：「前 7 张卡片有把 doi 识别成小标题的 bug」。

两个真实成因（都必须修）：
  ① **DOI 行被并进标题块**：npj 第 1 页的 `https://doi.org/10.1038/s41746-026-02375-1`
     （8pt）紧贴在论文大标题（25.9pt）上方，两者被排进同一个 PyMuPDF 块 →
     整块（含 DOI）成了卡片里的一级标题；
     Nature Human Behaviour 更复杂：`Article`(10pt) + DOI(8pt) + 标题(26pt) 三行一块。
  ② **running head 上的 DOI 被判成章节标题**：DOI 行字号小、往往还加粗，
     会被 `mark_headings` 认成三级标题（`is_bold and ratio ≥ 0.95`）。

本文件同时守住两个**容易误伤的边界**（都在开发中实测踩过，被回归工具抓到）：
  · 拆「前导小字行」的条件必须要求**前导段以 URL/DOI 为主**，
    否则图注、表格里「小字标签 + 大字数值」的块也会被拆，9 篇样本的卡片结构全变；
  · 「第 1 页标题之上 → 封面信息」必须按**标题带**判断（最大标题 + 同号近邻），
    只取字号最大的块的话，ACM CHI'25 那篇被排成两块的大标题
    （'…Slow Noticing in Food' 17.0pt 与 'Gardens' 17.2pt）会把第一行误踢出卡片。
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

from utils import pdf_parser       # noqa: E402

SAMPLES = r"E:\segemented pdf reader\测试pdf"
NPJ = os.path.join(SAMPLES, "4.3测试", "自然互动中孤独症儿童面部表情动态的量化评估.pdf")
NATURE = os.path.join(SAMPLES, "for 4.2", "s41562-026-02534-0.pdf")
ACM_CHI = os.path.join(SAMPLES, "for 4.2", "3772318.3791719.pdf")

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
print("① 单元测试：URL 占比判据")
check("纯 DOI 行的占比 ≥0.9",
      pdf_parser.url_ratio("https://doi.org/10.1038/s41562-026-02534-0") >= 0.9,
      repr(pdf_parser.url_ratio("https://doi.org/10.1038/s41562-026-02534-0")))
check("正文里偶尔出现的链接不会被当成 DOI 行（占比 <0.45）",
      pdf_parser.url_ratio(
          "Data are available at https://osf.io/abc12 for review purposes only.")
      < pdf_parser.URL_DOMINANT_RATIO,
      repr(pdf_parser.url_ratio(
          "Data are available at https://osf.io/abc12 for review purposes only.")))
check("引用条目 '10 pages. https://doi.org/…' 算 DOI 行",
      pdf_parser.url_ratio("10 pages. https://doi.org/10.1145/3706598.3713992")
      >= pdf_parser.URL_DOMINANT_RATIO)

print()
print("② 单元测试：前导元数据行的拆分（_split_leading_metadata）")


def rec(text, size):
    return {"text": text, "max_size": size, "bold_ratio": 0.0, "bold_chars": 0,
            "chars": len(text), "bbox": (0, 0, 100, 10), "height": 10.0,
            "baseline": 0.0, "fonts": (), "math_font": False}


leading, rest = pdf_parser._split_leading_metadata(
    [rec("Article", 10.0), rec("https://doi.org/10.1038/s41562-026-02534-0", 8.0),
     rec("Hierarchical resource rationality explains", 26.0)])
check("Article + DOI + 标题 三行一块 → 拆出前两行",
      len(leading) == 2 and len(rest) == 1, f"{len(leading)} / {len(rest)}")

leading2, _rest2 = pdf_parser._split_leading_metadata(
    [rec("400", 7.0), rec("Average gaze duration (ms)", 12.0)])
check("图注里「小字标签 + 大字数值」不拆（前导段不是 URL）",
      leading2 == [], repr([r["text"] for r in leading2]))

print()
print("③ 目标样本：卡片里不再有 DOI / URL 小标题")
for path in (NPJ, NATURE):
    if not os.path.exists(path):
        skip(f"{os.path.basename(path)} 的断言")
        continue
    result = analyze(path)
    name = os.path.basename(path)
    bad = [(card.card_index, str(text)[:50])
           for card in result["cards"] for kind, text in card.segments
           if kind == "heading" and pdf_parser.url_ratio(str(text)) >= pdf_parser.URL_DOMINANT_RATIO]
    check(f"{name[:22]}：没有卡片把 DOI/URL 当标题", bad == [], repr(bad[:2]))

print()
print("④ npj：DOI 行单独成块并归封面信息，真标题不受影响")
if not os.path.exists(NPJ):
    skip("npj 的断言")
else:
    result = analyze(NPJ)
    # 只看「以 DOI 为主」的块：参考文献条目里也会出现 doi.org，那是另一回事
    doi_blocks = [b for b in result["blocks"]
                  if pdf_parser.url_ratio(b.text) >= pdf_parser.URL_DOMINANT_RATIO]
    page1_doi = [b for b in doi_blocks if b.page == 1]
    check("首页 DOI 独立成块（不再与标题同块）",
          bool(page1_doi) and all(len(b.text) < 60 for b in page1_doi),
          repr([(b.page, len(b.text), b.text[:46]) for b in doi_blocks[:3]]))
    check("DOI 块归封面信息 / 页眉（都不进卡片）",
          all(b.role in ("front_matter", "header_footer") for b in doi_blocks),
          repr([(b.page, b.role) for b in doi_blocks[:3]]))
    first_card = result["cards"][0]
    headings = [str(text) for kind, text in first_card.segments if kind == "heading"]
    check("第 1 张卡的标题是真标题（不含 DOI、不含期刊名 banner）",
          headings and all("doi.org" not in h and "digital medicine" not in h for h in headings),
          repr(headings[:3]))
    check("真标题本身仍是章节标题",
          any("Naturalistic facial dynamics" in b.text and b.role == "heading"
              for b in result["blocks"]))

print()
print("⑤ 边界：被排成两块的大标题不能被拆散（ACM CHI'25 实测）")
if not os.path.exists(ACM_CHI):
    skip("ACM CHI'25 的断言")
else:
    result = analyze(ACM_CHI)
    wanted = ("Tuning into Everyday", "Gardens")
    title_lines = [b for b in result["blocks"]
                   if b.page == 1 and b.text.strip().startswith(wanted)]
    check("标题的两块都还在",
          len(title_lines) == 2, repr([b.text[:40] for b in title_lines]))
    check("两块都是章节标题（第一行没有被当成「标题之上」踢出卡片）",
          len(title_lines) == 2 and all(b.role == "heading" for b in title_lines),
          repr([(b.text[:30], b.role) for b in title_lines]))

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed} · 跳过 {skipped}")
sys.exit(1 if failed else 0)
