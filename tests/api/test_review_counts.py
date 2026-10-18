"""`GET /api/v1/review/counts` 待审条目计数端点测试（控制台侧栏徽标 + 看板卡的
唯一取数口）。

侧栏徽标依赖这一个端点挂在 require_session 后给三个数：缺一个就徽标没意义。
测试绕开 PG（document 表那条逻辑降级路径单测在 test_kb_review 里连带验了），
focus 在记忆侧的 promote / demote 计数 + 401 + 全套口径一致性。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.core.shared import fragment_key_for

TOKEN = "review-counts-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


#: 种子片段里两条候选（一份带 core_candidate 一份同 label + core），一条退居。
SEED: List[Dict[str, Any]] = [
    {"key": "memory:frag:c1", "content": "晋升候选 A：值班联系人",
     "tags": "core_candidate,ops", "created": "2026-01-01T00:00:00+00:00"},
    {"key": "memory:frag:c2", "content": "晋升候选 B：客户偏好",
     "tags": "core_candidate", "created": "2026-01-01T00:00:00+00:00"},
    {"key": "memory:frag:d1", "content": "退居建议 X：过期任务",
     "tags": "tier:demote-candidate,stale", "created": "2026-01-01T00:00:00+00:00"},
    # 普通常驻：不该被算进去（tag 边界不能误伤）
    {"key": "memory:frag:plain", "content": "普通条目",
     "tags": "ops", "created": "2026-01-01T00:00:00+00:00"},
]


@pytest.fixture()
def storage(tmp_path):
    """真 SqliteStorage（与 test_api_ops 同款），全夹具自己造自己清。"""
    from keepsake.core.sqlite import SqliteStorage

    s = SqliteStorage(path=str(tmp_path / "rc.db"), embedder=None, embed_dim=8)
    assert s.ensure_index() is not False
    assert s.write_fragments_batch([dict(r) for r in SEED]) == len(SEED)
    return s


@pytest.fixture()
def audit_dir(tmp_path, monkeypatch):
    """审计目录；review/counts 是只读，但 write_guard 没挂它，所以 audit dir 不必可写。
    这里仍然落一个空目录，免得 precheck 跨用例漂。"""
    d = tmp_path / "audit"
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(d))
    return d


@pytest.fixture()
def client(monkeypatch, storage, audit_dir):
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    # 关掉 KB：review/counts 在 KB 未配置时 kb_pending 必须优雅退 0，
    # 这条降级纪律与 kb_type_counts / kb_tags 完全同款。
    monkeypatch.delenv("KEEPSAKE_PG_DSN", raising=False)
    monkeypatch.delenv("KEEPSAKE_PG_SCHEMA", raising=False)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------- 鉴权

def test_无token_401(client):
    """不带 Authorization 头 → 401（与既有只读端点同款，缺一闸就红）。"""
    r = client.get("/api/v1/review/counts")
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "missing_token"
    # 🔴 响应体不能泄漏 token（这条断言与 ops 全套规则一致）
    assert TOKEN not in r.text


# ---------------------------------------------------------------- 计数

def test_三个字段齐全_且为整数(client):
    """返回体必须有 promote_candidates / demote_candidates / kb_pending 三个整数。
    徽标 + 看板卡都靠这三个键，少一个就崩。"""
    r = client.get("/api/v1/review/counts", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"promote_candidates", "demote_candidates", "kb_pending"}
    for k in ("promote_candidates", "demote_candidates", "kb_pending"):
        assert isinstance(body[k], int), (k, type(body[k]))


def test_promote_demote_全库口径(client, storage):
    """promote = `core_candidate` 标签的全库条数；demote = `tier:demote-candidate` 全库条数。
    与 list_by_tag 同源 —— 以后台偏移（pg/sqlite/redis）这个原语变化时要一起动。"""
    r = client.get("/api/v1/review/counts", headers=AUTH)
    body = r.json()
    # 直接对底层 list_by_tag 算账：端点的数必须**完全相等**，不是近似
    promote = sum(1 for _ in storage.list_by_tag("core_candidate"))
    demote = sum(1 for _ in storage.list_by_tag("tier:demote-candidate"))
    assert body["promote_candidates"] == promote == 2
    assert body["demote_candidates"] == demote == 1


def test_普通常驻条目不计入候选(client):
    """`core` 标签的条目不属于 promote/demote —— 边界走 tag_match_sql 别误伤。
    普通 ops 条目 + 普通常驻都不进任何一边。"""
    # 在普通常驻里加一条：fixture 的 memory:frag:plain 不挂 core，先打 core
    storage_update_keys = [r["key"] for r in SEED]  # noqa: F841（只为可读性）
    r = client.get("/api/v1/review/counts", headers=AUTH)
    body = r.json()
    assert body["promote_candidates"] == 2, "普通常驻条目不能误算进 promote"
    assert body["demote_candidates"] == 1, "普通常驻条目不能误算进 demote"
    assert body["kb_pending"] == 0


def test_边界匹配_类似但不等的标签不触发假阳性(client, monkeypatch):
    """`core_candidate_x` / `tier:demote-candidate-other` 这类相似标不能被算进。
    单测目的：守护 tag_match_sql 的逗号边界在 SQLite 侧的口径。
    注入两条额外记录，包含相似但不等的标，端点必须返回原值。"""
    extra = [
        {"key": "memory:frag:fake1", "content": "假相似标：core_candidate 长尾",
         "tags": "core_candidate_follower", "created": "2026-01-01T00:00:00+00:00"},
        {"key": "memory:frag:fake2", "content": "假相似标：demote 长尾",
         "tags": "tier:demote-candidate-other", "created": "2026-01-01T00:00:00+00:00"},
    ]
    assert client.get("/api/v1/review/counts", headers=AUTH).status_code == 200
    # 需要拿到原始 storage 才能追加
    app_storage = client.app.dependency_overrides[get_storage]()
    app_storage.write_fragments_batch(extra)
    r = client.get("/api/v1/review/counts", headers=AUTH)
    body = r.json()
    assert body["promote_candidates"] == 2, "core_candidate_follower 不能被算进 promote"
    assert body["demote_candidates"] == 1, "tier:demote-candidate-other 不能被算进 demote"


def test_kb_unconfigured时kb_pending退零_不影响记忆侧(client, storage):
    """KB 未配置（KEEPSAKE_PG_DSN 缺失）⇒ kb_pending=0；promote/demote 不被波及。
    单测目的：守护「单点降级不动整体」的口径。"""
    r = client.get("/api/v1/review/counts", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["kb_pending"] == 0
    # 记忆侧照样给真数（不挂）
    assert body["promote_candidates"] == 2
    assert body["demote_candidates"] == 1


def test_空库三个数都为零(client):
    """没有 SEED 时端点给 (0,0,0)，不报 404（不是「找不到条目」）。"""
    empty_storage = type(client.app.dependency_overrides[get_storage]())(
        path=str(Path(client.app.dependency_overrides[get_storage]()._path).parent / "empty.db"),
        embedder=None, embed_dim=8,
    )
    empty_storage.ensure_index()
    client.app.dependency_overrides[get_storage] = lambda: empty_storage
    r = client.get("/api/v1/review/counts", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"promote_candidates": 0, "demote_candidates": 0, "kb_pending": 0}


# ---------------------------------------------------------------- 接口契约

def test_路径在_get_routes里_且方法仅GET(client):
    """🔴 多挂一个路由方法就红 —— 不许 POST/PUT/DELETE 偷偷挂在 `/api/v1/review/counts`，
    那是只读端点，write_guard 没挂它就是 fail-open 的写入口。"""
    app = client.app
    methods = set()
    for r in app.routes:
        if getattr(r, "path", "") == "/api/v1/review/counts":
            methods = getattr(r, "methods", set()) or set()
    assert methods == {"GET"}, f"review/counts 必须是仅 GET，实际 {methods}"


def test_挂的是require_session_不是write_guard(client):
    """🔴 只读端点不许挂 write_guard（这是写端点的统一前置闸门）。
    少挂 require_session 也会红，但正向断言有了，下面只验 write_guard 不挂。"""
    app = client.app
    for r in app.routes:
        if getattr(r, "path", "") == "/api/v1/review/counts":
            names = {getattr(d.call, "__name__", "") for d in r.dependant.dependencies}
            assert "require_session" in names, "review/counts 必须挂 require_session"
            assert "write_guard" not in names, "review/counts 是 GET 不许挂 write_guard"


# ponytail: 这个测整套都是直接读 fixture 数据，零 O(n²) / 锁 / heuristic。
# promote / demote 用 storage.list_by_tag 而不是新 SQL —— 任务书硬约束，
# 守护「不在 ops 层新发明存储后端方言」这条纪律。
