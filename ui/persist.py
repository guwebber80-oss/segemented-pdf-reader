"""
ui.persist —— 把 utils.store 的磁盘缓存接到 Streamlit 的会话状态上（阶段 6.1）

分工很清楚：
  · `utils/store.py`  只管文件读写（纯 Python，不认识 Streamlit）
  · `ui/persist.py`   只管「控件的初始值从哪来 / 用户的改动存到哪去」这层黏合

两条必须守住的规矩：

  ① **预置控件状态一定要在控件被创建之前做完**。
     Streamlit 不允许给「已经创建过的、带 key 的控件」改 session_state（会抛异常），
     所以恢复设置、恢复元数据修正、恢复 GROBID 结果都放在渲染之前统一做。

  ② **控件的默认值一律由本模块预置，控件自己不再写 value= / index=**。
     两者同时用会触发 Streamlit 的「默认值与 Session State 冲突」警告
     （这个坑在语言开关上已经踩过一次），因此设置项全部改成 key + 预置。

要持久化的东西分三类：
  · 设置（合并/连字符/目标词数/表格呈现/显示全部）——恢复后解析结果与上次一致，指纹也随之命中
  · 用户的成果（元数据修正、GROBID 结果、读到第几张）——这是「人工修正」的价值所在，必须留住
  · 译文（全局表，键=后端|目标语言|原文哈希）——最贵的那部分，跨文献、跨解析版本复用
"""

import json

import streamlit as st

from utils import store, translate_plan

from .cards import translate_all

# 设置项清单：(控件 key, 记录里的字段名, 默认值, 取值范围或可选项)
# 取值范围写成 (最小, 最大) 的二元组；可选项写成候选值元组；None = 不做校验。
SETTINGS_SPEC = (
    ("set_merge_on", "merge_on", True, None),
    ("set_dehyphenate_on", "dehyphenate_on", True, None),
    ("set_target_words", "target_words", 200, (100, 400)),
    ("set_table_mode_label", "table_mode_label", "截图（推荐）", ("截图（推荐）", "保留文字")),
    ("set_show_all", "show_all", False, None),
)

# 元数据面板里十个可编辑字段的控件 key（与 ui/metadata_panel.py 保持一致）
METADATA_WIDGET_KEYS = (
    "in_title", "in_doi", "in_abstract", "in_authors", "in_first_author",
    "in_corresponding", "in_corresponding_email", "in_affiliations",
    "in_supplementary", "in_author_emails",
)


# ============================================================
# 1. 设置项的预置与回收
# ============================================================
def _sanitize(value, default, rule):
    """把磁盘上的值清洗成能安全塞进控件的值（越界就退回默认值）"""
    if rule is None:
        # 类型对不上就退回默认值（比如布尔开关被写成了字符串）
        return value if isinstance(value, bool) == isinstance(default, bool) else default
    if (isinstance(rule, tuple) and len(rule) == 2
            and all(isinstance(bound, int) for bound in rule)):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        low, high = rule
        return number if low <= number <= high else default
    return value if value in rule else default


def seed_settings(record) -> None:
    """
    把设置控件的初始值放进 session_state（**必须在控件创建之前调用**）。

    有记录：用上次的设置（解析指纹随即命中，结果与上次一致）；
    没记录：用默认值。越界或类型不对的值会被清洗成默认值，避免控件报错。
    """
    saved = (record or {}).get("settings")
    saved = saved if isinstance(saved, dict) else {}
    for widget_key, field, default, rule in SETTINGS_SPEC:
        st.session_state[widget_key] = _sanitize(saved.get(field), default, rule)


def settings_snapshot() -> dict:
    """把当前设置回收成字典（写记录用）"""
    return {field: st.session_state.get(widget_key, default)
            for widget_key, field, default, _rule in SETTINGS_SPEC}


# ============================================================
# 2. 元数据修正 / GROBID 结果的恢复
# ============================================================
def restore_edits(record) -> None:
    """
    恢复（或清空）元数据输入框的内容。

    有记录：把上次保存的十个字段填回去——**用户手动改正过的内容优先级最高**，
            这正是「下次打开不用重来」的核心；
    没记录：清掉控件状态，让输入框重新取本次自动提取的值。
    """
    saved = (record or {}).get("edits")
    saved = saved if isinstance(saved, dict) else {}
    for key in METADATA_WIDGET_KEYS:
        if key in saved:
            st.session_state[key] = saved[key]
        else:
            st.session_state.pop(key, None)


