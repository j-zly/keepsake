"""注意力追踪 — 统计用户对各个话题的关注频率。

用户反复提起某个话题 -> 该话题关注度上升 -> 相关碎片在搜索中权重更高。

存储: Redis Sorted Set `fragmented:attention`
  - member: 话题词（由 jieba/关键词提取来）
  - score: 关注度累计值（每次提及 +2，情绪烈度加权）

三套时间窗口（同 hot_topics 模式）:
  - 全局: fractured:attention (7天)
  - 日榜: fractured:attention:daily (2天)
  - 周榜: fractured:attention:weekly (14天)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import redis

logger = logging.getLogger(__name__)

# Redis key 前缀
ATTENTION_SET = "fragmented:attention"
ATTENTION_DAILY = "fragmented:attention:daily"
ATTENTION_WEEKLY = "fragmented:attention:weekly"

# 过期时间
_ATTENTION_TTL = {
    ATTENTION_SET: 86400 * 7,        # 全局：7天
    ATTENTION_DAILY: 86400 * 2,      # 日榜：2天
    ATTENTION_WEEKLY: 86400 * 14,    # 周榜：14天
}

# 基础关注增量（每次提及）
BASE_ATTENTION_INCREMENT = 2.0

# 情绪烈度放大系数
EMOTION_BOOST_FACTOR = 1.5  # intensity × 此值加到增量


def record_attention(
    client: redis.Redis,
    text: str,
    emotion_intensity: float = 0.0,
    keywords: Optional[List[str]] = None,
) -> None:
    """记录用户对一段文本中话题的关注。"""
    if not client or not text:
        return

    try:
        # 没有关键词时跳过（正常路径由 store() 传入，不会走到这里）
        if not keywords:
            return

        increment = BASE_ATTENTION_INCREMENT + emotion_intensity * EMOTION_BOOST_FACTOR

        for kw in keywords:
            kw_lower = kw.lower().strip()
            if len(kw_lower) < 2:
                continue
            for topic_set in (ATTENTION_SET, ATTENTION_DAILY, ATTENTION_WEEKLY):
                client.zincrby(topic_set, increment, kw_lower)
                client.expire(topic_set, _ATTENTION_TTL.get(topic_set, 86400))

    except Exception as e:
        logger.debug("attention: record_attention error: %s", e)


def get_attention_score(client: redis.Redis, keyword: str) -> float:
    """查某个词在当前注意力分数中的排名分。"""
    if not client or not keyword:
        return 0.0
    try:
        score = client.zscore(ATTENTION_SET, keyword.lower().strip())
        return score if score is not None else 0.0
    except Exception:
        return 0.0


def get_top_attention(client: redis.Redis, limit: int = 10,
                      period: str = "all") -> List[Dict[str, Any]]:
    """获取关注度最高的词。"""
    key = {
        "all": ATTENTION_SET,
        "daily": ATTENTION_DAILY,
        "weekly": ATTENTION_WEEKLY,
    }.get(period, ATTENTION_SET)

    if not client:
        return []
    try:
        raw = client.zrevrange(key, 0, limit - 1, withscores=True)
        results = []
        for t, s_raw in raw:
            topic = t.decode("utf-8") if isinstance(t, bytes) else t
            if isinstance(s_raw, bytes):
                s_raw = s_raw.decode("utf-8")
            results.append({"topic": topic, "score": round(float(s_raw), 1)})
        return results
    except Exception as e:
        logger.debug("attention: get_top_attention error: %s", e)
        return []


def match_attention_boost(
    client: redis.Redis,
    content: str,
    top_n: int = 10,
    boost_max: float = 1.5,
) -> float:
    """检查碎片内容的注意力关注度加权值。

    从全局注意力取 top N 话题，看碎片内容命中几个。
    命中越多权重越高，最高 boost_max。
    """
    if not client or not content:
        return 1.0

    try:
        raw = client.zrevrange(ATTENTION_SET, 0, top_n - 1, withscores=True)
        if not raw:
            return 1.0

        content_lower = content.lower()
        total_score = 0.0
        max_score = 0.0

        for topic_b, score_raw in raw:
            topic = topic_b.decode("utf-8") if isinstance(topic_b, bytes) else topic_b
            if isinstance(score_raw, bytes):
                score_raw = score_raw.decode("utf-8")
            sc = float(score_raw)  # noqa: F841
            if isinstance(sc, (int, float)):
                if len(topic) >= 2 and topic in content_lower:
                    total_score += sc
                max_score += sc

        if max_score <= 0:
            return 1.0

        # 归一化到 1.0~boost_max，命中越高越接近 boost_max
        ratio = min(total_score / max_score, 1.0)
        return 1.0 + (boost_max - 1.0) * ratio

    except Exception as e:
        logger.debug("attention: match_attention_boost error: %s", e)
        return 1.0
