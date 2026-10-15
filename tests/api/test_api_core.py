"""`GET /api/v1/core?q=` 搜索参数的用例（keepsake_memory_crud_fe）。

只覆盖新增的 `q` 三个口径：命中 / 不命中 / 空串=全量。既有断言在
`tests/api/test_api_readonly.py` 里，一条都没动。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

ROWS = [
    {"key": "memory:frag:a1", "content": "Postgres 隔离库白名单", "tags": "core,shared",
     "updated_at": "2026-10-13T00:00:00+00:00"},
    {"key": "memory:frag:b2", "content": "Redis HMGET 要补取 is_archived", "tags": "core",
     "updated_at": "2026-10-13T00:00:01+00:00"},
    {"key": "memory:frag:c3", "content": "无关的一条", "tags": "core,agent:planner",
     "updated_at": "2026-10-13T00:00:02+00:00"},
]


class FakeStorage:
    """只实现 core 列表用到的那一个原语（`list_by_tag`）。"""

    def __init__(self) -> None:
        self.rows = list(ROWS)

    def list_by_tag(self, tag):
        return [r for r in self.rows if tag in r["tags"].split(",")]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: FakeStorage()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_core_q_正文子串命中_大小写不敏感(client):
    body = client.get("/api/v1/core?q=postgres", headers=AUTH).json()
    assert body["count"] == 1
    assert [i["key"] for i in body["items"]] == ["memory:frag:a1"]
    # 大小写不敏感：小写词能命中「Postgres」
    assert client.get("/api/v1/core?q=POSTGRES", headers=AUTH).json()["count"] == 1


def test_core_q_也能命中标签(client):
    body = client.get("/api/v1/core?q=agent:planner", headers=AUTH).json()
    assert body["count"] == 1
    assert body["items"][0]["key"] == "memory:frag:c3"


def test_core_q_不命中_空列表但count为0(client):
    body = client.get("/api/v1/core?q=根本不存在的词", headers=AUTH).json()
    assert body["count"] == 0
    assert body["items"] == []


def test_core_q_空串等于全量_与不带q一致(client):
    full = client.get("/api/v1/core", headers=AUTH).json()
    blank = client.get("/api/v1/core?q=", headers=AUTH).json()
    blank2 = client.get("/api/v1/core?q=%20%20", headers=AUTH).json()
    assert full["count"] == len(ROWS)
    assert blank == full
    assert blank2 == full


def test_core_q_分页在过滤之后_口径与不分页一致(client):
    """🔴 `count` 必须是**过滤后**的条数，分页切片也来自过滤集。"""
    body = client.get("/api/v1/core?q=无关&page=1&page_size=10", headers=AUTH).json()
    assert body["count"] == 1
    assert [i["key"] for i in body["items"]] == ["memory:frag:c3"]
    # 命中但翻到下一页 ⇒ 空列表，count 不变
    far = client.get("/api/v1/core?q=无关&page=2&page_size=1", headers=AUTH).json()
    assert far["items"] == []
    assert far["count"] == 1
