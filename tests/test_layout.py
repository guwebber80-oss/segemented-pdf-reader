r"""阶段 6.5 验收：阅读布局（卡内滚动 / 固定页面 / 沉浸模式）——ui/layout.py

这一层是「生成 CSS + 列宽比例」的纯逻辑，可以完全离线验证。重点守四件事：

  ① **只用自己的 key**：生成的 CSS 只能出现 `.st-key-<自己的key>` 这类官方约定，
     绝不允许出现 `data-testid`（Streamlit 内部结构会随版本变，6.3 的分析里已列为不可靠做法）；
  ② **回退开关真的能回退**：`layout_css(scroll=False)` 必须返回空串 = 完全回到改动前；
  ③ **沉浸模式两条规则**：隐藏左右两栏 + 中栏铺满（列宽比例），且侧栏虽然被隐藏，
     列宽仍留了极小值（Streamlit 才会照常渲染里面的控件，上传的文件/语言/设置不会丢）；
  ④ **高度档位**：三档互不相同、认不出的档位退回默认档。

配套的端到端验证在 `.dsh-scratch/smoke_app_upload.py`（真跑 app.py 点沉浸按钮）。
"""

import os
import re
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


print("=" * 78)
print("① 卡内滚动：交给原生 st.container(height=N)，不靠自定义 CSS")
print("-" * 78)
css = layout.layout_css(scroll=True, immersive=False, height="中")
check("注入的 CSS 里不再有「用 CSS 定高/滚动」的规则（改用原生 height=，不赌 CSS）",
      "100vh" not in css and "overflow-y:auto" not in css and "calc(" not in css, css[:160])
check("只保留滚动条样式（细一点，让人看出滚的是哪一块）",
      "::-webkit-scrollbar" in css and "scrollbar-width:thin" in css)
check("非沉浸时没有任何 display:none（不误伤界面）", "display:none" not in css)
check("绝不会碰 Streamlit 内部结构（没有 data-testid / stColumn / stHorizontalBlock）",
      "data-testid" not in css and "stColumn" not in css and "stHorizontalBlock" not in css)

check("容器高度：开启时按档位给像素",
      layout.side_box_height(True, "中") == layout.side_height("中")
      and layout.card_box_height(True, "中") == layout.HEIGHT_PRESETS["中"])
check("容器高度：左右两栏比卡片高一点（栏里没有页头与进度条）",
      layout.side_box_height(True, "中") > layout.card_box_height(True, "中"))
check("容器高度：关掉卡内滚动 → 不传 height（完全回退）",
      layout.side_box_height(False, "中") is None
      and layout.card_box_height(False, "中") is None
      and layout.box_kwargs(None) == {})
check("沉浸模式：左右两栏压到 1px（原生隐藏，不依赖 CSS）",
      layout.side_box_height(True, "中", immersive=True) == layout.IMMERSIVE_BOX_HEIGHT == 1
      and layout.box_kwargs(layout.IMMERSIVE_BOX_HEIGHT) == {"height": 1})
check("沉浸模式不影响卡片区高度（沉浸不等于放弃卡内滚动）",
      layout.card_box_height(True, "中") == layout.HEIGHT_PRESETS["中"])
check("沉浸模式即使关掉卡内滚动，两栏仍然压成 1px（隐藏不依赖滚动开关）",
      layout.side_box_height(False, "中", immersive=True) == 1)
check("三档高度都是正数且互不相同（原生 height 不接受 0 或 None）",
      len(set(layout.HEIGHT_PRESETS.values())) == len(layout.HEIGHT_PRESETS)
      and all(isinstance(value, int) and value > 0 for value in layout.HEIGHT_PRESETS.values()),
      repr(layout.HEIGHT_PRESETS))

print()
print("② 回退开关：关掉卡内滚动 = 完全回到改动前")
print("-" * 78)
check("scroll=False 且非沉浸 → 返回空字符串（不注入任何样式）",
      layout.layout_css(scroll=False, immersive=False, height="中") == "")
check("回退时容器也不再传 height（app 侧拼接 kwargs 的依据）",
      layout.container_height(False, "高") is None)

print()
print("③ 沉浸模式：隐藏左右两栏，中栏铺满")
print("-" * 78)
css_im = layout.layout_css(scroll=True, immersive=True, height="中")
hide_rules = re.findall(r"([^\n{}]*)\{display:none\}", css_im)
check("沉浸时恰好有一条 display:none 规则，同时点名左栏与右栏（没误伤卡片区）",
      len(hide_rules) == 1
      and layout.KEY_LEFT_BOX in hide_rules[0] and layout.KEY_RIGHT_BOX in hide_rules[0]
      and layout.KEY_CARD not in hide_rules[0], repr(hide_rules))
check("沉浸时卡片区照常滚动（沉浸不等于放弃卡内滚动）",
      layout.KEY_CARD not in hide_rules[0]
      and layout.container_height(True, "中") is not None)

spec = layout.columns_spec(True)
check("沉浸时中栏占满（比例 ≥ 99.9%）",
      spec[1] / sum(spec) > 0.999, repr(spec))
check("侧栏列宽不是 0（Streamlit 仍会渲染它们，控件状态才不会丢）",
      spec[0] > 0 and spec[2] > 0, repr(spec))
check("非沉浸时是原来的三栏比例", layout.columns_spec(False) == [0.85, 2.7, 1.05])
check("两种模式的列宽清单互不影响（返回的是副本）",
      layout.columns_spec(False) is not layout.NORMAL_SPEC)

check("沉浸时正文留白比例收窄（正文不至于宽到 1200px）",
      layout.text_pad_spec(True) == [1.6, 5, 1.6] and layout.text_pad_spec(False) == [1, 6, 1])
check("沉浸时插图列更宽", layout.figures_pad_spec(True) == [0.5, 9, 0.5]
      and layout.figures_pad_spec(False) == [0.4, 8, 0.4])

print()
print("④ 卡片高度档位")
print("-" * 78)
heights = {mode: layout.card_height(mode) for mode in layout.HEIGHT_PRESETS}
check("三档高度互不相同、都是正像素（矮 < 中 < 高）",
      len(set(heights.values())) == len(heights)
      and heights["矮"] < heights["中"] < heights["高"], repr(heights))
check("认不出的档位退回默认档",
      layout.card_height("很大") == layout.card_height(layout.DEFAULT_HEIGHT))
check("侧栏高度 = 卡片高度 + 固定余量（栏里没有页头/进度条）",
      all(layout.side_height(mode) == layout.card_height(mode) + layout.SIDE_EXTRA
          for mode in layout.HEIGHT_PRESETS))
check("档位进入 CSS 的调用不会炸（height 参数保留但当前不参与生成）",
      isinstance(layout.layout_css(scroll=True, immersive=True, height="矮"), str))

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed} · 跳过 {skipped}")
sys.exit(1 if failed else 0)
