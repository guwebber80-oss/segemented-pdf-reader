"""
webapp.server —— 本地 HTTP 服务（自建前端的后端，**只用 Python 标准库**）

为什么不用 Flask/FastAPI：本机自用的小工具，**不想让你再多装依赖**（`pip install` 一次就
可能因为网络/权限卡住）。标准库的 `ThreadingHTTPServer` 足够撑住"一个人、一篇论文"的负载。

接口一览（全部同源，不需要 CORS）：

    GET  /                     前端页面（webapp/ui/index.html）
    GET  /app.css  /app.js     前端静态文件
    POST /api/open             body = PDF 原始字节，头 X-Filename = 文件名
                               → {ok, file, key, cache_hit, position, cards, images...}
    GET  /api/img/<id>?size=   → PNG（插图缩略图 / 插图原图 / 公式表格区域截图）
    POST /api/translate        {"texts":[...]} → {"translations":[...]}（先查本地译文表）
    POST /api/position         {"index": n} → 记下"读到第几张"
    GET  /api/status           → 缓存概况 / 译文覆盖度 / 是否有文献

启动：`python -m webapp.server`（或双击项目根目录的「启动网页版.bat」）
"""

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils import store                                    # noqa: E402
from utils.translator import load_config, backend_availability   # noqa: E402

from . import payload as payload_module                     # noqa: E402
from .state import STATE                                   # noqa: E402

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
DEFAULT_PORT = 8765
MAX_UPLOAD_BYTES = 200 * 1024 * 1024        # 200MB 上限：够放任何论文，也防手滑传大文件
STATIC_TYPES = {".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8"}


