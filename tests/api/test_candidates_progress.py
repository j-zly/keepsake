"""晋升候选「复核进度」读端点（2026-11-14 用户："晋升候选显示一下进度，已经被命中多少次了"）。

钉住的纪律（每条都是一个真会出错的方向）：

1. 🔴 **数法与班车必须是同一份实现**：`candidates_progress()` 的命中数直接对
   `_hits_since_nomination()` 的结果 —— 页面和自动审核说的得是同一件事。
2. 🔴 **窗口锚在提名日**（不是滚动 7 天）：提名时补的基数要仍然算数，否则
   班车驳回、页面说"到线"，两边互相打脸。
3. **三种预告要分开**：未满窗 `waiting` / 到线 `approve` / 不够 `reject` ——
   "会驳回"这件事必须提前看得见，人才能先手动批。
4. 🔴 **人工（无 `tier:auto-hot:` 戳）提名不给进度**：它们不参与自动复核，
   画一条进度条就是骗人；只给滚动窗命中数。
5. **阈值与天数只来自配置**（端点走 `_tiering_settings()`，不许自己 resolve）：
   改了 `promote_approve_hits`，`pct` 要跟着动，前端一个数字都不写。
6. **已不是候选 / 已删除的 key** 从 items 里消失，不报错也不留残影。
7. **纯读**：调一次前后，碎片标签、正文、打点条数一个字都不许变。
8. 未登录 401；超上限报 400 而不是静默截断（截断会让"没进度"看起来像数据坏了）。

存储用临时 SQLite（真后端），不连 PG / 生产库。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.api import config as api_config
from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.api.routers import ops as ops_mod
from keepsake.core.sqlite import SqliteStorage
from keepsake.core.tiering import (
    AUTO_HOT_PREFIX,
    CANDIDATE_TAG,
    CORE_TAG,
    _hits_since_nomination,
    candidates_progress,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
TOKEN = "progress-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _cfg(**over: Any) -> Dict[str, Any]:
    """生效配置（与生产同量级：提名 3000 / 批准 5000 / 观察 7 天）。"""
    t: Dict[str, Any] = {
        "enabled": True,
        "promote_nominate": {"window_days": 7, "nominate_min_hits": 3000},
        "promote_auto": {"review_window_days": 7, "promote_approve_hits": 5000},
        "demote_pressure": {"warn_ratio": 0.9, "release_ratio": 0.8, "force_at_full": True},
        "demote_exempt": {"explicit_core": True},
        "batch_cap": 50,
        "nominate_backfill_hits": 3000,
    }
    t.update(over)
    return t


def _row(key: str, *, days_ago: float | None, tags: str = "demo") -> Dict[str, Any]:
    """一条候选；`days_ago=None` ⇒ 人工提名（没有 auto-hot 戳）。"""
    parts = [tags, CANDIDATE_TAG]
    if days_ago is not None:
        parts.append(f"{AUTO_HOT_PREFIX}:{(NOW - timedelta(days=days_ago)).date().isoformat()}")
    return {"key": key, "content": f"{key} 的正文" * 6, "tags": ",".join(parts),
            "created": (NOW - timedelta(days=(days_ago or 3) + 1)).isoformat()}


def _seed_hits(storage: Any, key: str, n: int, *, days_ago: float = 0.0) -> None:
    ts = (NOW - timedelta(days=days_ago)).isoformat()
    with storage._write() as cur:      # noqa: SLF001 —— 打点表造数据只能直写
        cur.executemany("INSERT INTO ks_hit_event (key, ts) VALUES (?, ?)", [(key, ts)] * n)


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "config.json"))
    st = SqliteStorage(str(tmp_path / "k.db"))
    yield st
    st.close()


@pytest.fixture()
def client(storage):
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    return TestClient(app)


# --------------------------------------------------------------------------
# 判据本身
# --------------------------------------------------------------------------

def test_未满窗_giving_waiting_且带复核日期(storage):
    storage.write_fragments_batch([_row("memory:frag:w1", days_ago=2)])
    _seed_hits(storage, "memory:frag:w1", 3010, days_ago=2)
    out = candidates_progress(storage, ["memory:frag:w1"], t=_cfg(), now=NOW)
    it = out["items"][0]
    assert it["verdict"] == "waiting" and it["due"] is False
    assert it["hits"] == 3010
    # review_at = 提名日 + 观察窗（不是"今天 + 7 天"）
    assert it["nominated_at"] == (NOW - timedelta(days=2)).date().isoformat()
    assert it["review_at"] == ((NOW - timedelta(days=2)) + timedelta(days=7)).date().isoformat()
    assert out["approve_hits"] == 5000 and out["review_window_days"] == 7


def test_到窗不够_reject_且算出差多少(storage):
    storage.write_fragments_batch([_row("memory:frag:r1", days_ago=8)])
    _seed_hits(storage, "memory:frag:r1", 3050, days_ago=8)
    it = candidates_progress(storage, ["memory:frag:r1"], t=_cfg(), now=NOW)["items"][0]
    assert it["verdict"] == "reject" and it["due"] is True
    assert it["gap"] == 5000 - 3050
    assert it["pct"] == round(3050 / 5000, 4)


def test_到窗达标_approve(storage):
    storage.write_fragments_batch([_row("memory:frag:a1", days_ago=8)])
    _seed_hits(storage, "memory:frag:a1", 5000, days_ago=8)
    it = candidates_progress(storage, ["memory:frag:a1"], t=_cfg(), now=NOW)["items"][0]
    assert it["verdict"] == "approve" and it["gap"] == 0 and it["pct"] == 1.0


def test_边界_正好阈值算达标_差一条算驳回(storage):
    storage.write_fragments_batch([_row("memory:frag:b1", days_ago=9),
                                   _row("memory:frag:b2", days_ago=9)])
    _seed_hits(storage, "memory:frag:b1", 5000, days_ago=9)
    _seed_hits(storage, "memory:frag:b2", 4999, days_ago=9)
    out = {x["key"]: x for x in
           candidates_progress(storage, ["memory:frag:b1", "memory:frag:b2"],
                               t=_cfg(), now=NOW)["items"]}
    assert out["memory:frag:b1"]["verdict"] == "approve"
    assert out["memory:frag:b2"]["verdict"] == "reject"


def test_窗口锚在提名日_与班车同一数法(storage):
    """提名日之前的命中**不进**本候选的账（班车就是这么数的）。

    这条是防"UI 自己数滚动窗"的回归闸：把命中打在提名日之前，进度必须是 0。
    """
    storage.write_fragments_batch([_row("memory:frag:m1", days_ago=2)])
    _seed_hits(storage, "memory:frag:m1", 4000, days_ago=10)     # 提名前
    _seed_hits(storage, "memory:frag:m1", 30, days_ago=1)        # 提名后
    # `get_fragments_batch` 回的是 `{key: row}`，**row 里没有 key 字段** —— 直接
    # 把 values() 喂给班车的数法会被它按"无 key"跳过（这正是 candidates_progress
    # 里那句 `row.setdefault("key", key)` 存在的原因，别删）。
    rows = [{"key": k, **v} for k, v in storage.get_fragments_batch(["memory:frag:m1"]).items()]
    it = candidates_progress(storage, ["memory:frag:m1"], t=_cfg(), now=NOW)["items"][0]
    assert it["hits"] == 30 == _hits_since_nomination(storage, rows).get("memory:frag:m1")


def test_人工提名不给进度只给滚动窗命中(storage):
    storage.write_fragments_batch([_row("memory:frag:h1", days_ago=None)])
    _seed_hits(storage, "memory:frag:h1", 12, days_ago=3)
    it = candidates_progress(storage, ["memory:frag:h1"], t=_cfg(), now=NOW)["items"][0]
    assert it["source"] == "manual" and it["verdict"] == "manual"
    assert it["pct"] is None and it["gap"] is None
    assert it["review_at"] == "" and it["due"] is False
    assert it["hits"] == 12 and it["window_days"] == 7


def test_已不是候选与已删除的key_静默消失(storage):
    storage.write_fragments_batch([_row("memory:frag:g1", days_ago=3),
                                   {"key": "memory:frag:g2", "content": "已批",
                                    "tags": f"demo,{CORE_TAG}",
                                    "created": NOW.isoformat()}])
    out = candidates_progress(storage, ["memory:frag:g1", "memory:frag:g2",
                                        "memory:frag:不存在"], t=_cfg(), now=NOW)
    assert [x["key"] for x in out["items"]] == ["memory:frag:g1"]


def test_阈值改配置_progress_跟着改(storage):
    storage.write_fragments_batch([_row("memory:frag:c1", days_ago=8)])
    _seed_hits(storage, "memory:frag:c1", 3000, days_ago=8)
    lo = candidates_progress(storage, ["memory:frag:c1"],
                             t=_cfg(promote_auto={"review_window_days": 7,
                                                  "promote_approve_hits": 3000}),
                             now=NOW)["items"][0]
    assert lo["verdict"] == "approve" and lo["pct"] == 1.0
    hi = candidates_progress(storage, ["memory:frag:c1"], t=_cfg(), now=NOW)["items"][0]
    assert hi["verdict"] == "reject" and hi["pct"] == 0.6


def test_空入参与全空白_key_不炸(storage):
    out = candidates_progress(storage, ["", "   "], t=_cfg(), now=NOW)
    assert out["items"] == [] and out["approve_hits"] == 5000


def test_纯读_一个字段都不写(storage):
    storage.write_fragments_batch([_row("memory:frag:p1", days_ago=8)])
    _seed_hits(storage, "memory:frag:p1", 100, days_ago=8)
    before = storage.get_fragment("memory:frag:p1")
    with storage._write() as cur:      # noqa: SLF001
        n = cur.execute("SELECT COUNT(*) FROM ks_hit_event").fetchone()[0]
    candidates_progress(storage, ["memory:frag:p1"], t=_cfg(), now=NOW)
    assert storage.get_fragment("memory:frag:p1")["tags"] == before["tags"]
    assert storage.get_fragment("memory:frag:p1")["content"] == before["content"]
    with storage._write() as cur:      # noqa: SLF001
        assert cur.execute("SELECT COUNT(*) FROM ks_hit_event").fetchone()[0] == n


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------

def _get(client, keys: List[str]):
    return client.get("/api/v1/tiering/candidates-progress",
                      params={"keys": ",".join(keys)}, headers=AUTH)


def test_端点返回阈值与逐条进度(client, monkeypatch):
    st = client.app.dependency_overrides[get_storage]()
    st.write_fragments_batch([_row("memory:frag:e1", days_ago=8)])
    _seed_hits(st, "memory:frag:e1", 3100, days_ago=8)
    monkeypatch.setattr(ops_mod, "_tiering_settings", lambda: _cfg())
    r = _get(client, ["memory:frag:e1"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["approve_hits"] == 5000 and d["enabled"] is True
    it = d["items"][0]
    assert it["hits"] == 3100 and it["verdict"] == "reject" and it["gap"] == 1900
    assert "content" not in it, "遥测端点不搬正文（列表已有，别把这条变成第二个体面数据源）"


def test_端点阈值来自配置读取口_不许自己resolve(monkeypatch):
    """🔴 单一取数口：端点必须走 `_tiering_settings()`（与班车状态端点同一个）。

    自己 `resolve_tiering()` 会绕过那条路径的容错回落，页面读到的阈值就可能与
    班车实际生效值不一致。
    """
    src = open(ops_mod.__file__, encoding="utf-8").read()
    start = src.index('"/api/v1/tiering/candidates-progress"')
    body = src[start:src.index('@router.get("/api/v1/tiering/status"', start)]
    assert "_tiering_settings()" in body
    assert "resolve_tiering(" not in body, "端点自己 resolve = 第二份配置读法"


def test_无_keys_参数返回空表(client, monkeypatch):
    monkeypatch.setattr(ops_mod, "_tiering_settings", lambda: _cfg())
    r = client.get("/api/v1/tiering/candidates-progress", headers=AUTH)
    assert r.status_code == 200 and r.json()["items"] == []


def test_超上限报400_不静默截断(client, monkeypatch):
    monkeypatch.setattr(ops_mod, "_tiering_settings", lambda: _cfg())
    keys = [f"memory:frag:x{i}" for i in range(ops_mod.PROGRESS_MAX_KEYS + 1)]
    r = _get(client, keys)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "too_many_keys"


def test_未登录_401(client):
    r = client.get("/api/v1/tiering/candidates-progress", params={"keys": "k"})
    assert r.status_code == 401


def test_api_config_路径读取口存在(client):
    """护栏：`_tiering_settings` 用的 `keepsake_config_file()` 必须还在（改名会让
    端点静默读到空配置 ⇒ 阈值回落到缺省，页面与班车又分家）。"""
    assert callable(getattr(api_config, "keepsake_config_file", None))
