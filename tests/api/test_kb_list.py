"""知识库只读列表接口（P9：`GET /kb/documents` 与 `GET /kb/documents/{doc_id}/chunks`）。

背景：P9 之前知识库页只有 summary（3 个计数）与 search（q 必填），
**没有任何文档列表接口**，前端打开就是空的 —— 本文件就是补上这块的护栏。

88 上没有 KEEPSAKE_PG_DSN、也不许连 180，所以一律用假 PG 驱动覆盖。
假驱动顺手做两件真库做不到的断言：
1) 记下所有执行的 SQL —— 断言全是 SELECT，且 doc_id 从来没被拼进 SQL 文本（只进参数）；
2) 列名可以自由给（探到哪列用哪列），验证「不许写死列名」。
"""

from __future__ import annotations

import re
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.api.routers import kb as kb_router

from conftest import flat_routes

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FAKE_DSN = "postgresql://fake/fake"          # 假串、无口令，只为让 _dsn() 通过

COLS = {"documents": ["id", "title", "updated_at", "content_tsv"],
        "chunks": ["id", "doc_id", "content", "content_tsv"]}
DOCS = [
    {"doc_id": "d1", "title": "运维手册", "updated_at": "2026-10-13T00:00:00+00:00",
     "chunk_count": 3},
    {"doc_id": "d2", "title": "常见问题", "updated_at": "2026-10-12T00:00:00+00:00",
     "chunk_count": 1},
]
CHUNKS = [{"chunk_id": f"{DOCS[0]['doc_id']}-c{i}", "snippet": f"运维手册 第{i}段"}
          for i in range(3)]


class _Cur:
    """按 SQL 关键词分派的假游标；同时把每条 SQL 记进 driver.sqls 供断言。"""

    def __init__(self, driver):
        self.d = driver
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        params = list(params or [])
        self.d.sqls.append((flat, params))
        if "information_schema.columns" in flat:
            self._rows = [{"table_name": t, "column_name": c}
                          for t, cols in self.d.columns.items() for c in cols]
        elif flat.startswith("SELECT 1"):
            self._rows = [{"?column?": 1}] if params and params[0] == "d1" else []
        elif "chunk_count" in flat:                       # 文档列表正文
            rows = self.d.docs
            if ("ILIKE" in flat or "plainto_tsquery" in flat) and params:
                kw = params[0].strip("%")
                rows = [d for d in rows if kw in d["title"]]
            self._rows = rows
        elif flat.startswith("SELECT COUNT"):             # 总数
            self._rows = [{"n": len(self.d.docs) if "documents" in flat else len(CHUNKS)}]
        elif "snippet" in flat:                           # 某文档的切片
            self._rows = ([dict(c, score=0.0) for c in CHUNKS]
                          if params and params[0] == "d1" else [])
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, driver):
        self.d = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _Cur(self.d)


class _Driver:
    def __init__(self, columns=None, docs=None):
        self.columns = columns if columns is not None else COLS
        self.docs = list(DOCS if docs is None else docs)
        self.sqls = []
        self.connected_with = None

    def connect(self, dsn, cursor_factory=None, connect_timeout=5):
        assert dsn, "DSN 不能为空（未配置应在 _dsn() 就被拦下）"
        self.connected_with = (dsn, cursor_factory)
        return _Conn(self)


def _install_fake_pg2(monkeypatch, *, columns=None, docs=None):
    """塞假 psycopg2，并让 psycopg3 不可用（驱动只能走 psycopg2 分支）。"""
    driver = _Driver(columns=columns, docs=docs)
    mod = SimpleNamespace(connect=driver.connect,
                          extras=SimpleNamespace(RealDictCursor=object()))
    monkeypatch.setitem(sys.modules, "psycopg2", mod)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", mod.extras)
    monkeypatch.setitem(sys.modules, "psycopg", None)
    return driver


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def kb_env(monkeypatch):
    """装假驱动 + 清列名缓存（列名缓存是进程级的，测试之间必须隔离）。"""
    monkeypatch.setenv("KEEPSAKE_PG_DSN", FAKE_DSN)
    saved = dict(kb_router._COLS_CACHE)
    kb_router._COLS_CACHE.clear()
    yield monkeypatch
    kb_router._COLS_CACHE.clear()
    kb_router._COLS_CACHE.update(saved)


# ---------------------------------------------------------------- 文档列表

