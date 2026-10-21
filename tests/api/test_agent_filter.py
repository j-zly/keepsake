"""控制台「按代理筛选」的接口用例（常驻层 /api/v1/core 与记忆条目 /api/v1/fragments）。

钉四件事：

1. 传 `agents` ⇒ 只回**该代理名下**的条目（判据 = tags 里的 `agent:<id>`）；
2. 不传 `agents` ⇒ **全量**（现状不变，回归线）；
3. 过滤**发生在分页之前** —— count 是过滤后的总数，不是当前页条数
   （只筛当前页 = 「看着筛了、其实漏了」）；
4. 归属标签按**逗号边界的完整标签相等**匹配：`agent:worker` 不命中 `agent:worker-2`。

存储层用假实现（`dependency_overrides` 注入），不连真实后端。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: 假数据：两个代理各有一条、另有一条 `worker-2`（测逗号边界）与一条**无归属**老条目。
ROWS = [
    {"key": "k-main", "content": "主脑记得部署流程",
     "tags": "core,agent:main-brain", "updated_at": "2026-10-20T00:00:00+00:00"},
    {"key": "k-worker", "content": "工人记得端口占用",
     "tags": "core,agent:worker", "updated_at": "2026-10-20T00:01:00+00:00"},
    {"key": "k-worker2", "content": "工人记得回滚步骤",
     "tags": "core,agent:worker", "updated_at": "2026-10-20T00:02:00+00:00"},
    {"key": "k-worker-2", "content": "另一个代理的记忆",
     "tags": "core,agent:worker-2", "updated_at": "2026-10-20T00:03:00+00:00"},
    {"key": "k-none", "content": "没有归属的老条目",
     "tags": "core", "updated_at": "2026-10-20T00:04:00+00:00"},
]


class FakeStorage:
    """最小存储替身：标签匹配按逗号边界的完整标签相等（与存储层同判据）。"""

    def __init__(self) -> None:
        self.rows = list(ROWS)

    def list_by_tag(self, tag):
        return [dict(r) for r in self.rows if tag in r["tags"].split(",")]

    def search(self, query, tag_filter="", agent_id="", is_primary=None):
        hits = [dict(r, _combined_score=1.0) for r in self.rows if query in r["content"]]
        if tag_filter:
            hits = [h for h in hits if tag_filter in h["tags"].split(",")]
        return hits

    def get_fragment(self, key):
        for r in self.rows:
            if r["key"] == key:
                return dict(r)
        return None

    def get_fragments_batch(self, keys):
        want = set(keys)
        return {r["key"]: dict(r) for r in self.rows if r["key"] in want}

    def scan_fragment_keys(self, cursor="", limit=200, prefix="memory:frag:"):
        # 假存储的 key 不带前缀，全给（真后端按 prefix 过滤，这里不设障）
        keys = [r["key"] for r in self.rows]
        page = keys[:limit]
        return ("", page)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: FakeStorage()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _keys(body):
    return [i["key"] for i in body["items"]]


# ---------------------------------------------------------------- core

def test_core_传agents_只回该代理名下(client):
    body = client.get("/api/v1/core?agents=worker", headers=AUTH).json()
    assert _keys(body) == ["k-worker", "k-worker2"]
    assert body["count"] == 2


def test_core_不传agents_全量回归(client):
    body = client.get("/api/v1/core", headers=AUTH).json()
    assert body["count"] == len(ROWS)
    assert set(_keys(body)) == {r["key"] for r in ROWS}


def test_core_多代理取并集(client):
    body = client.get("/api/v1/core?agents=worker,main-brain", headers=AUTH).json()
    assert set(_keys(body)) == {"k-worker", "k-worker2", "k-main"}
    assert body["count"] == 3


def test_core_逗号边界_不误伤相似id(client):
    """`agent:worker` 不许命中 `agent:worker-2`（逗号边界的完整标签相等）。"""
    body = client.get("/api/v1/core?agents=worker", headers=AUTH).json()
    assert "k-worker-2" not in _keys(body)


def test_core_无归属条目不进筛选结果(client):
    body = client.get("/api/v1/core?agents=worker", headers=AUTH).json()
    assert "k-none" not in _keys(body)


def test_core_过滤在分页之前_总数不被当前页吃掉(client):
    p1 = client.get("/api/v1/core?agents=worker&page=1&page_size=1", headers=AUTH).json()
    p2 = client.get("/api/v1/core?agents=worker&page=2&page_size=1", headers=AUTH).json()
    assert p1["count"] == p2["count"] == 2          # 过滤后的总数，不是当前页条数
    assert _keys(p1) == ["k-worker"]
    assert _keys(p2) == ["k-worker2"]


# ---------------------------------------------------------------- fragments

def test_fragments_传agents_只回该代理名下(client):
    body = client.get("/api/v1/fragments?agents=worker", headers=AUTH).json()
    assert set(_keys(body)) == {"k-worker", "k-worker2"}
    assert body["count"] == 2
    assert body["count_reason"] == "agent_tags"


def test_fragments_不传agents_全量回归(client):
    body = client.get("/api/v1/fragments", headers=AUTH).json()
    assert body["count"] == len(ROWS)
    assert set(_keys(body)) == {r["key"] for r in ROWS}


def test_fragments_多代理取并集(client):
    body = client.get("/api/v1/fragments?agents=worker,main-brain", headers=AUTH).json()
    assert set(_keys(body)) == {"k-worker", "k-worker2", "k-main"}


def test_fragments_与关键词叠加(client):
    """筛选可与关键词搜索叠加（两个条件都要满足）。"""
    body = client.get("/api/v1/fragments?q=记得&agents=worker", headers=AUTH).json()
    assert set(_keys(body)) == {"k-worker", "k-worker2"}
    assert body["count_reason"] == "search_candidates"

    body2 = client.get("/api/v1/fragments?q=主脑&agents=worker", headers=AUTH).json()
    assert body2["items"] == [] and body2["count"] == 0


def test_fragments_与标签叠加(client):
    body = client.get("/api/v1/fragments?tag=core&agents=main-brain", headers=AUTH).json()
    assert _keys(body) == ["k-main"]
    assert body["count"] == 1


def test_fragments_过滤在分页之前(client):
    p1 = client.get("/api/v1/fragments?agents=worker&page=1&page_size=1", headers=AUTH).json()
    p2 = client.get("/api/v1/fragments?agents=worker&page=2&page_size=1", headers=AUTH).json()
    assert p1["count"] == p2["count"] == 2
    assert _keys(p1) == ["k-worker"] and _keys(p2) == ["k-worker2"]


def test_fragments_重复代理id不重复计数(client):
    body = client.get("/api/v1/fragments?agents=worker,worker", headers=AUTH).json()
    assert body["count"] == 2
