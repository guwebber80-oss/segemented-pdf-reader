r"""
科研文献 PDF 智能阅读器 —— 主程序（界面层）
================================================
阶段 2：卡片中英文一键切换

文件分工：
    app.py         ← 本文件，只管界面：上传、设置、渲染卡片、诊断面板、语言切换
    pdf_parser.py  ← 解析逻辑：文本块提取、分栏判断、阅读顺序、段落合并、标题识别
    translator.py  ← 翻译逻辑：DeepL / OpenAI / Google 三种后端，带超时与重试

运行命令（本项目约定：不激活虚拟环境，直接用 venv 的 python）：
    .\.venv\Scripts\python.exe -m streamlit run app.py

翻译需要 .env 里有 API Key（复制 .env.example 为 .env 后填写），见 README「阶段 2 说明」。
"""

import streamlit as st

from pdf_parser import count_words, escape_markdown, parse_pdf
from translator import (
    BACKEND_LABELS,
    TARGET_LABELS,
    TranslationError,
    backend_availability,
    load_config,
    probe_backend,
    translate,
)

# ============================================================
# 第 0 部分：页面配置（必须是第一个 Streamlit 调用）
# ============================================================
st.set_page_config(
    page_title="科研文献 PDF 智能阅读器",
    page_icon="📚",
    layout="wide",
)

# ============================================================
# 第 1 部分：会话状态初始化
# ============================================================
# Streamlit 的机制是「任何交互都会把整个脚本从头重跑一遍」，
# 所以【跨交互要保留的东西】必须放进 st.session_state，否则每次点击都会丢。
#   translations     翻译缓存：{(后端, 目标语言, 原文): 译文}，命中就不再调 API（省钱、省时间）
#   translate_stats  计数：用来验证「重复点击不会重复请求」
if "translations" not in st.session_state:
    st.session_state["translations"] = {}
if "translate_stats" not in st.session_state:
    st.session_state["translate_stats"] = {"requests": 0, "hits": 0, "chars": 0, "errors": 0}

# 语言切换控件的两个选项（英文原文 / 中文译文）
LANG_EN, LANG_ZH = "EN", "中文"


def translate_cached(text: str, backend: str, target: str):
    """
    带缓存的翻译。返回 (译文, 错误消息)，两者必有一个是 None。

    缓存键是「后端 + 目标语言 + 原文」，所以：
      · 同一张卡片来回点 中/EN，第二次起直接读缓存，不会再花额度
      · 换后端或换目标语言会重新翻译（因为缓存键变了）
    """
    if not backend:
        return None, "没有可用的翻译后端：请先在 .env 里配置 API Key。"

    cache = st.session_state["translations"]
    stats = st.session_state["translate_stats"]
    cache_key = (backend, target, text)

    if cache_key in cache:
        stats["hits"] += 1
        return cache[cache_key], None

    try:
        with st.spinner("正在翻译…"):
            result = translate(text, target=target, backend=backend)
    except TranslationError as exc:
        # TranslationError 的文本已经是给用户看的中文提示
        stats["errors"] += 1
        print(f"[translate][失败] {backend} {target} {len(text)}字符: {exc}", flush=True)
        return None, str(exc)
    except Exception as exc:
        stats["errors"] += 1
        print(f"[translate][异常] {type(exc).__name__}: {exc}", flush=True)
        return None, f"未预期的错误 {type(exc).__name__}: {exc}"

    cache[cache_key] = result
    stats["requests"] += 1
    stats["chars"] += len(text)
    # 同时打到终端，方便对照界面上的计数一起验证「有没有重复请求」
    print(f"[translate][成功] {backend} {target} {len(text)}字符 → {len(result)}字符", flush=True)
    return result, None


def render_language_toggle(key: str):
    """
    卡片里的中/EN 切换控件，返回当前选中的语言。

    注意：这里用「先往 session_state 里塞默认值、控件不带 default」的写法，
    而不是给控件传 default=——因为两者同时用会触发 Streamlit 的冲突警告。
    """
    if key not in st.session_state:
        st.session_state[key] = LANG_EN
    return st.segmented_control(
        "显示语言",
        [LANG_EN, LANG_ZH],
        key=key,
        label_visibility="collapsed",
    )


st.title("📚 科研文献 PDF 智能阅读器")
st.caption("阶段 2：卡片式阅读 + 中英文一键切换（译文按需请求，带缓存）")

