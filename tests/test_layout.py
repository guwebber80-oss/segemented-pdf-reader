r"""阶段 6.6 验收（小步 1）：卡片区固定高度 + 卡内滚动 —— ui/layout.py

这一小步只做一件事：把「卡片正文 + 该卡插图」放进一个**固定高度的原生可滚动容器**，
于是翻页按钮与进度条不随卡片长短上下跳。

套件守着四条底线（前两条是 2026-09-16 那次失败的教训，防止走回头路）：
  ① **不产出任何 CSS**：不许出现 style 标签 / st-key / data-testid / display:none——
     上次就是把效果押在「注入 CSS + 类名命中」上，在用户环境里完全没生效；
  ② **不碰左右两栏**：不许有列宽/隐藏相关的东西（上次把列宽压到 0.0001 把界面弄乱了）；
  ③ **可回退**：关掉开关就不传 height=，完全回到改动前；
  ④ 高度档位合法 + 接线真的存在（app.py 里确实用原生容器包住了卡片区）。

不需要浏览器、不联网、不依赖 Streamlit 运行时。
"""

import ast
import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

# ---- 控制台编码兜底（批处理用 chcp 936，GBK 下打印生僻字符会抛异常）----
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

from ui import layout       # noqa: E402

passed, failed, skipped = 0, 0, 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK] {label}")
    else:
        failed += 1
        print(f"  [!!] {label}  {detail}")


def read_source(relative):
    with open(os.path.join(PROJECT, relative), encoding="utf-8") as handle:
        return handle.read()


layout_source = read_source(os.path.join("ui", "layout.py"))
app_source = read_source("app.py")

print("=" * 78)
print("① 不许再用「注入 CSS / 类名命中」那套（上次失败的原因）")
print("-" * 78)
def strip_docstrings(source):
    """只留真正会执行的代码：去掉所有 docstring（注释本来就不进 AST）。

    为什么要这样：docstring/注释里**提到** style 标签、st-key、data-testid 是刻意的
    （那里正是在写"别这么干"），不能当成违规；但真代码里出现就必须拦住。
    """
    tree = ast.parse(source)
    for node in list(ast.walk(tree)):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)
    return ast.unparse(tree)


banned = ["<style>", "st-key-", "data-testid", "display:none"]
code_only = strip_docstrings(layout_source)
found = [token for token in banned if token in code_only]
check("布局模块的**代码**里没有任何 CSS 注入 / 类名命中 / 内联隐藏手法", found == [], repr(found))
check("app.py 这块接线用的是原生容器（st.container(key=KEY_CARD, **card_kwargs)）",
      "st.container(key=KEY_CARD, **card_kwargs)" in app_source)

print()
print("② 不碰左右两栏的布局")
print("-" * 78)
check("模块里没有列宽/隐藏相关的接口（不压缩、不隐藏任何一栏）",
      not any(hasattr(layout, name) for name in
              ("columns_spec", "text_pad_spec", "figures_pad_spec", "IMMERSIVE_SPEC")))
check("模块里没有「沉浸」相关开关（沉浸还没做，等这一小步验完）",
      not any(hasattr(layout, name) for name in ("IMMERSIVE_KEY", "immersive", "side_box_height")))

print()
print("③ 高度档位与回退开关")
print("-" * 78)
heights = {mode: layout.card_height(mode) for mode in layout.HEIGHT_PRESETS}
check("三档高度：正整数、互不相同、矮 < 中 < 高",
      all(isinstance(v, int) and v > 0 for v in heights.values())
      and len(set(heights.values())) == len(heights)
      and heights["矮"] < heights["中"] < heights["高"], repr(heights))
check("认不出的档位退回默认档",
      layout.card_height("很大") == layout.card_height(layout.DEFAULT_HEIGHT))
check("开启时给出档位对应的像素高度",
      layout.card_box_height(True, "中") == layout.HEIGHT_PRESETS["中"])
check("关掉开关 → 不传 height（完全回退）",
      layout.card_box_height(False, "中") is None)
check("kwargs 拼装正确（None 不传、数字传 height）",
      layout.box_kwargs(360) == {"height": 360} and layout.box_kwargs(None) == {})
check("默认是「开 + 中档」（用户不调也是个合理默认）",
      layout.DEFAULT_SCROLL is True and layout.DEFAULT_HEIGHT in layout.HEIGHT_PRESETS)
check("卡片区的 key 存在且是普通标识（不参与 CSS）",
      isinstance(layout.KEY_CARD, str) and bool(layout.KEY_CARD) and "-" not in layout.KEY_CARD)

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed} · 跳过 {skipped}")
sys.exit(1 if failed else 0)