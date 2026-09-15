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
              "metadata", "grobid_client", "metadata_compare", "translator", "store",
              "translate_plan"],
    "ui": ["media", "cards", "persist", "theme", "layout"],
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
print("⑦ 未定义名检查：函数体里不许出现「既不是参数、也不是模块级、还不是内置」的名字")
# 为什么必须查：阶段 5 把面板搬进 ui/ 时，漏一个参数或漏一句 import 都不会报错，
# 只有用户上传 PDF、页面跑到那一段时才会炸出 NameError（本轮真发生过一次：
# ui/cards.py 引用 app.py 里的 LANG_EN）。这里按作用域静态检查，提前拦住。
BUILTINS = set(dir(__builtins__)) | {"__file__", "__name__", "self"}
undefined = []


def bound_names(node, into):
    """把一个作用域里所有「绑定」的名字收进来（参数、赋值、for/with/except、import、def/class、lambda 参数）"""
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            into.add(child.id)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            into.add(child.name)
        elif isinstance(child, ast.Lambda):
            into.update(arg.arg for arg in child.args.args)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                into.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(child, ast.ExceptHandler) and child.name:
            into.add(child.name)
        elif isinstance(child, ast.arg):
            into.add(child.arg)


TARGET_FILES = ([os.path.join(UTILS, name) for name in module_files(UTILS)]
                + [os.path.join(UI, name) for name in module_files(UI)]
                # app.py 也要查：实测给它加三栏布局时漏导入 is_formula_item /
                # is_table_item / paragraph_formula_payload，只有真跑到那一段才会 NameError
                + [os.path.join(PROJECT, "app.py")])

for path in TARGET_FILES:
    name = os.path.basename(path)
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    module_bound = set()
    bound_names(tree, module_bound)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        own = set()
        bound_names(node, own)
        loaded = {child.id for child in ast.walk(node)
                  if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)}
        missing = sorted(loaded - own - module_bound - BUILTINS)
        if missing:
            undefined.append(f"{name}::{node.name} 缺少 {missing}")
    # 模块级代码也要查：实测漏过一次——ui/diagnostics.py 的 BACKGROUND_GROUPS
    # 用到 6 个 ROLE_* 常量却没 import，结果**一 import 这个模块就崩**，
    # 而只查函数体的检查完全看不见（模块级赋值不是 FunctionDef）。
    module_loaded = {child.id for statement in tree.body
                     if not isinstance(statement, ast.Assign)
                     for child in ast.walk(statement)
                     if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            module_loaded |= {child.id for child in ast.walk(statement.value)
                              if isinstance(child, ast.Name)
                              and isinstance(child.ctx, ast.Load)}
    module_missing = sorted(module_loaded - module_bound - BUILTINS)
    if module_missing:
        undefined.append(f"{name}::模块级 缺少 {module_missing}")
check("utils / ui / app.py 的函数与模块级代码都没有未定义名", undefined == [], repr(undefined[:3]))

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
print("⑧ 模块级顺序检查：with / for 的表达式不许在它被赋值之前引用")
# 为什么单独查这一条：上面的未定义名检查是「集合比对」，只要这个名字在文件里**任何地方**
# 被赋值过就算通过，看不见**顺序**。实测踩到过：把左栏内容改写成 with left_box: 时，
# 那句 with 跑到了 `left_box = st.container(...)` 之前（同一次批量替换把创建句也换掉了），
# 结果是运行期 NameError: name 'left_box' is not defined —— 10 条断言全绿却没拦住。
# with/for 的表达式是在进入语句体之前求值的，所以只需拿「此前语句绑定过的名字」比对。
ordered = []
app_path = os.path.join(PROJECT, "app.py")
app_tree = ast.parse(open(app_path, encoding="utf-8").read(), app_path)
seen = set()
for statement in app_tree.body:
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                              ast.Import, ast.ImportFrom)):
        bound_names(statement, seen)          # 定义/导入本身就完成了绑定
        continue
    early = []
    if isinstance(statement, ast.With):
        for item in statement.items:
            early += [child.id for child in ast.walk(item.context_expr)
                      if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)]
    elif isinstance(statement, (ast.For, ast.AsyncFor)):
        early += [child.id for child in ast.walk(statement.iter)
                  if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)]
    missing = sorted(set(early) - seen - BUILTINS)
    if missing:
        ordered.append(f"第 {statement.lineno} 行：{missing}")
    own = set()
    bound_names(statement, own)
    seen |= own
check("app.py 里 with / for 用到的名字都在之前绑定过", ordered == [], repr(ordered[:2]))

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed}")
sys.exit(1 if failed else 0)