# ============================================================
# 第 2 部分：侧边栏
# ============================================================
with st.sidebar:
    # ---- 2.1 解析设置 ----
    st.header("⚙️ 解析设置")
    merge_on = st.toggle(
        "段落自动合并", value=True,
        help="把被 PDF 拆成多块的同一段落拼回去。若发现两个独立段落被错误粘在一起，关掉它。",
    )
    dehyphenate_on = st.toggle(
        "行尾连字符合并", value=True,
        help="英文排版会把单词在行尾断开（informa- / tion），开启后自动拼成 information。",
    )
    max_words = st.slider(
        "单张卡片最长词数", min_value=80, max_value=400, value=200, step=20,
        help="只有超过 300 词的超长段落才会被切开；切开后每张卡片控制在这么多词左右。",
    )
    expand_n = st.slider("默认展开卡片数", min_value=0, max_value=10, value=3)

    st.divider()

    # ---- 2.2 翻译设置 ----
    st.header("🌐 翻译设置")
    config = load_config()
    availability = backend_availability(config)
    usable_backends = [name for name, ready in availability.items() if ready]

    if usable_backends:
        default_backend = config["backend"] if config["backend"] in usable_backends else usable_backends[0]
        backend = st.selectbox(
            "翻译后端", usable_backends,
            index=usable_backends.index(default_backend),
            format_func=lambda name: BACKEND_LABELS.get(name, name),
        )
        target = st.selectbox(
            "目标语言", list(TARGET_LABELS),
            format_func=lambda code: TARGET_LABELS[code],
        )

        col_test, col_reload = st.columns(2)
        if col_test.button("测试连接"):
            try:
                sample = probe_backend(backend, target)
                st.success(f"连接正常 ✅ 示例：{sample}")
            except TranslationError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"未预期的错误 {type(exc).__name__}: {exc}")
        if col_reload.button("重读 .env", help="改完 .env 后点这里，不用重启服务"):
            load_config(force_reload=True)
            st.rerun()
    else:
        backend, target = None, "zh"
        st.warning(
            "没有检测到任何翻译 API Key，中英切换不可用。\n\n"
            "请把项目根目录的 `.env.example` 复制成 `.env`，填入 `DEEPL_API_KEY`，"
            "然后点下面的「重读 .env」。"
        )
        if st.button("重读 .env"):
            load_config(force_reload=True)
            st.rerun()

    if st.button("清空翻译缓存"):
        st.session_state["translations"] = {}
        st.session_state["translate_stats"] = {"requests": 0, "hits": 0, "chars": 0, "errors": 0}
        st.rerun()

    st.caption("改完 .env 需要点「重读 .env」或重启服务才会生效。")

    # 计数占位符：侧边栏在页面最前面渲染，而翻译发生在后面的卡片里，
    # 所以先留一个空位，等所有卡片渲染完再把真实计数填进来（否则显示的永远是上一轮的数字）。
    stats_slot = st.empty()

# ============================================================
# 第 3 部分：上传
# ============================================================
uploaded_file = st.file_uploader(
    label="请上传一篇 PDF 文献",
    type=["pdf"],
    accept_multiple_files=False,
    help="支持有文字层的单栏 / 双栏 PDF。扫描版（纯图片）本阶段不支持。",
)

if uploaded_file is None:
    st.info(
        "👆 请在上方选择一篇 PDF。\n\n"
        "读起来之后，展开任意一张卡片，用里面的 **EN / 中文** 按钮切换语言。"
    )
    st.stop()      # 没有文件就停下，避免下面到处写 None 判断

pdf_bytes = uploaded_file.getvalue()

# ============================================================
# 第 4 部分：解析（带缓存）
# ============================================================
# 按「文件 + 设置」的指纹判断要不要重新解析，避免每次点击都重算一遍 PDF。
file_key = f"{uploaded_file.name}|{uploaded_file.size}|{merge_on}|{dehyphenate_on}|{max_words}"

if st.session_state.get("parse_key") != file_key:
    with st.spinner("正在解析 PDF…"):
        try:
            st.session_state["parse_result"] = parse_pdf(pdf_bytes, merge_on, dehyphenate_on, max_words)
            st.session_state["parse_key"] = file_key
            st.session_state["parse_error"] = None
        except Exception as exc:
            # 解析失败不能让页面白屏，要把错误原样显示出来方便排查
            st.session_state["parse_result"] = None
            st.session_state["parse_key"] = None
            st.session_state["parse_error"] = f"{type(exc).__name__}: {exc}"

if st.session_state.get("parse_error"):
    st.error("解析失败：" + st.session_state["parse_error"])
    st.caption("请把上面的完整报错、以及这个 PDF 的特征（单栏 / 双栏 / 扫描版）发给我，我来定位。")
    st.stop()

result = st.session_state["parse_result"]
blocks = result["blocks"]
pages = result["pages"]

# ============================================================
# 第 5 部分：解析概览
# ============================================================
st.subheader("📊 解析概览")

total_words = sum(count_words(b.text) for b in blocks)
heading_count = sum(1 for b in blocks if b.kind == "heading")
layouts = " / ".join(f"第{p['页码']}页 {p['检测排版']}" for p in pages)

col1, col2, col3, col4 = st.columns(4)
col1.metric("页数", len(pages))
col2.metric("卡片数", len(blocks))
col3.metric("总词数", f"{total_words:,}")
col4.metric("解析耗时", f"{result['elapsed']} 秒")

