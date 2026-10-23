"""退居提名列表的**归属代理字段**（`FragmentItem.agent`）。

🔴 归属判据只有一把尺子：`govern.visibility.fragment_owner`（条目 tags 里的
`agent:<id>`，**逗号边界的完整标签相等** —— `agent:win` 不命中
`agent:win-desktop`）。控制台那一列「归属代理」直接吃这个字段，前端不必
再自己拆一遍 tags（拆两遍必漂）。

覆盖：
* 提名列表（`?tag=tier:demote-candidate`）的每一行都带 `agent`；
* 有归属 ⇒ 给 id；无归属 ⇒ 空串（**不编一个代理名**）；
* `agent:win` 不误伤 `agent:win-desktop`（逗号边界口径）。

存储用假实现（`dependency_overrides` 注入）—— 不连 Redis / PG / 生产库。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

DEMOTE_TAG = "tier:demote-candidate"


class FakeStorage:
    """最小存储替身：只实现控制台列表用到的只读原语。"""

    def __init__(self) -> None:
        self.rows = [
            {"key": "memory:frag:d1", "content": "工人写的",
             "tags": f"core,{DEMOTE_TAG},agent:worker-1", "updated_at": "2026-10-23T00:00:00"},
            {"key": "memory:frag:d2", "content": "没归属的",
             "tags": f"core,{DEMOTE_TAG}", "updated_at": "2026-10-23T00:01:00"},
            {"key": "memory:frag:d3", "content": "同前缀但不是同一个",
             "tags": f"core,{DEMOTE_TAG},agent:worker-10", "updated_at": "2026-10-23T00:02:00"},
        ]

    def list_by_tag(self, tag):
        return [r for r in self.rows if tag in r["tags"].split(",")]

    def search(self, query, tag_filter="", agent_id="", is_primary=None):
        return []

    def get_fragment(self, key):
        return next((dict(r) for r in self.rows if r["key"] == key), None)

    def get_fragments_batch(self, keys):
        return {r["key"]: r for r in self.rows if r["key"] in set(keys)}

    def scan_fragment_keys(self, cursor="", limit=200, prefix="memory:frag:"):
        keys = [r["key"] for r in self.rows if r["key"].startswith(prefix)]
        return "", keys[:limit]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: FakeStorage()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_提名列表每行都带agent字段(client):
    """🔴 提名列表（维护页那张表的数据源）必须给出归属代理 —— 列不许是空的。"""
    r = client.get(f"/api/v1/fragments?tag={DEMOTE_TAG}", headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3
    for it in items:
        assert "agent" in it, "每行都要有 agent 字段（前端那列直接吃它）"


def test_归属取值与fragment_owner同口径(client):
    """有归属给 id；无归属给空串；`agent:worker-1` 不命中 `agent:worker-10`。"""
    r = client.get(f"/api/v1/fragments?tag={DEMOTE_TAG}", headers=AUTH)
    got = {it["key"]: it["agent"] for it in r.json()["items"]}
    assert got["memory:frag:d1"] == "worker-1"
    assert got["memory:frag:d2"] == "", "无归属给空串（不编一个代理名）"
    assert got["memory:frag:d3"] == "worker-10"


def test_归属与govern的尺子逐字一致(client):
    """🔴 后端这一列必须 = `govern.visibility.fragment_owner`（不许另写一份拆法）。"""
    from keepsake.govern.visibility import fragment_owner

    r = client.get(f"/api/v1/fragments?tag={DEMOTE_TAG}", headers=AUTH)
    for it in r.json()["items"]:
        assert it["agent"] == fragment_owner(it["tags"])