def test_documents_无q_直接列出文档(client, kb_env, monkeypatch):
    """🔴 P9 回归：不输词也要有数据（这是知识库页的默认视图）。"""
    _install_fake_pg2(monkeypatch)
    r = client.get("/api/v1/kb/documents", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [d["doc_id"] for d in body["items"]] == ["d1", "d2"]
    assert body["items"][0]["title"] == "运维手册"
    assert body["items"][0]["updated_at"] == "2026-10-13T00:00:00+00:00"
    assert body["items"][0]["chunk_count"] == 3
    assert body["count"] == 2 and body["page"] == 1


def test_documents_切片数按doc_id分组计数(client, kb_env, monkeypatch):
    driver = _install_fake_pg2(monkeypatch)
    client.get("/api/v1/kb/documents", headers=AUTH)
    sql = next(s for s, _ in driver.sqls if "chunk_count" in s)
    assert f"{kb_router.config.KB_TABLE_CHUNKS}" in sql
    assert "COUNT(*)" in sql and "GROUP BY" not in sql   # 相关子查询即可，不必 GROUP BY


def test_documents_有q_按词过滤(client, kb_env, monkeypatch):
    driver = _install_fake_pg2(monkeypatch)
    r = client.get("/api/v1/kb/documents?q=常见", headers=AUTH)
    assert r.status_code == 200, r.text
    assert [d["doc_id"] for d in r.json()["items"]] == ["d2"]
    # 检索词只进参数，绝不拼进 SQL 文本
    sqls = [s for s, p in driver.sqls if p]
    assert all("常见" not in s for s in sqls)


@pytest.mark.parametrize("qs,ok", [
    ("?page=0", False), ("?page=-1", False), ("?page_size=0", False),
    ("?page_size=101", False), ("?page=1&page_size=50", True),
])
def test_documents_分页参数校验(client, kb_env, monkeypatch, qs, ok):
    """分页参数越界 ⇒ 422（FastAPI 层拦，不进 SQL）。"""
    _install_fake_pg2(monkeypatch)
    r = client.get(f"/api/v1/kb/documents{qs}", headers=AUTH)
    assert (r.status_code == 200) is ok, r.text


def test_documents_未配置DSN_500(client, kb_env, monkeypatch):
    _install_fake_pg2(monkeypatch)
    kb_env.delenv("KEEPSAKE_PG_DSN", raising=False)
    r = client.get("/api/v1/kb/documents", headers=AUTH)
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "kb_unconfigured"


def test_documents_缺表_500(client, kb_env, monkeypatch):
    """探不到列（表不存在）⇒ kb_table_missing，不是空列表装成功。"""
    _install_fake_pg2(monkeypatch, columns={})
    r = client.get("/api/v1/kb/documents", headers=AUTH)
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "kb_table_missing"


def test_documents_列名不写死(client, kb_env, monkeypatch):
    """换个列名体系（无 id/无 title/无时间列）照样跑通：出参留空，不报错。"""
    _install_fake_pg2(monkeypatch, columns={"documents": ["uuid", "doc_title", "created_at"],
                                            "chunks": ["uuid", "document_id", "page_content"]})
    r = client.get("/api/v1/kb/documents", headers=AUTH)
    assert r.status_code == 200, r.text
    assert set(r.json()["items"][0]) == {"doc_id", "title", "updated_at", "chunk_count"}


def test_documents_无tsvector列_退标题ILIKE(client, kb_env, monkeypatch):
    """documents 没有全文索引列时按标题模糊（与 /kb/search 同一手法降级）。"""
    driver = _install_fake_pg2(monkeypatch,
                               columns={"documents": ["id", "title", "updated_at"],
                                        "chunks": ["id", "doc_id", "content"]})
    r = client.get("/api/v1/kb/documents?q=常见", headers=AUTH)
    assert r.status_code == 200, r.text
    assert "ILIKE" in next(s for s, _ in driver.sqls if "chunk_count" in s)
    assert [d["doc_id"] for d in r.json()["items"]] == ["d2"]


def test_documents_有tsvector列_走tsquery(client, kb_env, monkeypatch):
    driver = _install_fake_pg2(monkeypatch)
    client.get("/api/v1/kb/documents?q=运维", headers=AUTH)
    sql = next(s for s, _ in driver.sqls if "chunk_count" in s)
    assert "plainto_tsquery('simple'" in sql      # 中文语料同 /kb/search：simple 分词


def test_documents_chunks缺外键列_500_kb_schema_unknown(client, kb_env, monkeypatch):
    """chunks 探不到 doc_id 这类外键 ⇒ 500 kb_schema_unknown，不静默返回 0 计数。"""
    _install_fake_pg2(monkeypatch, columns={"documents": ["id", "title", "updated_at"],
                                            "chunks": ["id", "content"]})
    r = client.get("/api/v1/kb/documents", headers=AUTH)
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "kb_schema_unknown"


def test_documents_只有documents没有chunks_500(client, kb_env, monkeypatch):
    _install_fake_pg2(monkeypatch, columns={"documents": ["id", "title", "updated_at"]})
    r = client.get("/api/v1/kb/documents", headers=AUTH)
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["error"] == "kb_table_missing" and kb_router.config.KB_TABLE_CHUNKS in detail["message"]


def test_documents_空库_返回空列表而非报错(client, kb_env, monkeypatch):
    """表在但没数据 ⇒ 200 + 空 items（这才是「没数据」，与「接口坏了」分开）。"""
    _install_fake_pg2(monkeypatch, docs=[])
    r = client.get("/api/v1/kb/documents", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"items": [], "count": 0, "page": 1, "page_size": 20}


def test_documents_回显里没有连接串(client, kb_env, monkeypatch):
    driver = _install_fake_pg2(monkeypatch)
    body = client.get("/api/v1/kb/documents", headers=AUTH).text
    assert FAKE_DSN not in body and TOKEN not in body
    assert driver.connected_with[0] == FAKE_DSN      # 用的是环境变量里的那个串


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer wrong-token"}, {"Authorization": "Basic abc"},
])
def test_documents_未带对会话_401(client, kb_env, monkeypatch, headers):
    _install_fake_pg2(monkeypatch)
    assert client.get("/api/v1/kb/documents", headers=headers).status_code == 401


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer wrong-token"},
])
def test_chunks_未带对会话_401(client, kb_env, monkeypatch, headers):
    _install_fake_pg2(monkeypatch)
    assert client.get("/api/v1/kb/documents/d1/chunks", headers=headers).status_code == 401


