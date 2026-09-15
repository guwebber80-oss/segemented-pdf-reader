"""
utils.store —— 本地持久化缓存（阶段 6.1）

为什么要有它：翻译要花 API 额度、解析要等几秒，而「同一篇文献下次再打开」不该重新花钱、
重新等待。这里的安排是：

  1）**论文身份 = PDF 文件字节的 SHA-256**（不认文件名）
     改名、挪目录、重新上传同一份 PDF 都能命中；内容变了（期刊给了新版本）就自然分家。

  2）**译文单独放全局表，键 = 后端 | 目标语言 | 原文哈希**
     与「是不是同一篇 PDF」解耦：同一段文字换个地方出现也能命中，
     而且解析规则升级（卡片怎么切变了）不会让已有的译文作废——这是最贵的那部分。

  3）**图片永不落盘**
     插图、公式与表格截图体积大（一篇几十 MB），存下来不值当；
     下次按需重新渲染（200 DPI，几百毫秒）即可，只把「哪里要出图」的结论留在解析结果里。

文件布局（都在项目根的 .cache/ 下，已写进 .gitignore）：

    .cache/papers/<key>.json   一篇文献一条记录（设置 / 元数据修正 / GROBID 结果 / 读到第几张）
    .cache/index.json          轻量索引（key → 文件名·标题·DOI·时间），用来按 DOI / 标题找回记录
    .cache/translations.json   全局译文表

可靠性约定：
  · 所有写入都是「写临时文件 → os.replace 原子替换」，中断不会留下半个 JSON；
  · 读取失败（文件不存在 / 内容坏了 / 编码不对）一律当成「没有缓存」，返回 None——
    缓存坏掉绝不能影响正常阅读，最多是重新翻译一次；
  · 多标签页同时用同一个应用时是「后写覆盖」，不会写坏文件（原子替换保证）。

本模块是纯 Python（不依赖 Streamlit），界面侧怎么用它见 `ui/persist.py`。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time

# ============================================================
# 1. 路径与常量
# ============================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CACHE_VERSION = 1                          # 记录结构版本：以后改字段就 +1，旧记录仍能读
ENV_CACHE_DIR = "PDF_READER_CACHE_DIR"     # 可用环境变量把缓存挪到别的盘
DEFAULT_CACHE_DIR = os.path.join(PROJECT_ROOT, ".cache")

PAPERS_DIRNAME = "papers"
INDEX_FILENAME = "index.json"
TRANSLATIONS_FILENAME = "translations.json"
UI_PREFS_FILENAME = "ui.json"

PAPER_KEY_LEN = 16        # 论文 key：SHA-256 前 16 个十六进制字符
TEXT_HASH_LEN = 20        # 译文键里的原文哈希长度

# 供测试在进程内改缓存目录（也方便把缓存整体搬到容量更大的盘上）
_cache_root_override = ""
# 译文表的内存副本（懒加载；None = 还没读过盘）
_translations = None
_translations_dirty = False


def cache_root() -> str:
    """缓存根目录：临时设置 > 环境变量 > 项目根下的 .cache"""
    if _cache_root_override:
        return _cache_root_override
    return os.environ.get(ENV_CACHE_DIR) or DEFAULT_CACHE_DIR


def set_cache_root(path: str) -> None:
    """换缓存根目录（测试用；换目录后内存里的译文表作废，下次访问重新读盘）"""
    global _cache_root_override, _translations, _translations_dirty
    _cache_root_override = path
    _translations = None
    _translations_dirty = False


def papers_dir() -> str:
    return os.path.join(cache_root(), PAPERS_DIRNAME)


def index_path() -> str:
    return os.path.join(cache_root(), INDEX_FILENAME)


def translations_path() -> str:
    return os.path.join(cache_root(), TRANSLATIONS_FILENAME)


def paper_path(key: str) -> str:
    return os.path.join(papers_dir(), f"{key}.json")


# ============================================================
# 2. 底层读写（原子写 + 容错读）
# ============================================================
def _ensure_dir(path: str) -> None:
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)


def _read_json(path: str):
    """
    读一个 JSON 文件；任何异常都当成「没有这个东西」，返回 None。

    这一点是有意为之：缓存是加速手段，不是数据源。哪怕用户手动删了一半、
    或者磁盘写满留下半个文件，应用也必须照常跑起来（顶多重新解析、重新翻译）。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: str, data, pretty: bool = False) -> bool:
    """
    原子写 JSON：先写同目录临时文件，再 os.replace 顶上去。

    为什么不用 open(path, "w") 直接写：Streamlit 每次交互都会重跑整个脚本，
    一边写一边被读到半个文件是真实存在的；os.replace 在 Windows / Linux 上
    都是原子替换，读者要么看到旧的完整文件，要么看到新的完整文件。
    """
    folder = os.path.dirname(path)
    try:
        _ensure_dir(folder)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=folder, suffix=".tmp")
        try:
            with handle:
                json.dump(data, handle, ensure_ascii=False,
                          indent=1 if pretty else None,
                          separators=None if pretty else (",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, path)
            return True
        except Exception:
            # 写失败要把临时文件清掉，不然 .cache 里全是垃圾
            try:
                os.unlink(handle.name)
            except Exception:
                pass
            return False
    except Exception:
        return False


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# 3. 论文记录：一篇文献一条
# ============================================================
def paper_key(pdf_bytes: bytes) -> str:
    """论文身份：PDF 字节的 SHA-256 前 16 位（认内容不认文件名）"""
    return hashlib.sha256(pdf_bytes).hexdigest()[:PAPER_KEY_LEN]


def load_paper(key: str):
    """按 key 读记录；没有 / 坏了都返回 None"""
    if not key:
        return None
    record = _read_json(paper_path(key))
    if record and record.get("key") == key:
        return record
    return None


def load_paper_for(pdf_bytes: bytes):
    """一次拿到 (key, 记录或 None)，省得调用方自己算两遍"""
    key = paper_key(pdf_bytes)
    return key, load_paper(key)


def save_paper(record: dict) -> bool:
    """
    保存一条论文记录（同时刷新索引里的标题 / DOI，供按 DOI 标题兜底找回）。

    record 至少要有 "key"；version 与 saved_at 由这里统一盖章。
    """
    if not isinstance(record, dict):
        return False
    key = str(record.get("key") or "")
    if not key:
        return False

    record = dict(record)
    record["version"] = CACHE_VERSION
    record["saved_at"] = now_text()
    if not _write_json(paper_path(key), record, pretty=True):
        return False

    index = load_index()
    index[key] = _index_entry(record)
    # 索引写失败不影响记录本身（只是下次按 DOI 找不回），所以返回值只看记录
    _write_json(index_path(), index, pretty=True)
    return True


def _index_entry(record: dict) -> dict:
    meta = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    return {
        "file_name": record.get("file_name", ""),
        "file_size": record.get("file_size", 0),
        "title": (meta.get("title") or "")[:300],
        "doi": (meta.get("doi") or "")[:120],
        "saved_at": record.get("saved_at", ""),
    }


def load_index() -> dict:
    """轻量索引：{论文 key: {文件名/标题/DOI/时间}}"""
    index = _read_json(index_path())
    return index if isinstance(index, dict) else {}


def list_papers() -> list:
    """列出本地保存过的文献（按保存时间倒序），给「已读文献」之类的界面用"""
    items = []
    for key, entry in load_index().items():
        if isinstance(entry, dict):
            items.append({"key": key, **entry})
    return sorted(items, key=lambda item: item.get("saved_at", ""), reverse=True)


def _normalize_doi(text: str) -> str:
    """DOI 归一化：去掉 https://doi.org/ 前缀与大小写差异"""
    if not text:
        return ""
    value = re.sub(r"^\s*(https?://(dx\.)?doi\.org/|doi:\s*)", "", str(text), flags=re.I)
    return value.strip().strip(". ").lower()


def _normalize_title(text: str) -> str:
    """标题归一化：只留字母数字，压掉空格与大小写差异（换行断词也能对上）"""
    if not text:
        return ""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text).lower())


