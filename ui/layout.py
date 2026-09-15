"""
ui.layout —— 阅读布局的纯逻辑（阶段 6.5：固定页面 / 卡内滚动 / 沉浸模式）

三件事的来龙去脉见 README 的阶段 6.5；这里只记**为什么这么做**：

1) **为什么用 `st-key-<key>` 而不是 `data-testid`**
   Streamlit 前端（1.63 bundle 里 `function Af(e){return e?`st-key-`+e.trim().replace(...)`}`）
   会给带 `key` 的容器加上 `st-key-<key>` 类名，这是**官方约定**；而 `data-testid="stColumn"`
   之类是内部结构，随版本就变（6.3 的分析里已把它列为不可靠做法）。所以本模块只生成
   `.st-key-xxx` 选择器，并在测试里断言「绝不出现 data-testid」。

2) **为什么用 `calc(100vh - Npx)` 而不是 `st.container(height=N)`**
   Streamlit 不把视口高度交给 Python（6.3 已核对），写死像素会在小屏上把页面顶出滚动条、
   在大屏上留一大截空白。用 vh 让浏览器自己算，N 只表示「装饰部分」（页头、标题、进度条、
   内边距）的高度估计，做成「卡片高度：矮 / 中 / 高」三档让用户按显示器挑。

3) **必须有回退开关**（本项目对判据/布局类改动的纪律）
   `layout_css(scroll=False)` 返回空字符串 = 完全回到改动前的行为。

类名重复写两遍（`.st-key-x.st-key-x`）是为了提高优先级：Streamlit 自己的样式表也是单类选择器，
同优先级下谁后注入谁赢，写两遍就不用赌顺序，也不必用 `!important`。
"""

import streamlit as st

from utils import store

# ---------------- 会话键（同时也是控件 key） ----------------
IMMERSIVE_KEY = "ui_immersive"        # 沉浸模式：隐藏左右两栏
SCROLL_KEY = "ui_scroll_layout"       # 卡内滚动开关（回退用）
HEIGHT_KEY = "ui_card_height"         # 卡片高度档位

# ---------------- 三块滚动区域的 key（CSS 靠它们定位） ----------------
KEY_LEFT_BOX = "reader_left_box"
KEY_CARD = "reader_card"
KEY_RIGHT_BOX = "reader_right_box"

# ---------------- 高度档位 ----------------
DEFAULT_HEIGHT = "中"
# 「装饰部分」的像素估计：卡片区要用 100vh 减掉它（页头 + 文件信息 + 标题 + 进度条 + 内边距）
HEIGHT_OFFSETS = {"矮": 380, "中": 320, "高": 260}
# 左右两栏要减掉的（页头 + 内边距，栏里没有别的装饰）
SIDE_OFFSET = 150

# 沉浸模式下左右栏的列宽：给到极小而不是 0，Streamlit 仍然会渲染它们
# （控件照常创建 → 上传的文件、语言、设置都不会被 Streamlit 回收掉），
# 视觉上再由 CSS 的 display:none 彻底藏起来。
IMMERSIVE_SPEC = [0.0001, 9.0, 0.0001]
NORMAL_SPEC = [0.85, 2.7, 1.05]


def card_height_offset(mode: str) -> int:
    """档位名 → 卡片区要减掉的像素（不认识的档位退回默认档）"""
    return HEIGHT_OFFSETS.get(mode, HEIGHT_OFFSETS[DEFAULT_HEIGHT])


def _cls(key: str) -> str:
    """`reader_card` → `.st-key-reader_card.st-key-reader_card`（重复一次提高优先级）"""
    return f".st-key-{key}.st-key-{key}"


def layout_css(scroll: bool = True, immersive: bool = False, height: str = DEFAULT_HEIGHT) -> str:
    """
    生成布局 CSS。`scroll=False` 返回空串（完全回退到改动前的行为）。
    """
    rules = []
    if scroll:
        boxes = f"{_cls(KEY_LEFT_BOX)}, {_cls(KEY_RIGHT_BOX)}"
        card = _cls(KEY_CARD)
        offset = card_height_offset(height)
        rules.append(
            "/* 卡内滚动（阶段 6.5）：左右两栏与卡片各自滚动，整页不再跟着滚 */\n"
            f"{boxes}{{height:calc(100vh - {SIDE_OFFSET}px);overflow-y:auto;"
            "overscroll-behavior:contain;padding-right:4px}}\n"
            f"{card}{{height:calc(100vh - {offset}px);overflow-y:auto;"
            "overscroll-behavior:contain;padding-right:8px}}\n"
            "/* 滚动条做细一点，让人一眼看出「滚的是这一块」 */\n"
            f"{boxes}::-webkit-scrollbar,{card}::-webkit-scrollbar{{width:10px;height:10px}}\n"
            f"{boxes}::-webkit-scrollbar-thumb,{card}::-webkit-scrollbar-thumb"
            "{background:#c9d3e0;border-radius:99px;border:3px solid transparent;"
            "background-clip:content-box}\n"
            f"{boxes}, {card}{{scrollbar-width:thin;scrollbar-color:#c9d3e0 transparent}}"
        )
    if immersive:
        # 沉浸模式：把左右两栏的内容藏掉（注意：控件仍在脚本里被创建，状态不会丢）
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
    （上次用的档位/沉浸状态），再否则用默认值。
    只补缺失的键，不覆盖用户在本次会话里的选择。
    """
    prefs = store.load_ui_prefs()
    defaults = {
        IMMERSIVE_KEY: bool(prefs.get("immersive", False)),
        SCROLL_KEY: bool(prefs.get("scroll", True)),
        HEIGHT_KEY: prefs.get("height", DEFAULT_HEIGHT) if prefs.get("height") in HEIGHT_OFFSETS
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
