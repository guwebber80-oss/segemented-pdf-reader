r"""阶段 6.1 验收：本地持久化缓存（utils/store.py）

这一层不用 Streamlit，所以可以完全离线、纯函数式地验证。重点守四件事：

  ① **身份**：论文 key 由 PDF 字节决定，与文件名无关（改名/挪目录照样命中），内容变则分家；
  ② **原子与容错**：写入不留临时文件；文件被写坏 / 删掉 / 乱码时一律降级成「没有缓存」，
     绝不能把异常抛给界面（缓存坏了最多重新翻译一次）；
  ③ **译文表的键**：后端 | 目标语言 | 原文哈希——换后端换目标语言都要重新翻，
     同一段文字换一篇文献出现要命中；
  ④ **清理与概况**：删一篇 / 清空全部 / 数字统计都要对得上。

测试全程用临时目录（set_cache_root），不会碰到项目里的真实缓存。
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

from utils import store       # noqa: E402

passed, failed, skipped = 0, 0, 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK] {label}")
    else:
        failed += 1
        print(f"  [!!] {label}  {detail}")


# 每个测试段开始前都换一个干净的临时缓存目录，互不干扰
SANDBOX = tempfile.mkdtemp(prefix="pdfreader_cache_")


def fresh(name):
    """切到一个全新的缓存目录（模拟「换一台机器 / 清空缓存」）"""
    path = os.path.join(SANDBOX, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    store.set_cache_root(path)
    return path


def record_for(key, **overrides):
    data = {
        "version": store.CACHE_VERSION,
        "key": key,
        "file_name": "样本.pdf",
        "file_size": 12345,
        "settings": {"merge_on": True, "dehyphenate_on": False, "target_words": 250,
                     "table_mode_label": "保留文字", "show_all": True},
        "metadata": {"title": "Deep learning for eye movement analysis",
                     "doi": "10.1038/s41746-025-01665-3"},
        "edits": {"in_title": "人工改过的标题", "in_doi": "10.1038/s41746-025-01665-3"},
        "grobid": {"title": "GROBID 抽到的标题", "authors": ["A", "B"]},
        "card_index": 7,
    }
    data.update(overrides)
    return data


print("=" * 78)
print("① 论文身份：认字节不认文件名")
print("-" * 78)
pdf_a = b"%PDF-1.7 first paper" * 10
pdf_a_copy = bytes(pdf_a)                     # 同一份内容的另一个对象（模拟重新上传）
pdf_b = b"%PDF-1.7 second paper" * 10

key_a = store.paper_key(pdf_a)
key_b = store.paper_key(pdf_b)
check("同一份 PDF 每次算出的 key 完全一样（稳定）", key_a == store.paper_key(pdf_a_copy))
check("内容不同 → key 不同", key_a != key_b)
check("key 是 SHA-256 的前 16 位（文件名不参与计算）",
      len(key_a) == store.PAPER_KEY_LEN and all(c in "0123456789abcdef" for c in key_a))

print()
print("② 记录存取：设置 / 元数据修正 / GROBID / 阅读位置原样回来")
print("-" * 78)
fresh("roundtrip")
check("还没保存过 → load 返回 None（不是抛异常）", store.load_paper(key_a) is None)

record = record_for(key_a)
check("save_paper 成功", store.save_paper(record) is True)
loaded = store.load_paper(key_a)
check("读回来了", isinstance(loaded, dict))
check("设置原样", loaded["settings"] == record["settings"])
check("元数据修正原样（人工改过的标题不能丢）", loaded["edits"]["in_title"] == "人工改过的标题")
check("GROBID 结果原样", loaded["grobid"]["title"] == "GROBID 抽到的标题")
check("阅读位置（读到第几张）原样", loaded["card_index"] == 7)
check("保存时自动盖了版本号与时间戳",
      loaded["version"] == store.CACHE_VERSION and len(loaded["saved_at"]) >= 19)

check("load_paper_for 一次拿到 key 与记录",
      store.load_paper_for(pdf_a)[0] == key_a and store.load_paper_for(pdf_a)[1] is not None)

print()
print("③ 按 DOI / 标题兜底找回（PDF 字节变了也能接上）")
print("-" * 78)
check("按 DOI 命中（大小写 + https://doi.org/ 前缀都能归一化）",
      (store.find_by_metadata(doi="https://doi.org/10.1038/S41746-025-01665-3") or {}).get("key") == key_a)
check("按标题命中（大小写与空格差异不影响）",
      (store.find_by_metadata(title="deep learning   FOR eye movement analysis") or {}).get("key") == key_a)
check("标题被 PDF 断行切开也能对上（只比字母数字）",
      (store.find_by_metadata(title="Deep learning for eye move-\nment analysis") or {}).get("key") == key_a)
check("都不匹配 → None（不乱认）",
      store.find_by_metadata(title="完全不相干的另一篇论文", doi="10.1000/xyz") is None)
check("空标题空 DOI → None，不做任何猜测", store.find_by_metadata() is None)
check("exclude_key 能把自己排除掉",
      store.find_by_metadata(title="Deep learning for eye movement analysis",
                             exclude_key=key_a) is None)

print()
print("④ 译文表：键 = 后端 | 目标语言 | 原文哈希")
print("-" * 78)
fresh("translations")
text_one = "The mice were divided into two groups."
text_two = "Results were analyzed with a mixed-effects model."

check("没存过 → None", store.get_translation("deepl", "zh", text_one) is None)
store.put_translation("deepl", "zh", text_one, "小鼠被分成两组。")
store.put_translations("deepl", "zh", [(text_two, "结果用混合效应模型分析。")])
check("未 flush 前，内存里已经能查到", store.get_translation("deepl", "zh", text_one) == "小鼠被分成两组。")
check("flush 成功", store.flush_translations() is True)

before = open(store.translations_path(), encoding="utf-8").read()
store.flush_translations()                     # 没有任何新译文
after = open(store.translations_path(), encoding="utf-8").read()
check("没有新译文时 flush 不重写文件（避免每次翻页都写盘）", before == after)

# 模拟「重启进程」：清掉内存副本，强制重新读盘
store.reload_translations()
check("刷新进程后仍能从磁盘读到（这就是「下次不用重翻」的关键）",
      store.get_translation("deepl", "zh", text_one) == "小鼠被分成两组。")
check("批量写入的第二条也在", store.get_translation("deepl", "zh", text_two) == "结果用混合效应模型分析。")
check("译文条数对得上", store.translations_count() == 2)

check("换目标语言（en）不命中 → 必须重新翻",
      store.get_translation("deepl", "en", text_one) is None)
check("换后端（google）不命中 → 必须重新翻",
      store.get_translation("google", "zh", text_one) is None)
check("换一段文字不命中（哈希按原文算）",
      store.get_translation("deepl", "zh", text_one + " ") is None)

store.put_translation("deepl", "zh", "   ", "  ")
check("空译文（空字符串）是合法值，读取时不会被当成没命中",
      store.get_translation("deepl", "zh", "   ") == "  ")

print()
print("⑤ 原子写与容错：坏文件绝不能把界面带崩")
print("-" * 78)
fresh("robust")
store.save_paper(record_for(key_a))
leftovers = [name for name in os.listdir(store.papers_dir()) if name.endswith(".tmp")]
check("目录里没有 .tmp 残留（写临时文件 → 原子替换）", leftovers == [], repr(leftovers))

with open(store.paper_path(key_a), "w", encoding="utf-8") as handle:
    handle.write('{"key": "abc", "settings": {')          # 故意写坏（模拟断电）
check("半个 JSON → load 返回 None，不抛异常", store.load_paper(key_a) is None)
check("记录写坏了，索引仍可读（索引与记录是两份文件）",
      isinstance(store.load_index(), dict) and key_a in store.load_index())

with open(store.paper_path(key_b), "w", encoding="utf-8") as handle:
    handle.write('"其实不是个字典"')
check("JSON 合法但不是字典 → 也当成没有", store.load_paper(key_b) is None)

shutil.rmtree(store.cache_root(), ignore_errors=True)
check("整个缓存目录被删掉后依然不炸", store.load_paper(key_a) is None
      and store.get_translation("deepl", "zh", "x") is None
      and store.cache_stats()["papers"] == 0)
check("删掉目录后还能重新写进去（自动重建目录）", store.save_paper(record_for(key_a)) is True)

print()
print("⑥ 清理与概况")
print("-" * 78)
fresh("cleanup")
store.save_paper(record_for(key_a))
store.save_paper(record_for(key_b, metadata={"title": "第二篇", "doi": "10.1/x"},
                           file_name="b.pdf"))
store.put_translation("deepl", "zh", text_one, "小鼠被分成两组。")
store.flush_translations()

stats = store.cache_stats()
check("概况：文献 2 篇", stats["papers"] == 2, repr(stats["papers"]))
check("概况：译文 1 条", stats["translations"] == 1, repr(stats["translations"]))
check("概况：占用字节 > 0", stats["bytes"] > 0, repr(stats["bytes"]))
check("概况：带缓存目录路径（界面上要显示给用户）", stats["root"] == store.cache_root())

listing = store.list_papers()
check("list_papers 能列出两篇，且带文件名", len(listing) == 2
      and {item["file_name"] for item in listing} == {"样本.pdf", "b.pdf"})

check("delete_paper 删掉一篇", store.delete_paper(key_a) is True)
check("删掉之后读不到、索引里也没了",
      store.load_paper(key_a) is None and key_a not in store.load_index()
      and len(store.load_index()) == 1)
check("删一篇不影响另一篇", store.load_paper(key_b) is not None)

cleared = store.clear_all()
check("clear_all 返回各自清掉的条数（记录 1 + 译文 1）",
      cleared == {"papers": 1, "translations": 1}, repr(cleared))
check("清空后索引与译文都空",
      store.load_index() == {} and store.translations_count() == 0)
check("清空后再查译文 → None（不会拿到旧值）",
      store.get_translation("deepl", "zh", text_one) is None)

print()
print("⑦ 缓存目录可迁移（环境变量 / set_cache_root）")
print("-" * 78)
store.set_cache_root("")                       # 清掉临时设置
os.environ[store.ENV_CACHE_DIR] = os.path.join(SANDBOX, "from_env")
check("环境变量 PDF_READER_CACHE_DIR 生效",
      store.cache_root() == os.path.join(SANDBOX, "from_env"))
os.environ.pop(store.ENV_CACHE_DIR)
check("没有环境变量时回到项目根下的 .cache",
      store.cache_root() == store.DEFAULT_CACHE_DIR
      and store.cache_root().endswith(".cache"))

# 收尾：把内存里的译文表清掉，别把临时目录的状态带出去
store.set_cache_root("")
shutil.rmtree(SANDBOX, ignore_errors=True)

print()
print("=" * 78)
print(f"断言结果：通过 {passed} · 失败 {failed} · 跳过 {skipped}")
sys.exit(1 if failed else 0)