def find_by_metadata(title: str = "", doi: str = "", exclude_key: str = ""):
    """
    按 DOI / 标题找回记录：PDF 字节变了（重新下载、期刊更新版本、换了个导出版本）时的兜底。

    只读索引（不逐个打开记录文件），所以很快。DOI 优先，其次标题完全一致，
    最后是标题互相包含（≥ 20 个字符才敢这么比，避免短标题乱命中）。
    """
    want_doi = _normalize_doi(doi)
    want_title = _normalize_title(title)
    if not want_doi and not want_title:
        return None

    candidates = [(key, entry) for key, entry in load_index().items()
                  if isinstance(entry, dict) and key != exclude_key]

    if want_doi:
        for key, entry in candidates:
            if _normalize_doi(entry.get("doi", "")) == want_doi:
                return load_paper(key)

    if want_title:
        for key, entry in candidates:
            if _normalize_title(entry.get("title", "")) == want_title:
                return load_paper(key)
        if len(want_title) >= 20:
            for key, entry in candidates:
                other = _normalize_title(entry.get("title", ""))
                if other and (want_title in other or other in want_title):
                    return load_paper(key)
    return None


def delete_paper(key: str) -> bool:
    """删掉一篇记录（索引里也删）"""
    if not key:
        return False
    removed = False
    try:
        os.unlink(paper_path(key))
        removed = True
    except Exception:
        removed = False
    index = load_index()
    if key in index:
        index.pop(key, None)
        _write_json(index_path(), index, pretty=True)
    return removed


def clear_papers() -> int:
    """清空全部论文记录，返回删掉的条数"""
    count = 0
    try:
        for name in os.listdir(papers_dir()):
            if name.endswith(".json"):
                try:
                    os.unlink(os.path.join(papers_dir(), name))
                    count += 1
                except Exception:
                    pass
    except Exception:
        pass
    try:
        os.unlink(index_path())
    except Exception:
        pass
    return count


# ============================================================
# 4. 译文表：全局共享，键 = 后端 | 目标语言 | 原文哈希
# ============================================================
def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:TEXT_HASH_LEN]


