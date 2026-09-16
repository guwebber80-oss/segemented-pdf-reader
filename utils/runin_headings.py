"""
utils.runin_headings —— 「行内小标题」补丁的纯函数层（阶段 4.5）
================================================================
本模块【不依赖 Streamlit】，也不依赖项目里任何其它模块：全是纯函数、无副作用、
不发网络请求，因此可以完全离线单测（见 tests/test_runin_headings.py）。

背景（实测见项目根《GROBID对比结论.md》）：
    有一类小标题叫「行内小标题」（run-in heading）——形如

        Word recognizer.  Here we describe the word recognizer used …

    它**以句点结尾、与后文同处一段**，字号与正文完全一样，
    所以纯字号 + 版式规则天然抓不到（我们的 title 规则要求字号更大或整行加粗）。
    GROBID 的 TEI 能把这类标题标出来（`<head>Word recognizer.</head>`），
    所以做法是：拿 GROBID 的 head 当**候选**，用下面几条规则狠狠过滤一遍，
    再在段落视图里把「标题 + 正文」拆开——这就是「小标题补丁」。

    过滤规则（四条同时满足才算）：
        ① 词数 ≤ 12            太长的多半是整句正文，不是标题；
        ② 以「.」结尾           行内小标题的典型特征，一条就能滤掉 `Fig. 1 |` 这类；
        ③ 开头不是 Fig / Table / Extended Data / Supplementary
                               GROBID 实测把图注、表注、扩展数据声明也写成 head；
        ④ 至少含一个真实字母词 滤掉 `'s 60 s 90 s a Heat maps'` 这种纯垃圾串。

设计约定（与「稳定性优先于功能完整」一致）：
    · 只做**加法**：这个补丁默认关闭（`runin_heads=None`），关掉时卡片结构与改动前完全一致；
    · 只动**派生视图**：句子拆分只发生在阅读卡片的 segments 上，
      原子块（Block.text）与卡片正文（card.text）一个字符都不改，所以词数口径不变；
    · 全部函数对「空值 / 畸形输入」返回安全值（None / False / []），调用方不必先判空。
"""

import re
import unicodedata

# 行内小标题的词数上限：实测这类标题都在 2~8 词（`Reading under time pressure.`），
# 放到 12 是为了给「带限定语的标题」留余量，同时把整句正文挡在外面。
DEFAULT_MAX_WORDS = 12

# 【噪音前缀】图上标注、表格、扩展数据、补充材料——GROBID 实测会把它们写成 <head>：
#   `Fig. 1 |` `Fig. 2 |` `. 1 |` `Extended Data` `Extended Data Fig. 3 | …` `Supplementary Table 1`
# 判定方式：开头是这几个词，且紧接着是「.」「数字」「空白」或就此结束。
# 后面必须紧跟这几种字符，否则 `Figures.`（真正的行内小标题）会被误杀。
_NOISE_PREFIX = re.compile(
    r"^(?:fig|table|extended\s+data|supplementary)(?:\.|\d|\s|$)",
    re.IGNORECASE,
)

# 一个「字母」：`[^\W\d_]` 即「是词字符、但不是数字、也不是下划线」，与语言无关
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def _normalize(text: str) -> str:
    """
    归一化：NFKC（把全角字母、连字 ﬁ 之类拉回标准形式）→ 压缩空白 → 去首尾空白 → 转小写。

    只用于**比较**，绝不用于输出——输出一律用原文里拼出来的字符串，
    这样卡片上显示的标题与 PDF 里的写法一模一样。
    """
    replaced = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", replaced).strip().lower()


def _words(text: str) -> list:
    """按空白切词（与卡片词数统计同一口径：空白分词，够用）"""
    return [word for word in re.split(r"\s+", (text or "").strip()) if word]


