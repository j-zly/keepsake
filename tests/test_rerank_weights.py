"""重排权重旋钮「配了就是不是死配置」的测试。

两处曾经把配置项当摆设：

1. `storage_shared.rerank_with_decay()` 的热门话题权重直接读模块常量
   `HOT_TOPIC_BOOST`，从不看 `storage._hot_topic_boost` ⇒ `hot_topic_boost`
   配成 1.0 和 1.6，输出**逐字相同**。现在读实例值，常量仅作兜底。
2. `scripts/eval_compare_backends.py` 的 `build_redis()` / `build_pg()` 只传
   连接与 embedder，七个权重键一个没传 ⇒ 「按权重调参做 A/B」恒等于
   两份默认配置的对照，得出的任何「加权无影响」都是假阴性。

零网络零数据库：storage 类在测试里被打桩，只验接线，不连真库。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from keepsake import storage, storage_pg, storage_shared

ROOT = Path(storage.__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import eval_compare_backends as ecb  # noqa: E402


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

class _Recorder:
    """打桩用：只记住构造 kwargs，供断言「键真的传进去了」。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture()
def redis_recorder(monkeypatch):
    monkeypatch.setattr(storage, "RedisStorage", _Recorder)
    return storage


@pytest.fixture()
def pg_recorder(monkeypatch):
    monkeypatch.setattr(storage_pg, "PgStorage", _Recorder)
    return storage_pg


#: 旋钮键 → 后端实例上的属性名
KNOB_ATTRS = {
    "decay_half_days": "_decay_half_days",
    "hot_topic_boost": "_hot_topic_boost",
    "hot_topic_decay_half_days": "_hot_topic_decay_half_days",
    "attention_boost_max": "_attention_boost_max",
    "emotion_intensity_factor": "_emotion_intensity_factor",
    "feedback_positive_boost": "_feedback_positive_boost",
    "feedback_negative_penalty": "_feedback_negative_penalty",
}


@pytest.mark.parametrize("key,attr", sorted(KNOB_ATTRS.items()))
def test_eval_script_forwards_every_knob(redis_recorder, pg_recorder, key, attr):
    """配置里有的每个旋钮，两个 build 函数都必须原样透传。"""
    want = {"decay_half_days": 90, "hot_topic_decay_half_days": 45,
            "attention_boost_max": 2.0, "emotion_intensity_factor": 0.9,
            "feedback_positive_boost": 1.7, "feedback_negative_penalty": 0.2,
            "hot_topic_boost": 1.6}[key]
    cfg = {key: want}
    for build in (ecb.build_redis, ecb.build_pg):
        assert build(cfg).kwargs[key] == want


def test_eval_script_omits_absent_knobs(redis_recorder, pg_recorder):
    """键不存在就不传 —— 后端落回自己的构造默认值，默认行为不变。"""
    for build in (ecb.build_redis, ecb.build_pg):
        assert ecb.rank_knobs({}) == {}
        assert not (set(build({}).kwargs) & set(KNOB_ATTRS))


def test_eval_script_knob_change_is_observable(redis_recorder, pg_recorder):
    """🔴 核心红线：改一个键 ⇒ 必须能观测到重排结果变化。

    把 build_redis 实际透传的 kwargs 灌进桩 storage 再跑 rerank ——
    「没接线」的旋钮在这里必然表现为两组输出逐字相同，测试转红。
    """
    def weights_with(cfg, fb=2.0):
        st = _StubStorage(**{KNOB_ATTRS[k]: v for k, v in
                             ecb.build_redis(cfg).kwargs.items()
                             if k in KNOB_ATTRS})
        return _rerank(st, fb=fb)

    pairs = [
        ({"hot_topic_boost": 1.0}, {"hot_topic_boost": 1.6}, 2.0),
        ({"emotion_intensity_factor": 0.0}, {"emotion_intensity_factor": 0.9}, 2.0),
        ({"feedback_positive_boost": 1.0}, {"feedback_positive_boost": 2.0}, 2.0),
        ({"feedback_negative_penalty": 1.0}, {"feedback_negative_penalty": 0.2}, -2.0),
        ({"attention_boost_max": 1.0}, {"attention_boost_max": 3.0}, 2.0),
        ({"decay_half_days": 1}, {"decay_half_days": 365}, 2.0),
    ]
    for lo, hi, fb in pairs:
        w_lo, w_hi = weights_with(lo, fb), weights_with(hi, fb)
        assert w_lo != w_hi, f"{list(lo)[0]} 改了也没变化 → 没接线"


def test_eval_script_default_matches_backend_defaults():
    """空配置 ⇒ 七个旋钮全都不传 ⇒ 与后端 `__init__` 默认值逐字相同。"""
    import inspect
    sig_r = inspect.signature(storage.RedisStorage.__init__)
    sig_p = inspect.signature(storage_pg.PgStorage.__init__)
    for key, _ in ecb.RERANK_KNOBS:
        assert sig_r.parameters[key].default == sig_p.parameters[key].default, key
    assert sig_r.parameters["hot_topic_boost"].default == 1.2