def translation_key(backend: str, target: str, text: str) -> str:
    return f"{backend}|{target}|{text_hash(text)}"


def reload_translations() -> None:
    """丢掉内存副本，下次访问重新读盘（测试与「外部改了文件」时用）"""
    global _translations, _translations_dirty
    _translations = None
    _translations_dirty = False


def load_translations() -> dict:
    """译文表（懒加载，内存里留一份；返回的是活字典，只允许本模块改写）"""
    global _translations
    if _translations is None:
        data = _read_json(translations_path()) or {}
        entries = data.get("entries")
        _translations = dict(entries) if isinstance(entries, dict) else {}
    return _translations


def translations_count() -> int:
    return len(load_translations())


def get_translation(backend: str, target: str, text: str):
    """查译文；没有则 None（注意：空字符串是合法译文，不要用 `if result` 判断）"""
    if not backend or not text:
        return None
    return load_translations().get(translation_key(backend, target, text))


def put_translation(backend: str, target: str, text: str, translated: str) -> None:
    """记一条译文到内存（真正落盘在 flush_translations()）"""
    global _translations_dirty
    if not backend or not text or translated is None:
        return
    load_translations()[translation_key(backend, target, text)] = translated
    _translations_dirty = True


def put_translations(backend: str, target: str, pairs) -> int:
    """批量记译文；pairs 是 (原文, 译文) 的可迭代对象，返回写入条数"""
    count = 0
    for text, translated in pairs:
        put_translation(backend, target, text, translated)
        count += 1
    return count


def flush_translations(force: bool = False) -> bool:
    """
    把内存里的译文表落盘（没改动就不写，避免每次翻页都重写几十 KB）。

    **写之前先跟磁盘上的现状合并**（内存版本优先）：译文是按文本哈希存的、
    只增不减，所以合并是安全的；好处是「开着两个窗口/两个进程读同一篇」时，
    后写的一方不会把对方刚存下的译文覆盖掉——实测踩过这个坑：一个进程写盘时
    只把自己内存里的那几张卡写进去，之前进程存下的译文就没了。

    整体重写 + 原子替换：文件不大（一篇论文全译约几十 KB），够快也够安全。
    """
    global _translations_dirty
    if _translations is None:
        return True
    if not _translations_dirty and not force:
        return True

    on_disk = _read_json(translations_path()) or {}
    entries = on_disk.get("entries")
    if isinstance(entries, dict) and entries:
        # 磁盘上多出来的条目也吸收进内存，界面上查得到、不会白存
        for key, value in entries.items():
            _translations.setdefault(key, value)

    payload = {"version": CACHE_VERSION, "saved_at": now_text(), "entries": _translations}
    if not _write_json(translations_path(), payload):
        return False
    _translations_dirty = False
    return True


def clear_translations() -> int:
    """清空译文表（内存 + 磁盘），返回清掉的条数"""
    global _translations, _translations_dirty      # 少了这行会只改到局部变量，内存里还是旧值
    count = translations_count()
    try:
        os.unlink(translations_path())
    except Exception:
        pass
    _translations = {}
    _translations_dirty = False
    return count


# ============================================================
# 5. 概况与整体清理（界面上给用户看的数字）
# ============================================================
def _dir_size(path: str) -> int:
    total = 0
    try:
        for root, _dirs, names in os.walk(path):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except Exception:
                    pass
    except Exception:
        pass
    return total


def cache_stats() -> dict:
    """缓存概况：文献条数 / 译文条数 / 占用字节 / 目录路径"""
    root = cache_root()
    return {
        "root": root,
        "papers": len(load_index()),
        "translations": translations_count(),
        "bytes": _dir_size(root),
        "index": load_index(),
    }


def clear_all() -> dict:
    """清空整个缓存（记录 + 译文），返回各自清掉的条数"""
    return {"papers": clear_papers(), "translations": clear_translations()}


# ============================================================
# 6. 界面偏好（阶段 6.5：卡内滚动开关 / 卡片高度档位 / 沉浸模式）
# ============================================================
# 这类偏好不属于某一篇论文，所以单独一个小文件；都是「小、可重建」的值，
# 坏了/删了都只影响外观，不影响任何阅读数据。
def ui_prefs_path() -> str:
    return os.path.join(cache_root(), UI_PREFS_FILENAME)


def load_ui_prefs() -> dict:
    """读界面偏好；没有或坏了都返回 {}（调用方自己用默认值兜底）"""
    payload = _read_json(ui_prefs_path()) or {}
    prefs = payload.get("prefs")
    return prefs if isinstance(prefs, dict) else {}


def save_ui_prefs(prefs: dict) -> bool:
    """写界面偏好（原子写；只保留 JSON 装得下的简单值）"""
    if not isinstance(prefs, dict):
        return False
    payload = {"version": CACHE_VERSION, "saved_at": now_text(),
               "prefs": {str(k): v for k, v in prefs.items()
                         if isinstance(v, (bool, int, float, str))}}
    return _write_json(ui_prefs_path(), payload, pretty=True)
