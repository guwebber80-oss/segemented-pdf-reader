"""
本地规则 vs GROBID 的交叉校验（阶段 4.3）
================================================================
本模块【不依赖 Streamlit】，只做纯数据处理，可以单独测试。

它回答三个问题：
    ① 两边抽出来的字段是不是一致？（一致 → 可信度大增，用户不用管）
    ② 不一致时，谁更全 / 谁更干净？（给出可读结论，而不是只把两串字摆在用户面前）
    ③ 能不能只「补空」而不「覆盖」？（本地缺的项用 GROBID 补上，两边都有的保持本地）

设计原则（用户定下的）：
    · **准确性优先**：不做黑箱自动合并，只给「结论 + 一键采信」的选项，最终由人拍板；
    · 比对必须**归一化**（大小写、重音、空白、标点、序号），否则
      「Steve van Pelt」和「Steve Van Pelt」会被判成冲突，白白制造焦虑；
    · 每个字段都要说清楚「GROBID 到底有没有这个能力」——例如通讯作者与附件链接
      GROBID 不提供，界面上不能显示成「不一致」误导用户。
"""

import re
import unicodedata

# ------------------------------------------------------------
# 一、字段定义
# ------------------------------------------------------------
# kind: "scalar" 单值 ｜ "list" 多行列表 ｜ "unsupported" GROBID 不提供
# session_key 与 app.py 里可编辑输入框的 key 对应，供「一键采信」直接写回
FIELD_SPECS = [
    {"key": "title", "label": "标题", "kind": "scalar", "session_key": "in_title"},
    {"key": "doi", "label": "DOI", "kind": "scalar", "session_key": "in_doi"},
    {"key": "abstract", "label": "摘要", "kind": "scalar", "session_key": "in_abstract"},
    {"key": "authors", "label": "作者列表", "kind": "list", "session_key": "in_authors"},
    {"key": "first_author", "label": "第一作者", "kind": "scalar",
     "session_key": "in_first_author"},
    {"key": "affiliations", "label": "作者单位", "kind": "list",
     "session_key": "in_affiliations"},
    {"key": "author_emails", "label": "作者邮箱", "kind": "list",
     "session_key": "in_author_emails"},
    {"key": "corresponding_author", "label": "通讯作者", "kind": "unsupported",
     "session_key": "in_corresponding",
     "hint": "GROBID 不识别通讯作者（它只给作者与通讯邮箱），此项只能靠本地规则或人工填写"},
    {"key": "supplementary_links", "label": "附件链接", "kind": "unsupported",
     "session_key": "in_supplementary",
     "hint": "GROBID 只处理论文头部，不提供附件/数据链接"},
]


# 被排版拆开的重音：GROBID 与部分 PDF 会把 "Angéline" 写成 "Ange ´lina"
# （尖音符单独成一个字符，后面还跟空格）。归一化时必须一并处理，
# 否则同一个人会被判成「两边各有独有项」，白白制造告警。
_SPLIT_ACCENT_RE = re.compile(r"(?<=[A-Za-z])\s*[´`]\s*")


