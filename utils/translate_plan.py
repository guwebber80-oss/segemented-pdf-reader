"""
utils.translate_plan —— 译文覆盖度与「整篇翻译」的待办清单（阶段 6.4）

**为什么单独一个模块**：这两件事都是纯计算——给定卡片与本地译文表，算出
「哪些段落还没有译文」「几张卡已经能离线看中文」。放在这里就能脱离 Streamlit 单测，
界面层只负责画按钮与进度条。

**为什么需要它**（用户 2026-09-16 实测报的现象）：
翻译是**读到哪翻到哪**的懒加载——只有真正显示过的卡片才会被翻译、才会落盘。
于是「关掉进程 → 重新打开 → 重新上传同一篇」时，**没看过的卡片**仍然要联网翻一次，
断网就只能退回英文原文。用户原话：「第一次切换页面时仍要重新翻译，断开网络后翻译失败」。
本地缓存本身没坏（实测：看过的那几张卡 100% 命中），缺的是「把整篇补齐」这一步。

于是本模块提供两种查询：
  · `pending_texts(cards, backend, target)` → 还没译文的段落（喂给「翻译整篇」）
  · `coverage(cards, backend, target)`      → 多少张卡已能离线读（界面上如实显示）
"""

from . import store


def card_segment_texts(card) -> list:
    """
    取一张卡片里**需要翻译**的文本段落。

    卡片带 segments 时只取 heading / text（公式与表格保持原文截图，不翻译）；
    没有 segments 的（「显示全部内容」模式的段落）退回整段 card.text。
    空白段落直接丢掉，避免拿空串去查缓存。
    """
    segments = getattr(card, "segments", None)
    if segments:
        texts = [payload for kind, payload in segments if kind in ("heading", "text")]
    else:
        texts = [getattr(card, "text", "")]
    return [text for text in texts if text and text.strip()]


def collect_texts(cards) -> list:
    """把全部卡片的待译段落收成一个列表：**去重且保持首次出现的顺序**。

    去重是有意义的：同一段文字在卡片间重复出现（跨章节标题、重复的页眉句）时只需翻译一次，
    缓存键本来就按文本哈希算，重复请求纯属浪费额度。
    """
    seen = {}
    for card in cards:
        for text in card_segment_texts(card):
            seen.setdefault(text, None)
    return list(seen)


def pending_texts(cards, backend: str, target: str) -> list:
    """还没译文的段落（会真的去查本地译文表，但不发任何网络请求）"""
    if not backend:
        return []
    return [text for text in collect_texts(cards)
            if store.get_translation(backend, target, text) is None]


def coverage(cards, backend: str, target: str) -> dict:
    """
    译文覆盖度。返回：
        cards           卡片总数
        covered_cards   全部段落都有译文的卡片数（这些卡断网也能看中文）
        texts           待译段落总数（已去重）
        covered_texts   已有译文的段落数
        pending         还没译文的段落列表（界面拿它的长度当「待翻译 N 段」）
    """
    total_texts = collect_texts(cards)
    pending = pending_texts(cards, backend, target) if backend else list(total_texts)
    covered_texts = len(total_texts) - len(pending)

    covered_cards = 0
    if backend:
        for card in cards:
            texts = card_segment_texts(card)
            # 只有公式/表格的卡片不需要翻译，天然算「能离线读」
            if not texts or all(store.get_translation(backend, target, text) is not None
                                for text in texts):
                covered_cards += 1

    return {
        "cards": len(cards),
        "covered_cards": covered_cards,
        "texts": len(total_texts),
        "covered_texts": covered_texts,
        "pending": pending,
    }
