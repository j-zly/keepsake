"""规则三·**信号轨**：负反馈 ⇒ 直接提名退居（不看水位、不受 explicit_core 豁免）。

## 为什么单开一个文件（不是追加进 test_tiering.py）

任务书写的是「tiering 既有文件追加信号轨用例」，但 `tests/test_tiering.py` 在本机
是 **root 所有、0644**，执行账号（claude_user，uid 1000）写不进去 —— 硬写需要
sudo（本机 sudo 要密码，且不在允许的操作面内）。所以新起本文件，用同一套 fixture
口径（真 SQLite 临时库 + 审计/备份/状态文件全指 tmp_path），**既有断言零改动**。

## 这块钉死的三件事

  1. **不看水位**：水位 5% 的常驻条目只要吃了负反馈就进退居建议（压力轨那条
     「最不活跃先走」在常驻层是弱信号，年龄排序只是预算保命阀）；
  2. **不豁免 explicit_core**：手写核心（`memory` 标）也一样被提名 ——
     豁免挡的是「班车替人做主自动降级」这个**动作**，提名不是动作；
     但 100% 强降那条自动动作**仍然**豁免（同一个用例里两半一起验）；
  3. **解析与幂等**：文本列的脏形态（'' / 'abc' / '−2' / '3'）判据，以及
     二次跑不重复打标。

## 解析判据：`feedback_of` 的八种形态（见参数化那条）
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from keepsake.core.tiering import (
    CORE_TAG,
    DEMOTE_CANDIDATE_TAG,
    feedback_of,
    run_tiering,
)

NOW = "2026-10-16T12:00:00+00:00"
TOKEN = "tiering-signal-test-token-not-a-real-secret"


# ---------------------------------------------------------------------------
# fixture：与 test_tiering.py 同款（真 SQLite + 三处外部路径全指 tmp_path）
# ---------------------------------------------------------------------------

@pytest.fixture()
def storage(tmp_path):
    from keepsake.core.sqlite import SqliteStorage

    s = SqliteStorage(path=str(tmp_path / "signal.db"), embedder=None, embed_dim=8)
    assert s.ensure_index() is not False
    return s


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setenv("KEEPSAKE_TIERING_STATE", str(tmp_path / "tiering_state.json"))
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)


def _seed(storage: Any, rows: List[Dict[str, Any]]) -> None:
    assert storage.write_fragments_batch([dict(r) for r in rows]) == len(rows)


def _tags(storage: Any, key: str) -> List[str]:
    row = storage.get_fragment(key) or {}
    return [t.strip() for t in str(row.get("tags", "")).split(",") if t.strip()]


def _cfg(budget: int = 100000) -> Dict[str, Any]:
    return {"tiering": {"enabled": True, "batch_cap": 50},
            "retrieval": {"core_char_budget": budget}}


def _cores(n: int = 4, *, tags: str = "", score: Any = None) -> List[Dict[str, Any]]:
    rows = []
    for i in range(n):
        row: Dict[str, Any] = {
            "key": f"memory:frag:core{i:02d}",
            "content": "字" * 100,
            "tags": f"{CORE_TAG}{',' + tags if tags else ''}",
            "created": NOW,
        }
        if score is not None:
            row["feedback_score"] = score
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 一、解析判据（脏形态全在这条里）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expect", [
    ("-2", -2.0),          # record_feedback(False) 累加出来的常态
    ("-1", -1.0),          # correct_fragments 打的已纠正标
    ("−2", -2.0),      # U+2212 减号（人手输的）
    ("- 2", -2.0),         # 带空格
    ("3", 3.0),            # 正反馈：不提名
    ("", 0.0),             # 空串（DEFAULT '0' 的历史行）⇒ 中性
    (None, 0.0),
    ("abc", 0.0),          # 读不懂 ⇒ **0**，绝不猜成负反馈（宁可漏不误伤）
])
def test_feedback_of_五种形态(raw: Any, expect: float):
    assert feedback_of({"feedback_score": raw}) == expect


def test_feedback_of_缺键也当0():
    assert feedback_of({}) == 0.0


# ---------------------------------------------------------------------------
# 二、信号轨本体
# ---------------------------------------------------------------------------

def test_负反馈_水位五成就被提名(storage):
    """水位远低于告警线（压力轨不会动），信号轨照样提名。"""
    rows = _cores(4, score=-2)            # 4 条都吃了负反馈
    rows[0]["feedback_score"] = "-1"
    _seed(storage, rows)
    assert 400 / 100000 < 0.9

    stats = run_tiering(storage, _cfg(), dry_run=False, now=NOW)

    assert stats["demote"]["warned"] is False, "本用例前提：压力轨根本没告警"
    assert stats["demote"]["nominated"]["count"] == 0
    sig = stats["signal_nominate"]
    assert sig["count"] == 4, sig
    for i in range(4):
        tags = _tags(storage, f"memory:frag:core{i:02d}")
        assert DEMOTE_CANDIDATE_TAG in tags
        assert CORE_TAG in tags, "提名只是建议，未获批准前不许真降常驻"


def test_正反馈与中性不提名(storage):
    rows = _cores(2, score=5)
    rows.append({"key": "memory:frag:core99", "content": "字" * 100, "tags": CORE_TAG,
                 "created": NOW})          # 没有 feedback_score 键 ⇒ 中性
    _seed(storage, rows)

    stats = run_tiering(storage, _cfg(), dry_run=False, now=NOW)

    assert stats["signal_nominate"]["count"] == 0
    for i in (0, 1, 99):
        assert DEMOTE_CANDIDATE_TAG not in _tags(storage, f"memory:frag:core{i:02d}")


def test_手写核心_负反馈照样提名但强降仍豁免(storage):
    """🔴 一条用例钉住「豁免只挡自动降级、不挡提名」这半拍的区别。

    前半：水位 5% + 手写核心（`memory` 标）+ 负反馈 ⇒ **被提名**（信号轨）。
    后半：水位 100% + 同一条 ⇒ **不被强降**（自动动作仍享豁免），一条都不降。
    """
    _seed(storage, _cores(4, tags="memory", score=-2))

    low = run_tiering(storage, _cfg(), dry_run=False, now=NOW)
    assert low["signal_nominate"]["count"] == 4, "信号轨不认 explicit_core 豁免"

    full = run_tiering(storage, _cfg(budget=400), dry_run=False, now=NOW)  # 400/400 = 100%
    assert full["demote"]["forced"]["count"] == 0, "强降是自动动作，豁免继续生效"
    assert all(CORE_TAG in _tags(storage, f"memory:frag:core{i:02d}") for i in range(4))


def test_幂等_二次跑不重复打标(storage):
    _seed(storage, _cores(3, score=-2))

    first = run_tiering(storage, _cfg(), dry_run=False, now=NOW)
    second = run_tiering(storage, _cfg(), dry_run=False, now=NOW)

    assert first["signal_nominate"]["count"] == 3
    assert second["signal_nominate"]["count"] == 0
    assert second["signal_nominate"]["already"] == 3, "已在队列的跳过（幂等）"
    tags = _tags(storage, "memory:frag:core00")
    assert tags.count(DEMOTE_CANDIDATE_TAG) == 1, "标签不许被刷新成两条"


def test_dry_run只算不写(storage):
    _seed(storage, _cores(2, score=-2))

    stats = run_tiering(storage, _cfg(), dry_run=True, now=NOW)

    assert stats["signal_nominate"]["count"] == 2
    assert all(DEMOTE_CANDIDATE_TAG not in _tags(storage, f"memory:frag:core{i:02d}")
               for i in range(2))


def test_总开关关着时信号轨零写(storage):
    """`enabled=false` 仍是第一行就返回：信号轨也不许偷偷跑。"""
    _seed(storage, _cores(2, score=-2))
    cfg = {"tiering": {"enabled": False}, "retrieval": {"core_char_budget": 100000}}

    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)

    assert stats == {"skipped": "disabled", "enabled": False, "dry_run": False,
                     "reason": stats["reason"]}
    assert all(DEMOTE_CANDIDATE_TAG not in _tags(storage, f"memory:frag:core{i:02d}")
               for i in range(2))


def test_批次上限封顶(storage):
    _seed(storage, _cores(4, score=-2))
    cfg = {"tiering": {"enabled": True, "batch_cap": 2},
           "retrieval": {"core_char_budget": 100000}}

    stats = run_tiering(storage, cfg, dry_run=False, now=NOW)

    assert stats["signal_nominate"]["count"] == 2
    assert sum(1 for i in range(4)
               if DEMOTE_CANDIDATE_TAG in _tags(storage, f"memory:frag:core{i:02d}")) == 2
