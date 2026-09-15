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

import os
import threading

from utils import grobid_client, image_extractor, metadata as metadata_module, pdf_parser, store
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
        self.edits = {}            # 人工修正过的元数据字段（键与 Streamlit 版一致，见 payload.EDIT_KEYS）
        self.settings = payload_module.settings_payload({})   # 解析设置（与 Streamlit 版同名字段）
        self.registry = {}
        self.position = 0
        self.cache_hit = False
        self.error = None

    # ---------------- 打开一篇文献 ----------------
    def open(self, pdf_bytes: bytes, file_name: str, merge_on=True, dehyphenate=True,
             target_words=200, table_mode="image", show_all=False) -> dict:
        """
        解析（或命中缓存）并返回给前端的 JSON（不含 registry）。

        解析设置（合并/连字符/目标词数/表格呈现/显示全部）由前端传进来，并**按 Streamlit 版
        同名字段写进记录**——两个前端共用 `.cache/`，换前端时设置不会突然变。
        """
        with self._lock:
            self.reset()
            self.pdf_bytes = pdf_bytes
            self.file_name = file_name
            self.settings = payload_module.settings_payload({
                "merge_on": merge_on, "dehyphenate_on": dehyphenate,
                "target_words": target_words, "table_mode": table_mode, "show_all": show_all})
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

            # 上次读到第几张 / 人工修正过的元数据：命中本地记录就接上
            if record:
                try:
                    self.position = int(record.get("card_index") or 0)
                except Exception:
                    self.position = 0
                edits = record.get("edits")
                self.edits = dict(edits) if isinstance(edits, dict) else {}

            data = payload_module.build_paper_payload(
                pdf_bytes, file_name, self.parse_result, self.image_result, None,
                self.metadata, position=self.position, cache_hit=self.cache_hit,
                backend=None, target="zh", edits=self.edits, settings=self.settings)
            self.registry = data.pop("_registry", {})
            data["ok"] = True
            data["error"] = None
            # 解析设置 / 元数据修正 / 阅读位置一起写回记录（两个前端共用同一份）
            self._persist()
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

    # ---------------- 阅读位置 / 元数据修正 / 缓存 ----------------
    def _persist(self) -> bool:
        """
        把当前状态合并写回本地记录。

        ⚠️ **必须"读旧记录再改字段"**，不能新建一个空记录覆盖：
        记录里还装着解析设置、人工修正的元数据（edits）、GROBID 结果等，
        覆盖写会把它们抹掉（实测：网页版翻页时会把 Streamlit 版存下的元数据修正清空 ✗）。
        """
        if self.pdf_bytes is None:
            return False
        existing = store.load_paper(self.key) or {}
        record = dict(existing)
        record.update({
            "version": store.CACHE_VERSION,
            "key": self.key,
            "file_name": self.file_name,
            "file_size": len(self.pdf_bytes),
            "card_index": int(self.position or 0),
            "edits": dict(self.edits),
            "settings": dict(self.settings),
        })
        record.setdefault("settings", {})
        if self.metadata is not None:
            record["metadata"] = {"title": self.metadata.title.value or "",
                                  "doi": self.metadata.doi.value or ""}
        return store.save_paper(record)

    def save_position(self, index: int) -> bool:
        """把"读到第几张"写进本地记录（下次打开接着读）"""
        with self._lock:
            self.position = int(index or 0)
            return self._persist()

    def save_metadata(self, edits: dict) -> dict:
        """
        保存人工修正的元数据。

        键用**与 Streamlit 版相同的那套**（`in_title` 等），这样两个前端改的是同一份记录、
        互相都看得见（两套前端共用 `.cache/`）。未知键直接丢弃，避免脏数据写进记录。
        """
        with self._lock:
            cleaned = {}
            for key, value in (edits or {}).items():
                # 只认 `in_xxx` 这套存储键（AUTO_FIELDS 的键就是它们；EDIT_KEYS 是反过来的映射）
                if key in payload_module.AUTO_FIELDS and isinstance(value, str):
                    cleaned[key] = value
            # 值与原值相同时不算"修正"，省得记录里全是和自动提取一样的冗余
            for key, value in list(cleaned.items()):
                field = payload_module.AUTO_FIELDS.get(key)
                if not field:
                    continue
                auto = getattr(self.metadata, field, None)
                if auto is not None and (auto.value or "") == value:
                    cleaned.pop(key)
            self.edits = cleaned
            ok = self._persist()
            return {"ok": ok, "edits": self.edits}

    def clear_metadata_edits(self) -> dict:
        """丢掉人工修正，回到自动提取的值（对应 Streamlit 版的「↺ 用自动提取结果覆盖」）"""
        with self._lock:
            self.edits = {}
            ok = self._persist()
            return {"ok": ok, "edits": {}}

    def refresh_metadata_view(self):
        """返回（自动值 + 人工修正叠加后）的元数据，供前端渲染"""
        with self._lock:
            return payload_module.metadata_payload(self.metadata, self.edits)

    # ---------------- 整篇翻译的待译清单 ----------------
    def pending_texts(self, backend: str, target: str = "zh", limit: int = 400) -> list:
        """
        还没译文的段落（前端拿它循环调用 `/api/translate` 做"翻译整篇"）。
        只返回文本本身，前端按批请求；已有译文的不再返回（服务端与磁盘缓存都命中就不会重复花钱）。
        """
        with self._lock:
            if not self.parse_result:
                return []
            pending = translate_plan.pending_texts(self.parse_result["cards"], backend, target)
            return pending[:limit]

    # ---------------- GROBID 交叉校验（第二意见） ----------------
    def grobid_status(self) -> dict:
        """探测本地 GROBID 服务（不抛异常，返回给前端如实显示）"""
        base = os.environ.get("GROBID_URL", grobid_client.DEFAULT_BASE_URL)
        alive, message = grobid_client.is_alive(base)
        return {"alive": alive, "message": message, "url": base}

    def run_grobid(self) -> dict:
        """
        调 GROBID 抽头部信息，并与本地规则逐字段对照。

        结果存进论文记录（`grobid` 字段，与 Streamlit 版同名字段）→ 下次打开不必重跑（省 5~10 秒）。
        """
        from utils import metadata_compare

        with self._lock:
            if self.pdf_bytes is None:
                return {"ok": False, "error": "还没有打开文献", "rows": [], "alive": False}
            status = self.grobid_status()
            if not status["alive"]:
                return {"ok": False, "error": status["message"], "rows": [],
                        "alive": False, "url": status["url"]}
            try:
                result = grobid_client.header_from_pdf(
                    self.pdf_bytes, base_url=status["url"], filename=self.file_name)
            except grobid_client.GrobidError as exc:
                return {"ok": False, "error": str(exc), "rows": [], "alive": True}
            except Exception as exc:                       # 兜底：任何异常都不能打断阅读
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                        "rows": [], "alive": True}
            self.grobid_result = result
            record = dict(store.load_paper(self.key) or {})
            record["grobid"] = result
            store.save_paper(record)
            rows = metadata_compare.build_comparison(self.metadata, result)
            return {"ok": True, "error": None, "alive": True,
                    "summary": grobid_client.summarize(result),
                    "rows": payload_module.comparison_payload(rows)}

    def apply_grobid(self, store_key: str, value: str, is_list: bool = False) -> dict:
        """
        采信 GROBID 的某个字段。

        列表字段走 `metadata_compare.merge_missing`（**只补空、不覆盖**，与 Streamlit 版同一套逻辑）；
        单值字段直接写入人工修正。两者都落进记录的 `edits`，两个前端都看得见。
        """
        from utils import metadata_compare

        with self._lock:
            if store_key not in payload_module.AUTO_FIELDS:
                return {"ok": False, "error": f"不认识的字段键：{store_key}"}
            if is_list:
                current = self.edits.get(store_key)
                if current is None:
                    field = getattr(self.metadata, payload_module.AUTO_FIELDS[store_key], None)
                    current = (field.value if field else "") or ""
                merged, added = metadata_compare.merge_missing(current, [value])
                self.edits[store_key] = merged
                note = f"已补进 {len(added)} 条" if added else "该条目已存在，未重复添加"
            else:
                self.edits[store_key] = value
                note = "已采用 GROBID 的值"
            ok = self._persist()
            return {"ok": ok, "note": note, "edits": self.edits,
                    "metadata": payload_module.metadata_payload(self.metadata, self.edits)}

    # ---------------- 解析诊断（核对用） ----------------
    def diagnostics(self) -> dict:
        """把"解析过程发生了什么"整理成前端可显示的数据（口径对齐 Streamlit 版诊断面板）"""
        with self._lock:
            if not self.parse_result:
                return {}
            result = self.parse_result
            images = self.image_result.get("images", [])
            clusters = result.get("formula_clusters", [])
            tables = result.get("table_regions", [])
            return {
                "blocks": len(result.get("blocks", [])),
                "paragraphs": len(result.get("paragraphs", [])),
                "cards": len(result.get("cards", [])),
                "total_chars": result.get("total_chars", 0),
                "body_size": result.get("body_size", 0),
                "elapsed": result.get("elapsed", 0),
                "pages": [{"page": p["页码"], "layout": p["检测排版"]}
                          for p in result.get("pages", [])],
                "formula_regions": [
                    {"id": payload_module.region_id(c.page, c.rect), "page": c.page,
                     "members": getattr(c, "member_count", 0),
                     "preview": (c.text or "")[:70]}
                    for c in clusters],
                "table_regions": [
                    {"id": payload_module.region_id(t.get("page", 0), t.get("rect", (0, 0, 0, 0))),
                     "page": t.get("page"), "rows": t.get("行数"), "cols": t.get("列数"),
                     "caption": (t.get("caption") or "")[:60]}
                    for t in tables],
                "images": [{"id": f"fig_{i}", "page": img.page, "card": img.card_order,
                            "pixels": f"{img.pixel_w}×{img.pixel_h}",
                            "display": f"{img.disp_w:.0f}×{img.disp_h:.0f}pt"}
                           for i, img in enumerate(images)],
                "skipped_images": [str(item)[:80]
                                   for item in self.image_result.get("skipped", [])][:10],
            }
    # ---------------- 缓存管理 ----------------
    def clear_cache(self, scope: str = "paper") -> dict:
        """清缓存：`paper`=只删这篇的记录（译文是全局的，不动）；`all`=记录 + 全部译文"""
        with self._lock:
            if scope == "all":
                cleared = store.clear_all()
                self.cache_hit = False
                self.edits = {}
                return {"ok": True, "scope": "all", "cleared": cleared}
            deleted = store.delete_paper(self.key) if self.key else False
            self.cache_hit = False
            self.edits = {}
            return {"ok": True, "scope": "paper", "deleted": bool(deleted)}

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