def current_backend():
    """按 .env 里配好的 Key 选一个可用后端（与 Streamlit 版同一套判定）"""
    try:
        config = load_config()
        availability = backend_availability(config)
        usable = [name for name, ready in availability.items() if ready]
        if not usable:
            return None
        return config["backend"] if config.get("backend") in usable else usable[0]
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "SegmentedPdfReader/1.0"

    # ---------------- 基础工具 ----------------
    def log_message(self, fmt, *args):        # 默认日志太吵，这里压成一行简短输出
        sys.stdout.write("  %s %s\n" % (self.command, self.path))
        sys.stdout.flush()

    def _send(self, code, body: bytes, content_type: str, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                              # 浏览器提前断开（翻页时常见）不算错

    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _send_error_json(self, message, code=500):
        self._send_json({"ok": False, "error": message}, code=code)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        if length > MAX_UPLOAD_BYTES:
            raise ValueError(f"文件太大（{length / 1024 / 1024:.0f} MB），上限 "
                             f"{MAX_UPLOAD_BYTES / 1024 / 1024:.0f} MB")
        chunks, remaining = [], length
        while remaining > 0:
            chunk = self.rfile.read(min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_json(self) -> dict:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------------- 路由 ----------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path in ("/app.css", "/app.js"):
            return self._serve_static(path.lstrip("/"))
        if path.startswith("/api/img/"):
            return self._api_image(path.rsplit("/", 1)[-1],
                                   parse_qs(urlparse(self.path).query).get("size", ["thumb"])[0])
        if path == "/api/pending":
            return self._api_pending()
        if path == "/api/grobid/status":
            return self._send_json({"ok": True, **STATE.grobid_status()})
        if path == "/api/diagnostics":
            return self._send_json({"ok": True, "diagnostics": STATE.diagnostics()})
        if path == "/api/status":
            return self._api_status()
        return self._send_error_json("没有这个地址：" + path, code=404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/open":
                return self._api_open()
            if path == "/api/translate":
                return self._api_translate()
            if path == "/api/position":
                return self._api_position()
            if path == "/api/metadata":
                return self._api_metadata()
            if path == "/api/metadata/reset":
                return self._api_metadata_reset()
            if path == "/api/cache/clear":
                return self._api_cache_clear()
            if path == "/api/reparse":
                return self._api_reparse()
            if path == "/api/grobid":
                return self._api_grobid()
            if path == "/api/grobid/apply":
                return self._api_grobid_apply()
        except ValueError as exc:                       # 预期内的输入问题
            return self._send_error_json(str(exc), code=400)
        except Exception as exc:                        # 兜底：任何异常都别让服务崩掉
            import traceback
            traceback.print_exc()
            return self._send_error_json(f"{type(exc).__name__}: {exc}", code=500)
        return self._send_error_json("没有这个地址：" + path, code=404)

    # ---------------- 各接口实现 ----------------
    def _serve_static(self, name: str):
        path = os.path.join(UI_DIR, name)
        if not os.path.isfile(path):
            return self._send_error_json(f"前端文件缺失：{name}", code=404)
        with open(path, "rb") as handle:
            body = handle.read()
        kind = STATIC_TYPES.get(os.path.splitext(name)[1], "application/octet-stream")
        self._send(200, body, kind)

    def _api_image(self, ident: str, size: str):
        blob = STATE.image_bytes(ident, size)
        if not blob:
            return self._send_error_json("取不到这张图（可能换了一篇文献）", code=404)
        self._send(200, blob, "image/png", {"Cache-Control": "public, max-age=600"})

    def _api_reparse(self):
        """用当前（或新传入的）设置重新解析**同一篇**文献——不用重新上传文件"""
        if STATE.pdf_bytes is None:
            return self._send_error_json("还没有打开文献", code=400)
        body = self._read_json()
        settings = STATE.settings
        if isinstance(body.get("settings"), dict):
            settings = {**settings, **body["settings"]}
        started = time.time()
        data = STATE.open(STATE.pdf_bytes, STATE.file_name,
                          merge_on=settings.get("merge_on", True),
                          dehyphenate=settings.get("dehyphenate_on", True),
                          target_words=settings.get("target_words", 200),
                          table_mode=settings.get("table_mode", "image"),
                          show_all=settings.get("show_all", False),
                          runin_patch=settings.get("runin_patch", False),
                          figure_region=settings.get("figure_region", False))
        data["backend"] = current_backend()
        data["elapsed_total"] = round(time.time() - started, 2)
        print(f"  重新解析：{STATE.file_name} · 卡片 {len(data.get('cards') or [])} 张 · "
              f"用时 {data['elapsed_total']}s")
        self._send_json(data)

    def _api_grobid(self):
        """运行 GROBID 交叉校验（返回逐字段对照；服务不在时如实说明）"""
        result = STATE.run_grobid()
        self._send_json(result)

    def _api_grobid_apply(self):
        """采信 GROBID 的某个字段（列表字段只补空不覆盖）"""
        body = self._read_json()
        store_key = body.get("store_key") or ""
        value = body.get("value") or ""
        is_list = bool(body.get("is_list"))
        if not store_key:
            return self._send_error_json("缺少 store_key", code=400)
        self._send_json(STATE.apply_grobid(store_key, value, is_list))

    def _api_pending(self):
        """整篇翻译用的待译清单（前端分批调用 /api/translate，自带进度）"""
        backend = current_backend()
        if not backend:
            return self._send_json({"ok": False, "texts": [],
                                    "error": "没有可用的翻译后端：请在 .env 里配置 API Key"})
        texts = STATE.pending_texts(backend, "zh")
        self._send_json({"ok": True, "texts": texts, "count": len(texts), "backend": backend})

    @staticmethod
    def _parse_settings(query: dict) -> dict:
        """从查询串解析解析设置（PDF 本体占满请求体，所以设置只能走 URL）"""
        def flag(name, default):
            raw = (query.get(name) or [""])[0]
            return default if raw == "" else raw not in ("0", "false", "False")

        def number(name, default):
            try:
                return int((query.get(name) or [str(default)])[0])
            except ValueError:
                return default

        # 统一交给 payload.settings_payload 规范化（字段名与 Streamlit 版一致），
        # 这样 `state.open()` 拿到的一定是它认识的键（早先直接把 table_mode_label 传进去，
        # 触发 TypeError → /api/open 500，是端到端测试抓出来的）
        return payload_module.settings_payload({
            "merge_on": flag("merge", True),
            "dehyphenate_on": flag("dehyphenate", True),
            "target_words": number("words", 200),
            "table_mode_label": (query.get("table") or ["截图（推荐）"])[0],
            "show_all": flag("show_all", False),
            "runin_patch": flag("runin", False),
            "figure_region": flag("fig", False),
        })

    def _api_open(self):
        pdf = self._read_body()
        if not pdf[:5].startswith(b"%PDF"):
            return self._send_error_json("这不是一个 PDF 文件（缺少 %PDF 头）", code=400)
        file_name = self.headers.get("X-Filename") or "文献.pdf"
        file_name = unquote(file_name)          # 文件名走请求头，中文要解码
        settings = self._parse_settings(parse_qs(urlparse(self.path).query))
        started = time.time()
        data = STATE.open(pdf, file_name,
                          merge_on=settings["merge_on"],
                          dehyphenate=settings["dehyphenate_on"],
                          target_words=settings["target_words"],
                          table_mode=settings["table_mode"],
                          show_all=settings["show_all"],
                          runin_patch=settings["runin_patch"],
                          figure_region=settings["figure_region"])
        if not data.get("ok"):
            return self._send_json(data, code=200)      # 解析失败也要让前端显示原因
        data["backend"] = current_backend()
        data["elapsed_total"] = round(time.time() - started, 2)
        print(f"  打开：{file_name} · 卡片 {len(data['cards'])} 张 · "
              f"缓存命中 {data['cache_hit']} · 用时 {data['elapsed_total']}s")
        self._send_json(data)

    def _api_translate(self):
        body = self._read_json()
        texts = body.get("texts") or []
        backend = body.get("backend") or current_backend()
        target = body.get("target") or "zh"
        if not backend:
            return self._send_json({"ok": False, "translations": [],
                                    "error": "没有可用的翻译后端：请在 .env 里配置 API Key"})
        if not texts:
            return self._send_json({"ok": True, "translations": [], "error": None})
        result = STATE.translate(texts, backend, target)
        result["backend"] = backend
        self._send_json(result)

    def _api_position(self):
        body = self._read_json()
        ok = STATE.save_position(body.get("index", 0))
        self._send_json({"ok": ok})

    def _api_metadata(self):
        """保存人工修正的元数据（键沿用 Streamlit 版那套 in_xxx，两个前端共用一份记录）"""
        body = self._read_json()
        result = STATE.save_metadata(body.get("edits") or {})
        result["metadata"] = STATE.refresh_metadata_view()
        self._send_json(result)

    def _api_metadata_reset(self):
        """丢掉人工修正，回到自动提取的值"""
        result = STATE.clear_metadata_edits()
        result["metadata"] = STATE.refresh_metadata_view()
        self._send_json(result)

    def _api_cache_clear(self):
        body = self._read_json()
        scope = body.get("scope") or "paper"
        if scope not in ("paper", "all"):
            return self._send_error_json("scope 只能是 paper 或 all", code=400)
        result = STATE.clear_cache(scope)
        stats = store.cache_stats()
        result["cache"] = {"papers": stats.get("papers", 0),
                           "translations": stats.get("translations", 0),
                           "kb": round((stats.get("bytes", 0) or 0) / 1024, 1)}
        self._send_json(result)

    def _api_status(self):
        stats = store.cache_stats()
        backend = current_backend()
        pending = STATE.pending_texts(backend, "zh") if backend else []
        self._send_json({
            "ok": True,
            "has_paper": STATE.pdf_bytes is not None,
            "file": {"name": STATE.file_name, "size": len(STATE.pdf_bytes or b"")},
            "key": STATE.key,
            "cache_hit": STATE.cache_hit,
            "backend": backend,
            "coverage": STATE.coverage(backend, "zh") if backend else {},
            "pending_count": len(pending),
            "cache": {"papers": stats.get("papers", 0),
                      "translations": stats.get("translations", 0),
                      "kb": round((stats.get("bytes", 0) or 0) / 1024, 1),
                      "root": stats.get("root", "")},
            "version": "6.7-web",
        })


def serve(port: int = DEFAULT_PORT, open_browser: bool = True, quiet: bool = False):
    """启动服务（阻塞）。返回的 server 对象在测试里用来 shutdown。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)      # 只监听本机
    url = f"http://127.0.0.1:{port}/"
    if not quiet:
        print("=" * 68)
        print("  科研文献 PDF 智能阅读器 · 网页版（自建前端）")
        print(f"  打开：{url}")
        print("  关闭这个窗口就是停止服务（或在窗口里按 Ctrl+C）")
        print("=" * 68)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")
    finally:
        httpd.server_close()
    return httpd


def main():
    parser = argparse.ArgumentParser(description="科研文献 PDF 智能阅读器 · 网页版")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    serve(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
