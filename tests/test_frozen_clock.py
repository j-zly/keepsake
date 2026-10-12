"""评分时钟可冻结 —— `scoring_now()` / `frozen_scoring_now()` 的契约测试。

背景（2026-10-12 实测）：`rerank_with_decay` 的时衰减、`match_hot_topics` 的热词衰减
都以「查询那一刻」求值 ⇒ 同一行两次查询分数就不同；两条 `bm25`/`sim` 逐位相同、只差
1e-7 级衰减项的行 **1.5 秒内会互相穿过**（诊断实录：round0 A 领先 5.2e-8、round1 B
反超 1.3e-8）⇒ 评测 `--repeat 3` 的「同题连跑 3 轮 key 序列逐字相同」变成掷骰子。

修法 = 评分时钟可注入：
  * 生产默认不冻结 ⇒ `scoring_now()` 返回真实墙钟，行为与改动前逐字一致（下面第 1、4 条
    测试就是钉这个「零变化」的）；
  * 评测器用 `frozen_scoring_now()` 钉死 ⇒ 块内每轮同一时刻 ⇒ 同题同序可验证。

🔴 只有「按当前时间算权重」的读路径走这个时钟；写路径（`store()` 落 `created`、
timeline 时间戳）一律不许读 —— 冻住写时间会污染数据。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from keepsake.storage_shared import (
    frozen_scoring_now,
    rerank_with_decay,
    scoring_now,
    scoring_now_ts,
)

HALF_DAYS = 30.0


class _StubStorage:
    """只给 rerank 需要的实例权重；**故意不提供** match_hot_topics / match_attention，
    这样 _combined_score = sim × decay × 1 × 1，能和手算值逐位对齐。"""

    _decay_half_days = HALF_DAYS
    _emotion_intensity_factor = 0.4
    _feedback_positive_boost = 1.5
    _feedback_negative_penalty = 0.5
    _hot_topic_boost = 1.2


def _frag(created: datetime) -> dict:
    return {
        "key": "memory:frag:test",
        "content": "测试正文",
        "created": created.isoformat(),
        "tags": "",
        "_bm25_score": 1.0,
    }


def test_default_clock_is_real_wall_clock():
    """🔴 零变化兜底：没冻结时 `scoring_now()` 就是当前墙钟（生产行为不变）。"""
    before = datetime.now(timezone.utc)
    got = scoring_now()
    ts = scoring_now_ts()
    after = datetime.now(timezone.utc)
    assert before <= got <= after, f"默认时钟必须落在真实墙钟区间，拿到 {got}"
    assert got.tzinfo is not None, "必须带时区，否则 age_days 算出来是错的"
    # 秒值同样夹在同一区间里（ts 必须在 before/after **之间**取，取晚了就跑到 after 外）
    assert before.timestamp() <= ts <= after.timestamp(), f"{before.timestamp()} <= {ts} <= {after.timestamp()}"


def test_freeze_pins_then_restores():
    """冻结块内恒等于给定时刻；退出立刻回到真实墙钟。"""
    pin = datetime(2026, 10, 12, 3, 4, 5, tzinfo=timezone.utc)
    with frozen_scoring_now(pin) as got:
        assert got == pin
        assert scoring_now() == pin, "块内必须恒等于冻结时刻"
        # 冻结是可嵌套/可恢复的：块内再冻一次，退出后应回到上一层的 pin
        with frozen_scoring_now() as inner:
            assert inner == pin, "缺省参数应取「进入时刻」，而此刻被外层冻在 pin"
        assert scoring_now() == pin
    assert scoring_now() != pin, "退出必须解冻"
    assert datetime.now(timezone.utc) - scoring_now() < timedelta(seconds=5)


def test_frozen_rounds_are_bit_identical():
    """同一冻结时刻下连跑两轮重排，分数逐位相同 —— 这是评测跨轮可复现的依据。"""
    pin = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    created = pin - timedelta(days=3)
    with frozen_scoring_now(pin):
        r1 = rerank_with_decay(_StubStorage(), [_frag(created)])
        r2 = rerank_with_decay(_StubStorage(), [_frag(created)])
    assert r1[0]["_combined_score"] == r2[0]["_combined_score"], "冻结下必须逐位相同"

    # 且等于手算值：sim = 1.0 / max(1.0) = 1.0，其余权重 1.0 ⇒ combined = decay
    expect = 2.0 ** (-3.0 / HALF_DAYS)
    assert abs(r1[0]["_combined_score"] - expect) < 1e-12, (
        f"手算 decay={expect!r}，实际 {r1[0]['_combined_score']!r}")


def test_decay_moves_with_clock():
    """反向证明：时钟往后挪，同一行分数必须变 —— 确定性不是「分数本来就不动」换来的。"""
    created = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    with frozen_scoring_now(datetime(2026, 10, 12, tzinfo=timezone.utc)):
        early = rerank_with_decay(_StubStorage(), [_frag(created)])[0]["_combined_score"]
    with frozen_scoring_now(datetime(2026, 10, 27, tzinfo=timezone.utc)):
        late = rerank_with_decay(_StubStorage(), [_frag(created)])[0]["_combined_score"]
    assert late < early, f"15 天后同一行分数必须更低：{late!r} vs {early!r}"
    # created=10-01，两次时钟相隔 15 天 ⇒ 分数比值 = 2^(-15/半衰期30)（不是 0.5：
    # 半衰期是「年龄每加 30 天腰斩一次」，此时年龄已是 11 天 ≠ 0）
    assert abs(late / early - 2.0 ** (-15.0 / HALF_DAYS)) < 1e-9, (
        f"比值 {late / early!r} vs 期望 {2.0 ** (-15.0 / HALF_DAYS)!r}")
