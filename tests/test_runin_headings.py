r"""阶段 4.5 验收：「行内小标题补丁」的过滤规则与拆分行为

背景（实测见项目根《GROBID对比结论.md》）：
    GROBID 能把「行内小标题」（`Word recognizer.` 这种以句点结尾、与后文同处一段的标题）
    标成 TEI 的 <head>，而我们的字号规则天然抓不到它。但它同时会把图注、表注、
    报告清单也写成 <head>（`Fig. 1 |` `. 1 |` `Extended Data Fig. 3 | …`），
    所以补丁的做法是「GROBID 当候选 + 四条规则狠狠过滤 + 只拆卡片段落视图」。

本文件全部是**纯函数与离线**断言（不发网络、不读 PDF），因此任何机器上都能跑：
    · ① has_real_word：垃圾串（单字母 / 纯数字）必须被认出来
    · ② is_runin_heading：四条过滤规则各自的通过与拒绝 + 两条不误杀的边界
    · ③ filter_runin_heads：批量过滤、去重、压缩空白
    · ④ match_runin：取最长命中、大小写与空白归一化、只认开头
    · ⑤ split_runin：返回 (heading, rest)、标题保持原文写法、rest 为空必须返回 None
    · ⑥ grobid_client.parse_tei_heads：<figure> 里的 head 必须被结构性地跳过
    · ⑦ build_reading_cards 集成：开关打开只多出 heading 段，卡片数 / 卡片文本 / 词数不变
"""

import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

# ---- 控制台编码兜底（与其它套件一致：批处理用 GBK 控制台，生僻字符不能把脚本打断） ----
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

from utils import grobid_client, pdf_parser, runin_headings       # noqa: E402

passed, failed = 0, 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK] {label}")
    else:
        failed += 1
        print(f"  [!!] {label}  {detail}")


# 真实 GROBID 实测输出的候选（Nature Human Behaviour 那篇，见《GROBID对比结论.md》第 2 节）
REAL_HEADS = ["Word recognizer.", "Text reader.", "Memory system.",
              "Reading under time pressure.", "Simulation and validation."]
REAL_PARAGRAPH = ("Word recognizer. At the lowest time scale, the agent performs a "
                  "word-recognition process that identifies the next word to process.")

print("=" * 78)
print("① has_real_word：至少一个「≥2 个字母的真实词」")
check("`Word recognizer.` 有真实词", runin_headings.has_real_word("Word recognizer.") is True)
check("`'s 60 s 90 s a` 没有真实词（单字母与数字都不算）",
      runin_headings.has_real_word("'s 60 s 90 s a") is False)
check("`120 456 -7 .` 没有真实词（纯数字串）",
      runin_headings.has_real_word("120 456 -7 .") is False)
check("空串没有真实词", runin_headings.has_real_word("") is False)

print()
print("② is_runin_heading：四条规则同时满足才算")
check("规则①通过：`Word recognizer.` 是行内小标题",
      runin_headings.is_runin_heading("Word recognizer.") is True)
check("规则①通过：`Reading under time pressure.`（4 词）也是",
      runin_headings.is_runin_heading("Reading under time pressure.") is True)
check("规则①拒绝：超过 12 词的整句正文不是标题（13 词）",
      runin_headings.is_runin_heading(
          "This sentence clearly contains far more than twelve words in total altogether now.")
      is False)
check("规则①边界：恰好 12 词仍然通过（上限是「≤ 12」）",
      runin_headings.is_runin_heading("one two three four five six seven eight "
                                      "nine ten eleven twelve.") is True)
check("规则②拒绝：不以句点结尾的 head 全部不要（`Word recognizer` 无句点）",
      runin_headings.is_runin_heading("Word recognizer") is False)
check("规则③拒绝：`Fig. 1 |` 这类图注前缀",
      runin_headings.is_runin_heading("Fig. 1 | Illustration of the model.") is False)
check("规则③拒绝：`Table 2.` 这类表注前缀",
      runin_headings.is_runin_heading("Table 2.") is False)
check("规则③拒绝：`Extended Data` 与 `Extended Data Fig. 3 | …`",
      runin_headings.is_runin_heading("Extended Data.") is False
      and runin_headings.is_runin_heading("Extended Data Fig. 3 | Illustration.") is False)
check("规则③拒绝：`Supplementary information.`",
      runin_headings.is_runin_heading("Supplementary information.") is False)
