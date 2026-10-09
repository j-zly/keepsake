"""storage_from_config：postgres 分支的排序权重键必须与 redis 分支同源生效。

修前这 10 个键（decay_half_days / hot_topic_boost / v2_min_score …）在 PG 分支
被整体忽略 —— config.json 里配了也不生效（PgStorage 吃形参默认值）。
不需要真库：PgStorage 惰性建连，构造不触网。
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from keepsake.storage import RedisStorage, storage_from_config
from keepsake.storage_shared import (
    DECAY_HALF_DAYS,
    FEEDBACK_NEGATIVE_PENALTY,
    FEEDBACK_POSITIVE_BOOST,
    HOT_TOPIC_BOOST,
    HOT_TOPIC_DECAY_HALF_DAYS,
)

# 键名 → (实例属性, 模块常量/字面量)
KNOBS = [
    ("decay_half_days", "_decay_half_days", DECAY_HALF_DAYS),
    ("attention_boost_max", "_attention_boost_max", 1.5),
    ("attention_base_increment", "_attention_base_increment", 2.0),
    ("attention_emotion_factor", "_attention_emotion_factor", 1.5),
    ("emotion_intensity_factor", "_emotion_intensity_factor", 0.4),
    ("feedback_positive_boost", "_feedback_positive_boost", FEEDBACK_POSITIVE_BOOST),
    ("feedback_negative_penalty", "_feedback_negative_penalty", FEEDBACK_NEGATIVE_PENALTY),
    ("hot_topic_boost", "_hot_topic_boost", HOT_TOPIC_BOOST),
    ("hot_topic_decay_half_days", "_hot_topic_decay_half_days", HOT_TOPIC_DECAY_HALF_DAYS),
    ("v2_min_score", "_v2_min_score", 0.05),
]

CFG = {
    "decay_half_days": 45,
    "attention_boost_max": 2.5,
    "attention_base_increment": 3.5,
    "attention_emotion_factor": 2.5,
    "emotion_intensity_factor": 0.9,
    "feedback_positive_boost": 1.9,
    "feedback_negative_penalty": 0.9,
    "hot_topic_boost": 1.6,
    "hot_topic_decay_half_days": 11,
    "v2_min_score": 0.42,
}


def _pg_cfg(extra: Dict[str, Any]) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "storage": {"backend": "postgres", "postgres": {"host": "127.0.0.1", "dbname": "keepsake"}},
    }
    cfg.update(extra)
    return cfg


@pytest.mark.parametrize("key,attr,const", KNOBS)
def test_pg_knob_from_config_effective(key: str, attr: str, const: Any) -> None:
    """配了就生效（修前恒等于 const）。"""
    assert getattr(storage_from_config(config=_pg_cfg(CFG)), attr) == CFG[key]


@pytest.mark.parametrize("key,attr,const", KNOBS)
def test_pg_knob_default_is_module_constant(key: str, attr: str, const: Any) -> None:
    """不配 = 模块常量（默认行为不变）。"""
    assert getattr(storage_from_config(config=_pg_cfg({})), attr) == const


def test_pg_hot_topic_boost_reverts_to_constant_when_key_absent() -> None:
    """删键 → 回到常量（证明不是写死成配置值）。"""
    lo = _pg_cfg({"hot_topic_boost": 1.0})
    hi = _pg_cfg({"hot_topic_boost": 1.6})
    assert storage_from_config(config=lo)._hot_topic_boost == 1.0
    assert storage_from_config(config=hi)._hot_topic_boost == 1.6
    assert storage_from_config(config=_pg_cfg({}))._hot_topic_boost == HOT_TOPIC_BOOST


def test_redis_branch_untouched() -> None:
    """redis 分支行为不变：provider 口径的 kwargs 仍逐键落到 RedisStorage。"""
    got = {}
    storage_from_config(
        config={"storage": {"backend": "redis"}},
        redis_cls=lambda **kw: got.update(kw),
        **CFG,
    )
    assert got == CFG
    real = RedisStorage(**CFG)
    for key, attr, _const in KNOBS:
        assert getattr(real, attr) == CFG[key]