def has_real_word(text: str) -> bool:
    """
    是否至少含一个「≥2 个字母的真实词」。

    用途：滤掉 `'s 60 s 90 s a Heat maps'` 这类由单字母、纯数字、符号拼起来的串——
    它们一个真正的英文词都没有（`s`、`60`、`a` 都不算）。
    """
    for word in _words(text):
        if len(_LETTER.findall(word)) >= 2:
            return True
    return False


def is_runin_heading(text: str, max_words: int = DEFAULT_MAX_WORDS) -> bool:
    """
    一个 GROBID head 是不是「行内小标题」候选。四条规则**同时**满足才为真：

        ① 词数 ≤ max_words（默认 12）
        ② 以「.」结尾
        ③ 开头不是 Fig / Table / Extended Data / Supplementary
        ④ 至少含一个真实字母词

    任何一条不满足都返回 False（宁可漏掉，不可放进噪音——放进来会在卡片里多一条假标题）。
    """
    text = (text or "").strip()
    if not text:
        return False
    if len(_words(text)) > max_words:                      # ①
        return False
    if not text.endswith("."):                             # ②
        return False
    if _NOISE_PREFIX.match(_normalize(text)):               # ③
        return False
    return has_real_word(text)                             # ④


def filter_runin_heads(heads) -> list:
    """
    批量过滤 GROBID 的 head 列表，返回可用的行内小标题（保持原顺序、去重）。
    空白会被压缩（TEI 里换行缩进很常见），但**大小写与标点保持原样**。
    """
    out = []
    for head in heads or []:
        text = re.sub(r"\s+", " ", str(head or "")).strip()
        if text and text not in out and is_runin_heading(text):
            out.append(text)
    return out


def match_runin(paragraph_text: str, heads) -> str:
    """
    段落是否**以**某个 head 开头（归一化后比较：NFKC、空白折叠、忽略大小写）。

    多个都命中时返回**最长**的那个——否则 `Methods` 会抢先匹配掉
    `Methods and materials.` 这种更具体的标题，切出来的正文就少了半句。
    都不命中返回 None。
    """
    normalized = _normalize(paragraph_text)
    if not normalized:
        return None

    best, best_length = None, 0
    for head in heads or []:
        text = re.sub(r"\s+", " ", str(head or "")).strip()
        candidate = _normalize(text)
        if not candidate or not normalized.startswith(candidate):
            continue
        if len(candidate) > best_length:
            best, best_length = text, len(candidate)
    return best


def split_runin(paragraph_text: str, heads):
    """
    把段落切成 (标题, 剩余正文)；没命中、或剩余正文为空时返回 None。

    返回的标题用**原文里的写法**（大小写、标点原样），剩余正文去掉前导空白。
    「剩余为空」必须返回 None：否则 `Word recognizer.` 这种整段就是一个标题的段落
    会被拆成「标题 + 空正文」，卡片上就只剩一行加粗文字，比不拆更糟。
    """
    matched = match_runin(paragraph_text, heads)
    if matched is None:
        return None

    text = paragraph_text or ""
    # 优先用「正则 + 忽略大小写」在原段落里定位：命中区间直接取自原文，
    # 于是 heading 一定是原文写法（这也是测试里 `Word recognizer.` 能原样恢复的原因）。
    parts = [re.escape(part) for part in _normalize(matched).split(" ")]
    found = re.match(r"^\s*" + r"\s+".join(parts), text, re.IGNORECASE)
    if found:
        heading, rest = text[:found.end()], text[found.end():]
    else:
        # 兜底：NFKC 可能改动了字符（`ﬁ` → `fi`），这时正则对不上原文，
        # 但**词数不会变**，所以按词数在原文里切，标题仍是原文写法。
        tokens = re.split(r"(\s+)", text.strip())
        count = 2 * len(_normalize(matched).split(" ")) - 1
        heading, rest = "".join(tokens[:count]), "".join(tokens[count:])

    heading, rest = heading.strip(), rest.strip()
    if not heading or not rest:
        return None
    return heading, rest
