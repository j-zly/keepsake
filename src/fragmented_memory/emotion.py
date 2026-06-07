"""情绪烈度分析 — 检测用户表达中的情绪强度，不判断正负。

输入一段文本，返回情绪烈度值 (0.0~2.0)，越高表示用户越激动。

检测维度:
  - 标点密度: !! 和 ?? 的数量
  - 中文程度副词: 太、非常、极其、到底、完全、真的
  - 重复字符: 啊啊啊、对对对、真的真的
  - 反复问: 连续两个以上问句
  - 英文全大写词 (English only)
"""

from __future__ import annotations

import re
from typing import Tuple

# 中文程度副词（单个字 + 常见组合）
_INTENSITY_ADVERBS = frozenset({
    "太", "超", "极", "巨", "贼", "老", "特", "爆",
    "非常", "极其", "超级", "格外", "分外", "过于",
    "无比", "绝顶", "十分", "相当", "特别",
    "完全", "根本", "彻底", "绝对",
    "真的", "真是", "简直", "实在",
    "到底", "究竟", "明明",
})

# 重复字符检测（连续重复 3+ 次）
_RE_REPEATED_CHAR = re.compile(r"(.)\1{2,}")

# 重复词检测（连续重复 2+ 次）
_RE_REPEATED_WORD = re.compile(r"(.{2,4})\1{1,}")

# 问号/叹号簇
_RE_EXCLAMATION_CLUSTER = re.compile(r"!{2,}")
_RE_QUESTION_CLUSTER = re.compile(r"\?{2,}")
_RE_MIXED_PUNCT = re.compile(r"[!?！？]{2,}")

# 全大写英文词（至少 3 字母）
_RE_CAPS_WORD = re.compile(r"\b[A-Z]{3,}\b")

# 否定词（用于检测负面）
_NEGATORS = frozenset({"不", "没", "别", "勿", "无", "not", "no", "never"})


def analyze_emotion(text: str) -> Tuple[float, str]:
    """分析一段文本的情绪烈度和情感极性。

    返回:
        (intensity, label):
            intensity: 0.0~2.0 情绪烈度
            label: "strong_positive", "positive", "negative", "strong_negative", "neutral"
    """
    if not text or not text.strip():
        return 0.0, "neutral"

    raw = text.strip()

    # ---- 1. 情绪烈度计算 ----
    intensity = 0.0

    # 1a: 感叹号/问号密度
    excl = _RE_EXCLAMATION_CLUSTER.findall(raw)
    qst = _RE_QUESTION_CLUSTER.findall(raw)
    mixed = _RE_MIXED_PUNCT.findall(raw)

    # 每个重复标点簇贡献 0.15，最多 0.6
    punct_score = (len(excl) + len(qst) + len(mixed)) * 0.15
    intensity += min(punct_score, 0.6)

    # 混合 !? 连用额外加分（如 "真的吗?!"）
    for m in mixed:
        if "!" in m and "?" in m:
            intensity += 0.2

    # 1b: 程度副词密度
    adverb_hits = sum(1 for adv in _INTENSITY_ADVERBS if adv in raw)
    intensity += min(adverb_hits * 0.2, 0.6)

    # 1c: 重复字符（啊啊啊、对对对）
    repeated_chars = _RE_REPEATED_CHAR.findall(raw)
    intensity += min(len(repeated_chars) * 0.15, 0.3)

    # 1d: 重复词（真的真的、完全完全）
    repeated_words = _RE_REPEATED_WORD.findall(raw)
    intensity += min(len(repeated_words) * 0.2, 0.4)

    # 1e: 全大写英文词
    caps_words = _RE_CAPS_WORD.findall(raw)
    intensity += min(len(caps_words) * 0.15, 0.3)

    # 1f: 反问+连续问句
    q_markers = sum(1 for ch in raw if ch in ("?", "？"))
    if q_markers >= 2:
        intensity += min((q_markers - 1) * 0.1, 0.3)

    # 裁剪到 0.0~2.0
    intensity = max(0.0, min(2.0, intensity))

    # ---- 2. 情感极性判断（弱化版关键词检测） ----
    # 仍然检测关键词，但只影响极性标签，不决定强度
    pos_kw = {"喜欢", "不错", "满意", "nice", "good", "great",
              "太棒", "爱了", "绝了", "牛逼", "推荐", "值得"}
    neg_kw = {"垃圾", "恶心", "讨厌", "烂", "烦", "失望", "差",
              "bad", "terrible", "hate", "useless", "废物"}

    text_lower = raw.lower()
    pos_score = 0.0
    neg_score = 0.0

    for kw in pos_kw:
        if kw in text_lower:
            pos_score += 1.0

    for kw in neg_kw:
        if kw in text_lower:
            neg_score += 1.0

    # 否定词检测：作为独立词出现时才生效（排除"不错"里嵌入的"不"）
    negators_set = set()
    if "不" in text_lower and not any(kw in text_lower for kw in ("不错", "不错过")):
        negators_set.add("不")
    for n in _NEGATORS:
        if n in ("不",):  # already handled
            continue
        if n in text_lower:
            negators_set.add(n)

    if pos_score > 0 and not negators_set:
        label = "strong_positive" if intensity >= 1.2 else "positive"
    elif neg_score > 0 or negators_set:
        label = "strong_negative" if intensity >= 1.2 else "negative"
    else:
        label = "neutral"

    return round(intensity, 4), label
