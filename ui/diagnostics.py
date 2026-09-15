"""
ui.diagnostics —— 论文背景信息面板与解析诊断面板（阶段 5 从 app.py 拆出）

诊断面板是「自查工具」：每页排版、段落明细、公式区域、表格区域都能就地核对，
用来定位「某段为什么没进卡片」「某张图框多了」这类问题。
"""

import streamlit as st

from utils.pdf_parser import (
    ROLE_AUTHOR,
    ROLE_COPYRIGHT,
    ROLE_FRONT_MATTER,
    ROLE_HEADER_FOOTER,
    ROLE_LABEL,
    ROLE_NAMES,
    ROLE_REFERENCE,
    count_words,
    escape_markdown,
)

BACKGROUND_GROUPS = [
    ("✍️ 作者与单位", (ROLE_AUTHOR, ROLE_FRONT_MATTER),
     "作者名、单位、联系方式，以及封面页上的 Highlights / In Brief 等导读内容"),
    ("📰 期刊信息与栏目", (ROLE_COPYRIGHT, ROLE_LABEL),
     "期刊名与栏目名（Report / Highlights / CCS Concepts…）、版权与许可声明、资助信息、"
     "以及图内的面板标签"),
    ("📚 参考文献", (ROLE_REFERENCE,), "参考文献列表（保持原文顺序）"),
    ("📄 页眉页脚", (ROLE_HEADER_FOOTER,), "每页重复出现的 running head、页码、页脚"),
]


def render_background_panel(background_blocks):
    st.subheader("📎 论文背景信息")
    st.caption(
        f"下面 {len(background_blocks)} 段**不属于正文**，所以没有进阅读卡片。"
        "如果发现哪一段其实是正文，打开侧边栏的「显示全部内容」就能把它找出来。"
    )

    for group_title, group_roles, group_hint in BACKGROUND_GROUPS:
        group_items = [b for b in background_blocks if b.role in group_roles]
        if not group_items:
            continue
        with st.expander(f"{group_title}（{len(group_items)} 段）", expanded=False):
            st.caption(group_hint)
            for b in group_items:
                text = b.text.strip().replace("\n", " ")
                if len(text) > 400:
                    text = text[:400] + "…"
                st.markdown(f"- 第 {b.page} 页 · {escape_markdown(text)}")




_COLUMN_NAME = {-1: "通栏", 0: "左栏", 1: "右栏"}


