"""
ui.layout —— 阅读布局的纯逻辑（阶段 6.6 小步 1：卡片区固定高度 + 卡内滚动）

**这一版只做一件事**：让「卡片正文 + 该卡插图」落在一个**固定高度、内部滚动**的区域里，
这样翻页按钮与进度条不会随内容长短上下跳。

三条硬约束（都是 2026-09-16 那次失败换来的教训，写在这里防止自己再走回头路）：

1) **只用 Streamlit 原生 API**：固定高度可滚动区域 = `st.container(height=N)`。
   不注入 `<style>`、不靠 `st-key-<key>` 类名命中、不碰 `data-testid`——
   那条链路在用户环境里没能生效，而我在沙箱里又验证不了（跑不起浏览器）。
   本模块**完全不产出 CSS**（测试里有一条断言禁止出现 style 标签 / st-key / data-testid）。
2) **不碰左右两栏的布局**：不动列宽、不压缩、不隐藏。第一次失败就是把列宽压到 0.0001、
   把内容塞进 1px 高容器「藏起来」，那是跟框架的布局打架，结果界面完全错乱。
3) **可回退**：关掉开关就不传 height=，完全回到改动前的样子。

高度只能给像素：原生 height= 只吃整数，Streamlit 也不把视口高度交给 Python，
所以做成「矮 / 中 / 高」三档让用户按显示器挑（README 里如实写明）。
"""

import streamlit as st

# ---------------- 会话键（同时也是控件 key） ----------------
SCROLL_KEY = "ui_scroll_layout"       # 卡片固定高度（卡内滚动）开关
HEIGHT_KEY = "ui_card_height"         # 卡片高度档位

# ---------------- 卡片滚动区的 key（只作标识，不参与任何 CSS） ----------------
KEY_CARD = "reader_card"

# ---------------- 高度档位（像素） ----------------
DEFAULT_HEIGHT = "中"
HEIGHT_PRESETS = {"矮": 360, "中": 440, "高": 520}
DEFAULT_SCROLL = True


def card_height(mode: str) -> int:
    """档位名 → 卡片区高度（像素）；不认识的档位退回默认档"""
    return HEIGHT_PRESETS.get(mode, HEIGHT_PRESETS[DEFAULT_HEIGHT])


def card_box_height(scroll: bool, mode: str):
    """要不要给卡片容器传 height：关掉开关返回 None（= 不传这个参数，完全回退）"""
    return card_height(mode) if scroll else None


def box_kwargs(height) -> dict:
    """把「高度或 None」变成 st.container() 的 kwargs（None = 什么都不传）"""
    return {} if height is None else {"height": height}


def ensure_state() -> None:
    """把布局相关的会话状态准备好（控件创建之前调用一次；只补缺失的键）"""
    st.session_state.setdefault(SCROLL_KEY, DEFAULT_SCROLL)
    st.session_state.setdefault(HEIGHT_KEY, DEFAULT_HEIGHT)