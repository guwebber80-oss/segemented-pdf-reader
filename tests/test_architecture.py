r"""阶段 5 架构守卫：分层约定不能被无意破坏

约定（见 README「项目结构」与 ui/__init__.py）：
  ① 核心逻辑都在 utils/ 里，而且**不依赖 Streamlit**——这样它们能脱离网页直接跑、
     直接测；一旦有人在里面 `import streamlit`，测试与离线调试就全废了；
  ② 包内模块之间用**相对导入**（`from . import xxx`），外部用 `from utils import xxx`；
  ③ app.py 是**唯一入口**，根目录不再有散落的业务模块；
  ④ ui/ 只放界面辅助（允许用 Streamlit），不参与解析与元数据逻辑。

这类约定最容易被"下次顺手加一行 import"破坏，而且破坏后不会立刻报错
（本阶段实测过一次：metadata.py 里一句函数内的 `from pdf_parser import …`
被 try 静默吞掉，作者列表就多了一个人）。所以专门写一个守卫。
"""

import ast
import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

UTILS = os.path.join(PROJECT, "utils")
UI = os.path.join(PROJECT, "ui")

passed, failed = 0, 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK] {label}")
    else:
        failed += 1
        print(f"  [!!] {label}  {detail}")


def module_files(folder):
    return sorted(name for name in os.listdir(folder)
                  if name.endswith(".py") and name != "__init__.py")


print("=" * 78)
print("① utils/ 里的核心逻辑不得依赖 Streamlit")
offenders = []
for name in module_files(UTILS):
    source = open(os.path.join(UTILS, name), encoding="utf-8").read()
    if "streamlit" in source:
        offenders.append(name)
check(f"{len(module_files(UTILS))} 个模块都不 import streamlit", offenders == [], repr(offenders))

print()
print("② 包内用相对导入，外部用 utils.xxx（不得再出现扁平的 import pdf_parser 之类）")
flat = []
for folder in (UTILS, UI):
    for name in module_files(folder):
        path = os.path.join(folder, name)
        for number, line in enumerate(open(path, encoding="utf-8"), 1):
            stripped = line.strip()
            for module in module_files(UTILS):
                stem = module[:-3]
                if stripped.startswith((f"import {stem}", f"from {stem} import")):
                    flat.append(f"{name}:{number}: {stripped}")
check("没有扁平导入（函数内的延迟导入也算）", flat == [], repr(flat[:3]))

print()
print("③ app.py 是唯一入口，根目录没有散落的业务模块")
root_modules = [name for name in os.listdir(PROJECT)
                if name.endswith(".py") and name != "app.py"]
check("根目录只有 app.py 一个 .py", root_modules == [], repr(root_modules))
check("app.py 存在且能解析", os.path.exists(os.path.join(PROJECT, "app.py")))

print()
print("④ 结构与公开接口（重构后仍要能被 app.py 用）")
expected = {
    "utils": ["pdf_parser", "formula_finder", "table_finder", "image_extractor",
              "metadata", "grobid_client", "metadata_compare", "translator"],
    "ui": ["media", "cards"],
}
for package, names in expected.items():
    folder = os.path.join(PROJECT, package)
    missing = [name for name in names if not os.path.exists(os.path.join(folder, f"{name}.py"))]
    check(f"{package}/ 包含 {len(names)} 个预期模块", missing == [], repr(missing))

try:
    from ui import cards, media                      # noqa: F401
    from utils import pdf_parser                     # noqa: F401
    check("utils / ui 都能正常导入", True)
except Exception as exc:                             # pragma: no cover
    check("utils / ui 都能正常导入", False, f"{type(exc).__name__}: {exc}")

print()
print("⑤ 被搬出去的界面函数仍然是纯函数形态（可用普通 python 调用）")
SAMPLES = r"E:\segemented pdf reader\测试pdf"
sample = os.path.join(SAMPLES, "双栏.pdf")
if not os.path.exists(sample):
    print("  [跳过] 样本不在本机")
else:
    from ui import cards
    result = pdf_parser.parse_pdf(open(sample, "rb").read(), True, True, 200, "image")
    label = cards.card_label(result["cards"][0])
    check("card_label 能对真实卡片生成标签（#编号 · §章节 · 原文页 · N 词）",
          label.startswith("#1") and "词" in label, repr(label[:50]))

print()
print("⑥ 语法与 AST 检查：所有模块都能解析（防止搬运时留下半截代码）")
broken = []
for folder in (UTILS, UI, os.path.join(PROJECT, "tests")):
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(folder, name)
        try:
            ast.parse(open(path, encoding="utf-8").read(), path)
        except SyntaxError as exc:
            broken.append(f"{name}: {exc}")
check("utils / ui / tests 下全部 .py 都能解析", broken == [], repr(broken[:2]))

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed}")
sys.exit(1 if failed else 0)
