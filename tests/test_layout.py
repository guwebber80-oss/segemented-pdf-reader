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
print("① 卡内滚动 CSS：三块区域各自的滚动区，整页不动")
print("-" * 78)
css = layout.layout_css(scroll=True, immersive=False, height="中")
# 用「解析规则」而不是「比对整串」：左右两栏共用一条选择器，写死字符串会很脆
height_rules = re.findall(r"([^\n{}]*)\{height:calc\(100vh - (\d+)px\);overflow-y:auto", css)
check("左右两栏共用一条滚动规则、卡片单独一条（共 2 条）",
      len(height_rules) == 2, repr([sel.strip() for sel, _ in height_rules]))
check("左右栏的滚动高度用 SIDE_OFFSET，卡片用档位偏移",
      any(layout.KEY_LEFT_BOX in sel and layout.KEY_RIGHT_BOX in sel
          and int(offset) == layout.SIDE_OFFSET for sel, offset in height_rules)
      and any(layout.KEY_CARD in sel and int(offset) == 320 for sel, offset in height_rules),
      repr(height_rules))
check("高度用 vh 计算（适配各种窗口，不需要知道视口像素）", "calc(100vh -" in css)
check("阻止滚动链（滚到卡片底部不会带动整页）", css.count("overscroll-behavior:contain") >= 2)
check("滚动条做了细化样式（让人看出「滚的是这一块」）",
      "::-webkit-scrollbar" in css and "scrollbar-width:thin" in css)
check("绝不会碰 Streamlit 内部结构（没有 data-testid）",
      "data-testid" not in css and "stColumn" not in css and "stHorizontalBlock" not in css)
check("只用官方 st-key- 类名约定", css.count(".st-key-") >= 4)

print()
print("② 回退开关：关掉卡内滚动 = 完全回到改动前")
print("-" * 78)
check("scroll=False 返回空字符串（不注入任何样式）",
      layout.layout_css(scroll=False, immersive=False, height="中") == "")
check("关掉滚动时也不残留沉浸规则的副作用（非沉浸下为空）",
      layout.layout_css(scroll=False, immersive=False, height="高") == "")

print()
print("③ 沉浸模式：隐藏左右两栏，中栏铺满")
print("-" * 78)
css_im = layout.layout_css(scroll=True, immersive=True, height="中")
hide_rules = re.findall(r"([^\n{}]*)\{display:none\}", css_im)
check("沉浸时恰好有一条 display:none 规则，同时点名左栏与右栏（没误伤卡片区）",
      len(hide_rules) == 1
      and layout.KEY_LEFT_BOX in hide_rules[0] and layout.KEY_RIGHT_BOX in hide_rules[0]
      and layout.KEY_CARD not in hide_rules[0], repr(hide_rules))
check("沉浸时卡片滚动区仍保留（沉浸不等于放弃卡内滚动）",
      f".st-key-{layout.KEY_CARD}" in css_im and "overflow-y:auto" in css_im)

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
offsets = {mode: layout.card_height_offset(mode) for mode in layout.HEIGHT_OFFSETS}
check("三档高度互不相同（矮/中/高 = 减掉的像素越少卡片越高）",
      len(set(offsets.values())) == len(offsets)
      and offsets["矮"] > offsets["中"] > offsets["高"], repr(offsets))
check("认不出的档位退回默认档",
      layout.card_height_offset("很大") == layout.card_height_offset(layout.DEFAULT_HEIGHT))
check("档位真的写进了 CSS（矮 → 减 380px）",
      "calc(100vh - 380px)" in layout.layout_css(scroll=True, immersive=False, height="矮"))
check("脏档位不会生成坏 CSS（会退回默认档）",
      "calc(100vh - 320px)" in layout.layout_css(scroll=True, immersive=False, height="？"),)

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed} · 跳过 {skipped}")
sys.exit(1 if failed else 0)
