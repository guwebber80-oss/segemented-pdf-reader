"""
webapp.state —— 服务端"当前这篇文献"的会话状态（纯 Python，不依赖 Streamlit）

新前端是「本地服务 + 浏览器」：浏览器把 PDF 传上来，服务端解析一次、把结果留在内存里，
后续翻页/取图/翻译都走这个内存副本（而不是每次重新解析）。

设计取舍：
  · **单篇 + 单用户**：这是本机自用工具，不需要多会话隔离，所以只保留"最新一篇"，
    但用锁保护好（HTTP 服务是多线程的），避免两个请求同时改状态。
  · **图片不常驻大字节**：这里只留 PDF 字节与图片元信息，真正的 PNG 由 `/api/img/<id>`
    现场渲染（与 Streamlit 版一致：缩略图便宜，原图按需）。
  · 读过的文献与译文仍然落 `.cache/`（复用 utils.store），所以关掉网页再打开能接上。
"""

import threading

from utils import image_extractor, metadata as metadata_module, pdf_parser, store
from utils import translate_plan

from . import payload as payload_module


class PaperState:
    """当前打开的文献（含解析结果、图片清单、区域截图登记表）"""

    def __init__(self):
        self._lock = threading.RLock()
        self.reset()

    def reset(self):
        self.pdf_bytes = None
        self.file_name = ""
        self.key = ""
        self.parse_result = None
        self.image_result = {"images": [], "skipped": [], "elapsed": 0.0}
        self.metadata = None
        self.registry = {}
        self.position = 0
        self.cache_hit = False
        self.error = None

    # ---------------- 打开一篇文献 ----------------
    def open(self, pdf_bytes: bytes, file_name: str, merge_on=True, dehyphenate=True,
             target_words=200, table_mode="image") -> dict:
        """解析（或命中缓存）并返回给前端的 JSON（不含 registry）"""
        with self._lock:
            self.reset()
            self.pdf_bytes = pdf_bytes
            self.file_name = file_name
            self.key, record = store.load_paper_for(pdf_bytes)
            self.cache_hit = record is not None

            try:
                self.parse_result = pdf_parser.parse_pdf(
                    pdf_bytes, merge_on, dehyphenate, target_words, table_mode)
                self.image_result = image_extractor.extract_images(pdf_bytes)
                # ⚠️ 必须再做一次「图片 ↔ 卡片」关联：extract_images 只负责把图片抠出来，
                # 是 associate_with_cards 给每张图写上 card_order，前端才能把插图放到对应卡片后面
                # （这一步是端到端自测抓出来的：不调用它，payload 里一张 figure 段都没有）
                image_extractor.associate_with_cards(self.image_result["images"],
                                                     self.parse_result["cards"])
                self.metadata = metadata_module.extract_metadata(pdf_bytes,
                                                                self.parse_result["blocks"])
            except Exception as exc:                       # 解析失败也要让前端看到原因
                self.error = f"{type(exc).__name__}: {exc}"
                return {"ok": False, "error": self.error}

            # 上次读到第几张：命中本地记录就接着上次
            if record:
                try:
                    self.position = int(record.get("card_index") or 0)
                except Exception:
                    self.position = 0

            data = payload_module.build_paper_payload(
                pdf_bytes, file_name, self.parse_result, self.image_result, None,
                self.metadata, position=self.position, cache_hit=self.cache_hit,
                backend=None, target="zh")
            self.registry = data.pop("_registry", {})
            data["ok"] = True
            data["error"] = None
            return data

    # ---------------- 取图 ----------------
    def image_bytes(self, ident: str, size: str = "thumb"):
        """
        按 id 取 PNG：
          · 插图（figure）：`thumb` 用解析时生成好的缩略图，`full` 现场按原分辨率渲染；
          · 公式 / 表格区域：整块区域截图（本就是"原排版"，没有缩略图概念）。
        """
        with self._lock:
            item = self.registry.get(ident)
            if not item or self.pdf_bytes is None:
                return None
            if item["type"] == "region":
                region = item["payload"]
                return image_extractor.render_region_image(
                    self.pdf_bytes, region["page"], region["rect"])
            image = item["image"]
            if size == "full":
                # 签名是 (pdf_bytes, xref, smask, max_width)：大图按需渲染
                return image_extractor.render_full_image(
                    self.pdf_bytes, getattr(image, "xref", 0), getattr(image, "smask", 0) or 0)
            return getattr(image, "thumb", None)

    # ---------------- 翻译（复用全局译文缓存） ----------------
    def translate(self, texts, backend: str, target: str = "zh"):
        """
        整卡分段翻译：先查本地译文表（`.cache/translations.json`，键=后端|语言|原文哈希），
        只把没命中的送去 API —— 与 Streamlit 版同一套缓存，两边互相受益。
        """
        from utils.translator import TranslationError, translate_many

        wanted = list(texts or [])
        out, pending, pending_index = [None] * len(wanted), [], []
        for index, text in enumerate(wanted):
            hit = store.get_translation(backend, target, text)
            if hit is not None:
                out[index] = hit
            else:
                pending.append(text)
                pending_index.append(index)
        if not pending:
            return {"ok": True, "translations": out, "error": None, "from_cache": len(wanted)}
        try:
            fresh = translate_many(pending, target=target, backend=backend)
        except TranslationError as exc:
            return {"ok": False, "translations": out, "error": str(exc), "from_cache": 0}
        except Exception as exc:
            return {"ok": False, "translations": out,
                    "error": f"未预期的错误 {type(exc).__name__}: {exc}", "from_cache": 0}
        store.put_translations(backend, target, list(zip(pending, fresh)))
        store.flush_translations()
        for slot, translated in zip(pending_index, fresh):
            out[slot] = translated
        return {"ok": True, "translations": out, "error": None,
                "from_cache": len(wanted) - len(pending)}

    # ---------------- 阅读位置 / 覆盖率 ----------------
    def save_position(self, index: int) -> bool:
        """把"读到第几张"写进本地记录（下次打开接着读）"""
        with self._lock:
            if self.pdf_bytes is None or self.metadata is None:
                return False
            self.position = int(index or 0)
            record = {
                "version": store.CACHE_VERSION,
                "key": self.key,
                "file_name": self.file_name,
                "file_size": len(self.pdf_bytes),
                "settings": {},
                "metadata": {"title": self.metadata.title.value or "",
                             "doi": self.metadata.doi.value or ""},
                "edits": {},
                "grobid": None,
                "card_index": self.position,
            }
            return store.save_paper(record)

    def coverage(self, backend: str, target: str = "zh") -> dict:
        """译文覆盖度（哪些卡片已有译文、还差多少段）"""
        with self._lock:
            if not self.parse_result:
                return {}
            info = translate_plan.coverage(self.parse_result["cards"], backend, target)
            info.pop("pending", None)          # 待译清单太长，不塞进响应
            return info


# 全局唯一实例（本机自用，单篇单用户）
STATE = PaperState()
