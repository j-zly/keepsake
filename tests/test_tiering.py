"""常驻层自动分级班车（core_tiering）—— 规则 + 豁免判据 + 端点。

## 这个文件钉死的三件事

  1. **三条规则的正反双向**：够热才提名 / 够老够热才自动升 / 水位到才提名退居，
     每个「够」都配一条「不够」的反例（4 命中的不提名、预算不足的不升、
     未到水位的提名）；
  2. **豁免判据 `is_explicit_core` 三条件**：正面（Hermes 显式条目）+
     三种反面（distill 产物 / 子代理 `agent:` 域 / MCP 无 `memory` 标）；
  3. **默认关死**：`enabled=false` 时哪怕造够全部触发条件也是**零写**，
     且「零写」是双向断言（标签一字未动 + 状态文件都没生成）。

## 时间怎么注入（别 monkeypatch 时钟造 flaky）

`run_tiering(..., now=...)` 一个参数吃到底：所有 age / 窗口判据都读它。
命中事件则**按 now 的相对天数**写进打点表（`record_hits` 只认真实时钟，
所以用 `ts` 直接改存储层的表也不合适 —— 改用「窗口长度」来控制，见
`_seed_hits` 的注释），断言因此完全确定、不 sleep、不依赖挂钟。

## 后端

SQLite（`tmp_path` 临时库，**真后端** —— 升降走的就是真 `update_fragment_fields`
写路径，假存储只能测到本模块自己的 if/else）。PG 隔离库 / Redis 假 client
本单不需要：三条规则全部落在存储层原语之上，SQLite 全绿即语义全绿。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.core.tiering import (
    AUTO_HOT_PREFIX,
    AUTO_PROMOTED_TAG,
    CANDIDATE_TAG,
    CORE_TAG,
    DEMOTE_CANDIDATE_TAG,
    is_explicit_core,
    resolve_tiering,
    run_tiering,
    validate_tiering,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
TOKEN = "tiering-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# fixture：真 SQLite 存储 + 审计/备份/状态文件全部指到 tmp_path
# ---------------------------------------------------------------------------

def _cfg(**overrides: Any) -> Dict[str, Any]:
    """默认启用 + 缺省阈值的 config dict；`overrides` 按 `__` 分层覆盖。

    🟡 测试意图：本文件的用例想验证「阈值边界」「满窗 vs 不满窗」等**判据**，
    而不是「3000 vs 5000」这个具体数字。所以测试里通常会把 `nominate_min_hits` /
    `promote_approve_hits` 显式覆盖成 5 之类的小值，再用现成种子数据驱动 —
    边界靠「±1」这种判据钉死，而不是靠大量打点数据。
    """
    base: Dict[str, Any] = {
        "tiering": {
            "enabled": True,
            "promote_nominate": {"window_days": 7, "nominate_min_hits": 3000},
            "promote_auto": {"review_window_days": 7, "promote_approve_hits": 5000},
            "demote_pressure": {"warn_ratio": 0.90, "release_ratio": 0.80,
                                "force_at_full": True},
            "demote_exempt": {"explicit_core": True},
            "batch_cap": 50,
        },
        "retrieval": {"core_char_budget": 1000},
    }
    for dotted, value in overrides.items():
        section, _, key = dotted.partition("__")
        base[section][key] = value
    return base


def _set(tiering: Dict[str, Any], key: str, value: Any) -> Dict[str, Any]:
    tiering[key] = value
    return {"tiering": tiering, "retrieval": {"core_char_budget": 1000}}


@pytest.fixture()
def storage(tmp_path):
    from keepsake.core.sqlite import SqliteStorage

    s = SqliteStorage(path=str(tmp_path / "tiering.db"), embedder=None, embed_dim=8)
    assert s.ensure_index() is not False
    return s


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """审计 / 备份 / 班车状态文件全部指到 tmp_path（88 上没有 /root/.hermes）。"""
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setenv("KEEPSAKE_TIERING_STATE", str(tmp_path / "tiering_state.json"))
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)


def _seed(storage, rows: List[Dict[str, Any]]) -> None:
    """种子条目：`tags` / `content` / `created` 是本单唯一要控的三样东西。

    `created` 决定「挂候选标多久了」的回落口径（老数据没有 `tier:auto-hot:<日期>`
    标时用它，见 `_age_days`）。
    """
    assert storage.write_fragments_batch([dict(r) for r in rows]) == len(rows)


def _seed_hits(storage, key: str, n: int, *, days_ago: float = 0.0) -> None:
    """往打点表塞 n 条命中事件，`ts` = now - days_ago 天。

    为什么能直接写 `ks_hit_event`：`record_hits` 只认真实时钟，而本模块所有窗口
    判据都吃 `now=` —— 事件的**绝对时刻**只用来落在/不落在窗口里，两端都由测试
    用「远小于窗口」/「远大于窗口」拉开距离，不依赖真实挂钟。
    """
    from keepsake.core.sqlite import SqliteStorage

    ts = (NOW - timedelta(days=days_ago)).isoformat()
    with storage._write() as cur:      # noqa: SLF001 —— 测试直写打点表造数据
        cur.executemany("INSERT INTO ks_hit_event (key, ts) VALUES (?, ?)",
                        [(key, ts)] * n)


def _tags(storage, key: str) -> List[str]:
    row = storage.get_fragment(key) or {}
    return [t.strip() for t in str(row.get("tags", "")).split(",") if t.strip()]


def _core_keys(storage) -> List[str]:
    return [str(r["key"]) for r in storage.list_by_tag(CORE_TAG)]


# ---------------------------------------------------------------------------
# 规则一：升提名
# ---------------------------------------------------------------------------

def test_规则一_阈值内命中才提名(storage):
    """降阈值到 5（沿用原用例意图）：5 命中提名，4 命中不提名。

    边界 2999/3000 是另一个独立用例（test_规则一_默认阈值边界）—— 本条专注
    「数据差 1 ⇒ 不提名」的反向钉死，阈值的「5」只是显式覆盖（默认 3000 是
    真实生产值，不该让用例打 3000 次点把 IO 撑爆）。
    """
    _seed(storage, [{"key": "memory:frag:hot", "content": "热门条目" * 10, "tags": "demo"},
                    {"key": "memory:frag:lukewarm", "content": "差一点" * 10, "tags": "demo"}])
    _seed_hits(storage, "memory:frag:hot", 5)
    _seed_hits(storage, "memory:frag:lukewarm", 4)      # 差一次 ⇒ 不该提名
    cfg = {"tiering": {"enabled": True,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 5}},
           "retrieval": {"core_char_budget": 1000}}

    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)

    assert stats["nominate"]["count"] == 1
    assert stats["nominate"]["keys"] == ["memory:frag:hot"]
    assert CANDIDATE_TAG in _tags(storage, "memory:frag:hot")
    assert CANDIDATE_TAG not in _tags(storage, "memory:frag:lukewarm")


def test_规则一_默认阈值边界_2999不提名_3000提名(storage):
    """默认 3000 是用户在带流量时拍板的口径（口语碎片高频打点也能进的旧值
    已被淘汰），本条钉死边界：
      * 2999 次 ⇒ 不提名（差 1）
      * 3000 次 ⇒ 提名（刚好等于）
    """
    _seed(storage, [{"key": "memory:frag:edge_below", "content": "差一点" * 10,
                     "tags": "demo"},
                    {"key": "memory:frag:edge_on", "content": "刚好" * 10,
                     "tags": "demo"}])
    _seed_hits(storage, "memory:frag:edge_below", 2999)
    _seed_hits(storage, "memory:frag:edge_on", 3000)

    stats = run_tiering(storage, _cfg(), dry_run=False, now=NOW)

    assert stats["nominate"]["count"] == 1
    assert stats["nominate"]["keys"] == ["memory:frag:edge_on"]
    assert CANDIDATE_TAG in _tags(storage, "memory:frag:edge_on")
    assert CANDIDATE_TAG not in _tags(storage, "memory:frag:edge_below")


def test_规则一_提名标带来源与提名日(storage):
    _seed(storage, [{"key": "memory:frag:hot", "content": "热门条目" * 10, "tags": "demo"}])
    _seed_hits(storage, "memory:frag:hot", 6)
    cfg = {"tiering": {"enabled": True,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 5}},
           "retrieval": {"core_char_budget": 1000}}
    run_tiering(storage, cfg, dry_run=False, now=NOW)
    tags = _tags(storage, "memory:frag:hot")
    assert f"{AUTO_HOT_PREFIX}:2026-10-16" in tags, "提名标必须带提名日（挂多久了全靠它）"


def test_规则一_已是候选的不重复打(storage):
    _seed(storage, [{"key": "memory:frag:hot", "content": "热门条目" * 10,
                     "tags": f"demo,{CANDIDATE_TAG}"}])
    _seed_hits(storage, "memory:frag:hot", 9)
    cfg = {"tiering": {"enabled": True,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 5}},
           "retrieval": {"core_char_budget": 1000}}
    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)
    assert stats["nominate"]["count"] == 0
    assert stats["nominate"]["already"] == 1
    # 已在候选的**不重打提名日**（否则挂的天数每天被刷新，永远长不满 review_window_days）
    assert not any(t.startswith(AUTO_HOT_PREFIX) for t in _tags(storage, "memory:frag:hot"))


def test_规则一_常驻条目不提名(storage):
    _seed(storage, [{"key": "memory:frag:core", "content": "常驻条目" * 10,
                     "tags": f"{CORE_TAG},shared"}])
    _seed_hits(storage, "memory:frag:core", 20)
    cfg = {"tiering": {"enabled": True,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 5}},
           "retrieval": {"core_char_budget": 1000}}
    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)
    assert stats["nominate"]["count"] == 0
    assert CANDIDATE_TAG not in _tags(storage, "memory:frag:core")


def test_规则一_窗口与阈值可注入(storage):
    _seed(storage, [{"key": "memory:frag:hot", "content": "热门条目" * 10, "tags": "demo"}])
    _seed_hits(storage, "memory:frag:hot", 3)
    # 显式把 nominate_min_hits 设成 10 ⇒ 3 次不够
    high = {"tiering": {"enabled": True,
                        "promote_nominate": {"window_days": 7, "nominate_min_hits": 10}},
            "retrieval": {"core_char_budget": 1000}}
    assert run_tiering(storage, high, dry_run=True, now=NOW)["nominate"]["count"] == 0
    tiering = {"promote_nominate": {"window_days": 7, "nominate_min_hits": 3}}
    stats = run_tiering(storage, _set(tiering, "enabled", True), dry_run=False, now=NOW)
    assert stats["nominate"]["count"] == 1
    # 窗口收窄到 7 天：400 天前的事件不算
    _seed_hits(storage, "memory:frag:late", 5, days_ago=400)
    _seed(storage, [{"key": "memory:frag:late", "content": "很久以前的热门" * 10,
                     "tags": "demo"}])
    narrow = {"promote_nominate": {"window_days": 7, "nominate_min_hits": 5}}
    out = run_tiering(storage, _set(narrow, "enabled", True), dry_run=False, now=NOW)
    assert "memory:frag:late" not in out["nominate"]["keys"], "窗口外的事件不该提名"


def test_规则一_batch_cap封顶(storage):
    rows = [{"key": f"memory:frag:h{i:02d}", "content": "热门" * 10, "tags": "demo"}
            for i in range(6)]
    _seed(storage, rows)
    for r in rows:
        _seed_hits(storage, r["key"], 5)
    cfg = {"tiering": {"enabled": True, "batch_cap": 2,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 5}},
           "retrieval": {"core_char_budget": 1000}}
    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)
    assert stats["nominate"]["count"] == 2
    assert sum(1 for r in rows if CANDIDATE_TAG in _tags(storage, r["key"])) == 2


# ---------------------------------------------------------------------------
# 规则二：自动审核（候选满 review_window_days → 通过/驳回 二选一）
# ---------------------------------------------------------------------------

def _candidate_row(key: str, *, nominated_days_ago: float, content: str = "候选条目" * 10,
                   tags: str = f"demo,{CANDIDATE_TAG}") -> Dict[str, Any]:
    """一条「已经挂着候选标、提名于 N 天前」的条目。

    提名日用**带日期的提名标**表达（与生产同一判据），`created` 只作旧数据回落。
    """
    day = (NOW - timedelta(days=nominated_days_ago)).date()
    return {
        "key": key,
        "content": content,
        "tags": f"{tags},{AUTO_HOT_PREFIX}:{day.isoformat()}",
        "created": (NOW - timedelta(days=nominated_days_ago + 1)).isoformat(),
    }


def test_规则二_满窗且命中足自动通过(storage):
    """默认 review_window_days=7、promote_approve_hits=5000：

    旧用例沿用小数据：把阈值显式降到 3 ⇒ 提名 8 天前 + 命中 3 ⇒ 自动通过；
    提名 6 天前 + 命中 3 ⇒ 未满窗，仍待审。
    """
    _seed(storage, [_candidate_row("memory:frag:old", nominated_days_ago=8),
                    _candidate_row("memory:frag:young", nominated_days_ago=6)])
    _seed_hits(storage, "memory:frag:old", 3)
    _seed_hits(storage, "memory:frag:young", 3)
    cfg = {"tiering": {"enabled": True,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 3000},
                       "promote_auto": {"review_window_days": 7,
                                        "promote_approve_hits": 3}},
           "retrieval": {"core_char_budget": 1000}}

    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)

    assert stats["auto_review"]["approved_count"] == 1
    assert stats["auto_review"]["approved"] == ["memory:frag:old"]
    assert stats["auto_review"]["still_pending"] == 1
    assert "memory:frag:old" in _core_keys(storage)
    tags = _tags(storage, "memory:frag:old")
    assert CORE_TAG in tags and AUTO_PROMOTED_TAG in tags
    assert CANDIDATE_TAG not in tags, "自动批准必须摘候选标"
    # 未满窗的那条原地不动
    assert "memory:frag:young" not in _core_keys(storage)
    assert CANDIDATE_TAG in _tags(storage, "memory:frag:young")


def test_规则二_满窗但命中不够自动驳回(storage):
    """满窗 + 命中不足 ⇒ 自动驳回：摘 core_candidate、加 tier:auto-rejected:<日期>，
    条目留检索层（不在 demote 队列、不打 demote_candidate）。
    """
    from keepsake.core.tiering import AUTO_REJECTED_PREFIX

    _seed(storage, [_candidate_row("memory:frag:weak", nominated_days_ago=10)])
    _seed_hits(storage, "memory:frag:weak", 2)         # 阈值 3 ⇒ 差 1
    cfg = {"tiering": {"enabled": True,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 3000},
                       "promote_auto": {"review_window_days": 7,
                                        "promote_approve_hits": 3}},
           "retrieval": {"core_char_budget": 1000}}

    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)

    assert stats["auto_review"]["approved_count"] == 0
    assert stats["auto_review"]["rejected_count"] == 1
    assert stats["auto_review"]["rejected"] == ["memory:frag:weak"]
    tags = _tags(storage, "memory:frag:weak")
    assert CANDIDATE_TAG not in tags, "驳回必须摘候选标"
    assert f"{AUTO_REJECTED_PREFIX}:{NOW:%Y-%m-%d}" in tags, "驳回必须带短戳"
    # auto-hot 历史戳保留（哪条提的、什么时候提的）
    assert any(t.startswith(AUTO_HOT_PREFIX) for t in tags), "驳回不动 auto-hot 历史戳"
    # 驳回 ≠ 降级：不进 demote 队列
    assert DEMOTE_CANDIDATE_TAG not in tags
    # 条目留在检索层（不是 archive / delete）
    assert storage.get_fragment("memory:frag:weak") is not None


def test_规则二_未满窗不动仍待审(storage):
    """提名 6 天（< review_window_days=7）⇒ 不动：approved=0, rejected=0, pending≥1。"""
    _seed(storage, [_candidate_row("memory:frag:young", nominated_days_ago=6)])
    _seed_hits(storage, "memory:frag:young", 100)
    cfg = {"tiering": {"enabled": True,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 3000},
                       "promote_auto": {"review_window_days": 7,
                                        "promote_approve_hits": 3}},
           "retrieval": {"core_char_budget": 1000}}

    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)

    assert stats["auto_review"]["approved_count"] == 0
    assert stats["auto_review"]["rejected_count"] == 0
    assert stats["auto_review"]["still_pending"] == 1
    assert CANDIDATE_TAG in _tags(storage, "memory:frag:young")


def test_规则二_驳回幂等_二次跑不重复打戳(storage):
    """已驳回的条目（无 core_candidate）天然不进 list_by_tag(CANDIDATE_TAG)
    ⇒ 二次跑 stats["auto_review"] 全 0、不打新戳、auto-hot 历史戳仍在。
    """
    from keepsake.core.tiering import AUTO_REJECTED_PREFIX

    _seed(storage, [_candidate_row("memory:frag:r1", nominated_days_ago=10)])
    _seed_hits(storage, "memory:frag:r1", 0)         # 0 命中 ⇒ 一定驳回
    cfg = {"tiering": {"enabled": True,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 3000},
                       "promote_auto": {"review_window_days": 7,
                                        "promote_approve_hits": 3}},
           "retrieval": {"core_char_budget": 1000}}

    first = run_tiering(storage, cfg, dry_run=False, now=NOW)
    second = run_tiering(storage, cfg, dry_run=False, now=NOW)

    assert first["auto_review"]["rejected_count"] == 1
    assert second["auto_review"]["rejected_count"] == 0
    tags = _tags(storage, "memory:frag:r1")
    assert tags.count(f"{AUTO_REJECTED_PREFIX}:{NOW:%Y-%m-%d}") == 1, "驳回戳不许重复"
    assert CANDIDATE_TAG not in tags


def test_规则二_预算不足_批准留队列不硬塞(storage):
    """自动批准也吃预算护栏；驳回不耗预算、不受护栏约束。

    预算 100，正文各 60 字符：批准第一条（60）后再批第二条会超 ⇒ 第二条留队列；
    但第一条如果命中不够会被驳回，不占预算，第三条正常批准。
    """
    _seed(storage, [_candidate_row("memory:frag:a", nominated_days_ago=10,
                                   content="候" * 60),
                    _candidate_row("memory:frag:b", nominated_days_ago=10,
                                   content="候" * 60)])
    _seed_hits(storage, "memory:frag:a", 5)
    _seed_hits(storage, "memory:frag:b", 5)
    cfg = {"tiering": {"enabled": True, "batch_cap": 50,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 3000},
                       "promote_auto": {"review_window_days": 7,
                                        "promote_approve_hits": 3}},
           "retrieval": {"core_char_budget": 100}}

    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)

    assert stats["auto_review"]["approved_count"] == 1
    assert stats["auto_review"]["held_count"] == 1
    assert CANDIDATE_TAG in _tags(storage, "memory:frag:b"), "预算不足必须留队列"
    assert "memory:frag:b" not in _core_keys(storage)


def test_规则二_默认阈值边界_4999驳回_5000通过(storage):
    """默认 promote_approve_hits=5000 是用户在带流量时拍板的通过阈值，
    本条钉死边界：4999 ⇒ 驳回，5000 ⇒ 通过。
    """
    _seed(storage, [_candidate_row("memory:frag:edge_below", nominated_days_ago=10),
                    _candidate_row("memory:frag:edge_on", nominated_days_ago=10)])
    _seed_hits(storage, "memory:frag:edge_below", 4999)
    _seed_hits(storage, "memory:frag:edge_on", 5000)
    cfg = {"tiering": {"enabled": True,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 5},
                       "promote_auto": {"review_window_days": 7,
                                        "promote_approve_hits": 5000}},
           "retrieval": {"core_char_budget": 100000}}

    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)

    review = stats["auto_review"]
    assert review["approved"] == ["memory:frag:edge_on"], review
    assert review["rejected"] == ["memory:frag:edge_below"], review
    assert "memory:frag:edge_on" in _core_keys(storage)
    assert CANDIDATE_TAG not in _tags(storage, "memory:frag:edge_on")


def test_规则二_旧auto_promote段保留停用_防旧解析崩(storage):
    """旧解析若按 stats["auto_promote"]["count"] 等字段读，不许崩：本条钉死
    auto_promote 段恒 count=0 + note="replaced by auto_review"。
    """
    _seed(storage, [_candidate_row("memory:frag:hot", nominated_days_ago=10)])
    _seed_hits(storage, "memory:frag:hot", 5)
    cfg = {"tiering": {"enabled": True,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 5},
                       "promote_auto": {"review_window_days": 7,
                                        "promote_approve_hits": 3}},
           "retrieval": {"core_char_budget": 1000}}

    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)

    assert stats["auto_promote"]["count"] == 0
    assert stats["auto_promote"]["keys"] == []
    assert "replaced by auto_review" in stats["auto_promote"]["note"]


# ---------------------------------------------------------------------------
# 规则三：降级（压力轨 / 强降 / 豁免）
# ---------------------------------------------------------------------------

def _core_rows(n: int, *, content_len: int, tags: str = "shared",
               oldest_first: bool = True) -> List[Dict[str, Any]]:
    """n 条常驻条目，正文等长；`created` 依次往前推（最旧的一条排在最前）。"""
    rows = []
    for i in range(n):
        days = (n - i) if oldest_first else i
        rows.append({
            "key": f"memory:frag:core{i:02d}",
            "content": "字" * content_len,
            "tags": f"{CORE_TAG},{tags}",
            "created": (NOW - timedelta(days=days)).isoformat(),
        })
    return rows


def test_规则三_九成水位提名最尾部降到八成为止(storage):
    # 10 条 × 100 字 = 1000 字，预算 1000 ⇒ 100% ；但先看 90% 档：
    cfg = {"tiering": {"enabled": True, "batch_cap": 50},
           "retrieval": {"core_char_budget": 1100}}     # 1000/1100 ≈ 90.9% ⇒ 触发告警
    _seed(storage, _core_rows(10, content_len=100))

    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)

    demote = stats["demote"]
    assert demote["warned"] is True
    # 降到 release_ratio=0.80 ⇒ 需要释放 1000-880=120 字 ⇒ 100 字一条 ⇒ 2 条
    assert demote["nominated"]["count"] == 2, demote["nominated"]
    # 「最不活跃先走」= 最旧的两条（core00 / core01 按 created 从旧到新）
    assert demote["nominated"]["keys"] == ["memory:frag:core00", "memory:frag:core01"]
    for k in demote["nominated"]["keys"]:
        assert DEMOTE_CANDIDATE_TAG in _tags(storage, k)
        assert CORE_TAG in _tags(storage, k), "提名只是建议，未获批准前不许真降"


def test_规则三_未到告警水位零提名(storage):
    cfg = {"tiering": {"enabled": True}, "retrieval": {"core_char_budget": 100000}}
    _seed(storage, _core_rows(10, content_len=100))
    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)
    assert stats["demote"]["warned"] is False
    assert stats["demote"]["nominated"]["count"] == 0


def test_规则三_百分之百强降恰好一条(storage):
    cfg = {"tiering": {"enabled": True, "batch_cap": 50},
           "retrieval": {"core_char_budget": 1000}}     # 10×100 = 1000 ⇒ 100%
    _seed(storage, _core_rows(10, content_len=100))

    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)

    demote = stats["demote"]
    assert demote["forced"]["count"] == 1, "强降只降 1 条（保守：其余走人工）"
    victim = demote["forced"]["keys"][0]
    assert CORE_TAG not in _tags(storage, victim)
    assert DEMOTE_CANDIDATE_TAG not in _tags(storage, victim)
    assert len(_core_keys(storage)) == 9
    # 其余仍在队列里等人审（不是一次性全降）
    assert sum(1 for r in storage.list_by_tag(CORE_TAG)
               if DEMOTE_CANDIDATE_TAG in _tags(storage, str(r["key"]))) >= 1


def test_规则三_force_at_full关掉就不强降(storage):
    cfg = {"tiering": {"enabled": True,
                       "demote_pressure": {"warn_ratio": 0.90, "release_ratio": 0.80,
                                           "force_at_full": False}},
           "retrieval": {"core_char_budget": 1000}}
    _seed(storage, _core_rows(10, content_len=100))
    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)
    assert stats["demote"]["forced"]["count"] == 0
    assert len(_core_keys(storage)) == 10


def test_规则三_排序先看命中数再看新旧(storage):
    """有真实热度差时，最热的**最后**走（命中多的先留）。"""
    rows = _core_rows(4, content_len=100)
    for i, r in enumerate(rows):
        r["created"] = (NOW - timedelta(days=10 - i)).isoformat()   # core00 最旧
    cfg = {"tiering": {"enabled": True, "batch_cap": 50},
           "retrieval": {"core_char_budget": 410}}      # 400/410 ≈ 97.5% ⇒ 告警 + 未到 100%
    _seed(storage, rows)
    _seed_hits(storage, "memory:frag:core00", 50)       # 最旧但最热 ⇒ 留到最后

    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)
    assert DEMOTE_CANDIDATE_TAG not in _tags(storage, "memory:frag:core00")


def test_规则三_explicit_core任何水位都不自动降(storage):
    """豁免条目：水位 100% 也不被强降（只进建议 / 连建议都不进队列时被跳过）。"""
    rows = _core_rows(4, content_len=100)
    for r in rows:
        r["tags"] = f"{CORE_TAG},memory"          # Hermes 显式写入的恒带 memory 标
    cfg = {"tiering": {"enabled": True, "batch_cap": 50},
           "retrieval": {"core_char_budget": 400}}
    _seed(storage, rows)

    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)

    assert stats["demote"]["forced"]["count"] == 0, "豁免条目绝不自动降"
    assert len(_core_keys(storage)) == 4, "一条都没降"
    assert stats["demote"]["exempt_held"] >= 1, "豁免但可以被提名进建议队列"


def test_规则三_自动升进来的不豁免(storage):
    """「人写的才豁免」的反面：机器升进来的（`tier:auto-promoted`，含旧 auto_promote
    与新 auto_review_approved）照样能被降。

    两条都已在退居队列里（人工已审过方向），只差「谁享有豁免」这一个变量：
    手写那条（带 `memory` 标）留着，机器升那条被强降。
    """
    rows = _core_rows(4, content_len=100)
    rows[0]["tags"] = f"{CORE_TAG},memory,{DEMOTE_CANDIDATE_TAG}"     # 人写的 ⇒ 豁免
    rows[1]["tags"] = f"{CORE_TAG},{AUTO_PROMOTED_TAG},{DEMOTE_CANDIDATE_TAG}"
    rows[2]["tags"] = f"{CORE_TAG},memory"
    rows[3]["tags"] = f"{CORE_TAG},memory"
    cfg = {"tiering": {"enabled": True, "batch_cap": 50,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 5},
                       "promote_auto": {"review_window_days": 7,
                                        "promote_approve_hits": 3}},
           "retrieval": {"core_char_budget": 400}}          # 400/400 ⇒ 100%
    _seed(storage, rows)

    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)

    assert stats["demote"]["forced"]["count"] == 1
    assert stats["demote"]["forced"]["keys"] == ["memory:frag:core01"]
    assert CORE_TAG not in _tags(storage, "memory:frag:core01")
    assert CORE_TAG in _tags(storage, "memory:frag:core00"), "人写的那条豁免，不许强降"


def test_规则三_豁免关闭后条目可被强降(storage):
    rows = _core_rows(4, content_len=100)
    for r in rows:
        r["tags"] = f"{CORE_TAG},memory"
    cfg = {"tiering": {"enabled": True, "batch_cap": 50,
                       "demote_exempt": {"explicit_core": False}},
           "retrieval": {"core_char_budget": 400}}
    _seed(storage, rows)
    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)
    assert stats["demote"]["forced"]["count"] == 1


# ---------------------------------------------------------------------------
# 豁免判据 itself（钉死的可判据，正反三例）
# ---------------------------------------------------------------------------

def test_explicit_core_判据_正面():
    assert is_explicit_core("core,memory") is True
    assert is_explicit_core("core,memory,shared") is True
    assert is_explicit_core(" memory , core ") is True


def test_explicit_core_判据_反面三例():
    # 1) auto-distill 提炼产物：带 memory 标也不豁免
    assert is_explicit_core("core,memory,auto-distill") is False
    # 2) 子代理第三方域：agent: 前缀
    assert is_explicit_core("core,memory,agent:worker") is False
    # 3) MCP add / 控制台写入：压根没有 memory 标
    assert is_explicit_core("core,manual") is False
    assert is_explicit_core("core,ops") is False


def test_explicit_core_判据_标签边界不误伤():
    """`memory_notes` 不等于 `memory`（与 `_has_tag` 的逗号边界同一口径）。"""
    assert is_explicit_core("core,memory_notes") is False
    assert is_explicit_core("core,xmemory") is False
    # 开关关掉 ⇒ 一律不豁免
    assert is_explicit_core("core,memory", exempt=False) is False


# ---------------------------------------------------------------------------
# 默认关死 / dry-run / stats 形状
# ---------------------------------------------------------------------------

def test_enabled_false_零写(storage):
    """双向断言：标签一字未动 + 连状态文件都没生成。"""
    _seed(storage, [{"key": "memory:frag:hot", "content": "热门条目" * 10, "tags": "demo"},
                    {"key": "memory:frag:core", "content": "常驻" * 100,
                     "tags": f"{CORE_TAG},memory"}])
    _seed_hits(storage, "memory:frag:hot", 50)
    before = {k: _tags(storage, k) for k in ("memory:frag:hot", "memory:frag:core")}

    stats = run_tiering(storage, {"tiering": {}}, dry_run=False, now=NOW)

    assert stats["skipped"] == "disabled"
    assert {k: _tags(storage, k) for k in before} == before
    from keepsake.core import tiering as tmod
    assert not tmod.state_path().exists(), "关死时连状态文件都不许生成"


def test_config缺tiering节_按缺省关死():
    assert resolve_tiering({})["enabled"] is False
    assert resolve_tiering(None)["enabled"] is False
    assert run_tiering(None, {}, dry_run=False)["skipped"] == "disabled"


def test_非法值回落缺省且不崩():
    t = resolve_tiering({"tiering": {"enabled": "yes", "batch_cap": "不是数字",
                                     "promote_nominate": "整个节都不是对象"}})
    assert t["enabled"] is True
    assert t["batch_cap"] == 50
    assert t["promote_nominate"] == {"window_days": 7, "nominate_min_hits": 3000}
    assert t["promote_auto"] == {"review_window_days": 7, "promote_approve_hits": 5000}
    # enabled 非法值回落 false（回落成 true 才是事故）
    assert resolve_tiering({"tiering": {"enabled": 123}})["enabled"] is False


def test_dry_run只算不写(storage):
    _seed(storage, [{"key": "memory:frag:hot", "content": "热门条目" * 10, "tags": "demo"}])
    _seed_hits(storage, "memory:frag:hot", 9)
    cfg = {"tiering": {"enabled": True,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 5}},
           "retrieval": {"core_char_budget": 1000}}
    stats = run_tiering(storage, cfg, dry_run=True, now=NOW)
    assert stats["nominate"]["count"] == 1
    assert _tags(storage, "memory:frag:hot") == ["demo"], "dry 只算不写"


def test_stats_形状与明细截断(storage):
    _seed(storage, [{"key": "memory:frag:hot", "content": "热门条目" * 10, "tags": "demo"}])
    _seed_hits(storage, "memory:frag:hot", 5)
    cfg = {"tiering": {"enabled": True,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 5}},
           "retrieval": {"core_char_budget": 1000}}
    stats = run_tiering(storage, cfg, dry_run=True, now=NOW)
    for key in ("budget", "nominate", "auto_review", "auto_promote", "demote",
                "failed", "last_run"):
        assert key in stats, key
    # 旧 auto_promote 段保留停用
    assert stats["auto_promote"]["count"] == 0
    assert "replaced by auto_review" in stats["auto_promote"]["note"]
    assert set(stats["budget"]) == {"used", "total", "ratio", "used_after"}
    assert len(stats["nominate"]["keys"]) <= 20


def test_命中统计分块问存储层(storage):
    """>400 个 key 时分块问：一次摊成一条 IN(?,?,…) 会撞 SQLite 的 999 参数上限。"""
    from keepsake.core import tiering as tmod

    class ChunkyStorage:
        """只记 hits_stats 收到几批、每批多大（不落库，专测分块这件事）。"""

        def __init__(self) -> None:
            self.sizes: List[int] = []

        def hits_stats(self, window_days=7, min_hits=5, keys=None, limit=500):
            self.sizes.append(len(keys or []))
            return [{"key": k, "hits": 1, "last_hit": ""} for k in (keys or [])]

    st = ChunkyStorage()
    keys = [f"memory:frag:k{i:04d}" for i in range(tmod.HITS_KEY_CHUNK * 2 + 7)]
    hits, last_hit = tmod._hits_by_key(st, keys, 90)      # noqa: SLF001 —— 测分块这个私有原语
    assert st.sizes == [tmod.HITS_KEY_CHUNK] * 2 + [7]
    assert len(hits) == len(keys) and set(hits.values()) == {1}
    assert len(last_hit) == len(keys)


def test_单条失败不中断整轮(storage, monkeypatch):
    _seed(storage, [{"key": "memory:frag:h1", "content": "热门" * 10, "tags": "demo"},
                    {"key": "memory:frag:h2", "content": "热门" * 10, "tags": "demo"}])
    _seed_hits(storage, "memory:frag:h1", 5)
    _seed_hits(storage, "memory:frag:h2", 5)
    cfg = {"tiering": {"enabled": True,
                       "promote_nominate": {"window_days": 7, "nominate_min_hits": 5}},
           "retrieval": {"core_char_budget": 1000}}

    from keepsake.core import tiering as tmod

    real = tmod._write_tags

    def boom(storage_, key, tags, action):
        if key.endswith("h1"):
            raise RuntimeError("模拟后端拒写")
        return real(storage_, key, tags, action)

    monkeypatch.setattr(tmod, "_write_tags", boom)
    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)

    assert stats["nominate"]["count"] == 1
    assert stats["failed"] and stats["failed"][0]["key"] == "memory:frag:h1"


# ---------------------------------------------------------------------------
# 配置校验（PUT 端点的判据本体）
# ---------------------------------------------------------------------------

def test_校验_缺省补齐成完整一份():
    clean = validate_tiering({"enabled": True})
    assert clean["promote_nominate"] == {"window_days": 7, "nominate_min_hits": 3000}
    assert clean["promote_auto"] == {"review_window_days": 7, "promote_approve_hits": 5000}
    assert clean["batch_cap"] == 50


def test_校验_非法值与未知键拒收():
    for bad in ({"batch_cap": 0}, {"batch_cap": "abc"}, {"enabled": "也许"},
                {"promote_nominate": {"nominate_min_hits": 0}},
                {"promote_auto": {"promote_approve_hits": 0}},
                {"promote_auto": {"review_window_days": 0}},
                {"demote_pressure": {"warn_ratio": 2.0}},
                {"demote_pressure": {"release_ratio": 0.95, "warn_ratio": 0.90}},
                {"不认识的键": 1}, {"promote_nominate": 5}):
        with pytest.raises(ValueError):
            validate_tiering(bad)


# ---------------------------------------------------------------------------
# 端点：GET /api/v1/tiering/status、PUT /api/v1/settings/tiering
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(storage, tmp_path, monkeypatch):
    """API 端点用真 SQLite 存储 + 指向 tmp_path 的 config.json。"""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"retrieval": {"core_char_budget": 1000},
                                    "tiering": {"enabled": False}},
                                   ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_path))
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as c:
        yield c, cfg_path
    app.dependency_overrides.clear()


def test_status_免token_401(client):
    c, _ = client
    assert c.get("/api/v1/tiering/status").status_code == 401


def test_status_形状与默认关文案(client):
    c, _ = client
    _seed(c.app.dependency_overrides[get_storage](), [
        {"key": "memory:frag:core0", "content": "常驻" * 50, "tags": CORE_TAG}])
    body = c.get("/api/v1/tiering/status", headers=AUTH).json()

    assert set(body) >= {"enabled", "last_run", "last_stats", "budget", "settings"}
    assert body["enabled"] is False
    assert "默认关闭" in body["note"], body["note"]
    assert body["last_run"] is None and body["last_stats"] == {}
    assert body["budget"] == {"used": 100, "total": 1000, "ratio": 10.0}


def test_status_开起来后last_run有值(client, storage, tmp_path):
    c, _ = client
    state = tmp_path / "tiering_state.json"
    run_tiering(storage, _cfg(), dry_run=True, now=NOW)
    body = c.get("/api/v1/tiering/status", headers=AUTH).json()
    assert body["last_run"] == NOW.isoformat()
    assert body["last_stats"]["nominate"]["count"] == 0
    # status 也透出新键给设置页
    s = body["settings"]
    assert s["promote_nominate"]["nominate_min_hits"] == 3000
    assert s["promote_auto"]["review_window_days"] == 7
    assert s["promote_auto"]["promote_approve_hits"] == 5000
    assert state.exists()


def test_settings_非法值_400(client):
    c, _ = client
    r = c.put("/api/v1/settings/tiering",
              json={"tiering": {"batch_cap": 0}, "confirm": "false"}, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_tiering"


def test_settings_confirm不符_400(client):
    c, _ = client
    r = c.put("/api/v1/settings/tiering",
              json={"tiering": {"enabled": True}, "confirm": "true"}, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "confirm_mismatch"


def test_settings_缺confirm_400(client):
    c, _ = client
    r = c.put("/api/v1/settings/tiering", json={"tiering": {"enabled": True}},
              headers=AUTH)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "confirm_required"


def test_settings_免token_401(client):
    c, _ = client
    assert c.put("/api/v1/settings/tiering",
                 json={"tiering": {"enabled": True}, "confirm": "false"}).status_code == 401


def test_settings_写config只动tiering一键且先备份(client, tmp_path):
    c, cfg_path = client
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw["storage"] = {"backend": "redis", "redis": {"host": "127.0.0.1"}}
    order_before = list(raw)
    cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    r = c.put("/api/v1/settings/tiering",
              json={"tiering": {"enabled": True, "batch_cap": 7}, "confirm": "false"},
              headers=AUTH)
    assert r.status_code == 200

    after = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert after["tiering"]["enabled"] is True and after["tiering"]["batch_cap"] == 7
    assert after["tiering"]["promote_nominate"] == {"window_days": 7, "nominate_min_hits": 3000}
    assert after["tiering"]["promote_auto"] == {"review_window_days": 7, "promote_approve_hits": 5000}
    assert after["storage"] == raw["storage"], "别的节一个都不许动"
    assert list(after) == order_before, "顶层键序必须保序"
    assert (tmp_path / "backup").exists()
    backups = list((tmp_path / "backup").rglob("config.tiering.*.json"))
    assert backups, "写前必须整份备份 config.json"
    assert json.loads(backups[0].read_text(encoding="utf-8"))["tiering"]["enabled"] is False


def test_settings_开关真生效后status变true(client):
    c, _ = client
    c.put("/api/v1/settings/tiering",
          json={"tiering": {"enabled": True}, "confirm": "false"}, headers=AUTH)
    assert c.get("/api/v1/tiering/status", headers=AUTH).json()["enabled"] is True
    # 再关掉要按当前生效值 confirm（"true"）
    assert c.put("/api/v1/settings/tiering",
                 json={"tiering": {"enabled": False}, "confirm": "false"},
                 headers=AUTH).status_code == 400
    r = c.put("/api/v1/settings/tiering",
              json={"tiering": {"enabled": False}, "confirm": "true"}, headers=AUTH)
    assert r.status_code == 200
    assert c.get("/api/v1/tiering/status", headers=AUTH).json()["enabled"] is False


# ---------------------------------------------------------------------------
# cron 脚本：enabled=false 时干净退出（cron 不报红）
# ---------------------------------------------------------------------------

def _load_cron_script(name: str = "tiering_cron"):
    """按路径加载 `scripts/tiering_cron.py`（sys.path 补齐逻辑在脚本自身）。"""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "tiering_cron.py"
    spec = importlib.util.spec_from_file_location(name, str(script))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cron脚本_关着时打skipped并退出0(tmp_path, monkeypatch, capsys):
    mod = _load_cron_script()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"tiering": {"enabled": False}}), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg))

    assert mod.main(["--dry"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == "disabled"
    assert mod.main(["--run"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == "disabled"


def test_cron脚本_dry与run互斥且必选():
    mod = _load_cron_script("tiering_cron_args")
    for argv in ([], ["--dry", "--run"]):
        with pytest.raises(SystemExit):
            mod.main(argv)
