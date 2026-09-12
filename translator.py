"""
翻译模块 —— 多后端可切换，带超时与自动重试
================================================================
本模块【不依赖 Streamlit】，可以脱离网页单独运行、单独测试。

支持三种后端（在 .env 里用 TRANSLATOR_BACKEND 选择，默认 deepl）：
    deepl   官方 API，科研文本质量最好，免费版每月 100 万字符
    openai  走 chat/completions 接口，可自定义术语
    google  Google Cloud Translation v2，需要开通翻译 API

对外主要接口：
    translate(text, target="zh", backend=None) -> str      翻译一句话/一段话，失败抛 TranslationError
    backend_availability() -> dict                          哪些后端配好了 Key
    probe_backend(backend) -> str                           试翻译一小句，用来验证 Key 是否可用

失败时统一抛 TranslationError，它的 message 已经是【给用户看的中文提示】，
界面层直接显示即可，不需要再判断状态码。
"""

import functools
import os
import time

import requests
from dotenv import load_dotenv

# ---- 全局参数 ----
DEFAULT_TIMEOUT = 20      # 单次请求超时（秒）
MAX_RETRIES = 3           # 失败重试次数（含第一次）
RETRY_BACKOFF = 0.6       # 重试间隔基数：0.6s、1.2s……

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

# 后端显示名（界面用）
BACKEND_LABELS = {
    "deepl": "DeepL",
    "openai": "OpenAI",
    "google": "Google 翻译",
}

# 目标语言：内部统一用 zh / en，各后端自己映射
TARGET_LABELS = {"zh": "中文（简体）", "en": "英语"}
_TARGET_MAP = {
    "deepl": {"zh": "ZH-HANS", "en": "EN-US"},
    "openai": {"zh": "简体中文", "en": "English"},
    "google": {"zh": "zh-CN", "en": "en"},
}


class TranslationError(RuntimeError):
    """翻译失败。str(异常) 就是可以直接显示给用户的中文提示。"""


# ============================================================
# 一、配置读取
# ============================================================

@functools.lru_cache(maxsize=1)
def load_config(force_reload: bool = False) -> dict:
    """
    读取 .env 里的配置。

    override=True 表示以 .env 文件为准（改完 .env 点界面上的「重新读取 .env」即可生效，
    不用重启服务）。force_reload=True 会清掉缓存重新读。
    """
    if force_reload:
        load_config.cache_clear()
    load_dotenv(ENV_PATH, override=True)
    return {
        "deepl": os.environ.get("DEEPL_API_KEY", "").strip(),
        "openai": os.environ.get("OPENAI_API_KEY", "").strip(),
        "openai_base": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"),
        "openai_model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip(),
        "google": os.environ.get("GOOGLE_TRANSLATE_API_KEY", "").strip(),
        "backend": os.environ.get("TRANSLATOR_BACKEND", "deepl").strip().lower() or "deepl",
    }


def backend_availability(config: dict = None) -> dict:
    """返回 {后端名: 是否配好 Key}，界面用它决定下拉框里有哪些选项。"""
    config = config or load_config()
    return {
        "deepl": bool(config["deepl"]),
        "openai": bool(config["openai"]),
        "google": bool(config["google"]),
    }


def backend_api_key(backend: str, config: dict = None) -> str:
    config = config or load_config()
    return config.get(backend, "")


# ============================================================
# 二、带重试的请求
# ============================================================

def _request_with_retry(send, timeout: int, retries: int, log=None):
    """
    统一处理「网络异常」和「HTTP 状态码」，把各种失败翻译成人话。

    send(timeout) 是一个闭包，返回 requests.Response。
    可重试：超时、连接失败、429（请求过频）、5xx（服务端临时故障）
    不可重试：401/403（Key 不对）、456（DeepL 额度用尽）、其它 4xx（请求本身有问题）
    """
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            response = send(timeout)
        except requests.exceptions.Timeout:
            last_error = TranslationError(
                f"翻译服务 {timeout} 秒内没有响应（超时）。可能是网络慢或服务拥堵，请重试。"
            )
        except requests.exceptions.ConnectionError:
            last_error = TranslationError(
                "连不上翻译服务：请检查网络是否通畅（断网、代理、防火墙都可能造成）。"
            )
        except requests.exceptions.RequestException as exc:
            last_error = TranslationError(f"网络请求出错：{exc}")
        else:
            status = response.status_code

            if status == 200:
                return response

            if status in (401, 403):
                raise TranslationError(
                    f"API Key 被拒绝（HTTP {status}）。请检查 .env 里的 Key 是否填对、"
                    "是否已被停用。"
                )
            if status == 456:
                raise TranslationError(
                    "DeepL 本月免费额度已用完（HTTP 456）。可在 DeepL 官网查看用量，"
                    "或等月初额度重置，或在 .env 里换用其它后端。"
                )
            if status == 429:
                last_error = TranslationError("请求过于频繁被限流（HTTP 429），稍后会自动重试。")
            elif status >= 500:
                last_error = TranslationError(
                    f"翻译服务暂时故障（HTTP {status}），稍后会自动重试。"
                )
            else:
                # 4xx 里的其它情况：多半是参数问题，重试也没用
                raise TranslationError(
                    f"翻译服务返回错误 HTTP {status}：{response.text[:200]}"
                )

        if attempt < retries:
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            if log:
                log(f"第 {attempt} 次失败（{last_error}），{wait:.1f} 秒后重试…")
            time.sleep(wait)

    raise last_error