check("规则④拒绝：无真实词的垃圾串（`s 60 s 90 a .`）",
      runin_headings.is_runin_heading("s 60 s 90 a .") is False)
check("规则②拒绝：GROBID 实测的垃圾 head `'s 60 s 90 s a Heat maps'`",
      runin_headings.is_runin_heading("'s 60 s 90 s a Heat maps'") is False)
check("不误杀边界：`Figures.` 不是 Fig 前缀（前缀后必须紧跟 . / 数字 / 空白）",
      runin_headings.is_runin_heading("Figures.") is True)
check("不误杀边界：`Tableau.` 同理可用作行内小标题",
      runin_headings.is_runin_heading("Tableau.") is True)
check("空串 / None 一律 False（调用方不必先判空）",
      runin_headings.is_runin_heading("") is False
      and runin_headings.is_runin_heading(None) is False)

print()
print("③ filter_runin_heads：批量过滤（保持顺序、去重、压缩空白）")
mixed = ["Computational simulation of reading", "Fig. 1 |", "Word recognizer.",
         "Extended Data", ". 1 |", "'s 60 s 90 s a Heat maps'", "Text reader.",
         "  Word   recognizer. ", "Data availability"]
kept = runin_headings.filter_runin_heads(mixed)
check("只留下真正的行内小标题（顺序不变、重复项合并）",
      kept == ["Word recognizer.", "Text reader."], repr(kept))
check("空列表 / None 返回空列表", runin_headings.filter_runin_heads([]) == []
      and runin_headings.filter_runin_heads(None) == [])

print()
print("④ match_runin：段落是否以某个 head 开头")
check("命中：真实 GROBID head 能匹配真实段落",
      runin_headings.match_runin(REAL_PARAGRAPH, REAL_HEADS) == "Word recognizer.")
check("多个命中时取**最长**的那个（`Methods` 不能抢走 `Methods and materials.`）",
      runin_headings.match_runin("Methods and materials. We did X.", 
                                 ["Methods", "Methods and materials."]) == "Methods and materials.")
check("大小写不敏感（段落里全大写也命中）",
      runin_headings.match_runin("WORD RECOGNIZER. Here we go.", ["Word recognizer."])
      == "Word recognizer.")
check("空白归一化（段落里多个空格 / 换行都算命中）",
      runin_headings.match_runin("Word\n  recognizer.   Here we go.", ["Word recognizer."])
      == "Word recognizer.")
check("只认开头：标题出现在段落中间不算命中",
      runin_headings.match_runin("We describe the Word recognizer. Next.", ["Word recognizer."])
      is None)
check("都不命中 / 空输入返回 None",
      runin_headings.match_runin("Nothing to see here.", REAL_HEADS) is None
      and runin_headings.match_runin("", REAL_HEADS) is None
      and runin_headings.match_runin(REAL_PARAGRAPH, []) is None)

print()
print("⑤ split_runin：拆成 (标题, 剩余正文)")
check("正常拆分：剩余正文去掉前导空白",
      runin_headings.split_runin("Word recognizer.  Here we describe it.",
                                 ["Word recognizer."]) == ("Word recognizer.",
                                                           "Here we describe it."))
check("标题保留**原文里的写法**（GROBID 给的是 Word，段落里是小写 word，就用小写）",
      runin_headings.split_runin("word recognizer. Here we describe it.",
                                 ["Word recognizer."]) == ("word recognizer.",
                                                           "Here we describe it."))
check("剩余正文为空时返回 None（整段都是标题时不许拆，否则卡片上只剩一行加粗字）",
      runin_headings.split_runin("Word recognizer.", ["Word recognizer."]) is None)
check("不命中返回 None",
      runin_headings.split_runin("Nothing here.", REAL_HEADS) is None)
check("全角字符（NFKC 归一化命中）走按词数切分的兜底分支，标题仍是原文写法",
      runin_headings.split_runin("Ｗｏｒｄ recognizer. Here we describe it.",
                                 ["Word recognizer."]) == ("Ｗｏｒｄ recognizer.",
                                                           "Here we describe it."))
check("拆出来的两部分拼回去 = 原段落（只去掉中间多余空白，一个词都不丢）",
      " ".join(runin_headings.split_runin(REAL_PARAGRAPH, REAL_HEADS)).split()
      == REAL_PARAGRAPH.split())

