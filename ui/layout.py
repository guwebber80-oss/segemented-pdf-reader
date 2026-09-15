"""
ui.layout —— 阅读布局的纯逻辑（阶段 6.5：固定页面 / 卡内滚动 / 沉浸模式）

三件事的来龙去脉见 README 的阶段 6.5；这里只记**为什么这么做**：

1) **滚动用原生 `st.container(height=N)`，不靠自定义 CSS**
   2026-09-16 的教训：先做了一版「CSS 注入 `calc(100vh - Npx)` + `overflow-y:auto`」，
   用户反馈「功能未实现」——那条链路（`st.markdown` 里塞 `<style>` + 靠类名命中容器）
   在他的环境里没能生效，而我在沙箱里既没有浏览器、也无法逐条排除。
   既然 Streamlit 明确支持固定高度的可滚动容器，就改成用它：**官方 API，不赌**。
   代价是高度只能给像素（`vh` 拿不到视口高度），所以做成「卡片高度：矮 / 中 / 高」三档。

2) **CSS 只保留两件只有 CSS 能做的事**
   ① 沉浸模式隐藏左右两栏（`display:none`）；② 滚动条做细一点。
   类名用 `st-key-<key>`——这是 Streamlit 官方约定（前端会给带 key 的容器加这个类），
   不能用 `data-testid` 之类内部结构（会随版本变）。测试里有一条断言禁止出现内部选择器。
   类名重复写两遍（`.st-key-x.st-key-x`）是为了提高优先级，不必用 `!important`。

3) **必须有回退开关**（本项目对判据/布局类改动的纪律）
   关掉「卡内滚动」→ 不再给容器传 `height=`、也不再注入滚动条样式 = 完全回到改动前的行为。
"""

import streamlit as st

from utils import store

# ---------------- 会话键（同时也是控件 key） ----------------
IMMERSIVE_KEY = "ui_immersive"        # 沉浸模式：隐藏左右两栏
SCROLL_KEY = "ui_scroll_layout"       # 卡内滚动开关（回退用）
HEIGHT_KEY = "ui_card_height"         # 卡片高度档位

# ---------------- 三块滚动区域的 key（CSS 靠它们定位，容器也靠它们带 key） ----------------
KEY_LEFT_BOX = "reader_left_box"
KEY_CARD = "reader_card"
KEY_RIGHT_BOX = "reader_right_box"

# ---------------- 高度档位（像素） ----------------
DEFAULT_HEIGHT = "中"
HEIGHT_PRESETS = {"矮": 360, "中": 440, "高": 520}
SIDE_EXTRA = 120          # 左右两栏比卡片高一点：栏里没有页头与进度条占位

# 沉浸模式：左右两栏压到 1px 高（内容被裁掉，但控件照常渲染 → 状态不丢）。
# 为什么不用 CSS display:none 单独搞定：那条链路在用户环境里没能生效（见模块开头 ①），
# 所以用「原生 1px 容器 + 列宽压到 0.0001」做**不依赖 CSS** 的隐藏；CSS 那条只当锦上添花。
IMMERSIVE_BOX_HEIGHT = 1

# 沉浸模式下左右栏的列宽：给到极小而不是 0，Streamlit 仍然会渲染它们
# （控件照常创建 → 上传的文件、语言、设置都不会被 Streamlit 回收掉），
# 视觉上再由 CSS 的 display:none 彻底藏起来。
IMMERSIVE_SPEC = [0.0001, 9.0, 0.0001]
NORMAL_SPEC = [0.85, 2.7, 1.05]


def card_height(mode: str) -> int:
    """档位名 → 卡片区高度（像素）；不认识的档位退回默认档"""
    return HEIGHT_PRESETS.get(mode, HEIGHT_PRESETS[DEFAULT_HEIGHT])


def side_height(mode: str) -> int:
    """档位名 → 左右两栏滚动区的高度（像素）"""
    return card_height(mode) + SIDE_EXTRA


def container_height(scroll: bool, mode: str, side: bool = False):
    """
    要不要给容器传 `height=`：关掉卡内滚动就返回 None（回到改动前的行为）。
    None 的语义是「别传这个参数」，由调用方用 `box_kwargs()` 拼好。
    """
    if not scroll:
        return None
    return side_height(mode) if side else card_height(mode)


def side_box_height(scroll: bool, mode: str, immersive: bool = False):
    """
    左右两栏的高度：
      · 沉浸模式 → 1px（内容被裁掉看不见，但控件照常渲染，上传的文件/设置不会丢）
      · 正常模式 → 档位对应的高度（关掉卡内滚动则 None = 不传 height）
    """
    if immersive:
        return IMMERSIVE_BOX_HEIGHT
    return side_height(mode) if scroll else None


