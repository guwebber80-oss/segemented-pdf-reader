r"""界面面板冒烟：用**假 Streamlit** 真跑一遍被拆出去的面板（阶段 5.2b 验证）

为什么需要：Streamlit 的界面代码只有「上传 PDF 之后」才会执行，AppTest 停在
上传页，所以搬代码时漏一个参数 / 漏一句 import，静态检查过了、实际一点"中文"
或"放大图片"就炸 NameError（本轮真发生过：ui/cards.py 少了 translate、
ui/media.py 少了 render_full_image，被未定义名检查抓到）。这里用一个假 streamlit
模块把渲染调用全部变成记录，真正执行面板代码，从而提前抓住这类错误。

假 st 的约定：
  · 控件返回「合理的默认值」（文本框空串、按钮 False、下拉取第一项……），
    这样面板里的取值与写回逻辑都能走通；
  · st.columns(n) / st.tabs([...]) 返回对应数量的占位对象（保证解包成功）；
  · st.rerun() / st.stop() 抛一个被本测试捕获的哨兵异常（面板请求重跑是正常行为）。
"""

import os
import sys
import types

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

CALLS = []


class _Rerun(Exception):
    """面板请求 st.rerun() / st.stop() 时抛出，本测试捕获后视为正常"""


class _SessionState(dict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:                      # pragma: no cover
            raise AttributeError(item) from exc

    def __setattr__(self, key, value):
        self[key] = value


class _Stub:
    """万能占位：可调用、可当上下文管理器、可继续点属性"""

    def __init__(self, name="st"):
        object.__setattr__(self, "_name", name)

    def __call__(self, *args, **kwargs):
        CALLS.append((self._name, args, kwargs))
        name = self._name.split(".")[-1]
        if name == "columns":
            spec = args[0] if args else kwargs.get("spec", 1)
            count = spec if isinstance(spec, int) else len(spec)
            return [_Stub(f"{self._name}.col{i}") for i in range(count)]
        if name == "tabs" and args and isinstance(args[0], (list, tuple)):
            return [_Stub(f"{self._name}.tab{i}") for i in range(len(args[0]))]
        if name in ("text_input", "text_area"):
            return kwargs.get("value", "") or ""
        if name in ("selectbox", "radio"):
            options = args[1] if len(args) > 1 else kwargs.get("options", [])
            return options[0] if options else None
        if name == "button":
            return False
        if name == "toggle":
            return bool(kwargs.get("value", False))
        if name == "slider":
            return args[1] if len(args) > 1 else kwargs.get("value", 0)
        if name in ("number_input", "file_uploader"):
            return None if name == "file_uploader" else 0
        if name in ("rerun", "stop"):
            raise _Rerun()
        return _Stub(self._name)

    def __getattr__(self, item):
        if item.startswith("_"):
            raise AttributeError(item)
        return _Stub(f"{self._name}.{item}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_fake_streamlit():
    fake = types.ModuleType("streamlit")
    root = _Stub("st")
    for name in ("set_page_config", "title", "caption", "markdown", "subheader", "divider",
                 "image", "dataframe", "metric", "columns", "tabs", "expander", "button",
                 "text_input", "text_area", "selectbox", "radio", "toggle", "slider",
                 "number_input", "file_uploader", "spinner", "empty", "error", "warning",
                 "info", "success", "json", "code", "write", "link_button", "download_button",
                 "rerun", "stop", "checkbox", "multiselect", "form", "form_submit_button"):
        setattr(fake, name, getattr(root, name))
    fake.session_state = _SessionState()
    fake.__getattr__ = lambda item: getattr(root, item)          # 兜底
    sys.modules["streamlit"] = fake
    return fake


install_fake_streamlit()

from ui import cards, diagnostics, media, metadata_panel       # noqa: E402
from utils import image_extractor, metadata as metadata_mod, pdf_parser   # noqa: E402

SAMPLES = r"E:\segemented pdf reader\测试pdf"
SAMPLE = os.path.join(SAMPLES, "双栏.pdf")

passed, failed = 0, 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK] {label}")
    else:
        failed += 1
        print(f"  [!!] {label}  {detail}")


def run(label, func, *args, **kwargs):
    """执行一个界面函数，把 st.rerun()/st.stop() 当正常"""
    global passed, failed
    try:
        func(*args, **kwargs)
    except _Rerun:
        pass
    except Exception as exc:
        failed += 1
        print(f"  [!!] {label} 抛异常：{type(exc).__name__}: {exc}")
        return False
    passed += 1
    print(f"  [OK] {label}")
    return True


print("=" * 78)
if not os.path.exists(SAMPLE):
    print("  [跳过] 样本不在本机")
    sys.exit(0)

pdf_bytes = open(SAMPLE, "rb").read()
result = pdf_parser.parse_pdf(pdf_bytes, True, True, 200, "image")
blocks = result["blocks"]
paragraphs = result["paragraphs"]
clusters = result["formula_clusters"]
table_regions = result["table_regions"]
pages = result["pages"]
metadata = metadata_mod.extract_metadata(pdf_bytes, blocks)

# 与 app.py 的调用参数保持一致
image_data = image_extractor.extract_images(pdf_bytes)
images = image_data["images"]
association = image_extractor.associate_with_cards(images, result["cards"])
background_blocks = [b for b in paragraphs
                     if b.role in (pdf_parser.ROLE_AUTHOR, pdf_parser.ROLE_FRONT_MATTER,
                                   pdf_parser.ROLE_COPYRIGHT, pdf_parser.ROLE_LABEL,
                                   pdf_parser.ROLE_REFERENCE, pdf_parser.ROLE_HEADER_FOOTER)]

print("① 面板（阶段 5.2b 搬出去的部分）")
run("元数据面板 render_metadata_panel", metadata_panel.render_metadata_panel, metadata)
run("GROBID 交叉校验面板 render_grobid_panel", metadata_panel.render_grobid_panel,
    metadata_mod.__name__, metadata, pdf_bytes, types.SimpleNamespace(name="双栏.pdf", size=123))
run("论文背景信息面板 render_background_panel", diagnostics.render_background_panel,
    background_blocks)
run("解析诊断面板 render_diagnostics", diagnostics.render_diagnostics,
    image_extractor.ASSOC_MAX_GAP, association, blocks, clusters, image_data, images,
    metadata, pages, paragraphs, result, table_regions)

print()
print("② 卡片与媒体渲染（阶段 5.2a 搬出去的部分）")
run("card_label（真实卡片）", cards.card_label, result["cards"][0])
formula_item = next((c for c in result["cards"] if any(k == "formula" for k, _ in c.segments)), None)
table_item = next((c for c in result["cards"] if any(k == "table" for k, _ in c.segments)), None)
if formula_item is None or table_item is None:
    print("  [跳过] 这份样本里没有公式/表格段（换样本才测得到）")
else:
    run("render_card_content（含公式/表格段的卡片）", cards.render_card_content,
        formula_item, pdf_bytes)
    run("render_card_translated 走翻译失败分支（未配后端时应友好报错）",
        cards.render_card_translated, formula_item, pdf_bytes, "", "")

print()
print("③ 渲染调用确实发生过（不是空跑）")
check(f"假 st 记录到 {len(CALLS)} 次渲染/控件调用", len(CALLS) > 50, repr(len(CALLS)))
called = {name.split(".")[-1] for name, _args, _kwargs in CALLS}
for api in ("expander", "markdown", "text_input"):
    check(f"面板调用了 st.{api}", api in called, repr(sorted(called)[:8]))

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed}")
sys.exit(1 if failed else 0)
