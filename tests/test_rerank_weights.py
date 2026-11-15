"""重排权重旋钮「配了就是不是死配置」的测试。

两处曾经把配置项当摆设：

1. `storage_shared.rerank_with_decay()` 的热门话题权重直接读模块常量
   `HOT_TOPIC_BOOST`，从不看 `storage._hot_topic_boost` ⇒ `hot_topic_boost`
   配成 1.0 和 1.6，输出**逐字相同**。现在读实例值，常量仅作兜底。
2. （历史）`scripts/eval_compare_backends.py` 的旋钮透传断言随该脚本与 Redis
   后端一起移除（2026-11）—— 接线口径现在由 `core/factory.py` 的
   `_resolve_knobs` 单点 + tests/test_storage_pg_knobs.py 钉住。

零网络零数据库：storage 类在测试里被打桩，只验接线，不连真库。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from keepsake import storage_shared


# ---------------------------------------------------------------- 桩

class _StubStorage:
    """最小 storage 桩：只带重排真正会读的属性（与两个后端同名同义）。"""

    _decay_half_days = 60
    _emotion_intensity_factor = 0.4
    _feedback_positive_boost = 1.3
    _feedback_negative_penalty = 0.5
    _attention_boost_max = 1.5
    hits = 3.0

    def __init__(self, **knobs):
        self.hits = knobs.pop("hits", 3.0)
        for k, v in knobs.items():
            setattr(self, k, v)

    def match_hot_topics(self, content, limit=10):
        return self.hits

    def match_attention(self, content, top_n=10):
        # 与真实实现同形：注意力权重封顶在 _attention_boost_max
        return min(2.0, self._attention_boost_max)


def _frag(content="提到 热点 话题", score=1.0, fb=2.0):
    """一条让**每个**权重维度都真正参与运算的候选（否则旋钮改了也看不出来）。"""
    created = datetime.now(timezone.utc) - timedelta(days=100)
    return {"content": content, "_bm25_score": score, "tags": "",
            "created": created.isoformat(),
            "sentiment_score": 1.0, "feedback_score": fb}


def _rerank(stub, **frag_kw):
    out = storage_shared.rerank_with_decay(stub, [_frag(**frag_kw)], "_bm25_score")
    return out[0]["_weights"]


# ---------------------------------------------------------------- 缺陷一

def test_hot_topic_boost_is_not_dead():
    """配 1.0 与配 1.6 必须给出**不同**的 hot_topic 权重。"""
    a = _rerank(_StubStorage(_hot_topic_boost=1.0))
    b = _rerank(_StubStorage(_hot_topic_boost=1.6))
    assert a["hot_topic"] != b["hot_topic"], (
        f"hot_topic_boost 仍未生效：1.0→{a['hot_topic']} 与 1.6→{b['hot_topic']} 相同")


def test_hot_topic_boost_default_unchanged():
    """🔴 不传（对象上没有该属性）时仍回落到模块常量 1.2 —— 默认行为零变化。"""
    stub = _StubStorage()           # 故意不设 _hot_topic_boost
    assert not hasattr(stub, "_hot_topic_boost")
    w = _rerank(stub)
    assert w["hot_topic"] == storage_shared.HOT_TOPIC_BOOST == 1.2


def test_hot_topic_boost_partial_hits_scale():
    """1~2 次命中时按 (boost-1) 线性插值，插值端点也跟着配置走。"""
    w = _rerank(_StubStorage(_hot_topic_boost=1.6, hits=1))
    assert w["hot_topic"] == pytest.approx(1.0 + (1.6 - 1.0) / 3.0, abs=1e-4)


# ---------------------------------------------------------------- 缺陷二
