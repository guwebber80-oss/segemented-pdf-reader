"""
ui.theme —— 明 / 暗模式切换（阶段 6.2）

**先说清楚能做到什么、做不到什么**（结论来自对 Streamlit 1.63 源码的实测，不是猜的）：

  · 没有 `st.set_theme()` 这种运行时 API；`st.set_page_config()` 也没有主题参数；
  · 主题只有两条生效途径：
      ① 配置文件 `.streamlit/config.toml` 里的 `[theme] base = "light" | "dark"`（服务启动时读）；
      ② 浏览器里 ☰ → Settings → Theme 的手动切换（纯前端、立即生效、只存在这台浏览器里）；
  · 主题是随「新建会话」下发的（`app_session.py` 只在 NewSession 消息里带 custom_theme），
    所以代码改了配置**要刷新一次页面（F5）**才看得到；
  · 静态样式表里**没有任何 CSS 变量**（Streamlit 用 emotion 内联样式），
    「注入 CSS 局部改色」这条路在 1.63 上既不可靠也会随版本失效，因此本模块不做这种 hack，
    宁可把限制如实写在界面上。

所以这里的做法是：应用内给一个**明 / 暗切换按键**，它一次做两件事——
  1) `streamlit.config.set_option("theme.base", …)`：让**当前进程**按新主题下发（刷新即生效）；
  2) 把 `[theme] base` 写进 `.streamlit/config.toml`：重启服务后依然是这个主题。

点完按钮会明确提示「按 F5 刷新后生效」，不会让人觉得按钮没反应。
想在本浏览器里**立即**切换（不刷新），用 Streamlit 自带的 ☰ → Settings → Theme。
"""

import os
import re

import streamlit as st

# ============================================================
# 1. 常量与路径
# ============================================================
THEME_LIGHT, THEME_DARK = "light", "dark"
THEME_LABELS = {THEME_LIGHT: "☀️ 浅色", THEME_DARK: "🌙 深色"}

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_RELATIVE = os.path.join(".streamlit", "config.toml")


def config_path() -> str:
    """项目里的 Streamlit 配置文件路径"""
    return os.path.join(PROJECT_ROOT, CONFIG_RELATIVE)


