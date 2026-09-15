"""回归比对：把当前代码的输出与基线快照逐项比对，明确回答「这次更新有没有影响原有功能」

用法：
    python tests/regression_check.py                 # 与 tests/baseline.json 比对
    python tests/regression_check.py --update-baseline   # 确认差异可接受后，刷新基线
    python tests/regression_check.py --suites-dir ..\\.dsh-scratch   # 顺带跑功能测试套件

判定规则：
    · 「必须一致」的字段（卡片数、词数、角色分布、公式区域数、元数据各字段的值…）
      只要有一处不同，就以退出码 1 结束，并把差异逐条打印出来；
    · 「_允许变化」里的字段（耗时、来源说明文字）不参与判定；
    · 基线里没有的新样本、基线里有但现在找不到的样本，都算「提示」而非失败。

注意：**不要**为了让它变绿就随手 --update-baseline。差异要先确认是「本次故意改的」再刷新，
否则这个工具就失去意义了。
"""

import argparse
import json
import os
import subprocess
import sys

# ---- 控制台编码兜底 ----
# 批处理用 chcp 936 把控制台设成 GBK，而本项目会打印 PDF 里的原始字符
# （上下标 ᵢ ₖ、数学符号 −、表情符号…），其中不少不在 GBK 里，
# 直接 print 会抛 UnicodeEncodeError 把脚本整个打断。这里统一改成「不可编码就替换」，
# 中文照常显示，个别生僻字符显示为 ?，但脚本绝不因为打印而崩。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)
sys.path.insert(0, os.path.join(PROJECT, "tests"))

from snapshot import collect, DEFAULT_SAMPLES, DEFAULT_OUT      # noqa: E402

# 需要跑的功能测试套件（相对项目/工作区，按需存在才跑）
SUITES = [
    ("顺序断言（合成双栏/单栏）", "assert_order.py"),
    ("第九页公式聚类验收", "test_cluster_p9.py"),
    ("4.2 作者/单位提取（41 条断言）", "test_42_extract.py"),
    ("GROBID 客户端（19 条断言）", "test_grobid_offline.py"),
    ("交叉校验逻辑（27 条断言）", "test_compare_offline.py"),
]

# 提交进仓库的验收套件（就在 tests/ 目录里，任何机器上取得仓库后都能跑；
# 样本不在本机时套件自己会跳过对应断言）。这一组**总是**执行，
# 这样双击「回归检查.bat」就等于把基线比对 + 验收断言一起跑一遍。
PROJECT_SUITES = [
    ("4.4-A 通讯作者图标标记（17 条断言）", "test_corresponding.py"),
    ("4.4-B 表格区域截图（19 条断言）", "test_table.py"),
    ("4.4-B 修复：公式碎片堆种子 + 表注（28 条断言）", "test_formula_seeds.py"),
    ("标题/角色：DOI 不得当小标题（13 条断言）", "test_heading_roles.py"),
    ("阶段 5 架构守卫（12 条断言）", "test_architecture.py"),
    ("阶段 5 界面面板与放大弹层冒烟（14 条断言）", "test_ui_panels.py"),
    ("阶段 6.1 本地持久化缓存（51 条断言）", "test_store.py"),
    ("阶段 6.2 明暗模式切换（25 条断言）", "test_theme.py"),
    ("阶段 6.4 译文覆盖与整篇翻译（24 条断言）", "test_translate_plan.py"),
    ("阶段 6.6 卡片固定高度 + 卡内滚动（11 条断言）", "test_layout.py"),
    ("阶段 7.1 网页版载荷层（16 条断言）", "test_webapp_payload.py"),
]

# 允许变化、不参与判定的字段
SOFT_KEYS = {"_允许变化"}


def fmt(value) -> str:
    """把值压成一行短文本，便于打印差异"""
    if isinstance(value, list):
        text = "；".join(str(item) for item in value)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= 70 else text[:67] + "…"


def flatten(value, prefix: str = "") -> dict:
    """
    把嵌套结构压成「点分键 → 值」，便于逐项比对与打印。

    为什么必须展开：元数据是一个嵌套字典，整块打印会被截断（例如
    `{"标题": "…", "DOI": "…"…}`），差异藏在里面根本看不出来——
    实测把「通讯作者」改坏后，报告只显示「元数据」有差异却不说改了哪一项。
    """
    flat = {}
    if isinstance(value, dict):
        for key, item in value.items():
            flat.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    else:
        flat[prefix] = value
    return flat


def diff_sample(base: dict, current: dict) -> list:
    """比对单个样本，返回差异列表 [(字段, 基线值, 当前值)]（嵌套字典按子字段展开）"""
    differences = []
    base_flat = {key: value for key, value in flatten(base).items()
                 if key.split(".")[0] not in SOFT_KEYS}
    current_flat = flatten(current)
    for key, old in base_flat.items():
        new = current_flat.get(key, "（当前结果里没有这一项）")
        if old != new:
            differences.append((key, fmt(old), fmt(new)))
    return differences


