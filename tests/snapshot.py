"""输出指纹快照：把「解析 + 元数据提取」的结果压成可比对的结构

用途：**版本管理的第一道防线**。改动代码前先存一份基线，改动后重新采集并与基线比对，
就能明确回答「这次更新有没有影响原有功能」——而不是凭感觉说「应该没事」。

指纹里区分两类字段：
  · 「必须一致」：卡片数、词数、角色分布、公式区域数、元数据各字段的值……
    这些变了就说明原有功能受影响，比对会明确报出来。
  · 「允许变化」：耗时、来源说明文字（会随实现细节调整）等，不参与判定。

跑法：
    python tests/snapshot.py                 # 采集并打印
    python tests/snapshot.py --out 某文件.json   # 写到指定文件
"""

import argparse
import glob
import json
import os
import statistics
import sys

# ---- 控制台编码兜底 ----
# 批处理用 chcp 936 把控制台设成 GBK，而本项目会打印 PDF 里的原始字符
# （上下标 ᵢ ₖ、数学符号 −、表情符号…），其中不少不在 GBK 里，
# 直接 print 会抛 UnicodeEncodeError 把脚本整个打断。这里统一改成「不可编码就替换」，
# 中文照常显示，个别生僻字符显示为 ?，但脚本绝不因为打印而崩。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import time

# 让脚本能 import 到项目模块（本文件在 <项目>/tests/ 下）
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from utils import metadata       # noqa: E402
from utils import pdf_parser     # noqa: E402

DEFAULT_SAMPLES = r"E:\segemented pdf reader\测试pdf"
DEFAULT_OUT = os.path.join(PROJECT, "tests", "baseline.json")

# 参与比对的元数据字段（值必须一致；来源/备注属于「允许变化」）
METADATA_FIELDS = [
    ("标题", "title"),
    ("DOI", "doi"),
    ("摘要词数", "abstract_words"),
    ("作者列表", "authors"),
    ("第一作者", "first_author"),
    ("通讯作者", "corresponding_author"),
    ("通讯邮箱", "corresponding_email"),
    ("作者单位", "affiliations"),
    ("作者邮箱", "author_emails"),
    ("附件链接", "supplementary_links"),
]


def fingerprint_one(path: str, target_words: int = 200, table_mode: str = "image") -> dict:
    """采集单个 PDF 的输出指纹。

    table_mode 与 pdf_parser.parse_pdf 的开关对应：
        "image" 表格截图为图（默认，4.4-B 之后的行为）；
        "text"  不识别表格、表格文字留在文字流（4.4-B 之前的行为，回退开关）。
    """
    data = open(path, "rb").read()
    start = time.time()
    result = pdf_parser.parse_pdf(data, True, True, target_words, table_mode)
    parsed_seconds = time.time() - start

    cards = result["cards"]
    card_words = [pdf_parser.count_words(card.text) for card in cards] or [0]
    formula_segments = sum(1 for card in cards for kind, _ in card.segments
                           if kind == "formula")
    table_segments = sum(1 for card in cards for kind, _ in card.segments
                         if kind == "table")

    meta = metadata.extract_metadata(data, result["blocks"])

    return {
        "文件": os.path.basename(path),
        # ---- 必须一致 ----
        "页数": len(result["pages"]),
        "原子块数": len(result["blocks"]),
        "段落数": len(result["paragraphs"]),
        "卡片数": len(cards),
        "正文总词数": sum(card_words),
        "卡片词数中位": int(statistics.median(card_words)),
        "卡片词数最小": min(card_words),
        "卡片词数最大": max(card_words),
        "角色分布": dict(sorted(result["roles"].items())),
        "公式区域数": len(result["formula_clusters"]),
        "公式图片段落数": formula_segments,
        "表格区域数": len(result.get("table_regions", [])),
        "表格图片段落数": table_segments,
        "元数据": {
            "标题": meta.title.value,
            "DOI": meta.doi.value,
            "摘要词数": len(meta.abstract.value.split()),
            "作者列表": meta.author_list(),
            "第一作者": meta.first_author.value,
            "通讯作者": meta.corresponding_author.value,
            "通讯邮箱": meta.corresponding_email.value,
            "作者单位": meta.affiliation_list(),
            "作者邮箱": [line for line in meta.author_emails.value.splitlines() if line.strip()],
            "附件链接": [line for line in meta.supplementary_links.value.splitlines()
                         if line.strip()],
        },
        # ---- 允许变化（仅记录，不参与判定）----
        "_允许变化": {
            "解析耗时": round(parsed_seconds, 2),
            "元数据来源": {
                "标题": meta.title.source,
                "DOI": meta.doi.source,
                "通讯作者": meta.corresponding_author.source,
                "附件链接": meta.supplementary_links.source,
            },
        },
    }


def collect(samples_dir: str, target_words: int = 200, table_mode: str = "image") -> dict:
    """采集一个目录下（含子目录）所有 PDF 的指纹"""
    paths = sorted(glob.glob(os.path.join(samples_dir, "**", "*.pdf"), recursive=True))
    snapshot = {"采集时间": time.strftime("%Y-%m-%d %H:%M:%S"),
                "样本目录": samples_dir, "样本数": len(paths), "表格模式": table_mode,
                "样本": {}}
    for path in paths:
        name = os.path.basename(path)
        try:
            snapshot["样本"][name] = fingerprint_one(path, target_words, table_mode)
            print(f"  [OK] {name}")
        except Exception as exc:
            snapshot["样本"][name] = {"采集失败": f"{type(exc).__name__}: {exc}"}
            print(f"  [!!] {name}：{type(exc).__name__}: {exc}")
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="采集输出指纹快照")
    parser.add_argument("--samples", default=DEFAULT_SAMPLES, help="样本目录")
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出 JSON 路径")
    parser.add_argument("--target-words", type=int, default=200, help="卡片目标词数")
    args = parser.parse_args()

    print(f"样本目录：{args.samples}")
    snapshot = collect(args.samples, args.target_words)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)

    print(f"\n共 {snapshot['样本数']} 篇，已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