def test_chunks_404响应不含内部结构(client, kb_env, monkeypatch):
    """404 只回固定原因码，不回 SQL / 列名 / 连接串。"""
    _install_fake_pg2(monkeypatch)
    r = client.get("/api/v1/kb/documents/nope/chunks", headers=AUTH)
    assert r.json()["detail"] == {"error": "kb_doc_missing", "message": "知识库文档不存在：nope"}
    assert FAKE_DSN not in r.text


def test_chunks_切片取前300字(client, kb_env, monkeypatch):
    """snippet 走 SQL 侧的 LEFT(text, 300)，不在 Python 里截 —— 少搬一次数据。"""
    driver = _install_fake_pg2(monkeypatch)
    client.get("/api/v1/kb/documents/d1/chunks", headers=AUTH)
    assert "LEFT(" in next(s for s, _ in driver.sqls if "snippet" in s)


def test_chunks_列名不写死(client, kb_env, monkeypatch):
    """换列名体系（uuid/document_id/page_content）照样跑通。"""
    _install_fake_pg2(monkeypatch, columns={"documents": ["uuid", "doc_title"],
                                            "chunks": ["uuid", "document_id", "page_content"]})
    r = client.get("/api/v1/kb/documents/d1/chunks", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["snippet"] == "运维手册 第0段"


# ---------------------------------------------------------------- 文档切片
def test_chunks_列出切片(client, kb_env, monkeypatch):
    _install_fake_pg2(monkeypatch)
    r = client.get("/api/v1/kb/documents/d1/chunks", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_id"] == "d1"
    assert [c["chunk_id"] for c in body["items"]] == [f"d1-c{i}" for i in range(3)]
    assert body["items"][0]["snippet"] == "运维手册 第0段"
    assert body["count"] == 3


def test_chunks_文档不存在_404(client, kb_env, monkeypatch):
    driver = _install_fake_pg2(monkeypatch)
    r = client.get("/api/v1/kb/documents/nope/chunks", headers=AUTH)
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "kb_doc_missing"
    # doc_id 走参数位，不进 SQL 文本
    assert all("nope" not in s for s, _ in driver.sqls)


def test_chunks_未配置DSN_500(client, kb_env, monkeypatch):
    _install_fake_pg2(monkeypatch)
    kb_env.delenv("KEEPSAKE_PG_DSN", raising=False)
    r = client.get("/api/v1/kb/documents/d1/chunks", headers=AUTH)
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "kb_unconfigured"


def test_chunks_缺表_500(client, kb_env, monkeypatch):
    _install_fake_pg2(monkeypatch, columns={})
    r = client.get("/api/v1/kb/documents/d1/chunks", headers=AUTH)
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "kb_table_missing"


def test_chunks_分页参数校验(client, kb_env, monkeypatch):
    _install_fake_pg2(monkeypatch)
    assert client.get("/api/v1/kb/documents/d1/chunks?page_size=0", headers=AUTH).status_code == 422
    assert client.get("/api/v1/kb/documents/d1/chunks?page=1", headers=AUTH).status_code == 200


# ---------------------------------------------------------------- 只读 / 鉴权

def test_两个新接口都只跑SELECT(client, kb_env, monkeypatch):
    driver = _install_fake_pg2(monkeypatch)
    client.get("/api/v1/kb/documents", headers=AUTH)
    client.get("/api/v1/kb/documents/d1/chunks", headers=AUTH)
    assert driver.sqls
    for sql, _ in driver.sqls:
        assert sql.upper().startswith("SELECT"), sql
        # 词边界匹配：updated_at 里含 "UPDATE"，直接子串匹配会误报
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TRUNCATE)\b",
                             sql, re.I), sql


def test_两个新接口都要会话(client, kb_env, monkeypatch):
    _install_fake_pg2(monkeypatch)
    assert client.get("/api/v1/kb/documents").status_code == 401
    assert client.get("/api/v1/kb/documents/d1/chunks").status_code == 401


def test_既有接口零变化_仍是三条原路径(client, kb_env, monkeypatch):
    """/kb/summary 与 /kb/search 的存在与必填口径不许因新增接口而变。"""
    _install_fake_pg2(monkeypatch)
    app = create_app()
    paths = {r.path for r in flat_routes(app)}
    assert {"/api/v1/kb/summary", "/api/v1/kb/search"} <= paths
    # search 的 q 仍然必填：不带 q ⇒ 422（不是被新接口「顺手」改成可选）
    assert client.get("/api/v1/kb/search", headers=AUTH).status_code == 422
