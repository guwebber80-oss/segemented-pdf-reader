"""
ui.metadata_panel —— 文献元数据面板与 GROBID 交叉校验面板（阶段 5 从 app.py 拆出）

元数据十个字段都可编辑、各自标来源；GROBID 面板把本地结果与外部模型并排对照，
采信按钮**只补空、不覆盖**（逻辑在 utils/metadata_compare.py）。
这里只负责渲染与交互；字段怎么来的由 utils/metadata.py 决定。
"""

import os

import streamlit as st

from utils import grobid_client, metadata_compare
from utils.metadata import Metadata
from utils.pdf_parser import escape_markdown

# GROBID 服务地址：默认本机 8070，可用环境变量 GROBID_URL 覆盖
# （原来由 app.py 传进来，阶段 5 改成模块内自己算——面板不该依赖调用方准备常量）
GROBID_BASE_URL = os.environ.get("GROBID_URL", grobid_client.DEFAULT_BASE_URL)


def metadata_input(label: str, widget_key: str, meta_field, is_long: bool = False,
                   height: int = 170):
    """
    一个可编辑的元数据字段 + 它下方的来源说明。

    输入框的值存在 session_state 里，所以用户的修改会跨重跑保留下来，
    不会被 Streamlit 的「每次交互重跑整个脚本」冲掉。
    """
    if widget_key not in st.session_state:
        st.session_state[widget_key] = meta_field.value

    if meta_field.found:
        if is_long:
            st.text_area(label, key=widget_key, height=height)
        else:
            st.text_input(label, key=widget_key)
        caption = f"来源：{meta_field.source}"
        if meta_field.note:
            caption += f"　⚠️ {meta_field.note}"
        st.caption(caption)
    else:
        if is_long:
            st.text_area(f"{label}（未找到，请手动输入）", key=widget_key, height=height)
        else:
            st.text_input(f"{label}（未找到，请手动输入）", key=widget_key)
        st.caption(f"⚠️ {meta_field.note or '自动提取失败，请手动填写'}")


def render_metadata_panel(metadata):
    st.subheader("📋 文献元数据")

    head_left, head_right = st.columns([4, 1])
    with head_left:
        st.caption(
            "自动提取只是**候选值**。科研场景下准确性优先于自动化——"
            "请核对后直接修改，改动立即生效。"
        )
    with head_right:
        if st.button("↺ 用自动提取结果覆盖", help="把手动修改过的内容还原成程序提取的值"):
            st.session_state["in_title"] = metadata.title.value
            st.session_state["in_doi"] = metadata.doi.value
            st.session_state["in_abstract"] = metadata.abstract.value
            st.session_state["in_authors"] = metadata.authors.value
            st.session_state["in_first_author"] = metadata.first_author.value
            st.session_state["in_corresponding"] = metadata.corresponding_author.value
            st.session_state["in_corresponding_email"] = metadata.corresponding_email.value
            st.session_state["in_affiliations"] = metadata.affiliations.value
            st.session_state["in_supplementary"] = metadata.supplementary_links.value
            st.session_state["in_author_emails"] = metadata.author_emails.value
            st.rerun()

    if st.session_state.get("meta_error"):
        st.error("元数据提取失败：" + st.session_state["meta_error"])

    col_title, col_doi = st.columns([2, 1])
    with col_title:
        metadata_input("标题", "in_title", metadata.title)
    with col_doi:
        metadata_input("DOI", "in_doi", metadata.doi)

    metadata_input("摘要", "in_abstract", metadata.abstract, is_long=True)

    # ---- 阶段 4.2：作者 / 单位 / 通讯作者 / 附件链接 ----
    st.markdown("##### 👥 作者与单位")

    col_first, col_corr, col_mail = st.columns([1, 1, 1])
    with col_first:
        metadata_input("第一作者", "in_first_author", metadata.first_author)
    with col_corr:
        metadata_input("通讯作者", "in_corresponding", metadata.corresponding_author)
    with col_mail:
        metadata_input("通讯邮箱", "in_corresponding_email", metadata.corresponding_email)

    metadata_input("作者列表（每行一位，保持原文顺序）", "in_authors", metadata.authors,
                   is_long=True, height=130)
    metadata_input("作者单位（每行一个，保留原文编号）", "in_affiliations", metadata.affiliations,
                   is_long=True, height=130)

    with st.expander("📧 作者邮箱（逐作者列出，ACM 等排版常见）", expanded=False):
        metadata_input("作者邮箱", "in_author_emails", metadata.author_emails,
                       is_long=True, height=110)

    st.markdown("##### 📎 补充材料 / 数据链接")
    metadata_input("附件链接（每行一个 URL）", "in_supplementary",
                   metadata.supplementary_links, is_long=True, height=90)
    if metadata.supplementary_items:
        for item in metadata.supplementary_items:
            st.markdown(f"- **{item['类型']}**（第 {item['页码']} 页）："
                        f"[{item['链接']}]({item['链接']})　"
                        f"<span style='color:#888'>依据：{escape_markdown(item['上下文'][:70])}</span>",
                        unsafe_allow_html=True)
        st.caption("上面的分类是按链接域名与上下文关键词判的，可能有偏差；"
                   "链接本身都是从 PDF 的链接注释或正文里抓出来的，可以直接点开核对。")




def grobid_status(force: bool = False):
    """探测 GROBID 服务（结果缓存在会话里，避免每次交互都等网络超时）"""
    if force or "grobid_alive" not in st.session_state:
        alive, message = grobid_client.is_alive(GROBID_BASE_URL)
        st.session_state["grobid_alive"] = alive
        st.session_state["grobid_message"] = message
        st.session_state["grobid_version"] = grobid_client.server_version(GROBID_BASE_URL) \
            if alive else ""
    return st.session_state["grobid_alive"], st.session_state["grobid_message"]