st.caption(
    f"文件：{uploaded_file.name} · 正文字号基准 {result['body_size']} pt · "
    f"其中标题 {heading_count} 张 · 排版检测：{layouts}"
)

# 扫描版（没有文字层）要给出明确提示，不要让它静默变成空白页
if result["total_chars"] < 200:
    st.warning(
        f"⚠️ 这个 PDF 几乎提取不到文字（全文仅 {result['total_chars']} 个字符），"
        "很可能是**扫描版 / 图片版**。本阶段不做 OCR，请换一篇有文字层的 PDF 测试。"
    )

st.divider()

# ============================================================
# 第 6 部分：卡片流（含中英切换）
# ============================================================
st.subheader("📖 阅读卡片")
st.caption(
    "展开卡片后，用里面的 **EN / 中文** 按钮切换语言；"
    "译文只在第一次点击时请求，来回切换不会重复扣额度。"
)

_COLUMN_NAME = {-1: "通栏", 0: "左栏", 1: "右栏"}
expanded_used = 0        # 已展开的正文卡片数，用来实现「默认只展开前 N 张」
translated_cards = 0     # 本次渲染里显示为中文的卡片数（统计用）

for b in blocks:
    if b.kind == "heading":
        # 标题卡片：标题文字在左，语言切换在右
        level = min(max(b.level, 1), 4)
        col_text, col_lang = st.columns([6, 1])

        with col_lang:
            heading_lang = render_language_toggle(f"lang_{b.order}")

        with col_text:
            if heading_lang == LANG_ZH:
                zh, error = translate_cached(b.text, backend, target)
                if error:
                    st.error(f"翻译失败：{error}")
                    st.markdown(f"{'#' * (level + 1)} {escape_markdown(b.text)}")
                else:
                    translated_cards += 1
                    st.markdown(f"{'#' * (level + 1)} {escape_markdown(zh)}")
            else:
                st.markdown(f"{'#' * (level + 1)} {escape_markdown(b.text)}")

        st.caption(
            f"#{b.order} · 第{b.page}页 · 标题 L{b.level} · "
            f"{count_words(b.text)} 词 · {b.max_size:.1f}pt"
        )
    else:
        label = (
            f"#{b.order} · 第{b.page}页 · {_COLUMN_NAME[b.column]} · "
            f"{count_words(b.text)} 词 · {len(b.text)} 字符"
        )
        with st.expander(label, expanded=(expanded_used < expand_n)):
            card_lang = render_language_toggle(f"lang_{b.order}")

            if card_lang == LANG_ZH:
                zh, error = translate_cached(b.text, backend, target)
                if error:
                    # 翻译失败不能阻塞阅读：提示错误，同时继续显示英文原文
                    st.error(f"翻译失败：{error}")
                    st.caption("下面仍然显示英文原文；问题解决后重新点「中文」即可。")
                    st.markdown(escape_markdown(b.text))
                else:
                    translated_cards += 1
                    st.markdown(escape_markdown(zh))
            else:
                st.markdown(escape_markdown(b.text))

        expanded_used += 1

st.divider()

# ============================================================
# 第 7 部分：诊断面板（验证阅读顺序、定位双栏错位）
# ============================================================
with st.expander("🔍 解析诊断（验证阅读顺序、定位双栏错位）"):
    st.markdown("**① 每页排版检测**")
    st.dataframe(pages, hide_index=True)

    st.markdown("**② 文本块明细**（已按程序认定的阅读顺序编号，从上往下就是卡片顺序）")
    rows = [{
        "序号": b.order,
        "页码": b.page,
        "类型": "标题" if b.kind == "heading" else "正文",
        "栏": _COLUMN_NAME[b.column],
        "y0": round(b.y0, 1),
        "x0": round(b.x0, 1),
        "字号": round(b.max_size, 1),
        "粗体占比": round(b.bold_ratio, 2),
        "词数": count_words(b.text),
        "开头 40 字": b.text[:40].replace("\n", " "),
    } for b in blocks]
    st.dataframe(rows, hide_index=True, height=420)

    # 双栏页里，正常只有标题/摘要/跨栏图注会横跨中缝。
    # 如果这里出现大段正文，说明分栏判断可能出错，需要人工核对。
    spanning = [b for b in blocks if b.column == -1 and b.kind == "body" and count_words(b.text) > 120]
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

# ============================================================
# 第 8 部分：卡片全部渲染完之后，把翻译计数填进侧边栏的占位符
# ============================================================
stats = st.session_state["translate_stats"]
stats_slot.caption(
    f"**翻译计数**（用来验证不重复请求）\n\n"
    f"API 请求 **{stats['requests']}** 次 · 缓存命中 **{stats['hits']}** 次 · "
    f"失败 {stats['errors']} 次\n\n"
    f"累计翻译 {stats['chars']:,} 字符"
    + (f" · 当前 {translated_cards} 张卡片显示为中文" if translated_cards else "")
)