def run_one(label: str, path: str) -> tuple:
    """跑一个套件（stdout 直接继承，只收退出码；沙箱里不能用管道捕获子进程输出）"""
    if not os.path.exists(path):
        return label, "跳过（脚本不存在）"
    print(f"\n----- {label} -----")
    completed = subprocess.run([sys.executable, path], cwd=PROJECT)
    return label, ("通过 [OK]" if completed.returncode == 0 else "失败 [!!]")


def run_suites(suites_dir: str) -> tuple:
    """跑开发侧的临时套件目录（.dsh-scratch 那类，不进仓库）"""
    return [run_one(label, os.path.join(suites_dir, filename)) for label, filename in SUITES]


def run_project_suites() -> tuple:
    """跑仓库自带的验收套件（tests/ 目录里，随版本一起进 Git）"""
    here = os.path.dirname(os.path.abspath(__file__))
    return [run_one(label, os.path.join(here, filename)) for label, filename in PROJECT_SUITES]


def main() -> int:
    parser = argparse.ArgumentParser(description="与基线快照比对")
    parser.add_argument("--samples", default=DEFAULT_SAMPLES)
    parser.add_argument("--baseline", default=DEFAULT_OUT)
    parser.add_argument("--update-baseline", action="store_true",
                        help="把当前结果写回基线（确认差异是故意的之后再用）")
    parser.add_argument("--suites-dir", default="",
                        help="顺带跑这个目录下的功能测试套件（例如 ..\\.dsh-scratch）")
    parser.add_argument("--target-words", type=int, default=200)
    args = parser.parse_args()

    if args.update_baseline:
        print("[注意]  你选择了刷新基线：请确认当前差异都是「本次故意改动」造成的。\n")

    if not os.path.exists(args.baseline) and not args.update_baseline:
        print(f"[!!] 找不到基线文件：{args.baseline}")
        print("   先运行一次 python tests/snapshot.py 生成基线。")
        return 2

    baseline = {}
    if os.path.exists(args.baseline):
        # 用 utf-8-sig 读：容忍基线文件带 BOM（Windows 上用记事本/PowerShell 另存很容易带上），
        # 否则会直接抛 JSONDecodeError，看起来像「工具坏了」而不是「基线格式不对」。
        with open(args.baseline, encoding="utf-8-sig") as handle:
            baseline = json.load(handle)

    print(f"样本目录：{args.samples}")
    current = collect(args.samples, args.target_words)

    if args.update_baseline:
        with open(args.baseline, "w", encoding="utf-8") as handle:
            json.dump(current, handle, ensure_ascii=False, indent=2)
        print(f"\n[OK] 基线已刷新：{args.baseline}")
        return 0

    print("\n" + "=" * 78)
    print(f"与基线比对（基线采集于 {baseline.get('采集时间', '未知')}，"
          f"{baseline.get('样本数', 0)} 篇）")
    print("=" * 78)

    same, changed, missing, added = 0, 0, 0, 0
    for name, base_data in baseline.get("样本", {}).items():
        if name not in current["样本"]:
            print(f"[注意]  {name}：基线里有，现在找不到（文件被移走或改名了）")
            missing += 1
            continue
        if "采集失败" in base_data:
            print(f"[注意]  {name}：基线里就是采集失败，跳过比对")
            continue
        differences = diff_sample(base_data, current["样本"][name])
        if not differences:
            print(f"[OK] {name}：与基线完全一致")
            same += 1
        else:
            print(f"[!!] {name}：{len(differences)} 处差异")
            for key, old, new in differences:
                print(f"     {key}：{old}  →  {new}")
            changed += 1

    for name in current["样本"]:
        if name not in baseline.get("样本", {}):
            print(f"[注意]  {name}：新样本（基线里没有，未参与比对）")
            added += 1

    print("-" * 78)
    print(f"汇总：一致 {same} 篇 · 有差异 {changed} 篇 · 新增 {added} 篇 · 缺失 {missing} 篇")

    suite_results = list(run_project_suites())      # 仓库自带的验收套件：每次都跑
    if args.suites_dir:
        suites_dir = os.path.abspath(args.suites_dir)
        if os.path.isdir(suites_dir):
            suite_results += list(run_suites(suites_dir))
        else:
            print(f"\n[注意]  找不到测试套件目录：{suites_dir}（跳过）")
    if suite_results:
        print("\n" + "=" * 78)
        print("功能测试套件")
        print("=" * 78)
        for label, status in suite_results:
            print(f"  {status}  {label}")

    suites_ok = all(status.startswith(("通过", "跳过")) for _, status in suite_results)
    print("\n" + "=" * 78)
    if changed == 0 and suites_ok:
        print("判定：[OK] 原有功能未受影响" + ("（新样本已列出，可在基线刷新后纳入）" if added else ""))
        return 0
    print("判定：[!!] 存在差异或套件失败——请逐条确认；确认是本次故意改动后，"
          "再执行 --update-baseline 刷新基线")
    return 1


if __name__ == "__main__":
    sys.exit(main())
