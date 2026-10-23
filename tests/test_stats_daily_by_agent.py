"""`daily_by_agent`（看板「近 7 天记忆增量 · 按代理」）的契约测试。

覆盖：
  * **空库** ⇒ 按代理分列是**空列表**（不是一排 0 —— 补零/补日是图表口径，
    由前端按 `daily` 那段铺横轴，后端不重复发明一份）；
  * **双代理同日** ⇒ 两行各归各（不合并成一行「总」）；
  * **没有归属的老条目** ⇒ 归 `"-"` 桶（不假装它属于某个代理，也不摊到别人头上）；
  * 归属判据是**逗号边界的完整标签**（`agent:win` 不命中 `agent:win-desktop`）
    —— 与检索 / core 可见性同一把尺子；
  * 🔴 **老字段不许回归**：`daily` 段一字不改（仍补齐每一天、仍是总量）。

存储用**临时 SQLite**（真 `ks_fragment` 表、真 SQL），不连 Redis / PG / 生产库。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

STATS_URL = "/api/v1/stats"


def _day(offset: int = 0) -> str:
    """今天往前第 offset 天的日期（`YYYY-MM-DD`，UTC —— 与后端同一把尺子）。"""
    return (datetime.now(timezone.utc).date() - timedelta(days=offset)).isoformat()


def _stamp(offset: int = 0) -> str:
    """今天往前第 offset 天的 ISO 时间戳（写进 `created` 列）。"""
    return f"{_day(offset)}T12:00:00+00:00"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """控制台进程环境：临时 sqlite 存储 + 固定 token。"""
    from keepsake.core.sqlite import SqliteStorage

    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_TOKEN_FILE", str(tmp_path / "不存在.token"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_ADMIN_FILE", str(tmp_path / "不存在-admin.json"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_SESSION_FILE", str(tmp_path / "不存在-sess.json"))
    storage = SqliteStorage(path=str(tmp_path / "daily-by-agent.db"))
    assert storage.ensure_index() is True
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as client:
        yield client, storage
    app.dependency_overrides.clear()
    storage.close()


def _write(storage, rows) -> None:
    """写碎片（`created` 显式给 —— 判日就靠它前 10 位）。"""
    assert storage.write_fragments_batch([
        {"key": key, "content": content, "tags": tags, "created": created}
        for key, content, tags, created in rows
    ]) == len(rows)


def _by_agent(client, days: int = 7):
    """回包里的 `daily_by_agent` 段（按键归并成 {(日, 代理): 条数}）。"""
    resp = client.get(STATS_URL, params={"days": days}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    out = {}
    for row in body["daily_by_agent"]:
        out[(row["d"], row["agent"])] = row["n"]
    return body, out


# ---------------------------------------------------------------- 空与形状

def test_空库_按代理分列是空列表(env):
    """空库 ⇒ `daily_by_agent` 空列表（不是一排 0 —— 补零是前端图表口径）。"""
    client, _ = env
    body, out = _by_agent(client)
    assert body["daily_by_agent"] == []
    assert out == {}


def test_按代理分列只给有写入的格子(env):
    """两天有写入 ⇒ 只给这两天的格子（不补日、不补零）。"""
    client, storage = env
    _write(storage, [
        ("memory:frag:a1", "今天的", "agent:win", _stamp(0)),
        ("memory:frag:a2", "前天的", "agent:win", _stamp(1)),
    ])
    _, out = _by_agent(client)
    assert out == {(_day(0), "win"): 1, (_day(1), "win"): 1}


# ---------------------------------------------------------------- 分组

def test_双代理同日_各归各(env):
    """同一天两个代理 ⇒ 两行各归各（不合并成一行「总」）。"""
    client, storage = env
    _write(storage, [
        ("memory:frag:b1", "甲的一条", "agent:win", _stamp(0)),
        ("memory:frag:b2", "甲的另一条", "agent:win", _stamp(0)),
        ("memory:frag:b3", "乙的一条", "agent:mac", _stamp(0)),
    ])
    _, out = _by_agent(client)
    assert out == {(_day(0), "win"): 2, (_day(0), "mac"): 1}


def test_无归属的老条目归未标注桶(env):
    """老库里没打 `agent:` 标签的条目 ⇒ 归 `"-"` 桶（不假装属于某个代理）。"""
    client, storage = env
    _write(storage, [
        ("memory:frag:c1", "有归属", "agent:win", _stamp(0)),
        ("memory:frag:c2", "没归属", "note", _stamp(0)),
    ])
    _, out = _by_agent(client)
    assert out == {(_day(0), "win"): 1, (_day(0), "-"): 1}


def test_归属判据是逗号边界的完整标签(env):
    """🔴 `agent:win` 不该命中 `agent:win-desktop`（与可见性同一把尺子）。"""
    client, storage = env
    _write(storage, [
        ("memory:frag:d1", "win 的", "core,agent:win", _stamp(0)),
        ("memory:frag:d2", "win-desktop 的", "core,agent:win-desktop", _stamp(0)),
    ])
    _, out = _by_agent(client)
    assert out == {(_day(0), "win"): 1, (_day(0), "win-desktop"): 1}


def test_窗口外的旧行不计入(env):
    """8 天前写的老行不进「近 7 天」这一段（与 `daily` 同一把尺子）。"""
    client, storage = env
    _write(storage, [
        ("memory:frag:e1", "窗口内", "agent:win", _stamp(0)),
        ("memory:frag:e2", "窗口外", "agent:win", _stamp(8)),
    ])
    _, out = _by_agent(client, days=7)
    assert out == {(_day(0), "win"): 1}


# ---------------------------------------------------------------- 老字段不许回归

def test_老字段daily不回归(env):
    """🔴 `daily` 段一字不改：仍是**每天一格**（补齐没写入的那天）、仍是总量。"""
    client, storage = env
    _write(storage, [
        ("memory:frag:f1", "甲一条", "agent:win", _stamp(0)),
        ("memory:frag:f2", "甲两条", "agent:win", _stamp(0)),
        ("memory:frag:f3", "乙一条", "agent:mac", _stamp(2)),
    ])
    body, _ = _by_agent(client, days=7)
    daily = body["daily"]
    assert len(daily) == 7, "daily 仍要给满 7 天（补齐没写入的那天）"
    assert [d["date"] for d in daily] == [_day(6), _day(5), _day(4), _day(3),
                                        _day(2), _day(1), _day(0)]
    counts = {d["date"]: d["count"] for d in daily}
    assert counts[_day(0)] == 2
    assert counts[_day(2)] == 1
    assert counts[_day(1)] == 0, "没写入的那天是 0（不是空白）"
    # 总量 = 按代理分列的总量（两段必须对得上）
    assert sum(d["count"] for d in daily) == sum(r["n"] for r in body["daily_by_agent"])


def test_不查这一段时两段都是空的(env):
    """缺省 `days=0` ⇒ 两段都不查（老调用方的开销一字不变）。"""
    client, storage = env
    _write(storage, [("memory:frag:g1", "一条", "agent:win", _stamp(0))])
    resp = client.get(STATS_URL, headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["daily"] == []
    assert body["daily_by_agent"] == []


def test_无token_401(env):
    client, _ = env
    assert client.get(STATS_URL, params={"days": 7}).status_code == 401