def _strip_accents(text: str) -> str:
    """去重音：Angéline 与 Angeline 视为同一个名字（并修复被拆开的 ´ ）"""
    text = _SPLIT_ACCENT_RE.sub("\u0301", text or "")
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """
    比对用的归一化：**先去序号标记 → 再去重音 → 转小写 → 去标点 → 压缩空白**。

    例：`¹ Institute for Behavioral Genetics, University of Colorado` 与
        `Institute for Behavioral Genetics University of Colorado`
    归一化后相同，不会因为序号和逗号被判成冲突。

    注意顺序：**必须先去序号再去重音**。NFKD 会把上标 `¹` 拆成普通 `1`，
    如果先做 NFKD，后面的「上下标字符」正则就再也匹配不上了。
    """
    text = text or ""
    text = re.sub(r"[\u2070-\u209f\u00b9\u00b2\u00b3]", " ", text)      # 上下标序号
    text = re.sub(r"^\s*\d{1,2}\s*(?=[A-Za-z(])", "", text)            # 开头的 "1 " / "12 " 序号
    text = _strip_accents(text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def normalize_items(items) -> list:
    """列表归一化（去空行、去重、保持原顺序）"""
    seen, result = set(), []
    for item in items or []:
        key = normalize(item)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def compare_items(local_items, grobid_items) -> dict:
    """
    比较两个列表，返回：
        {added: [GROBID 有、本地没有的（原始写法）],
         missing: [本地有、GROBID 没有的（原始写法）],
         same: 两边都有的条数}
    """
    local_keys = normalize_items(local_items)
    grobid_keys = normalize_items(grobid_items)
    added = [item for item in grobid_items
             if normalize(item) and normalize(item) not in set(local_keys)]
    missing = [item for item in local_items
               if normalize(item) and normalize(item) not in set(grobid_keys)]
    # 去掉 added/missing 内部的归一化重复
    dedup_added, seen = [], set()
    for item in added:
        if normalize(item) not in seen:
            seen.add(normalize(item))
            dedup_added.append(item)
    dedup_missing, seen = [], set()
    for item in missing:
        if normalize(item) not in seen:
            seen.add(normalize(item))
            dedup_missing.append(item)
    return {"added": dedup_added, "missing": dedup_missing,
            "same": len(set(local_keys) & set(grobid_keys))}


def compare_text(local_text: str, grobid_text: str) -> str:
    """两个单值字段的关系：一致 / 只有本地 / 只有 GROBID / 内容不同"""
    local_norm = normalize(local_text)
    grobid_norm = normalize(grobid_text)
    if not local_norm and not grobid_norm:
        return "两边都没有"
    if not grobid_norm:
        return "只有本地"
    if not local_norm:
        return "只有 GROBID"
    if local_norm == grobid_norm:
        return "一致"
    if local_norm in grobid_norm:
        return "GROBID 更全"
    if grobid_norm in local_norm:
        return "本地更全"
    return "内容不同"


def _local_emails(local_meta) -> list:
    """
    本地规则抽到的全部邮箱：作者区逐个配对的那个 + 通讯邮箱（可能来自 mailto 链接或通讯段落）。

    为什么要合并再比：GROBID 头部抽取通常只给「通讯邮箱」，而本地的 `author_emails`
    只覆盖「作者区逐作者列邮箱」的排版（ACM 那类）。只拿后者去比，
    会把「Elsevier/SAGE 这类本地有通讯邮箱、只是放在另一个字段」的情况误判成
    「GROBID 更全」，制造假差异。
    """
    if not local_meta:
        return []
    emails = [line.strip() for line in (local_meta.author_emails.value or "").splitlines()
              if line.strip()]
    corresponding = (local_meta.corresponding_email.value or "").strip()
    if corresponding and not any(corresponding.lower() in item.lower() for item in emails):
        emails.append(corresponding)
    return emails


def build_comparison(local_meta, grobid: dict) -> list:
    """
    产出逐字段对比行，供界面直接渲染成表格。

    local_meta：metadata.Metadata（4.1/4.2 的结果）
    grobid     ：grobid_client.parse_tei_header 的结果
    返回 [{字段, 本地, GROBID, 结论, 新增(GROBID 独有的条目), 缺失(本地独有), 可采信}]
    """
    grobid = grobid or {}
    grobid_authors = [person.get("name", "") for person in grobid.get("authors", [])
                      if person.get("name")]
    grobid_emails = [person.get("email") for person in grobid.get("authors", [])
                     if person.get("email")]
    grobid_orcids = [person.get("orcid") for person in grobid.get("authors", [])
                     if person.get("orcid")]

    values = {
        "title": (local_meta.title.value if local_meta else "", grobid.get("title", "")),
        "doi": (local_meta.doi.value if local_meta else "", grobid.get("doi", "")),
        "abstract": (local_meta.abstract.value if local_meta else "",
                     grobid.get("abstract", "")),
        "authors": (local_meta.author_list() if local_meta else [], grobid_authors),
        "first_author": ((local_meta.author_list() or [""])[0] if local_meta else "",
                         grobid_authors[0] if grobid_authors else ""),
        "affiliations": (local_meta.affiliation_list() if local_meta else [],
                         grobid.get("affiliations", [])),
        "author_emails": (_local_emails(local_meta), grobid_emails),
    }

    rows = []
    for spec in FIELD_SPECS:
        row = {"字段": spec["label"], "字段键": spec["key"], "类型": spec["kind"],
               "会话键": spec["session_key"], "可采信": False,
               "新增": [], "缺失": [], "提示": spec.get("hint", "")}
        if spec["kind"] == "unsupported":
            local_value, _ = values.get(spec["key"], ("", ""))
            row["本地"] = _as_text(local_value)
            row["GROBID"] = "（不提供）"
            row["结论"] = "GROBID 不提供此字段"
            rows.append(row)
            continue

        local_value, grobid_value = values.get(spec["key"], ("", ""))
        if spec["kind"] == "list":
            diff = compare_items(local_value, grobid_value)
            row["本地"] = _as_text(local_value)
            row["GROBID"] = _as_text(grobid_value)
            # 「一键采信」要把**完整值**写回输入框，所以原始值单独留一份
            # （表格里显示的是截断后的版本，直接拿它写回会把内容截掉）
            row["本地原始"] = local_value
            row["GROBID原始"] = grobid_value
            row["新增"] = diff["added"]
            row["缺失"] = diff["missing"]
            if not local_value and not grobid_value:
                row["结论"] = "两边都没有"
            elif local_value and not grobid_value:
                row["结论"] = "只有本地"
            elif grobid_value and not local_value:
                row["结论"] = "只有 GROBID"
            elif not diff["added"] and not diff["missing"]:
                row["结论"] = f"一致（各 {len(normalize_items(local_value))} 条）"
            elif diff["added"] and not diff["missing"]:
                row["结论"] = f"GROBID 多 {len(diff['added'])} 条"
            elif diff["missing"] and not diff["added"]:
                row["结论"] = f"本地多 {len(diff['missing'])} 条"
            else:
                row["结论"] = (f"两边各有独有项（GROBID 多 {len(diff['added'])} 条、"
                               f"本地多 {len(diff['missing'])} 条）")
            row["可采信"] = bool(diff["added"])
        else:
            row["本地"] = _shorten(local_value)
            row["GROBID"] = _shorten(grobid_value)
            row["本地原始"] = local_value
            row["GROBID原始"] = grobid_value
            row["结论"] = compare_text(local_value, grobid_value)
            row["可采信"] = bool(grobid_value) and \
                normalize(local_value) != normalize(grobid_value)
        rows.append(row)

    # 顺带把 ORCID 作为附加信息返回（GROBID 独有、本地不做）
    if grobid_orcids:
        rows.append({"字段": "ORCID（附加）", "字段键": "orcid", "类型": "extra",
                     "会话键": "", "本地": "（本地不做）",
                     "GROBID": _as_text(grobid_orcids), "结论": "GROBID 独有",
                     "新增": [], "缺失": [], "可采信": False, "提示": ""})
    return rows


def _as_text(value) -> str:
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value)
    return str(value or "")


def _shorten(text: str, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit] + "…" if len(text) > limit else text


def merge_missing(local_text: str, grobid_items) -> tuple:
    """
    「只补空、不覆盖」：把 GROBID 里本地没有的条目追加进本地列表文本。
    返回 (新文本, 实际新增的条目)。
    """
    local_items = [line.strip() for line in (local_text or "").splitlines() if line.strip()]
    local_keys = set(normalize_items(local_items))
    added = []
    for item in grobid_items or []:
        key = normalize(item)
        if not key or key in local_keys:
            continue
        local_keys.add(key)
        added.append(item)
    if not added:
        return local_text, []
    merged = local_items + added
    return "\n".join(merged), added
