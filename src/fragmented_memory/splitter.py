"""文本切分工具 + 情感分析 — 按语义完整性切分文本，检测情感倾向。"""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Tuple

NEWLINE = "\n"

# ---------------------------------------------------------------------------
# 情感关键词表（支持中英文）
# ---------------------------------------------------------------------------

_POSITIVE_KEYWORDS: Dict[str, float] = {
    # 强烈正面 (×1.5)
    "太棒了": 1.5, "太好了": 1.5, "非常满意": 1.5, "爱了": 1.5,
    "绝了": 1.5, "牛逼": 1.5, "太强了": 1.5,
    # 中等正面 (×1.0)
    "喜欢": 1.0, "不错": 1.0, "好用": 1.0, "满意": 1.0,
    "推荐": 1.0, "值得": 1.0, "好使": 1.0, "方便": 0.8,
    "清晰": 0.8, "直观": 0.8, "nice": 0.8, "good": 0.8,
    "great": 1.0, "awesome": 1.2, "excellent": 1.3,
}

_NEGATIVE_KEYWORDS: Dict[str, float] = {
    # 强烈负面 (×1.5)
    "太差了": 1.5, "垃圾": 1.5, "恶心": 1.5, "废物": 1.5,
    "烂": 1.3, "垃圾东西": 1.5,
    # 中等负面 (×1.0)
    "讨厌": 1.0, "不好用": 1.0, "不满意": 1.0, "糟糕": 1.0,
    "烦": 0.8, "失望": 1.0, "差评": 1.0, "不行": 0.8,
    "bad": 1.0, "terrible": 1.3, "hate": 1.2, "useless": 1.0,
}

# 否定词 — 出现在情感词前 3 个字符内会反转情感
_NEGATORS = {"不", "没", "别", "勿", "无", "not", "no", "n't", "never"}


def analyze_sentiment(text: str) -> Tuple[float, str]:
    """分析文本情感倾向。

    返回 (sentiment_score, sentiment_label):
        sentiment_score: -1.0 ~ 1.0, 负值=负面, 正值=正面
        sentiment_label: "positive" / "negative" / "neutral"

    算法:
      1. 扫描正负面关键词，匹配时检测前文否定词
      2. 累计正负得分，归一化到 -1~1
      3. |score| < 0.15 视为 neutral
    """
    if not text or not text.strip():
        return 0.0, "neutral"

    text_lower = text.lower().strip()
    pos_score = 0.0
    neg_score = 0.0

    # 扫描正面关键词
    for kw, intensity in _POSITIVE_KEYWORDS.items():
        idx = text_lower.find(kw.lower())
        if idx == -1:
            continue
        # 检查前文是否有否定词
        start = max(0, idx - 4)
        prefix = text_lower[start:idx].strip()
        negated = any(n in prefix for n in _NEGATORS)
        if negated:
            neg_score += intensity * 0.5
        else:
            pos_score += intensity

    # 扫描负面关键词
    for kw, intensity in _NEGATIVE_KEYWORDS.items():
        idx = text_lower.find(kw.lower())
        if idx == -1:
            continue
        start = max(0, idx - 4)
        prefix = text_lower[start:idx].strip()
        negated = any(n in prefix for n in _NEGATORS)
        if negated:
            pos_score += intensity * 0.5
        else:
            neg_score += intensity

    # 归一化到 -1~1
    total = pos_score + neg_score
    if total == 0:
        return 0.0, "neutral"

    score = (pos_score - neg_score) / max(total, 0.1)
    score = max(-1.0, min(1.0, score))

    if score > 0.15:
        label = "positive"
    elif score < -0.15:
        label = "negative"
    else:
        label = "neutral"

    return round(score, 4), label


def extract_keywords(text: str, max_keywords: int = 5) -> List[str]:
    """从文本中提取关键词。

    不引入外部 NLP 库，用启发式规则提取有意义的短语：
      - 提取 2~4 字中文词（常见模式：两字词、三字词、四字成语）
      - 提取 3 字母以上英文词
      - 过滤常见停用词
      - 使用滑动窗口 + 词频筛选
    """
    if not text or not text.strip():
        return []

    _STOP_WORDS = {
        "这个", "那个", "什么", "怎么", "为什么", "可以", "没有",
        "但是", "如果", "因为", "所以", "而且", "然后", "还是",
        "就是", "不是", "一个", "我们", "你们", "他们", "已经",
        "可以", "可能", "应该", "需要", "这样", "那样", "这里",
        "那里", "这个", "这些", "那些", "之后", "之前", "时候",
        "the", "this", "that", "what", "why", "how", "and",
        "but", "for", "with", "not", "are", "was", "had",
        "its", "has", "all", "can", "use", "get", "set",
    }

    text_lower = text.lower().strip()
    candidates: List[str] = []

    # 1. 提取连续中文，取 2 字子串（中文词汇最常见长度）
    chinese_blocks = re.findall(r"[\u4e00-\u9fff]+", text_lower)
    for block in chinese_blocks:
        for i in range(len(block) - 1):
            candidates.append(block[i:i + 2])

    # 2. 提取 3 字词（常见专业术语如 "比特币" "区块链"）
    for block in chinese_blocks:
        for i in range(len(block) - 2):
            candidates.append(block[i:i + 3])

    # 2. 提取英文词（3 字母以上）
    eng_words = re.findall(r"\b[a-zA-Z]{3,}\b", text_lower)
    candidates.extend(eng_words)

    # 3. 过滤停用词 + 数字类词
    filtered = [w for w in candidates
                if w not in _STOP_WORDS
                and not w.isdigit()
                and len(set(w)) > 1]       # 过滤 "哈哈" "AA" 类重复词

    # 4. 按频次降序
    freq = Counter(filtered)
    sorted_words = sorted(freq.items(), key=lambda x: -x[1])
    return [w for w, _ in sorted_words[:max_keywords]]


def split_text(text: str, max_chars: int = 500) -> List[str]:
    """按语义完整性切分文本。

    策略：
      1. 先按段落（连续空行）切分
      2. 超长段落按句子边界（中英文句号/感叹号/问号）切分
      3. 短碎片（<10 字）丢弃

    参数:
        text: 要切分的文本
        max_chars: 单个碎片最大字符数

    返回:
        切分后的文本列表
    """
    if not text or not text.strip():
        return []

    raw_paras = re.split(r"\n\s*\n", text.strip())
    segments: List[str] = []

    for para in raw_paras:
        para = para.strip()
        if not para:
            continue

        if len(para) <= max_chars:
            segments.append(para)
            continue

        # 超长段落：按句子边界切
        sentences = re.split(r"(?<=[。！？.!?\n])", para)
        chunk = ""
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if len(chunk) + len(s) > max_chars and chunk:
                segments.append(chunk.strip())
                chunk = s
            else:
                chunk += s + NEWLINE
        if chunk.strip():
            segments.append(chunk.strip())

    return [s for s in segments if len(s) >= 10]