def render_grobid_panel(meta_key, metadata, pdf_bytes, uploaded_file):
    st.markdown("##### 🔍 GROBID 交叉校验")
    st.caption(
        "GROBID 是专门抽取论文头部信息的外部模型，这里把它当作**第二意见**："
        "两边结果一致说明可信度高；不一致时并排显示，你可以逐字段决定采信谁。"
        "它**不提供**通讯作者与附件链接（界面上会如实标注）。"
    )

    grobid_alive, grobid_message = grobid_status()

    if not grobid_alive:
        st.info(
            f"{grobid_message}\n\n"
            "需要时用这条命令启动（保持窗口开着即可）：\n"
            "```\ndocker run --rm --init --ulimit core=0 -p 8070:8070 grobid/grobid:0.9.1-crf\n```\n"
            "没启动也**不影响任何阅读功能**——上面的字段仍然是本地规则的结果。"
        )
    else:
        if st.session_state.get("grobid_key") != meta_key:
            # 换文件：清掉上一个文件的校验结果，并重新采一次（服务已在则自动跑）
            for key in ("grobid_result", "grobid_error", "grobid_error_detail"):
                st.session_state.pop(key, None)
            st.session_state["grobid_key"] = meta_key

        col_run, col_ver = st.columns([3, 2])
        with col_run:
            run_clicked = st.button("🔍 运行 GROBID 校验", key="grobid_run")
        with col_ver:
            st.caption(f"服务：{grobid_message}"
                       + (f" · 版本 {st.session_state.get('grobid_version', '')[:32]}"
                          if st.session_state.get("grobid_version") else ""))

        if (run_clicked or "grobid_result" not in st.session_state) \
                and not st.session_state.get("grobid_error_detail"):
            with st.spinner("正在调用 GROBID 抽取头部信息（首次约 5~10 秒）…"):
                try:
                    st.session_state["grobid_result"] = grobid_client.header_from_pdf(
                        pdf_bytes, base_url=GROBID_BASE_URL, filename=uploaded_file.name)
                    st.session_state["grobid_error"] = None
                except grobid_client.GrobidError as exc:
                    st.session_state["grobid_result"] = None
                    st.session_state["grobid_error"] = str(exc)
                except Exception as exc:                     # 兜底：任何异常都不能打断阅读
                    st.session_state["grobid_result"] = None
                    st.session_state["grobid_error"] = f"{type(exc).__name__}: {exc}"

        grobid_result = st.session_state.get("grobid_result")

        if st.session_state.get("grobid_error"):
            st.warning("GROBID 调用失败：" + st.session_state["grobid_error"])
        elif grobid_result:
            st.caption(grobid_client.summarize(grobid_result))

            rows = metadata_compare.build_comparison(metadata, grobid_result)
            st.dataframe([{"字段": row["字段"], "本地规则": row["本地"],
                           "GROBID": row["GROBID"], "结论": row["结论"]}
                          for row in rows], hide_index=True, height=260)

            actionable = [row for row in rows if row["可采信"]]
            if actionable:
                st.markdown("**一键采信**（写回上面的输入框，可再手动修改）")

                # 注意：这里必须用**回调**而不是就地赋值。
                # Streamlit 不允许在控件实例化之后再改它的 session_state，
                # 而本面板位于输入框下方；回调在脚本重跑之前执行，因此不受此限制。
                def take_grobid_scalar(target_key: str, value: str):
                    st.session_state[target_key] = value

                def take_grobid_list(target_key: str, items: list):
                    current = st.session_state.get(target_key, "")
                    merged, added = metadata_compare.merge_missing(current, items)
                    if added:
                        st.session_state[target_key] = merged
                        st.session_state["grobid_take_note"] = (
                            f"已补进 {len(added)} 条：{'、'.join(str(x)[:24] for x in added[:3])}"
                            + ("…" if len(added) > 3 else ""))

                for index, row in enumerate(actionable):
                    name = row["字段"]
                    if row["类型"] == "list":
                        label = f"用 GROBID 补进 {len(row['新增'])} 条{name}"
                        st.button(label, key=f"grobid_take_{index}_{row['字段键']}",
                                  on_click=take_grobid_list,
                                  args=(row["会话键"], row["新增"]))
                    else:
                        label = f"采信 GROBID 的{name}"
                        st.button(label, key=f"grobid_take_{index}_{row['字段键']}",
                                  on_click=take_grobid_scalar,
                                  args=(row["会话键"], row.get("GROBID原始", row["GROBID"])))

                if st.session_state.get("grobid_take_note"):
                    st.success(st.session_state.pop("grobid_take_note"))
                st.caption("列表字段采用**只补空、不覆盖**：GROBID 独有的条目追加进去，"
                           "已有的保持本地版本，不会把本地的结果冲掉。"
                           "想整段换成 GROBID 的结果，可以直接在输入框里手动替换。")
            else:
                st.success("所有可比字段两边一致，无需人工介入。")

            st.caption(
                "⚠️ GROBID 也会出错，实测过的例子：Nature 那篇**标题被抽成期刊名**"
                "「nature human behaviour」；SAGE 那篇把题记里的「George Washington」当成作者；"
                "Frontiers 那篇把侧栏的**编者与审稿人**当成作者；Cell Press 那篇的摘要"
                "（叫 SUMMARY）**一条都没抽到**。所以不一致时请对照原 PDF 判断，不要盲信任何一边。"
            )