def collect_edits() -> dict:
    """把当前十个输入框的内容回收成字典（写记录用）"""
    return {key: st.session_state.get(key, "") for key in METADATA_WIDGET_KEYS}


def restore_grobid(record, meta_key: str) -> bool:
    """
    把上次保存的 GROBID 结果放回会话，省掉一次 5~10 秒的外部调用。

    同时把 `grobid_key` 设成当前文件的指纹：面板据此知道「这份结果就是当前文件的」，
    既不会把它清掉，也不会自动再调一次。
    """
    result = (record or {}).get("grobid")
    if not isinstance(result, dict) or not result:
        return False
    st.session_state["grobid_result"] = result
    st.session_state["grobid_restored"] = True
    st.session_state["grobid_key"] = meta_key
    st.session_state.pop("grobid_error", None)
    return True


# ============================================================
# 3. 组装记录
# ============================================================
def _json_safe(value):
    """JSON 装不下的东西一律丢掉：缓存宁可少存一点，也不能因为它整体写不进去"""
    if value is None:
        return None
    try:
        json.dumps(value, ensure_ascii=False)
    except Exception:
        return None
    return value


def build_record(key: str, file_name: str, file_size: int, card_index: int,
                 grobid_result=None, title: str = "", doi: str = "") -> dict:
    """
    组装一篇文献的缓存记录。

    注意 title / doi 用的是**输入框里的当前值**（也就是可能被人工修正过的），
    这样索引里存的就是你认可的那一份，按 DOI / 标题兜底找回时才靠得住。
    """
    return {
        "version": store.CACHE_VERSION,
        "key": key,
        "file_name": file_name,
        "file_size": file_size,
        "settings": settings_snapshot(),
        "metadata": {"title": title or "", "doi": doi or ""},
        "edits": collect_edits(),
        "grobid": _json_safe(grobid_result),
        "card_index": int(card_index or 0),
    }


def save_record(**kwargs) -> bool:
    """组装并落盘（对调用方来说就是一个动作）"""
    return store.save_paper(build_record(**kwargs))


# ============================================================
# 5. 左栏的「译文覆盖」面板（阶段 6.4）
# ============================================================
def render_translation_coverage(cards, backend, target) -> None:
    """
    显示「多少张卡已经能离线看中文」，并给一个「翻译整篇」的入口。

    为什么要有它（用户 2026-09-16 实测报的现象）：平时翻译是**读到哪翻到哪**，
    没显示过的卡片既没有译文、也没有落盘；于是关掉进程重新打开、重新上传同一篇时，
    那些卡片仍要联网翻译一次，断网就只能看英文原文。缓存本身没坏——
    看过的那几张卡 100% 命中——缺的是「把整篇补齐」这一步。
    这个面板把缺口显式化，并给一次补齐的按钮（已译段落不会重复请求，所以安全）。
    """
    st.markdown("**🌐 译文覆盖**")
    if not backend:
        st.caption("没有可用的翻译后端，无法统计译文覆盖。")
        return
    if not cards:
        st.caption("还没有阅读卡片。")
        return

    info = translate_plan.coverage(cards, backend, target)
    st.caption(f"已能离线读中文：**{info['covered_cards']} / {info['cards']}** 张卡 · "
               f"段落 {info['covered_texts']} / {info['texts']} 段")

    if info["pending"]:
        st.caption(f"还有 **{len(info['pending'])}** 段没有译文——"
                   "没翻到的卡片不会自动翻译，所以断网时它们只能看英文原文。")
        if st.button(f"🌐 翻译整篇（补齐 {len(info['pending'])} 段）", key="translate_all_btn",
                     help="把全文还没译文的段落一次翻完并存进本地缓存。"
                          "已经译过的段落会命中缓存、不会重复请求，所以多点几次也安全。"
                          "注意：会消耗翻译额度。"):
            bar = st.progress(0.0, text="正在翻译整篇…")

            def on_progress(done, total, _bar=bar):
                _bar.progress(min(done / total, 1.0) if total else 1.0,
                              text=f"正在翻译整篇… {done} / {total} 段")

            result = translate_all(cards, backend, target, on_progress=on_progress)
            store.flush_translations()          # 立刻落盘：别等关进程时才存
            bar.empty()
            if result["error"]:
                st.session_state["translate_all_message"] = ("error",
                    f"翻译中断：{result['error']}（已完成 {result['done']} 段并已存入本地；"
                    "解决网络或额度问题后再点一次即可接着翻译，已译的不会重复请求）")
            else:
                st.session_state["translate_all_message"] = ("success",
                    f"整篇翻译完成：新增 {result['done']} 段（{result['requests']} 次请求）。"
                    "现在整篇都能离线看中文了。")
            st.rerun()
    else:
        st.success("整篇都有译文了：断网也能看中文。")

    message = st.session_state.pop("translate_all_message", None)
    if message:
        kind, text = message
        (st.error if kind == "error" else st.success)(text)


