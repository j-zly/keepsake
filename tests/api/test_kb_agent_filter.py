"""知识库列表的**来源代理**（`documents.agent_id`）：投影 + 筛选。

知识库对代理开放后，控制台的知识库页要能看出「这篇是谁存的」并按代理筛：

  1. 列表出参**带 `agent_id`**（有主的给出 agent_id；无主的**不出这个键** ——
     缺省不假装有值，前端按「无主」渲染）；
  2. `?agents=a,b` **服务端过滤**（切页之前）：只回选中代理名下的文档，
     `count` 走同一条 WHERE（= 过滤后总数，不是全库数）；
  3. 筛选值**只进参数**（不拼进 SQL 文本）；
  4. 老库**没有 agent_id 列**而调用方要按代理筛 ⇒ 400 `kb_schema_unknown`
     （筛不了就说筛不了，不静默给全量）。

**用真 PG**：与 `tests/api/test_kb_crud.py` 同一套接法（隔离 schema + 用完 DROP）。
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.api.routers import kb as kb_router

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST, ALLOWED_DBNAME = os.environ.get("KEEPSAKE_PG_TEST_HOST", "postgres.test.example"), "keepsake_test"
psycopg = pytest.importorskip("psycopg", reason="未装 psycopg")

TOKEN = "kbagent-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: 与生产同款 DDL（**含 `agent_id text NULL`** —— 归属列，可空 = 无主）
DDL = (
    'CREATE SCHEMA "{s}"',
    'CREATE TABLE "{s}".documents ('
    ' id bigserial PRIMARY KEY, source_type text NOT NULL, source_key text NOT NULL,'
    ' content_type text NOT NULL DEFAULT \'note\','
    ' tags text[] NOT NULL DEFAULT \'{{}}\','
    ' status text NOT NULL DEFAULT \'effective\','
    ' review_meta jsonb NOT NULL DEFAULT \'{{}}\','
    ' agent_id text,'
    ' title text, meta jsonb, content_hash text,'
    ' updated_at timestamptz NOT NULL DEFAULT now(),'
    ' UNIQUE (source_type, source_key))',
    'CREATE TABLE "{s}".chunks ('
    ' id bigserial PRIMARY KEY, document_id bigint NOT NULL'
    ' REFERENCES "{s}".documents(id), ord int NOT NULL, text text, source_path text)',
)

#: **迁移之前**的老表（没有 agent_id 列）—— 缺列窗口期的真实形状
DDL_NO_AGENT = (
    'CREATE SCHEMA "{s}"',
    'CREATE TABLE "{s}".documents ('
    ' id bigserial PRIMARY KEY, source_type text NOT NULL, source_key text NOT NULL,'
    ' content_type text NOT NULL DEFAULT \'note\','
    ' tags text[] NOT NULL DEFAULT \'{{}}\','
    ' status text NOT NULL DEFAULT \'effective\','
    ' review_meta jsonb NOT NULL DEFAULT \'{{}}\','
    ' title text, meta jsonb, content_hash text,'
    ' updated_at timestamptz NOT NULL DEFAULT now(),'
    ' UNIQUE (source_type, source_key))',
    'CREATE TABLE "{s}".chunks ('
    ' id bigserial PRIMARY KEY, document_id bigint NOT NULL'
    ' REFERENCES "{s}".documents(id), ord int NOT NULL, text text, source_path text)',
)


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


DSN = _read_dsn()
_pg_ready = bool(DSN and ALLOWED_HOST in DSN and ALLOWED_DBNAME in DSN)

PG_ONLY = pytest.mark.skipif(not _pg_ready,
                             reason="无测试库凭据，跳过 PG 真库")


@pytest.fixture()
def kb(monkeypatch, tmp_path) -> Iterator[SimpleNamespace]:
    yield from _kb_with(monkeypatch, tmp_path, DDL)


@pytest.fixture()
def legacy_kb(monkeypatch, tmp_path) -> Iterator[SimpleNamespace]:
    """**迁移之前**的老表（没有 agent_id 列）。"""
    yield from _kb_with(monkeypatch, tmp_path, DDL_NO_AGENT)


def _kb_with(monkeypatch, tmp_path, ddl) -> Iterator[SimpleNamespace]:
    if not _pg_ready:
        pytest.skip("无测试库凭据，跳过 PG 真库")
    schema = f"ks_kbagent_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        for stmt in ddl:
            cur.execute(stmt.format(s=schema))

    monkeypatch.setenv("KEEPSAKE_PG_DSN", DSN)
    monkeypatch.setenv("KEEPSAKE_PG_SCHEMA", schema)
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "ops"))
    saved = dict(kb_router._COLS_CACHE)
    kb_router._COLS_CACHE.clear()             # 列名缓存是进程级的，测试之间必须隔离
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: None
    with TestClient(app) as client:
        try:
            yield SimpleNamespace(client=client, schema=schema, dsn=DSN)
        finally:
            kb_router._COLS_CACHE.clear()
            kb_router._COLS_CACHE.update(saved)
            app.dependency_overrides.clear()
    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _has_agent_col(kb: SimpleNamespace) -> bool:
    """这张表有没有 `agent_id` 列（老库没有 ⇒ 播种时也不能带这根列）。"""
    with psycopg.connect(kb.dsn, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='documents' AND column_name='agent_id'",
                    [kb.schema])
        return cur.fetchone() is not None


def _seed(kb: SimpleNamespace, title: str, agent_id: str = "") -> int:
    """塞一篇**已生效**的文档（`agent_id` 空 = 无主）。"""
    cols = ('source_type, source_key, title, meta, content_hash, agent_id'
            if _has_agent_col(kb) else 'source_type, source_key, title, meta, content_hash')
    params = [f"seed:{title}", title, f"sha:{title}"]
    if _has_agent_col(kb):
        params.append(agent_id or None)
    with psycopg.connect(kb.dsn, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{kb.schema}".documents({cols})'
                    " VALUES ('agent',%s,%s,'{}'::jsonb,%s"
                    + (",%s" if _has_agent_col(kb) else "")
                    + ") RETURNING id", params)
        doc_id = cur.fetchone()[0]
        cur.execute(f'INSERT INTO "{kb.schema}".chunks(document_id, ord, text, source_path)'
                    ' VALUES (%s, 0, %s, %s)', [doc_id, "正文", f"seed:{title}"])
    return doc_id


def _docs(client: TestClient, **params: Any) -> Dict[str, Any]:
    r = client.get("/api/v1/kb/documents", params={"page_size": 100, **params}, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 投影

@PG_ONLY
def test_列表带agent_id_无主的不出这个键(kb):
    """🔴 有主的给出 agent_id；无主的**不出这个键**（缺省不假装有值）。"""
    _seed(kb, "有主的", "worker-a")
    _seed(kb, "无主的")
    items = {i["title"]: i for i in _docs(kb.client)["items"]}
    assert items["有主的"]["agent_id"] == "worker-a"
    assert "agent_id" not in items["无主的"]


@PG_ONLY
def test_控制台手建文档仍是无主(kb):
    """站内手建路径**不写归属**（NULL = 无主，存量语义不变）。"""
    r = kb.client.post("/api/v1/kb/documents",
                       json={"confirm": True, "title": "手写一篇", "content": "正文"},
                       headers=AUTH)
    assert r.status_code == 200, r.text
    with psycopg.connect(kb.dsn, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT agent_id FROM "{kb.schema}".documents WHERE title=%s', ["手写一篇"])
        assert cur.fetchone()[0] is None


# ---------------------------------------------------------------- 筛选

@PG_ONLY
def test_按来源代理筛选_只回该代理的且计数同口径(kb):
    """🔴 服务端过滤（切页之前）+ `count` 走同一条 WHERE（= 过滤后总数）。"""
    _seed(kb, "A的", "worker-a")
    _seed(kb, "B的", "worker-b")
    _seed(kb, "无主的")
    only_a = _docs(kb.client, agents="worker-a")
    assert [i["title"] for i in only_a["items"]] == ["A的"]
    assert only_a["count"] == 1                    # 不是全库 3
    both = _docs(kb.client, agents="worker-a,worker-b")
    assert {i["title"] for i in both["items"]} == {"A的", "B的"}
    assert both["count"] == 2
    # 不给 agents ⇒ 不过滤（= 现状不变）
    assert _docs(kb.client)["count"] == 3


@PG_ONLY
def test_按来源代理筛选_与其它筛选取AND(kb):
    _seed(kb, "A的", "worker-a")
    _seed(kb, "B的", "worker-b")
    r = kb.client.get("/api/v1/kb/documents", params={"agents": "worker-a", "q": "B的"},
                      headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


@PG_ONLY
def test_缺列_按代理筛_400不静默给全量(legacy_kb):
    """老库没有 agent_id 列而调用方要按代理筛 ⇒ 400（筛不了就说筛不了）。"""
    _seed(legacy_kb, "老文档")
    r = legacy_kb.client.get("/api/v1/kb/documents", params={"agents": "worker-a"},
                              headers=AUTH)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "kb_schema_unknown"
    assert "agent_id" in detail["message"] and "ALTER TABLE" in detail["message"]


@PG_ONLY
def test_缺列_读路径降级_列表照常不炸(legacy_kb):
    """老库上列表照常跑（**不出 agent_id 键** = 全库无主）。"""
    _seed(legacy_kb, "老文档")
    body = _docs(legacy_kb.client)
    assert [i["title"] for i in body["items"]] == ["老文档"]
    assert "agent_id" not in body["items"][0]