def card_box_height(scroll: bool, mode: str):
    """卡片滚动区的高度（沉浸模式不影响它：沉浸不等于放弃卡内滚动）"""
    return card_height(mode) if scroll else None


def box_kwargs(height) -> dict:
    """把「高度或 None」变成传给 `st.container()` 的 kwargs（None = 什么都不传）"""
    return {} if height is None else {"height": height}


def _cls(key: str) -> str:
    """`reader_card` → `.st-key-reader_card.st-key-reader_card`（重复一次提高优先级）"""
    return f".st-key-{key}.st-key-{key}"


def layout_css(scroll: bool = True, immersive: bool = False, height: str = DEFAULT_HEIGHT) -> str:
    """
    生成布局 CSS：只包含「滚动条样式」（可回退）与「沉浸模式隐藏两栏」。
    两者都不需要时返回空字符串（= 不注入任何样式）。

    height 参数目前不参与生成（高度改由原生 `height=` 决定），保留它是为了调用方签名稳定、
    以及以后若要做「按 vh 微调」时有地方加。
    """
    rules = []
    if scroll:
        boxes = f"{_cls(KEY_LEFT_BOX)}, {_cls(KEY_RIGHT_BOX)}"
        card = _cls(KEY_CARD)
        rules.append(
            "/* 滚动条做细一点，让人一眼看出「滚的是这一块」 */\n"
            f"{boxes}::-webkit-scrollbar,{card}::-webkit-scrollbar{{width:10px;height:10px}}\n"
            f"{boxes}::-webkit-scrollbar-thumb,{card}::-webkit-scrollbar-thumb"
            "{background:#c9d3e0;border-radius:99px;border:3px solid transparent;"
            "background-clip:content-box}\n"
            f"{boxes}, {card}{{scrollbar-width:thin;scrollbar-color:#c9d3e0 transparent}}"
        )
    if immersive:
        # 沉浸模式：把左右两栏的内容藏掉
        # （控件仍在脚本里被创建，状态不会丢——这一点有测试与冒烟守着）
        rules.append(
            "/* 沉浸模式：隐藏左右两栏，只留卡片 */\n"
            f"{_cls(KEY_LEFT_BOX)}, {_cls(KEY_RIGHT_BOX)}{{display:none}}"
        )
    return "\n".join(rules)


def columns_spec(immersive: bool) -> list:
    """三栏的列宽比例（沉浸模式把左右栏压到极小）"""
    return list(IMMERSIVE_SPEC if immersive else NORMAL_SPEC)


def text_pad_spec(immersive: bool) -> list:
    """
    卡片内容两侧留白的比例。

    沉浸模式中栏变宽了，如果仍按 1:6:1 留白，正文会宽到 1200px 以上（英文行长超过 100 字符
    很难读）。所以沉浸时改成 1.6:5:1.6，让正文落在 900~1000px 左右，图片列再单独放宽。
    """
    return [1.6, 5, 1.6] if immersive else [1, 6, 1]


def figures_pad_spec(immersive: bool) -> list:
    """插图列两侧留白的比例（论文图里小字号多，沉浸时给得更宽）"""
    return [0.5, 9, 0.5] if immersive else [0.4, 8, 0.4]


def ensure_layout_state() -> None:
    """
    把布局相关的会话状态准备好（控件创建之前调用一次）。

    顺序：会话里已有就用会话里的（本次会话改过），否则读本地偏好 `.cache/ui.json`
    （上次用的档位/沉浸状态），再否则用默认值。只补缺失的键，不覆盖用户在本次会话里的选择。
    """
    prefs = store.load_ui_prefs()
    defaults = {
        IMMERSIVE_KEY: bool(prefs.get("immersive", False)),
        SCROLL_KEY: bool(prefs.get("scroll", True)),
        HEIGHT_KEY: prefs.get("height", DEFAULT_HEIGHT) if prefs.get("height") in HEIGHT_PRESETS
                    else DEFAULT_HEIGHT,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def current_prefs() -> dict:
    """当前的布局偏好（写回本地时用）"""
    return {
        "immersive": bool(st.session_state.get(IMMERSIVE_KEY, False)),
        "scroll": bool(st.session_state.get(SCROLL_KEY, True)),
        "height": st.session_state.get(HEIGHT_KEY, DEFAULT_HEIGHT),
    }


def save_prefs() -> bool:
    """把布局偏好写进本地缓存（下次打开还是这个样子）"""
    return store.save_ui_prefs(current_prefs())