# ============================================================
# 4. 左栏的「本地缓存」面板
# ============================================================
def render_cache_panel(key: str, source: str = "", saved_ok: bool = True,
                       extra_note: str = "") -> None:
    """
    左栏的缓存面板：说清楚「这篇命中没有、缓存有多大、怎么清」。

    source 取值：
        "hash"  PDF 字节完全一致 → 设置 / 元数据修正 / 译文 / 阅读位置全部接上
        "meta"  字节变了但 DOI 或标题对得上 → 元数据修正与译文接上（设置沿用当前值）
        ""      没命中 → 这次重新解析、重新翻译，读完自动存下来
    """
    stats = store.cache_stats()
    paused = st.session_state.get("no_autosave_key") == key

    if paused:
        # 用户刚点过「清除本篇记录」：本次会话不再自动存回来，否则清完立刻又被写回去
        st.warning("已清除本篇的本地记录，**本次会话不再自动保存这篇**"
                   "（换文件或刷新页面后恢复自动保存）。")
    elif source == "hash":
        st.success("♻️ 命中本地缓存：这篇文献上次读过，设置、元数据修正、译文都已接上。")
    elif source == "meta":
        st.info("♻️ 按 DOI / 标题找到了这篇文献的本地记录：元数据修正与译文已接上"
                "（PDF 字节与上次不同，解析设置沿用当前值）。")
    else:
        st.caption("🆕 本地没有这篇的记录：本次正常解析与翻译，读完会自动存到本地，"
                   "下次打开就不用重来了。")

    if extra_note:
        st.caption(extra_note)

    st.caption(
        f"缓存目录：`{stats['root']}`\n\n"
        f"已保存 **{stats['papers']}** 篇文献记录 · 译文 **{stats['translations']:,}** 条 · "
        f"占用 **{stats['bytes'] / 1024:.0f} KB**"
        + ("" if saved_ok else "　⚠️ 本次写盘失败（磁盘满或没有写权限？）")
    )
    st.caption("图片不落盘：插图与公式/表格截图下次按需重新渲染（200 DPI，几百毫秒），"
               "避免缓存变成几百 MB。")

    col_one, col_all = st.columns(2)
    if col_one.button("清除本篇记录", key="cache_drop_one",
                      help="只删这篇的元数据修正与阅读位置；译文是全局的，不受影响"):
        store.delete_paper(key)
        for widget_key in METADATA_WIDGET_KEYS:
            st.session_state.pop(widget_key, None)
        st.session_state["cache_record"] = None
        st.session_state["cache_source"] = ""
        # 关键：关掉这篇的自动保存。否则脚本重跑时第 11 部分又会把记录写回来，
        # 用户会觉得「清除了但没清掉」。
        st.session_state["no_autosave_key"] = key
        st.rerun()
    if col_all.button("清空全部缓存", key="cache_drop_all",
                      help="删除所有文献记录与全部译文（下次阅读会重新解析、重新翻译）"):
        store.clear_all()
        st.session_state["translations"] = {}
        st.session_state["translate_stats"] = {"requests": 0, "hits": 0, "chars": 0, "errors": 0}
        st.session_state["cache_record"] = None
        st.session_state["cache_source"] = ""
        for widget_key in METADATA_WIDGET_KEYS:
            st.session_state.pop(widget_key, None)
        st.session_state["no_autosave_key"] = key
        st.rerun()