# ============================================================
# 2. 配置文件读写（行级合并，不解析 TOML）
# ============================================================
def _merge_toml(text: str, base: str) -> str:
    """
    在 TOML 文本里设置 / 删除 `[theme]` 段的 `base`，其它内容（注释、别的段）原样保留。

    为什么不解析 TOML：`tomllib` 只能读不能写，为了写一个键引入写库不值得；
    行级替换的出错面小得多，而且用户的注释、格式都能留住。
    base 传空字符串 = 删掉这个键（交回 Streamlit 自己决定）。
    """
    lines = text.splitlines()
    header = None
    for index, line in enumerate(lines):
        if line.strip() == "[theme]":
            header = index
            break

    if header is None:
        if not base:
            return text                      # 没有 [theme] 段、也不用设：原样返回
        body = text.rstrip("\n")
        prefix = (body + "\n\n") if body else ""
        return f'{prefix}[theme]\nbase = "{base}"\n'

    # 段的边界：下一个 [xxx] 段头
    end = len(lines)
    for index in range(header + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break

    key_line = None
    for index in range(header + 1, end):
        if re.match(r"^\s*base\s*=", lines[index]):
            key_line = index
            break

    if base:
        if key_line is None:
            lines.insert(header + 1, f'base = "{base}"')
        else:
            lines[key_line] = f'base = "{base}"'
    elif key_line is not None:
        del lines[key_line]
    return "\n".join(lines) + "\n"


def read_config_theme(path: str = "") -> str:
    """读配置文件里的 `[theme] base`；没写或读不到就返回空字符串"""
    target = path or config_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            text = handle.read()
    except Exception:
        return ""
    section = -1
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = 0 if stripped == "[theme]" else -1
            continue
        if section == 0:
            match = re.match(r'^\s*base\s*=\s*["\']([^"\']*)["\']', stripped)
            if match:
                value = match.group(1).strip().lower()
                return value if value in (THEME_LIGHT, THEME_DARK) else ""
    return ""


def write_config_theme(base: str, path: str = "") -> bool:
    """把主题写进配置文件（base 传空 = 删掉这个键）；返回是否写成功"""
    target = path or config_path()
    try:
        existing = ""
        if os.path.exists(target):
            with open(target, "r", encoding="utf-8") as handle:
                existing = handle.read()
        merged = _merge_toml(existing, base)
        if merged == existing:
            return True
        folder = os.path.dirname(target)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(merged)
        return True
    except Exception as exc:
        print(f"[theme] 写配置文件失败：{type(exc).__name__}: {exc}", flush=True)
        return False


# ============================================================
# 3. 运行时切换
# ============================================================
def runtime_theme() -> str:
    """当前进程里 `theme.base` 的值（没设过就是空字符串）"""
    try:
        from streamlit import config as st_config
        value = st_config.get_option("theme.base")
        return value if value in (THEME_LIGHT, THEME_DARK) else ""
    except Exception:
        return ""


def current_theme() -> str:
    """当前生效的主题：配置文件优先（重启后生效的那个），其次当前进程的设置"""
    return read_config_theme() or runtime_theme()


def _set_runtime_theme(base: str) -> bool:
    """
    让当前进程按新主题下发（只影响之后新建的会话，所以用户要刷新页面）。

    这里用的是 `streamlit.config.set_option`——它不属于公开文档的稳定 API，
    所以整段包在 try/except 里：万一将来改名，最坏情况是「要重启服务才生效」，
    而不是把界面弄崩。
    """
    try:
        from streamlit import config as st_config
        st_config.set_option("theme.base", base or THEME_LIGHT)
        return True
    except Exception as exc:
        print(f"[theme] 运行时切换失败：{type(exc).__name__}: {exc}", flush=True)
        return False


def apply_theme(base: str, path: str = "") -> tuple:
    """切换主题，返回 (是否成功, 给用户看的中文说明)；path 只给测试传"""
    if base not in (THEME_LIGHT, THEME_DARK):
        return False, f"不支持的主题：{base}"

    runtime_ok = _set_runtime_theme(base)
    file_ok = write_config_theme(base, path)
    label = THEME_LABELS[base]

    if not file_ok:
        return False, (f"已切到{label}，但**写配置文件失败**（{path or config_path()}）："
                       "重启服务后会回到原来的主题，请检查目录写权限。")
    note = "" if runtime_ok else "（当前进程没能改到运行时配置，需要重启服务才生效）"
    return True, f"已切到{label}：按 **F5 刷新页面**即可看到{note}，重启服务后也保持这个主题。"


# ============================================================
# 4. 左栏的切换按键
# ============================================================
def render_theme_control() -> None:
    """明 / 暗切换按键（放在左栏上传框下面，没选文件时也能用）"""
    st.markdown("**🌗 明 / 暗模式**")
    current = current_theme()

    col_light, col_dark = st.columns(2)
    clicked = ""
    if col_light.button("☀️ 浅色", key="theme_light", width="stretch",
                        disabled=(current == THEME_LIGHT),
                        help="把服务端默认主题设为浅色（刷新页面后生效）"):
        clicked = THEME_LIGHT
    if col_dark.button("🌙 深色", key="theme_dark", width="stretch",
                       disabled=(current == THEME_DARK),
                       help="把服务端默认主题设为深色（刷新页面后生效）"):
        clicked = THEME_DARK

    if clicked:
        ok, message = apply_theme(clicked)
        st.session_state["theme_message"] = ("✅ " if ok else "⚠️ ") + message
        st.rerun()

    if st.session_state.get("theme_message"):
        st.info(st.session_state.pop("theme_message"))

    st.caption(
        "切换的是**服务端默认主题**：刷新页面（F5）后生效，重启服务也保持。"
        "想在**本浏览器里立即**切换、不用刷新，用右上角 **☰ → Settings → Theme**"
        "（那个选择只存在本机浏览器里，换个浏览器要重新选）。"
        + ("" if current else "　当前：未指定。")
    )
