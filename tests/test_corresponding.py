r"""4.4-A 验收：通讯作者的「图标标记」识别（信封图标）与共同通讯作者

背景：npj Digital Medicine 这类期刊在通讯作者名字后面画一个**小信封图标**（矢量绘制），
而不是写 "Corresponding author" 文字；一篇论文可能有多位共同通讯作者。
只用文本规则永远看不到这种标记，所以改为「作者行带子里的小图形 + 左邻人名」来判。

本文件同时验证**负例**：没有图标标记的样本必须完全走原有逻辑（文字标注 / 邮箱反查），
结果与加入本功能之前一致——这是「更新不影响原有功能」的直接证据。

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

from utils import metadata       # noqa: E402
from utils import pdf_parser     # noqa: E402

SAMPLES = r"E:\segemented pdf reader\测试pdf"
NPJ = os.path.join(SAMPLES, "4.3测试", "自然互动中孤独症儿童面部表情动态的量化评估.pdf")
ELSEVIER = os.path.join(SAMPLES, "for 4.2", "1-s2.0-S002210312600020X-main.pdf")

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
    data = open(path, "rb").read()
    result = pdf_parser.parse_pdf(data, True, True, 200)
    import pymupdf
    doc = pymupdf.open(path)
    title = metadata.extract_title(doc[0], doc.metadata)
    names, _source, _note, positioned = metadata.extract_authors(result["blocks"], title.value)
    marked = metadata.find_icon_marked_authors(doc, positioned, names)
    meta = metadata.extract_metadata(data, result["blocks"])
    doc.close()
    return names, marked, meta


print("=" * 78)
print("① 目标样本：两位共同通讯作者由信封图标标记")
if not os.path.exists(NPJ):
    skip("npj 样本的全部断言")
else:
    names, marked, meta = analyze(NPJ)
    check("作者列表 8 位", len(names) == 8, repr(names))
    check("图标标记识别出 2 位作者",
          [name for name, _how in marked] == ["Wei Liu", "Shuang Liu"], repr(marked))
    check("通讯作者字段 = 两位共同通讯作者（顿号分隔）",
          meta.corresponding_author.value == "Wei Liu、Shuang Liu",
          repr(meta.corresponding_author.value))
    check("通讯作者来源标注为「图标标记」",
          "图标" in meta.corresponding_author.source, repr(meta.corresponding_author.source))
    check("通讯邮箱字段 = 两个邮箱，且顺序与图标一致",
          meta.corresponding_email.value == "lance1971@163.com、shuangliu@tju.edu.cn",
          repr(meta.corresponding_email.value))
    check("备注里写明了配对依据（便于人工核对）",
          "Wei Liu" in meta.corresponding_author.note
          and "shuangliu@tju.edu.cn" in meta.corresponding_author.note,
          meta.corresponding_author.note[:80])
    check("第一作者仍为 Minghao Du（未被本次改动影响）",
          meta.first_author.value == "Minghao Du", repr(meta.first_author.value))

print()
print("② 负例：没有图标标记的样本必须走原有逻辑")
if not os.path.exists(ELSEVIER):
    skip("Elsevier 样本的断言")
else:
    names, marked, meta = analyze(ELSEVIER)
    check("没有识别到图标标记（避免把徽标当图标）", marked == [], repr(marked))
    check("通讯作者仍由「通讯邮箱反查」得出（原有行为）",
          meta.corresponding_author.value == "Diane Pecher",
          repr(meta.corresponding_author.value))
    check("来源不是图标标记",
          "图标" not in meta.corresponding_author.source, repr(meta.corresponding_author.source))
    check("通讯邮箱仍为单个作者邮箱",
          meta.corresponding_email.value == "pecher@essb.eur.nl",
          repr(meta.corresponding_email.value))

print()
print("③ 负例升级：ORCID 圆徽章不能被当成信封图标（回归工具抓到的误判）")
NATURE = os.path.join(SAMPLES, "for 4.2", "s41562-026-02534-0.pdf")
if not os.path.exists(NATURE):
    skip("Nature 样本的断言")
else:
    names, marked, meta = analyze(NATURE)
    check("4 位作者都识别到了", len(names) == 4, repr(names))
    # 该样本作者行上确有 3 个 ORCID 圆形徽章（曾经被误判成 3 位通讯作者）+
    # 1 个真信封图标；但信封与左侧名字之间隔了 9.2pt（上标 2,6 前面有薄空格 U+2009），
    # 超出 ICON_GAP_PT=8.0 → 图标这一支**如实弃权**，改由原有的「邮箱反查」给出答案。
    # 结果是「既没有误判、答案也对」——比放宽间距去硬认更符合准确性优先。
    check("没有把 ORCID 圆徽章当成图标标记（弃权，交回原有逻辑）",
          marked == [], repr(marked))
    check("通讯作者 = Antti Oulasvirta（由邮箱反查得出，与加入本功能前一致）",
          meta.corresponding_author.value == "Antti Oulasvirta",
          repr(meta.corresponding_author.value))
    check("来源不是图标标记（说明走的是原有分支）",
          "图标" not in meta.corresponding_author.source, repr(meta.corresponding_author.source))
    emails = [item for item in meta.corresponding_email.value.split("、") if item]
    check("通讯邮箱没有重复配对（同一个邮箱只配给一位作者）",
          len(emails) == len(set(emails)), repr(meta.corresponding_email.value))
    check("通讯邮箱 = antti.oulasvirta@aalto.fi（未变）",
          emails == ["antti.oulasvirta@aalto.fi"], repr(meta.corresponding_email.value))

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed} · 跳过 {skipped}")
sys.exit(1 if failed else 0)
