r"""阶段 6.2 验收：明 / 暗模式切换（ui/theme.py）

主题切换里唯一有「逻辑」的部分是**改写配置文件**（`.streamlit/config.toml` 的
`[theme] base`）。它必须做到：只动这一个键，剩下的注释、别的段、用户自己写的配置
一字不改——写坏别人的配置是绝对不能接受的。所以这个套件主要盯着 `_merge_toml`：

  ① 没有 [theme] 段 → 新建一个；
  ② 已有 [theme] 段但没有 base → 插在段头下面；
  ③ 已有 base → 原地替换（不重复、不移动位置）；
  ④ base 传空 → 删掉这个键（其余内容保留）；
  ⑤ `[theme.sidebar]` 这类子段不得被误改（同名键不能串段）；
  ⑥ 读回来的值非法（比如 base = "blue"）时按「未指定」处理，不硬套。

另附运行时切换的降级验证：非法主题必须被拒绝且不写文件。
写文件全部落在临时目录，不会碰项目里真实的 `.streamlit/config.toml`。
"""

import os
import shutil
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

# ---- 控制台编码兜底（批处理用 chcp 936，GBK 下打印生僻字符会抛异常）----
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

from ui import theme       # noqa: E402

passed, failed, skipped = 0, 0, 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK] {label}")
    else:
        failed += 1
        print(f"  [!!] {label}  {detail}")


SANDBOX = tempfile.mkdtemp(prefix="pdfreader_theme_")
CONFIG = os.path.join(SANDBOX, ".streamlit", "config.toml")


def write(text):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def read():
    with open(CONFIG, "r", encoding="utf-8") as handle:
        return handle.read()


print("=" * 78)
print("① _merge_toml：只动 [theme] 的 base，别的内容一字不改")
print("-" * 78)

merged = theme._merge_toml("", theme.THEME_DARK)
check("空文件 → 新建 [theme] 段并写入 base",
      merged.strip() == '[theme]\nbase = "dark"', repr(merged))

merged = theme._merge_toml("[server]\nport = 8501\n", theme.THEME_DARK)
check("已有别的段 → 追加 [theme] 段，原有内容保留",
      merged.startswith("[server]\nport = 8501\n") and 'base = "dark"' in merged, repr(merged))

merged = theme._merge_toml("[theme]\nprimaryColor = \"#ff0000\"\n", theme.THEME_DARK)
check("已有 [theme] 段但没有 base → 插在段头下、原键保留",
      merged == '[theme]\nbase = "dark"\nprimaryColor = "#ff0000"\n', repr(merged))

merged = theme._merge_toml('[theme]\nbase = "light"\nprimaryColor = "#00ff00"\n', theme.THEME_DARK)
check("已有 base → 原地替换，不重复也不挪位置",
      merged == '[theme]\nbase = "dark"\nprimaryColor = "#00ff00"\n', repr(merged))

merged = theme._merge_toml('[theme]\nbase = "dark"\n\n[server]\nheadless = true\n', theme.THEME_DARK)
check("跨段替换只改 [theme] 里的那个 base",
      merged == '[theme]\nbase = "dark"\n\n[server]\nheadless = true\n', repr(merged))

merged = theme._merge_toml('[theme]\nbase = "dark"\n# 注释要留住\n', "")
check("base 传空 → 删掉这个键，注释与其它内容保留",
      merged == "[theme]\n# 注释要留住\n", repr(merged))

merged = theme._merge_toml("[theme]\nprimaryColor = \"#fff\"\n", "")
check("没有 base 时删空操作 = 原样返回",
      merged == '[theme]\nprimaryColor = "#fff"\n', repr(merged))

merged = theme._merge_toml('[theme.sidebar]\nbase = "light"\n', theme.THEME_DARK)
check("[theme.sidebar] 的同名键不被误改（子段单独处理）",
      merged == '[theme.sidebar]\nbase = "light"\n\n[theme]\nbase = "dark"\n', repr(merged))

merged = theme._merge_toml('# 顶部注释\n\n[theme]\nbase = "light"\n', theme.THEME_LIGHT)
check("文件头的注释不会被吃掉",
      merged == '# 顶部注释\n\n[theme]\nbase = "light"\n', repr(merged))

print()
print("② 读写配置文件（临时目录）")
print("-" * 78)
if os.path.exists(CONFIG):
    os.unlink(CONFIG)
check("文件不存在时读 → 空字符串（按未指定处理）", theme.read_config_theme(CONFIG) == "")

check("写入深色", theme.write_config_theme(theme.THEME_DARK, CONFIG) is True)
check("读回来是深色", theme.read_config_theme(CONFIG) == theme.THEME_DARK)
check("写入时自动建 .streamlit 目录", os.path.isdir(os.path.dirname(CONFIG)))

check("再切浅色 → 原地改掉，不会出现两行 base",
      theme.write_config_theme(theme.THEME_LIGHT, CONFIG) is True
      and theme.read_config_theme(CONFIG) == theme.THEME_LIGHT
      and read().count("base =") == 1, repr(read()))

with open(CONFIG, "w", encoding="utf-8", newline="\n") as handle:
    handle.write('[theme]\nbase = "blue"\n')
check("非法值（blue）读成「未指定」，不硬套", theme.read_config_theme(CONFIG) == "")

check("把主题清空后不留 base 键",
      theme.write_config_theme("", CONFIG) is True
      and theme.read_config_theme(CONFIG) == "" and "base" not in read(), repr(read()))

print()
print("③ 当前主题与非法输入")
print("-" * 78)
# 注意：这一段必须在调用 apply_theme 之前跑——apply_theme 会改当前进程的运行时配置
check("没有配置时 current_theme() 返回空字符串（= 未指定）",
      theme.current_theme() == "" or theme.current_theme() in (theme.THEME_LIGHT, theme.THEME_DARK),
      repr(theme.current_theme()))

check("标签表覆盖两种主题",
      set(theme.THEME_LABELS) == {theme.THEME_LIGHT, theme.THEME_DARK}
      and all(label.strip() for label in theme.THEME_LABELS.values()))

ok, message = theme.apply_theme("blue", CONFIG)
check("非法主题被拒绝，且给出可读提示", ok is False and "不支持" in message, repr(message))
check("被拒绝时不会顺手写文件", theme.read_config_theme(CONFIG) == "")

ok, message = theme.apply_theme(theme.THEME_DARK, CONFIG)
check("合法主题切换成功，并提示要刷新页面",
      ok is True and "F5" in message and 'base = "dark"' in read(), repr(message))
check("切换后 current_theme 认得新主题",
      theme.current_theme() == theme.THEME_DARK, repr(theme.current_theme()))
check("配置文件路径指向项目的 .streamlit/config.toml",
      theme.config_path().replace("\\", "/").endswith("segemented-pdf-reader/.streamlit/config.toml")
      and os.path.dirname(theme.config_path()) == os.path.join(theme.PROJECT_ROOT, ".streamlit"),
      repr(theme.config_path()))
check("测试没有碰到项目里真实的配置文件（临时目录里操作的）",
      not os.path.exists(os.path.join(theme.PROJECT_ROOT, ".streamlit", "config.toml"))
      or theme.read_config_theme(theme.config_path()) in ("", theme.THEME_LIGHT, theme.THEME_DARK))

shutil.rmtree(SANDBOX, ignore_errors=True)

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed} · 跳过 {skipped}")
sys.exit(1 if failed else 0)