def render_diagnostics(ASSOC_MAX_GAP, association, blocks, clusters, image_result, images,
                       metadata, pages, paragraphs, result, table_regions):
    with st.expander("🔍 解析诊断（验证阅读顺序、定位双栏错位）"):
        st.markdown("**⓪ 内容角色统计**（正文与图注进阅读卡片，其余归入「论文背景信息」）")
        st.dataframe([{
            "角色": ROLE_NAMES.get(role, role),
            "块数": count,
            "去向": "进卡片流" if role in ("body", "caption", "heading") else "→ 背景信息",
        } for role, count in sorted(result["roles"].items(), key=lambda kv: -kv[1])],
            hide_index=True)

        st.markdown("**① 每页排版检测**")
        st.dataframe(pages, hide_index=True)

        st.markdown("**② 段落明细**（已按程序认定的阅读顺序编号，从上往下就是卡片顺序）")
        rows = [{
            "序号": b.order,
            "页码": b.page,
            "类型": "标题" if b.kind == "heading" else "正文",
            "角色": ROLE_NAMES.get(b.role, b.role),
            "栏": _COLUMN_NAME[b.column],
            "y0": round(b.y0, 1),
            "x0": round(b.x0, 1),
            "字号": round(b.max_size, 1),
            "粗体占比": round(b.bold_ratio, 2),
            "词数": count_words(b.text),
            "拼了块": len(getattr(b, "atom_indices", []) or []),
            "公式": b.formula_tier or ("簇%d" % b.cluster_id if b.cluster_id >= 0 else "-"),
            "开头 40 字": b.text[:40].replace("\n", " "),
        } for b in paragraphs]
        st.dataframe(rows, hide_index=True, height=420)

        # 双栏页里，正常只有标题/摘要/跨栏图注会横跨中缝。
        # 如果这里出现大段正文，说明分栏判断可能出错，需要人工核对。
        spanning = [b for b in paragraphs
                    if b.column == -1 and b.kind == "body" and count_words(b.text) > 120]
        if spanning:
            st.markdown("**③ 疑似问题：跨栏的长正文块**（双栏页里出现这些，说明分栏可能有误）")
            for b in spanning:
                st.write(f"#{b.order} 第{b.page}页 · {count_words(b.text)} 词 · {b.text[:60]}…")
        else:
            st.success("③ 未发现异常：没有「跨栏的长正文块」。")

        st.caption(
            "自查方法：看 ① 表的「检测排版」是否与你的 PDF 相符，"
            "再对照片子里的正文，看 ② 表的顺序是不是「先把左栏读完，再读右栏」。"
        )

        st.markdown("**④ 图片与卡片的关联情况**")
        if not images:
            st.caption("这个 PDF 没有提取到位图插画（矢量图不算）。")
        else:
            st.write(
                f"共 {len(images)} 张 · 可靠关联 **{association['关联可靠']}** 张 · "
                f"存疑 {association['关联存疑']} 张 · 未关联 {association['未关联']} 张"
            )
            problems = association["未关联列表"] + association["存疑列表"]
            if problems:
                st.markdown(
                    f"下面 **{len(problems)}** 张没能可靠关联到文本卡片"
                    "（关联方式：取同页距离最近的文本块；距离超过 "
                    f"{ASSOC_MAX_GAP:.0f}pt 或同页无文本就算不可靠）："
                )
                st.dataframe([{
                    "图片": img.key,
                    "页码": img.page,
                    "位置": f"({img.bbox[0]:.0f}, {img.bbox[1]:.0f})",
                    "与最近卡片距离": f"{img.gap} pt" if img.gap >= 0 else "同页没有文本块",
                } for img in problems], hide_index=True)
            else:
                st.success("所有图片都关联到了文本卡片。")

        if image_result["skipped"]:
            st.markdown(f"**⑤ 被跳过的图片（{len(image_result['skipped'])} 张）**")
            st.dataframe(image_result["skipped"], hide_index=True)

        st.markdown("**⑥ 元数据提取详情**（自动提取的原始值与来源，方便对照你在上面改过的内容）")
        st.dataframe([{
            "字段": name,
            "来源": meta_field.source,
            "自动提取值": (meta_field.value[:70] + "…") if len(meta_field.value) > 70 else meta_field.value,
            "备注": meta_field.note,
        } for name, meta_field in [("标题", metadata.title),
                                   ("DOI", metadata.doi),
                                   ("摘要", metadata.abstract),
                                   ("作者列表", metadata.authors),
                                   ("第一作者", metadata.first_author),
                                   ("通讯作者", metadata.corresponding_author),
                                   ("通讯邮箱", metadata.corresponding_email),
                                   ("作者单位", metadata.affiliations),
                                   ("作者邮箱", metadata.author_emails),
                                   ("附件链接", metadata.supplementary_links)]], hide_index=True)
        if metadata.title.alternatives:
            st.caption(f"标题备选（来自 PDF 内嵌元数据，可自行取舍）：{metadata.title.alternatives}")
        if metadata.supplementary_items:
            st.markdown("**⑥-2 附件链接的证据**（页码 + 抓取依据）")
            st.dataframe(metadata.supplementary_items, hide_index=True)

        st.markdown(f"**⑦ 公式区域明细（{len(clusters)} 个）**"
                    "——每个区域渲染成一张图，右边列出被合进来的块，方便核对有没有合错")
        if not clusters:
            st.caption("这个 PDF 没有识别到数学公式区域（正文里的行内统计量仍以文字呈现）。")
        else:
            block_by_order = {b.order: b for b in blocks}
            st.dataframe([{
                "区域": f"#{cluster.cluster_id}",
                "页码": cluster.page,
                "类型": "算法清单" if cluster.kind == "listing" else "公式",
                "成员块数": cluster.member_count,
                "区域范围": f"({cluster.rect[0]:.0f}, {cluster.rect[1]:.0f})"
                            f"–({cluster.rect[2]:.0f}, {cluster.rect[3]:.0f})",
                "成员块（#阅读顺序号:文本）": " | ".join(
                    f"#{block_by_order[i].order}:{block_by_order[i].text.strip()[:22]}"
                    for i in cluster.atom_indices if i in block_by_order),
            } for cluster in clusters], hide_index=True)
            st.caption(
                "核对方法：区域范围应当刚好框住一处公式；成员块里如果混进了正文句子，"
                "把这一页的截图和一个具体例子发给我，我按这个明细来调。"
            )

        st.markdown(f"**⑧ 表格区域明细（{len(table_regions)} 个）**"
                    "——每张表整块截图；判据是「题注锚点 + ≥3 列 × ≥3 行的对齐网格」")
        if not table_regions:
            st.caption("这个 PDF 没有识别到表格区域"
                       "（2 列表格、跨页续表按设计不判；可用侧边栏「表格呈现 → 保留文字」对照）。")
        else:
            block_by_order = {b.order: b for b in blocks}
            st.dataframe([{
                "区域": f"T{region.region_id}",
                "页码": region.page,
                "判据": "题注" if region.kind == "captioned" else "几何兜底（无题注）",
                "行×列": f"{region.rows} × {region.columns}",
                "表注行": region.notes,
                "区域范围": f"({region.rect[0]:.0f}, {region.rect[1]:.0f})"
                            f"–({region.rect[2]:.0f}, {region.rect[3]:.0f})",
                "题注": region.caption[:40],
                "成员块（#阅读顺序号:文本）": " | ".join(
                    f"#{block_by_order[i].order}:{block_by_order[i].text.strip()[:18]}"
                    for i in region.atom_indices if i in block_by_order),
            } for region in table_regions], hide_index=True)
            st.caption(
                "核对方法：截图应当刚好框住「题注 + 表格本体」；如果框进了正文段、"
                "或者漏掉了某张表，把那一页截图发我，我按这个明细调判据。"
            )


