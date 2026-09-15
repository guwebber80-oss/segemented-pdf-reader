r"""阶段 6.4 验收：译文覆盖度与「翻译整篇」（utils/translate_plan.py + ui/cards.translate_all）

背景（用户 2026-09-16 实测报的现象）：「上传 → 看了几张卡 → 关掉阅读器进程 → 重开 →
再上传同一篇」，第一次切换页面仍要重新翻译，断网就直接失败退回英文原文。
排查结论：**本地缓存没坏**（看过的那几张卡 100% 命中），坏的是预期——翻译是
**读到哪翻到哪**的懒加载，没显示过的卡片从来没被翻译、也就没有落盘。
于是补上「整篇翻译 + 覆盖率显示」，这个套件守着它：

  ① 待译段落的收集规则：只取 heading / text（公式与表格保持原文截图，不翻译）、
     跨卡片去重、保持首次出现的顺序；
  ② 覆盖度：一张卡的**所有**段落都有译文才算「能离线读」，缺一段就不算；
     只有公式/表格的卡片不需要翻译，天然算已覆盖；
  ③ `translate_all` 的分批：按段数与总字符双重上限切批，失败时保留已完成的进度、
     再次调用能接着译（已译段落命中缓存，不会重复请求）。

测试全部用假卡片 + 临时缓存目录，不联网、不碰真实缓存。
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

from utils import store, translate_plan       # noqa: E402
from ui import cards                          # noqa: E402

passed, failed, skipped = 0, 0, 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK] {label}")
    else:
        failed += 1
        print(f"  [!!] {label}  {detail}")


class FakeCard:
    """只带 translate_plan 需要的两个属性（鸭子类型，不需要真卡片）"""

    def __init__(self, segments=None, text="", page=1):
        self.segments = segments
        self.text = text
        self.page = page


SANDBOX = tempfile.mkdtemp(prefix="pdfreader_plan_")
store.set_cache_root(SANDBOX)

BACKEND, TARGET = "deepl", "zh"

print("=" * 78)
print("① 待译段落怎么收：只取正文与标题、去重、保持顺序")
print("-" * 78)
mixed = FakeCard(segments=[("heading", "3 Method"), ("text", "We propose a model."),
                           ("formula", {"page": 1, "rect": (0, 0, 1, 1), "text": "E = mc^2"}),
                           ("table", {"page": 2, "rect": (0, 0, 1, 1), "text": "T"}),
                           ("text", "Results follow.")])
check("公式与表格段不进待译清单（它们保持原文截图）",
      translate_plan.card_segment_texts(mixed) == ["3 Method", "We propose a model.", "Results follow."],
      repr(translate_plan.card_segment_texts(mixed)))

plain = FakeCard(text="A card without segments.")
check("没有段落结构的卡片退回整段文本",
      translate_plan.card_segment_texts(plain) == ["A card without segments."])

check("空白段落被丢掉（不拿空串去查缓存）",
      translate_plan.card_segment_texts(FakeCard(text="   ")) == [])
check("只有公式的卡片没有待译段落",
      translate_plan.card_segment_texts(FakeCard(segments=[("formula", {})])) == [])

same_twice = [FakeCard(text="重复的一段"), FakeCard(text="重复的一段"), FakeCard(text="另一段")]
collected = translate_plan.collect_texts(same_twice)
check("跨卡片去重且保持首次出现的顺序", collected == ["重复的一段", "另一段"], repr(collected))

print()
print("② 覆盖度：段落全都有译文才算「这张卡能离线读」")
print("-" * 78)
card_a = FakeCard(segments=[("heading", "A1"), ("text", "A2")])
card_b = FakeCard(segments=[("heading", "B1"), ("text", "B2")])
card_c = FakeCard(segments=[("formula", {})])          # 只有公式，不需要翻译
sample_cards = [card_a, card_b, card_c]

info = translate_plan.coverage(sample_cards, BACKEND, TARGET)
check("还没翻译时：卡片总数 3 · 已覆盖 1（只有公式那张天然算覆盖）",
      info["cards"] == 3 and info["covered_cards"] == 1, repr(info))
check("待译段落 4 段（公式段不计入）", info["texts"] == 4 and len(info["pending"]) == 4, repr(info))
check("已译段落 0 段", info["covered_texts"] == 0)

store.put_translation(BACKEND, TARGET, "A1", "甲一")
info = translate_plan.coverage(sample_cards, BACKEND, TARGET)
check("只译了一段 → 那张卡还不算覆盖（缺一段就不算）",
      info["covered_cards"] == 1 and info["covered_texts"] == 1, repr(info))
check("待译清单里只剩没译的那 3 段",
      info["pending"] == ["A2", "B1", "B2"], repr(info["pending"]))

store.put_translation(BACKEND, TARGET, "A2", "甲二")
info = translate_plan.coverage(sample_cards, BACKEND, TARGET)
check("甲卡两段都译完 → 覆盖数 +1", info["covered_cards"] == 2, repr(info))

store.put_translations(BACKEND, TARGET, [("B1", "乙一"), ("B2", "乙二")])
info = translate_plan.coverage(sample_cards, BACKEND, TARGET)
check("全部译完 → 3 张全绿、待译 0 段",
      info["covered_cards"] == 3 and info["pending"] == [], repr(info))

info = translate_plan.coverage(sample_cards, "", TARGET)
check("没有可用后端时不假装已覆盖（已覆盖 0、待译全列）",
      info["covered_cards"] == 0 and len(info["pending"]) == 4, repr(info))
check("没有后端时 pending_texts 直接返回空（不查缓存）",
      translate_plan.pending_texts(sample_cards, "", TARGET) == [])

print()
print("③ translate_all：分批、进度回调、失败保留进度、可接着译")
print("-" * 78)
store.clear_translations()
store.reload_translations()

# 用假卡片凑出 45 段（每张卡 5 段），并把真实翻译函数换成假的，避免联网
many_cards = [FakeCard(segments=[("text", f"card{i}-seg{j}") for j in range(5)])
              for i in range(9)]
original_batch = cards.translate_cached_batch
BATCHES = []


def fake_batch(texts, backend, target):
    """假翻译：结果同时写进译文表——真函数就是这么做的（「命中缓存」的依据在这里）"""
    BATCHES.append(list(texts))
    translations = ["【译】" + text for text in texts]
    store.put_translations(backend, target, list(zip(texts, translations)))
    return translations, None


cards.translate_cached_batch = fake_batch
try:
    progress = []
    result = cards.translate_all(cards=many_cards, backend=BACKEND, target=TARGET,
                                 on_progress=lambda done, total: progress.append((done, total)))
    check("45 段全部翻完", result["texts"] == 45 and result["done"] == 45, repr(result))
    check("按 20 段一批 → 3 批（20 / 20 / 5）",
          [len(batch) for batch in BATCHES] == [20, 20, 5], repr([len(b) for b in BATCHES]))
    check("进度回调按批推进到最后一段",
          progress[-1] == (45, 45) and progress[0] == (20, 45), repr(progress[:2]))
    check("没有失败", result["failed"] == 0 and result["error"] is None)

    # 已经全部译过：再点一次应当零请求（这就是「多点几次也安全」）
    BATCHES.clear()
    again = cards.translate_all(cards=many_cards, backend=BACKEND, target=TARGET)
    check("再点一次：待译 0 段、一个请求都不发",
          again["texts"] == 0 and BATCHES == [], repr(again))

    # 长段落按字符上限分批：每段约 8000 字符、上限 2 万 → 每批最多 2 段
    # （每段末尾加序号，避免内容完全相同被 collect_texts 去重）
    store.clear_translations()
    store.reload_translations()
    long_cards = [FakeCard(segments=[("text", ("y" * 7999) + str(i)) for i in range(5)])]
    BATCHES.clear()
    cards.translate_all(cards=long_cards, backend=BACKEND, target=TARGET,
                        chunk_segments=20, chunk_chars=20000)
    check("长段落按字符上限切批（每批最多 2 段）",
          [len(batch) for batch in BATCHES] == [2, 2, 1], repr([len(b) for b in BATCHES]))

    # 失败：第 2 批报错 → 保留第 1 批的成果，错误传给界面
    store.clear_translations()
    store.reload_translations()
    calls = {"n": 0}

    def failing_batch(texts, backend, target):
        calls["n"] += 1
        if calls["n"] == 2:
            return None, "网络连接失败（示例）"
        translations = ["【译】" + text for text in texts]
        store.put_translations(backend, target, list(zip(texts, translations)))
        return translations, None

    cards.translate_cached_batch = failing_batch
    failed_result = cards.translate_all(cards=many_cards, backend=BACKEND, target=TARGET)
    check("第 2 批失败：已完成 20 段被保留下来",
          failed_result["done"] == 20 and failed_result["failed"] == 20, repr(failed_result))
    check("失败原因如实返回给界面", failed_result["error"] == "网络连接失败（示例）",
          repr(failed_result["error"]))

    # 恢复网络后接着译：第 1 批命中缓存，只请求剩下的 25 段
    cards.translate_cached_batch = fake_batch
    BATCHES.clear()
    resumed = cards.translate_all(cards=many_cards, backend=BACKEND, target=TARGET)
    check("接着译时只处理没译过的 25 段（已译的命中缓存）",
          resumed["texts"] == 25 and resumed["done"] == 25, repr(resumed))

    check("没有后端时不发请求", cards.translate_all(many_cards, "", TARGET)["texts"] == 0)
finally:
    cards.translate_cached_batch = original_batch
    store.clear_translations()
    store.set_cache_root("")
    shutil.rmtree(SANDBOX, ignore_errors=True)

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed} · 跳过 {skipped}")
sys.exit(1 if failed else 0)