print()
print("⑥ grobid_client.parse_tei_heads：图注 head 必须被结构性地跳过")
TEI = b"""<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <text><body>
    <div><head>Results</head><p>text</p>
      <figure><head>Fig. 1 | Illustration of the model</head>
        <figDesc>caption body</figDesc></figure>
      <div><head>Word recognizer.</head><p>run-in heading paragraph</p></div>
      <figure type="table"><head>Table 2 | Comparison of datasets</head></figure>
      <head>Results</head>
      <head>   </head>
    </div>
  </body></text>
</TEI>"""
heads = grobid_client.parse_tei_heads(TEI)
check("取出正文里的 head、按出现顺序、去重、空白项丢弃",
      heads == ["Results", "Word recognizer."], repr(heads))
check("<figure>（含嵌套在 <div> 里的）与表格 figure 的 head 全部被跳过",
      not any(item.startswith(("Fig.", "Table")) for item in heads), repr(heads))
check("命名空间无关：带前缀的 tei:head 也能取到",
      grobid_client.parse_tei_heads(
          b'<tei:TEI xmlns:tei="http://www.tei-c.org/ns/1.0"><tei:text><tei:body>'
          b"<tei:div><tei:head>Discussion</tei:head></tei:div></tei:body></tei:text></tei:TEI>")
      == ["Discussion"])
try:
    grobid_client.parse_tei_heads(b"<TEI><text>broken")
    check("截断的 XML 应抛 GrobidError", False, "没有抛异常")
except grobid_client.GrobidError:
    check("截断的 XML 应抛 GrobidError", True)
check("没有 head 的 TEI 返回空列表（不报错）",
      grobid_client.parse_tei_heads(b"<TEI><text><body><p>only text</p></body></text></TEI>") == [])

print()
print("⑦ build_reading_cards 集成：开关只加 heading 段，不改卡片与词数")


def paragraph(text, role="body", **extra):
    """造一个最小可用的段落视图（Paragraph 是 Block 的子类，字段都有默认值）"""
    item = pdf_parser.Paragraph(
        page=1, x0=0, y0=0, x1=100, y1=12, text=text,
        max_size=10.0, bold_ratio=0.0, line_height=12.0, role=role, atom_indices=[0])
    for key, value in extra.items():
        setattr(item, key, value)
    return item


body_para = paragraph(REAL_PARAGRAPH)
plain_cards = pdf_parser.build_reading_cards([body_para], 200, True)
patched_cards = pdf_parser.build_reading_cards([body_para], 200, True, runin_heads=REAL_HEADS)

check("关闭开关（runin_heads=None）：段落视图就是一条 text（与改动前一致）",
      plain_cards[0].segments == [("text", REAL_PARAGRAPH)], repr(plain_cards[0].segments))
check("打开开关：同一段被拆成 heading + text 两条",
      [kind for kind, _ in patched_cards[0].segments] == ["heading", "text"],
      repr(patched_cards[0].segments))
check("拆出的标题就是 GROBID 的 `Word recognizer.`，正文是剩下的部分",
      patched_cards[0].segments[0] == ("heading", "Word recognizer.")
      and patched_cards[0].segments[1][1].startswith("At the lowest time scale"),
      repr(patched_cards[0].segments[:2]))
check("卡片文本一个字符都不变（补丁只动 segments）",
      plain_cards[0].text == patched_cards[0].text)
check("卡片词数不变（词只是从 text 段挪到 heading 段）",
      pdf_parser.count_words(plain_cards[0].text)
      == pdf_parser.count_words(patched_cards[0].text)
      and sum(pdf_parser.count_words(t) for kind, t in patched_cards[0].segments
              if kind in ("heading", "text"))
      == pdf_parser.count_words(plain_cards[0].text))
check("卡片数量不变（聚合结果不受补丁影响）",
      len(plain_cards) == len(patched_cards) == 1)

formula_para = paragraph("E = m c squared", is_formula=True)
mixed_cards = pdf_parser.build_reading_cards([formula_para, body_para], 200, True,
                                             runin_heads=REAL_HEADS)
kinds = [kind for kind, _ in mixed_cards[0].segments]
check("公式段落不走拆分分支（仍是 formula 段，位置和内容都不变）",
      kinds == ["formula", "heading", "text"], repr(kinds))
check("公式段落的载荷仍是 {page, rect, text}",
      mixed_cards[0].segments[0][1] == {"page": 1, "rect": (0.0, 0.0, 100.0, 12.0),
                                        "text": "E = m c squared"},
      repr(mixed_cards[0].segments[0]))

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed}")
sys.exit(1 if failed else 0)