# ============================================================
# 三、各后端的具体实现
# ============================================================
# 统一按「多段文本」处理。为什么要支持一次多段：
# 中文视图需要**按段落分别翻译**（这样公式段才能保持图片、不参与翻译），
# 如果每段发一次请求，一张 200 词的卡片要等十几秒。
# DeepL 与 Google 都支持一次请求带多段文本，返回顺序与传入顺序一致。

_BATCH_SIZE = 25          # 一次请求最多带多少段（DeepL 上限 50，这里留足余量）


def _translate_deepl(texts, target: str, config: dict, timeout: int, retries: int, log):
    key = config["deepl"]
    # 免费版 Key 以 ":fx" 结尾，接口域名不一样；这里自动判断
    host = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"

    def send(t):
        return requests.post(
            f"{host}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {key}"},
            # 传列表时 requests 会编码成 text=a&text=b，DeepL 按顺序返回
            data={"text": list(texts), "target_lang": _TARGET_MAP["deepl"][target]},
            timeout=t,
        )

    payload = _request_with_retry(send, timeout, retries, log).json()
    return [item["text"] for item in payload["translations"]]


def _translate_openai(texts, target: str, config: dict, timeout: int, retries: int, log):
    """OpenAI 的 chat 接口没有批量翻译参数，只能逐段调用（其它后端仍是一次请求）"""
    key = config["openai"]
    language = _TARGET_MAP["openai"][target]
    results = []

    for text in texts:
        def send(t, text=text):
            return requests.post(
                f"{config['openai_base']}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": config["openai_model"],
                    "temperature": 0,          # 翻译要稳定，不要发挥
                    "messages": [
                        {"role": "system",
                         "content": f"You are a professional academic translator. "
                                    f"Translate the user's text into {language}. "
                                    f"Output ONLY the translation, no explanation, "
                                    f"keep technical terms accurate."},
                        {"role": "user", "content": text},
                    ],
                },
                timeout=t,
            )

        response = _request_with_retry(send, timeout, retries, log)
        results.append(response.json()["choices"][0]["message"]["content"].strip())
    return results


def _translate_google(texts, target: str, config: dict, timeout: int, retries: int, log):
    import html

    key = config["google"]

    def send(t):
        return requests.post(
            "https://translation.googleapis.com/language/translate/v2",
            params={"key": key},
            data={"q": list(texts), "target": _TARGET_MAP["google"][target], "format": "text"},
            timeout=t,
        )

    payload = _request_with_retry(send, timeout, retries, log).json()
    # Google 会把引号等符号转义成 HTML 实体（&quot; 之类），这里还原
    return [html.unescape(item["translatedText"]) for item in payload["data"]["translations"]]


_BACKENDS = {
    "deepl": _translate_deepl,
    "openai": _translate_openai,
    "google": _translate_google,
}


# ============================================================
# 四、对外接口
# ============================================================

def translate_many(texts, target: str = "zh", backend: str = None,
                   timeout: int = DEFAULT_TIMEOUT, retries: int = MAX_RETRIES, log=None):
    """
    一次翻译多段文本，返回顺序与输入完全一致。

    用途：中文视图要**按段落分别翻译**（公式段保持图片不翻译），
    但 DeepL / Google 支持一次请求带多段文本，所以仍然只发一次 HTTP 请求。
    空白段直接返回空字符串，不浪费额度。
    """
    texts = list(texts)
    if not texts:
        return []

    if target not in _TARGET_MAP["deepl"]:
        raise TranslationError(f"不支持的目标语言：{target}")

    config = load_config()
    backend = (backend or config["backend"]).lower()

    if backend not in _BACKENDS:
        raise TranslationError(
            f"未知的翻译后端：{backend}。可选：{'、'.join(_BACKENDS)}"
        )
    if not backend_api_key(backend, config):
        raise TranslationError(
            f"{BACKEND_LABELS.get(backend, backend)} 没有配置 API Key。"
            f"请在项目根目录的 .env 里填写对应的 Key。"
        )

    results = ["" for _ in texts]
    pending = [(index, text) for index, text in enumerate(texts) if (text or "").strip()]

    for start in range(0, len(pending), _BATCH_SIZE):
        chunk = pending[start:start + _BATCH_SIZE]
        translated = _BACKENDS[backend](
            [text for _, text in chunk], target, config, timeout, retries, log)

        if len(translated) != len(chunk):
            # 段数对不上就不能瞎对应，直接报错（宁可失败也不要错位）
            raise TranslationError(
                f"翻译服务返回的段数与请求不一致（请求 {len(chunk)} 段、返回 {len(translated)} 段）"
            )
        for (index, _), value in zip(chunk, translated):
            results[index] = value

    return results


def translate(text: str, target: str = "zh", backend: str = None,
              timeout: int = DEFAULT_TIMEOUT, retries: int = MAX_RETRIES, log=None) -> str:
    """
    翻译一段文本。失败抛 TranslationError（消息可直接展示给用户）。

    text    原文；空字符串直接返回空字符串（不浪费额度）
    target  "zh" 或 "en"
    backend "deepl" / "openai" / "google"；不传则用 .env 里的 TRANSLATOR_BACKEND
    """
    text = (text or "").strip()
    if not text:
        return ""
    return translate_many([text], target=target, backend=backend,
                          timeout=timeout, retries=retries, log=log)[0]


def probe_backend(backend: str, target: str = "zh", **kwargs) -> str:
    """
    试翻译一小句，用来在界面上验证 Key 是否可用。
    成功返回译文，失败抛 TranslationError。
    """
    return translate("Attention mechanisms improve translation quality.",
                     target=target, backend=backend, **kwargs)
