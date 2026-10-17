"""知识库写端点测试（增 / 改 / 真删 / 导入 / 导出）。

**用真 PG**：KB 是 PG 专有（documents/chunks 无 SQLite 对应物），所以真库用例连
`~/.keepsake_pg_test.env` 里的 8.140.192.91/keepsake_test，每个用例建一个独立
schema（表结构与生产同款 DDL），用完 `DROP SCHEMA ... CASCADE`；读不到凭据就
skip —— 与 `tests/test_list_by_tag_archived.py` 同一套接法。纯函数用例
（`split_chunks` / 单点断言）不碰库，任何环境都跑。

本文件钉死六件最容易悄悄坏掉的事：
  1. 增 → 能被 `GET /kb/documents?q=` 与 `GET /kb/search?q=`（ILIKE 路）检索到；
  2. **改正文必须重切 chunks** —— 旧词消失、新词可搜（双向断言，只断一头会漏掉
     「索引整个空了」这种假绿）；
  3. 真删 = 手输标题 + 删前备份全文（备份文件里必须找得到正文）；
  4. skill 域文档改/删一律 409（改了当晚就被灌入器覆盖或清理）；
  5. 导入 = 默认 dry-run 零写入、实跑手输计数、内容级幂等（重复导入 unchanged）；
  6. **切块单点**：API 与 `scripts/kb_ingest.py` 必须是同一个 `split_chunks`。
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.api.routers import kb as kb_router
from keepsake.kb.ingest import chunk_document, link_body, split_chunks

TOKEN = "kbcrud-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

CREATE = "/api/v1/kb/documents"
IMPORT = "/api/v1/kb/import"
EXPORT = "/api/v1/kb/export"

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST, ALLOWED_DBNAME = "8.140.192.91", "keepsake_test"
psycopg = pytest.importorskip("psycopg", reason="未装 psycopg")

#: 与生产 `keepsake` 库同款 DDL（含 (source_type, source_key) 唯一约束 ——
#: upsert 的 ON CONFLICT 依赖它）。
#: 🆕 `content_type text NOT NULL DEFAULT 'note'` —— 与生产迁移后的列**同形状**
#: （DEFAULT + NOT NULL 都要有：DEFAULT 保证存量 INSERT 不带这列也能落，
#: NOT NULL 保证 GROUP BY 出来的类型集合是全集而不是一堆 NULL）。
#: 🆕 `tags text[] NOT NULL DEFAULT '{}'` + GIN 索引 —— 自由标签列，同形状
#: （DEFAULT '{}' 让灌入器那种「不带这列」的 INSERT 照样落得下去）。
#: 🆕 `status text NOT NULL DEFAULT 'effective'` + `review_meta jsonb NOT NULL
#: DEFAULT '{}'` —— 审核两件套（批2），同形状。DEFAULT 'effective' 是承重的那一环：
#: 灌入器与站内会话一个字都不往这根列写，全靠 DEFAULT 落成已生效。
DDL = (
    'CREATE SCHEMA "{s}"',
    'CREATE TABLE "{s}".documents ('
    ' id bigserial PRIMARY KEY, source_type text NOT NULL, source_key text NOT NULL,'
    ' content_type text NOT NULL DEFAULT \'note\','
    ' tags text[] NOT NULL DEFAULT \'{{}}\','
    ' status text NOT NULL DEFAULT \'effective\','
    ' review_meta jsonb NOT NULL DEFAULT \'{{}}\','   # ⚠️ 双花括号：下面要 .format(s=schema)
    ' title text, meta jsonb, content_hash text,'
    ' updated_at timestamptz NOT NULL DEFAULT now(),'
    ' UNIQUE (source_type, source_key))',
    'CREATE INDEX idx_documents_tags_gin ON "{s}".documents USING gin(tags)',
    'CREATE INDEX idx_documents_status ON "{s}".documents(status)',
    'CREATE TABLE "{s}".chunks ('
    ' id bigserial PRIMARY KEY, document_id bigint NOT NULL'
    ' REFERENCES "{s}".documents(id), ord int NOT NULL, text text, source_path text)',
)

#: 迁移**之前**的老表形状（既没有 content_type 也没有 tags 列）——
#: 用来钉「缺列时人话报错不裸崩 / 读路径降级不炸」
DDL_NO_CTYPE = (
    'CREATE SCHEMA "{s}"',
    'CREATE TABLE "{s}".documents ('
    ' id bigserial PRIMARY KEY, source_type text NOT NULL, source_key text NOT NULL,'
    ' title text, meta jsonb, content_hash text,'
    ' updated_at timestamptz NOT NULL DEFAULT now(),'
    ' UNIQUE (source_type, source_key))',
    'CREATE TABLE "{s}".chunks ('
    ' id bigserial PRIMARY KEY, document_id bigint NOT NULL'
    ' REFERENCES "{s}".documents(id), ord int NOT NULL, text text, source_path text)',
)

#: 生产迁移 SQL（与主脑手执行的形状逐字一致；测试跑它来保证幂等 + 真能迁）
MIGRATE_SQL = (
    'ALTER TABLE "{s}".documents ADD COLUMN IF NOT EXISTS '
    'content_type text NOT NULL DEFAULT \'note\'',
    'UPDATE "{s}".documents SET content_type=\'skill\' '
    'WHERE source_type IN (\'skill\',\'skill_hk\')',
    'CREATE INDEX IF NOT EXISTS idx_documents_content_type ON "{s}".documents(content_type)',
)


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


DSN = _read_dsn()
#: 只认白名单里的 host/dbname —— 防止有人把生产 DSN 顺手写进这个文件就被测试打上
_pg_ready = bool(DSN and ALLOWED_HOST in DSN and ALLOWED_DBNAME in DSN)

PG_ONLY = pytest.mark.skipif(not _pg_ready,
                             reason="无 8.140.192.91/keepsake_test 凭据，跳过 PG 真库")


# ---------------------------------------------------------------- 纯函数（不碰库）

def test_split_chunks_标题分段():
    text = "## 第一段\n内容一\n## 第二段\n内容二\n"
    assert split_chunks(text) == ["## 第一段\n内容一", "## 第二段\n内容二"]


def test_split_chunks_超长硬切():
    out = split_chunks("a" * 2500, max_len=1200)
    assert [len(c) for c in out] == [1200, 1200, 100]


def test_split_chunks_空文兜底():
    """空文也返回一块（v3 口径）—— 否则调用方可能写出零切片的文档。"""
    assert split_chunks("") == [""]
    assert split_chunks("   \n\n  ") == ["   \n\n  "]
    assert split_chunks("a" * 2400)[-1] == "a" * 1200      # 整除时也不吞尾巴


def test_CLI与API共用同一个split_chunks():
    """🔴 单点判据：两处各切一次块，页面切片数与检索命中就会对不上。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    try:
        import kb_ingest
    finally:
        sys.path.pop(0)
    assert kb_ingest.split_chunks is split_chunks


def test_manual_source_key_内容级幂等():
    from keepsake.kb.ingest import content_hash, manual_source_key

    assert manual_source_key("运维", "正文") == manual_source_key("运维", "正文")
    assert manual_source_key("运维", "正文") != manual_source_key("运维", "正文2")
    assert manual_source_key("运维", "正文").startswith("manual:")
    assert len(manual_source_key("运维", "正文").split(":")[1]) == 12
    assert content_hash("运维", "正文") != manual_source_key("运维", "正文")  # 不同列，别混


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def kb(monkeypatch, tmp_path) -> Iterator[SimpleNamespace]:
    """隔离 schema 的真库 + 控制台客户端；用完 DROP SCHEMA CASCADE。"""
    yield from _kb_with(monkeypatch, tmp_path, DDL)


@pytest.fixture()
def legacy_kb(monkeypatch, tmp_path) -> Iterator[SimpleNamespace]:
    """**迁移之前**的老表（没有 content_type 列）—— 缺列窗口期的读/写行为。"""
    yield from _kb_with(monkeypatch, tmp_path, DDL_NO_CTYPE)


def _kb_with(monkeypatch, tmp_path, ddl) -> Iterator[SimpleNamespace]:
    if not _pg_ready:
        pytest.skip("无 8.140.192.91/keepsake_test 凭据，跳过 PG 真库")
    schema = f"ks_kb_{uuid.uuid4().hex[:12]}"
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


def raw(kb: SimpleNamespace, sql: str, params: Any = ()) -> List[tuple]:
    """直连隔离 schema 查一行断言（不经过 API）。"""
    with psycopg.connect(kb.dsn, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(sql.format(s=kb.schema), params)
        return list(cur.fetchall())


def seed_skill(kb: SimpleNamespace, title: str = "灌入器技能文档") -> int:
    """手工塞一篇 skill 域文档（模拟灌入器的产物：source_type=skill 且 content_type=skill）。"""
    with psycopg.connect(kb.dsn, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{kb.schema}".documents'
                    '(source_type, source_key, title, meta, content_hash, updated_at'
                    + (", content_type" if _has_ctype(kb) else "")
                    + ")"
                    " VALUES ('skill','node/skill/a.md',%s,'{}'::jsonb,'sha:x:a.md',now()"
                    + (", 'skill'" if _has_ctype(kb) else "")
                    + ") RETURNING id", [title])
        doc_id = cur.fetchone()[0]
        cur.execute(f'INSERT INTO "{kb.schema}".chunks(document_id, ord, text, source_path)'
                    ' VALUES (%s, 0, %s, %s)', [doc_id, "技能正文", "node/skill/a.md"])
    return doc_id


def _has_ctype(kb: SimpleNamespace) -> bool:
    return bool(raw(kb, "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema='{s}' AND table_name='documents' "
                        "AND column_name='content_type'"))


def create(client: TestClient, title: str, content: str, **kw: Any) -> Dict[str, Any]:
    r = client.post(CREATE, json={"confirm": True, "title": title,
                                  "content": content, **kw}, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def docs(client: TestClient, q: str = "", source_type: str = "",
         content_type: str = "", tag: str = "") -> Dict[str, Any]:
    r = client.get(f"/api/v1/kb/documents?page_size=100" + (f"&q={q}" if q else "")
                   + (f"&source_type={source_type}" if source_type else "")
                   + (f"&content_type={content_type}" if content_type else "")
                   + (f"&tag={tag}" if tag else ""),
                   headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def search(client: TestClient, q: str) -> Dict[str, Any]:
    r = client.get(f"/api/v1/kb/search?q={q}&page_size=100", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def snippets(client: TestClient, q: str) -> List[str]:
    return [i["snippet"] for i in search(client, q)["items"]]


# ---------------------------------------------------------------- 认证 / 守卫

@PG_ONLY
def test_四个写端点都要会话(kb):
    """漏挂 require_session 就是裸奔写面 —— 逐条打一遍（401，不是 400/500）。"""
    cases = [(CREATE, {"confirm": True, "title": "偷偷写", "content": "偷偷写"}),
             ("/api/v1/kb/documents/1/update", {"confirm": True, "content": "偷偷改"}),
             ("/api/v1/kb/documents/1/delete", {"confirm": "偷偷删"}),
             (IMPORT, {"items": [{"title": "偷偷导", "content": "偷偷导"}]}),
             (EXPORT, None)]
    for url, body in cases:
        r = kb.client.get(url) if body is None else kb.client.post(url, json=body)
        assert r.status_code == 401, (url, r.status_code)
        assert r.json()["detail"]["error"] == "missing_token"
        assert TOKEN not in r.text


def test_写端点守卫齐():
    """🔴 静态判据：4 条 POST 挂 write_guard，导出（只读）只挂 require_session。

    🆕 批2 审核：`create` / `update` 的认证依赖从 `require_session` 换成了
    `writer_identity`（**站内会话照旧放行，代理身份另判**，站内路径体验零变化）；
    仍然必须挂一个**认会话**的依赖 —— 换成什么都行，漏了不行。
    `delete` / `import` / 审核两端点（approve/reject）不接受代理身份，仍是纯
    `require_session`（审核动作只能由控制台的人做）。
    """
    app = create_app()
    want = {CREATE: ("POST", True),
            "/api/v1/kb/documents/{id}/update": ("POST", True),
            "/api/v1/kb/documents/{id}/delete": ("POST", True),
            IMPORT: ("POST", True),
            EXPORT: ("GET", False)}
    session_dep = ("require_session", "writer_identity")
    console_only = ("/api/v1/kb/documents/{id}/delete", IMPORT,
                    "/api/v1/kb/documents/{id}/approve",
                    "/api/v1/kb/documents/{id}/reject", EXPORT)
    seen = set()
    for r in app.routes:
        path = getattr(r, "path", "")
        if path not in want:
            continue
        method, need_guard = want[path]
        if method not in (getattr(r, "methods", set()) or ()):
            continue          # 同路径的另一个方法（GET /kb/documents）不算
        names = {getattr(d.call, "__name__", "") for d in r.dependant.dependencies}
        assert any(any(k in n for k in session_dep) for n in names), path
        if path in console_only:
            # 审核面与批量工具**不接代理身份**：只能控制台会话
            assert any("require_session" in n for n in names), path
            assert not any("writer_identity" in n for n in names), path
        assert any("write_guard" in n for n in names) is need_guard, (path, names)
        seen.add(path)
    assert seen == set(want), f"有端点没注册：{set(want) - seen}"


# ---------------------------------------------------------------- 新增 / 检索

@PG_ONLY
def test_新增_落库且能被两个只读接口检索到(kb):
    body = create(kb.client, "值班手册", "# 值班\n联系人是张三\n")
    assert body["ok"] is True and body["source_key"].startswith("manual:")
    assert body["chunks_count"] == 1
    assert [d["title"] for d in docs(kb.client, "值班")["items"]] == ["值班手册"]
    assert any("张三" in s for s in snippets(kb.client, "张三"))


@PG_ONLY
def test_新增_同内容重复提交是unchanged不重复入库(kb):
    first = create(kb.client, "值班手册", "联系人是张三")
    second = create(kb.client, "值班手册", "联系人是张三")
    assert (first["status"], second["status"]) == ("added", "unchanged")
    assert first["id"] == second["id"]
    assert len(docs(kb.client)["items"]) == 1


@PG_ONLY
@pytest.mark.parametrize("payload,error", [
    ({"confirm": True, "title": "", "content": "正文"}, "empty_document"),
    ({"confirm": True, "title": "标题", "content": "   "}, "empty_document"),
    ({"title": "标题", "content": "正文"}, "confirm_required"),
])
def test_新增_缺confirm或空内容_400(kb, payload, error):
    r = kb.client.post(CREATE, json=payload, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == error


@PG_ONLY
def test_新增_审计留痕(kb):
    create(kb.client, "值班手册", "联系人是张三")
    files = list(Path(os.environ["CONSOLE_AUDIT_DIR"]).glob("audit-*.jsonl"))
    assert files, "写操作必须留审计行"
    body = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert body["action"] == "kb.document.create" and body["ok"] is True


# ---------------------------------------------------------------- 列表的 source_type

@PG_ONLY
def test_列表带source_type_且skill域值不冒充manual(kb):
    """🔴 列表必须投影 source_type —— 前端按它判「写按钮只给手动域」；
    字段缺席时前端判据落空（曾经的实锤 bug：skill 文档渲染出编辑/删除）。"""
    create(kb.client, "值班手册", "联系人是张三")
    skill_id = seed_skill(kb)
    items = docs(kb.client)["items"]
    assert {i["source_type"] for i in items} == {"manual", "skill"}
    by_title = {i["title"]: i for i in items}
    assert by_title["灌入器技能文档"]["doc_id"] == str(skill_id)
    assert by_title["灌入器技能文档"]["source_type"] != "manual"


@PG_ONLY
def test_列表按source_type过滤_只回本域且计数同口径(kb):
    create(kb.client, "值班手册", "联系人是张三")
    seed_skill(kb)
    only_manual = docs(kb.client, source_type="manual")
    assert [i["source_type"] for i in only_manual["items"]] == ["manual"]
    assert only_manual["count"] == 1                    # count 走过滤后口径，不是全库 2
    assert [i["source_type"] for i in docs(kb.client, source_type="skill")["items"]] == ["skill"]
    # 与 q 同时给 = AND：词命中手动文档，但按 skill 过滤 ⇒ 空
    assert docs(kb.client, q="值班", source_type="skill")["items"] == []
    assert [i["title"] for i in docs(kb.client, q="值班", source_type="manual")["items"]] == ["值班手册"]


def test_列表source_type过滤_精确匹配且参数化_与q取AND():
    """🔴 不碰库的接线判据：把 WHERE 拼装与参数钉死。

    * 语义必须**精确** `= %s`（与导出端点 `kb_export?source_type=` 同一口径），
      值走参数不拼串；拼串就等于把表内容当代码解释；
    * 与 q 同时给时 AND，`count` 复用同一条 WHERE；
    * SELECT 必须带 `source_type AS source_type`，否则出参字段恒空串。
    """
    calls: List[tuple] = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            calls.append((" ".join(sql.split()), params))
        def fetchall(self):
            return [{"doc_id": "1", "title": "值班手册", "updated_at": "2026-10-15 00:00:00+00",
                     "source_type": "manual", "chunk_count": 3}]
        def fetchone(self):
            return {"n": 1}

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    cols = {"documents": {"id", "source_type", "title", "updated_at"},
            "chunks": {"id", "document_id", "text"}}
    saved_cols, saved_conn = kb_router._table_columns, kb_router._connect
    kb_router._table_columns = lambda schema, tables: cols   # type: ignore[assignment]
    kb_router._connect = lambda: _Conn()                     # type: ignore[assignment]
    try:
        body = kb_router.kb_documents(q="值班", source_type="manual", page=1, page_size=20)
    finally:
        kb_router._table_columns = saved_cols                # type: ignore[assignment]
        kb_router._connect = saved_conn                     # type: ignore[assignment]

    list_sql, list_params = calls[0]
    count_sql, count_params = calls[1]
    assert "source_type AS source_type" in list_sql
    assert "d.source_type = %s" in list_sql and "LIKE" in list_sql and " AND " in list_sql
    assert "manual" not in list_sql.replace("source_type", "")      # 值没进 SQL 文本
    assert list_params == ["%值班%", "manual", 20, 0]
    # 列表 SQL 的第一个 WHERE 在 chunk_count 子查询里 ⇒ 取最后一个（rsplit）
    assert count_sql.rsplit("WHERE", 1)[1].strip() == \
        list_sql.rsplit("WHERE", 1)[1].split("ORDER BY")[0].strip()
    assert count_params == list_params[:2]                 # count 同一条 WHERE、同一组参数
    assert body.items[0].source_type == "manual"
    assert body.count == 1
    # 空串不出参（拿不到来源就不假装有值）；前端严格判据把「缺席」与「非 manual」同样当只读
    assert body.model_dump()["items"][0]["source_type"] == "manual"
    assert "source_type" not in kb_router.KbDocument(doc_id="x").model_dump()


# ---------------------------------------------------------------- 改

@PG_ONLY
def test_改正文_旧词消失新词可搜_且chunks重切(kb):
    doc = create(kb.client, "值班手册", "# 值班\n原先写的是阿里云\n")
    old_id = doc["id"]
    r = kb.client.post(f"/api/v1/kb/documents/{old_id}/update",
                       json={"confirm": True, "content": "# 值班\n改成火山引擎了\n"},
                       headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["chunks_count"] == 1
    # 🔴 双向断言：只断「新词能搜」会漏掉「索引整个空了」
    assert not any("阿里云" in s for s in snippets(kb.client, "阿里云"))
    assert any("火山引擎" in s for s in snippets(kb.client, "火山引擎"))
    assert len(raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s', [old_id])) == 1


@PG_ONLY
def test_改正文_切片数随超长变化(kb):
    body = "# 值班\n" + "内容" * 2000
    doc = create(kb.client, "长文", body)
    assert doc["chunks_count"] > 1
    r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/update",
                       json={"confirm": True, "content": "短正文"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["chunks_count"] == 1
    assert len(raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s', [doc["id"]])) == 1


@PG_ONLY
def test_只改标题_不动chunks也不换source_key(kb):
    doc = create(kb.client, "值班手册", "联系人是张三")
    r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/update",
                       json={"confirm": True, "title": "值班手册（2026）"}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_key"] == doc["source_key"]      # 键是身份，不因改标题而变
    assert body["chunks_count"] == doc["chunks_count"]
    assert [d["title"] for d in docs(kb.client, "2026")["items"]] == ["值班手册（2026）"]


@PG_ONLY
@pytest.mark.parametrize("payload,status,error", [
    ({"confirm": False, "content": "x"}, 400, "confirm_required"),
    ({}, 400, "confirm_required"),
    ({"confirm": True}, 400, "nothing_to_update"),
    ({"confirm": True, "content": "  "}, 400, "empty_document"),
    ({"confirm": True, "title": "  "}, 400, "empty_document"),
])
def test_改_参数不合法_400(kb, payload, status, error):
    doc = create(kb.client, "值班手册", "联系人是张三")
    r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/update", json=payload, headers=AUTH)
    assert r.status_code == status, r.text
    assert r.json()["detail"]["error"] == error


@PG_ONLY
def test_改_文档不存在_404_非整数id_400(kb):
    r = kb.client.post("/api/v1/kb/documents/999999/update",
                       json={"confirm": True, "content": "x"}, headers=AUTH)
    assert r.status_code == 404 and r.json()["detail"]["error"] == "kb_doc_missing"
    r = kb.client.post("/api/v1/kb/documents/not-a-number/update",
                       json={"confirm": True, "content": "x"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "kb_doc_id_invalid"


@PG_ONLY
def test_改skill文档_409(kb):
    """skill 域文档由灌入器维护：改了当晚就被覆盖/清理，控制台直接拒。"""
    doc_id = seed_skill(kb)
    r = kb.client.post(f"/api/v1/kb/documents/{doc_id}/update",
                       json={"confirm": True, "content": "偷偷改"}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "kb_doc_immutable"
    assert raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s',
               [doc_id]) == [("技能正文",)]


# ---------------------------------------------------------------- 真删

@PG_ONLY
def test_真删_confirm不对_400且文档还在(kb):
    doc = create(kb.client, "值班手册", "联系人是张三")
    for confirm, error in (("", "confirm_required"), ("值班手册 ", "confirm_mismatch"),
                           ("不对的标题", "confirm_mismatch")):
        r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/delete",
                           json={"confirm": confirm}, headers=AUTH)
        assert r.status_code == 400, confirm
        assert r.json()["detail"]["error"] == error
    assert len(docs(kb.client)["items"]) == 1


@PG_ONLY
def test_真删_删干净且备份含全文(kb):
    doc = create(kb.client, "值班手册", "# 值班\n联系人是张三\n")
    r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/delete",
                       json={"confirm": "值班手册"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert docs(kb.client)["items"] == []
    assert not snippets(kb.client, "张三")
    # chunks 不许残留（外键没建级联时最容易漏）
    assert raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s', [doc["id"]]) == []
    backup = json.loads(Path(r.json()["backup"]).read_text(encoding="utf-8"))
    assert "张三" in "".join(backup["document"]["chunks"])
    assert backup["document"]["document"]["title"] == "值班手册"


@PG_ONLY
def test_删_文档不存在_404_删skill文档_409(kb):
    r = kb.client.post("/api/v1/kb/documents/999999/delete",
                       json={"confirm": "值班手册"}, headers=AUTH)
    assert r.status_code == 404 and r.json()["detail"]["error"] == "kb_doc_missing"
    doc_id = seed_skill(kb)
    r = kb.client.post(f"/api/v1/kb/documents/{doc_id}/delete",
                       json={"confirm": "灌入器技能文档"}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "kb_doc_immutable"
    assert raw(kb, 'SELECT title FROM "{s}".documents WHERE id=%s', [doc_id])


# ---------------------------------------------------------------- 导入

@PG_ONLY
def test_导入_dry_run零写入并报计数(kb):
    r = kb.client.post(IMPORT, json={"items": [{"title": "A", "content": "正文甲"},
                                                {"title": "B", "content": "正文乙"}]},
                       headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["dry_run"], body["total"], body["would_add"], body["unchanged"]) == \
        (True, 2, 2, 0)
    assert body["confirm_required"] == "2"
    assert docs(kb.client)["items"] == []          # dry-run 零写入


@PG_ONLY
def test_导入_实跑确认串不符_一条都不写(kb):
    r = kb.client.post(IMPORT, json={"items": [{"title": "A", "content": "正文甲"}],
                                     "dry_run": False, "confirm": "9"}, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "confirm_mismatch"
    assert docs(kb.client)["items"] == []


@PG_ONLY
def test_导入_实跑后重复导入unchanged(kb):
    items = [{"title": "A", "content": "正文甲"}, {"title": "B", "content": "正文乙"},
             {"title": "空", "content": "  "}]
    r = kb.client.post(IMPORT, json={"items": items, "dry_run": False, "confirm": "2"},
                       headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 2 and body["rejected_empty"] == 1
    assert len(docs(kb.client)["items"]) == 2
    again = kb.client.post(IMPORT, json={"items": items}, headers=AUTH).json()
    assert (again["would_add"], again["unchanged"]) == (0, 2)
    assert len(docs(kb.client)["items"]) == 2       # 没重复入库


@PG_ONLY
def test_导入_条目带source_type_422(kb):
    """导入只进手动域：让调用方指定来源类型 ⇒ 422，不给「往 skill 域灌数据」的口子。"""
    r = kb.client.post(IMPORT, json={"items": [{"title": "A", "content": "正文",
                                                 "source_type": "skill"}]}, headers=AUTH)
    assert r.status_code == 422


@PG_ONLY
def test_导入_超限_400不截断(kb):
    r = kb.client.post(IMPORT, json={"items": [{"title": "A", "content": "正文"}] * 2001},
                       headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "import_too_large"


# ---------------------------------------------------------------- 导出

@PG_ONLY
def test_导出_jsonl每行一篇带切片(kb):
    create(kb.client, "值班手册", "联系人是张三")
    seed_skill(kb)
    r = kb.client.get(EXPORT, headers=AUTH)
    assert r.status_code == 200, r.text
    rows = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    assert len(rows) == 2
    manual = [x for x in rows if x["source_type"] == "manual"][0]
    assert manual["title"] == "值班手册" and "张三" in "".join(manual["chunks"])
    assert manual["meta"] == {"origin": "console"}


@PG_ONLY
def test_导出_按source_type过滤与limit护栏(kb):
    create(kb.client, "值班手册", "联系人是张三")
    seed_skill(kb)
    only_skill = kb.client.get(f"{EXPORT}?source_type=skill", headers=AUTH).text
    assert [json.loads(l)["source_type"] for l in only_skill.splitlines()] == ["skill"]
    assert kb.client.get(f"{EXPORT}?limit=0", headers=AUTH).status_code == 422
    assert kb.client.get(f"{EXPORT}?limit=20001", headers=AUTH).status_code == 422
    r = kb.client.get(f"{EXPORT}?source_type=bad%20type", headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "bad_source_type"


def test_导出切片查询的id入参恒为int_不靠驱动隐式适配():
    """🔴 跨环境回归钉子（不碰库）：文档 id 是 `CAST(id AS text)` 查出来的 **str**，
    若原样进 `= ANY(%s)`，psycopg2 适配成 text[] ⇒ `bigint = text` 无操作符炸导出；
    psycopg3 传未定型字面量让 PG 自行推 bigint[] ⇒ 假绿。所以判据只能钉在
    「传参前 int 化 + SQL 显式 cast」这两件事上，与装的是哪个驱动无关。
    """
    calls: List[tuple] = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            calls.append((" ".join(sql.split()), params))
        def fetchall(self):
            sql = calls[-1][0]
            if "AS did" in sql:                      # 第二条：切片查询
                return [{"did": "1", "chunk_text": "切片甲"}, {"did": "2", "chunk_text": "切片乙"}]
            return [{"id": "1", "source_type": "manual", "source_key": "manual:aaa",
                     "title": "值班手册", "meta": {}},
                    {"id": "2", "source_type": "skill", "source_key": "s/b.md",
                     "title": "技能文档", "meta": None}]

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkey_names = {"src_type": "source_type", "src_key": "source_key", "title": "title",
                    "meta": "meta", "doc_id": "id", "chunk_doc": "document_id",
                    "chunk_text": "text", "chunk_ord": "ord",
                    "documents": "documents", "chunks": "chunks"}
    saved = kb_router._connect
    kb_router._connect = lambda: _Conn()             # type: ignore[assignment]
    try:
        lines = list(kb_router._export_lines("ks", monkey_names, "", 100))
    finally:
        kb_router._connect = saved

    chunk_sql, chunk_params = next((s, p) for s, p in calls if "AS did" in s)
    assert "= ANY(%s::bigint[])" in chunk_sql, chunk_sql
    assert chunk_params == [[1, 2]], f"id 入参必须是 int 列表，实得 {chunk_params!r}"
    # str 形态的文档 id（CAST 出来的）仍能正确落回每一篇
    rows = [json.loads(line) for line in lines]
    assert [r["chunks"] for r in rows] == [["切片甲"], ["切片乙"]]
    assert [r["id"] for r in rows] == ["1", "2"]        # 出参仍是 str


# ---------------------------------------------------------------- 内容类型（content_type）

# 纯函数用例（不碰库，任何环境都跑）：切块注册表是「唯一判据来源」

def test_chunk_document_注册表_文本类逐字走split_chunks():
    """🔴 note/article/skill 的切块必须与 `split_chunks` **一字不差**：
    分类只是分池子，切块粒度不许趁机改（否则老文档的检索命中会整体漂移）。"""
    text = "## 第一段\n内容一\n## 第二段\n内容二\n"
    for ct in ("skill", "note", "article", "image"):
        assert chunk_document(text, ct) == split_chunks(text), ct
    assert chunk_document("", "note") == [""]            # 空文兜底仍在
    assert [len(c) for c in chunk_document("a" * 2500, "note")] == [1200, 1200, 100]


def test_chunk_document_link_单块不切正文():
    """🔴 link = 一条记录一个块：`标题\\nURL\\n摘要`。
    切碎的后果是命中后半截只有裸 URL、snippet 是断头链接。"""
    assert chunk_document("一篇讲 PG 的文章", "link",
                          title="PG 教程", url="https://example.com/pg") == \
        ["PG 教程\nhttps://example.com/pg\n一篇讲 PG 的文章"]
    # 摘要可空 ⇒ 省略该行（不留悬空尾巴）
    assert chunk_document("", "link", title="空摘要", url="https://a.example") == \
        ["空摘要\nhttps://a.example"]
    # 再长也不切（刻意为之：链接是一张卡片，不是一篇文档）
    long_summary = "摘" * 5000
    assert len(chunk_document(long_summary, "link", title="长", url="https://a.example")) == 1


def test_chunk_document_未登记类型退文本_不抛():
    """脏值/未来新增类型不许把切块整条路炸掉（存量行可能是任何值）。"""
    assert chunk_document("## 甲\n内容", "future_type") == split_chunks("## 甲\n内容")


def test_link_body_逐行拼装_跳过空段():
    assert link_body("标题", "https://a.example", "摘要") == "标题\nhttps://a.example\n摘要"
    assert link_body("标题", "https://a.example", "") == "标题\nhttps://a.example"


# 落库形状（真库）

@PG_ONLY
def test_新增_note缺省类型_列表出参带content_type(kb):
    """不传 content_type ⇒ note（存量行的缺省口径），出参必须带出这根字段
    （前端 Tab 的判据落在它上面；字段缺席会让分类视图落空）。"""
    body = create(kb.client, "值班手册", "联系人是张三")
    assert body["content_type"] == "note"
    assert docs(kb.client)["items"][0]["content_type"] == "note"
    assert raw(kb, 'SELECT content_type FROM "{s}".documents WHERE id=%s',
               [body["id"]]) == [("note",)]


@PG_ONLY
def test_新增_link_单块正文与meta_不抓网页(kb):
    """link = 标题 + URL + 摘要 存**一块**；meta 存可寻址的 url/summary。
    🔴 不抓网页：库里不出现任何抓来的正文，只有调用方给的摘要。"""
    body = create(kb.client, "PG 教程", "一篇讲 PG 的文章",
                  content_type="link", url="https://example.com/pg")
    assert body["content_type"] == "link" and body["chunks_count"] == 1
    assert raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s ORDER BY ord',
               [body["id"]]) == [("PG 教程\nhttps://example.com/pg\n一篇讲 PG 的文章",)]
    assert raw(kb, 'SELECT meta FROM "{s}".documents WHERE id=%s',
               [body["id"]]) == [({"origin": "console", "url": "https://example.com/pg",
                                   "summary": "一篇讲 PG 的文章"},)]


@PG_ONLY
def test_新增_link_摘要可空_仍单块(kb):
    body = create(kb.client, "空摘要卡", "", content_type="link", url="https://a.example")
    assert body["chunks_count"] == 1
    assert raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s',
               [body["id"]]) == [("空摘要卡\nhttps://a.example",)]


@PG_ONLY
def test_同名同摘要_手记与链接不撞键不互相改型(kb):
    """🔴 「类型创建后不可变」在**写路径**也要成立：手记与链接拿同一个
    标题+摘要时，若内容键只按 `content` 算，两篇会算出同一个 `source_key`，
    `ON CONFLICT` 撞上就把先建的那篇悄悄改成后建的类型（用户看着「新增成功」，
    老那篇被改了型）。链接的键必须带上 URL。"""
    note = create(kb.client, "同一篇", "同一段摘要")
    link = create(kb.client, "同一篇", "同一段摘要",
                  content_type="link", url="https://a.example")
    assert note["id"] != link["id"]
    got = raw(kb, 'SELECT title, content_type FROM "{s}".documents ORDER BY id')
    assert got == [("同一篇", "note"), ("同一篇", "link")]
    # 反过来：先建链接再建手记，同理两行
    k2 = create(kb.client, "另一篇", "甲", content_type="link", url="https://b.example")
    n2 = create(kb.client, "另一篇", "甲")
    assert k2["id"] != n2["id"]


@PG_ONLY
@pytest.mark.parametrize("payload,error", [
    ({"content_type": "link"}, "url_required"),
    ({"content_type": "link", "url": "ftp://a.example"}, "bad_url"),
    ({"content_type": "link", "url": "javascript:alert(1)"}, "bad_url"),
    ({"content_type": "link", "url": "example.com/pg"}, "bad_url"),
    ({"content_type": "image"}, "image_not_open"),
    ({"content_type": "skill"}, "content_type_immutable"),
    ({"content_type": "wat"}, "bad_content_type"),
])
def test_新增_类型与链接校验_400(kb, payload, error):
    """link 必给合法 url；image 暂未开放；skill 归灌入器；未知类型 400。"""
    r = kb.client.post(CREATE, json={"confirm": True, "title": "标题",
                                     "content": "正文", **payload}, headers=AUTH)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == error
    assert docs(kb.client)["items"] == []          # 被拒的一条都不许落库


@PG_ONLY
def test_新增_link带非法url_正文不入库(kb):
    r = kb.client.post(CREATE, json={"confirm": True, "title": "标题", "content": "正文",
                                     "content_type": "link", "url": "http://ok"}, headers=AUTH)
    assert r.status_code == 200
    # http:// 是合法形态（只禁 https 之外的伪协议）
    assert r.json()["content_type"] == "link"


@PG_ONLY
def test_列表按content_type过滤_只回本类型且计数同口径(kb):
    """过滤口径与 source_type 同款：精确 `= %s`，count 走过滤后总数；
    与 q 同时给 = AND。"""
    create(kb.client, "手记一篇", "甲")
    create(kb.client, "文章一篇", "乙", content_type="article")
    create(kb.client, "链接一张", "丙", content_type="link", url="https://a.example")
    seed_skill(kb)
    notes = docs(kb.client, content_type="note")
    assert [i["title"] for i in notes["items"]] == ["手记一篇"]
    assert notes["count"] == 1                       # 不是全库 4
    assert {i["title"] for i in docs(kb.client, content_type="skill")["items"]} == \
        {"灌入器技能文档"}
    assert [i["title"] for i in docs(kb.client, content_type="link")["items"]] == ["链接一张"]
    # 与 q 同时给 = AND：q 走**标题**（列表接口的口径，不搜正文），
    # 标题命中但类型不匹配 ⇒ 空
    assert docs(kb.client, q="一篇", content_type="link")["items"] == []
    assert [i["title"] for i in docs(kb.client, q="一篇", content_type="note")["items"]] == \
        ["手记一篇"]


@PG_ONLY
def test_列表content_type过滤_未知值400不静默给全量(kb):
    """枚举外的值 400（不静默当「全部」—— 那是假装筛过）。"""
    r = kb.client.get("/api/v1/kb/documents?content_type=wat", headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "bad_content_type"


@PG_ONLY
def test_类型计数接口_与库里同口径(kb):
    """Tab 上的计数必须来自库里（前端不写死数字）。"""
    create(kb.client, "手记一篇", "甲")
    create(kb.client, "文章一篇", "乙", content_type="article")
    seed_skill(kb)
    body = kb.client.get("/api/v1/kb/type-counts", headers=AUTH)
    assert body.status_code == 200, body.text
    counts = body.json()["counts"]
    assert counts == {"note": 1, "article": 1, "skill": 1}
    assert sum(counts.values()) == docs(kb.client)["count"]


@PG_ONLY
def test_改_按文档自身类型重切_link改摘要仍单块(kb):
    """🔴 重切分派读**库里那行**的类型，不读请求里的：link 改摘要仍是一块，
    组合文本三行齐全（URL 不许被改没了）。"""
    doc = create(kb.client, "PG 教程", "旧摘要",
                 content_type="link", url="https://example.com/pg")
    r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/update",
                       json={"confirm": True, "content": "新摘要"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["chunks_count"] == 1 and r.json()["content_type"] == "link"
    assert raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s',
               [doc["id"]]) == [("PG 教程\nhttps://example.com/pg\n新摘要",)]
    # meta 里的 url 保留（没传 url 就不能丢）
    assert raw(kb, 'SELECT meta FROM "{s}".documents WHERE id=%s',
               [doc["id"]]) == [({"origin": "console", "url": "https://example.com/pg",
                                   "summary": "新摘要"},)]


@PG_ONLY
def test_改_link换url_组合文本与meta同步(kb):
    doc = create(kb.client, "PG 教程", "摘要",
                 content_type="link", url="https://old.example/pg")
    r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/update",
                       json={"confirm": True, "url": "https://new.example/pg"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["chunks_count"] == 1
    assert raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s',
               [doc["id"]]) == [("PG 教程\nhttps://new.example/pg\n摘要",)]
    assert raw(kb, 'SELECT meta->>\'url\' FROM "{s}".documents WHERE id=%s',
               [doc["id"]]) == [("https://new.example/pg",)]


@PG_ONLY
@pytest.mark.parametrize("payload,error", [
    # 空 URL 串 = 想改 URL 却给不出 ⇒ 400，不许悄悄把地址清空
    ({"url": "   "}, "url_required"),
    ({"url": "nope"}, "bad_url"),
    # 枚举外的类型与「换类型」是两回事，分别给两种错
    ({"title": "x", "content_type": "wat"}, "bad_content_type"),
    ({"title": "x", "content_type": "note"}, "content_type_immutable"),
    # 只传 content_type 什么也没改 ⇒ nothing_to_update
    ({"content_type": "link"}, "nothing_to_update"),
])
def test_改_link参数校验_400(kb, payload, error):
    doc = create(kb.client, "PG 教程", "摘要",
                 content_type="link", url="https://example.com/pg")
    r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/update",
                       json={"confirm": True, **payload}, headers=AUTH)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == error
    # 一条都不许改（正文块原封不动）
    assert raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s',
               [doc["id"]]) == [("PG 教程\nhttps://example.com/pg\n摘要",)]


@PG_ONLY
def test_改_note带url_400(kb):
    """url 只对 link 有意义：手记上传 url ⇒ 400（不是静默忽略，那会让人以为改成了链接）。"""
    doc = create(kb.client, "手记一篇", "甲")
    r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/update",
                       json={"confirm": True, "url": "https://a.example"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "url_not_allowed"


@PG_ONLY
@pytest.mark.parametrize("want", ["note", "article", "link", "skill", "image", ""])
def test_改_带异类型400_类型创建后不可变(kb, want):
    """🔴 类型双挡（后端这层）：请求带 content_type 且与库里不同 ⇒ 400。
    静默改类型 = 让「这篇是链接」和「切片早已被当长文切过」并存。"""
    doc = create(kb.client, "值班手册", "联系人是张三")
    payload = {"confirm": True, "title": "值班手册（新）"}
    if want:
        payload["content_type"] = want
    r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/update", json=payload, headers=AUTH)
    if want in ("note", ""):
        assert r.status_code == 200, r.text        # 同类型放行（前端编辑弹框回传同值）
        assert r.json()["content_type"] == "note"
    else:
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "content_type_immutable"
        assert docs(kb.client)["items"][0]["title"] == "值班手册"   # 标题也没改


@PG_ONLY
def test_改_note正文_仍按split_chunks重切(kb):
    """回归：分类不许顺手改切块粒度 —— 超长正文切块数与上期一致。"""
    doc = create(kb.client, "长文", "# 值班\n" + "内容" * 2000)
    assert doc["chunks_count"] > 1
    r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/update",
                       json={"confirm": True, "content": "# 值班\n" + "新内容" * 2000},
                       headers=AUTH)
    assert r.status_code == 200 and r.json()["chunks_count"] > 1
    assert len(raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s',
                   [doc["id"]])) > 1


@PG_ONLY
def test_导入_条目可带type_默认note(kb):
    items = [{"title": "A", "content": "正文甲"},
             {"title": "B", "content": "正文乙", "type": "article"},
             {"title": "卡", "content": "摘要丙", "type": "link", "url": "https://a.example"}]
    r = kb.client.post(IMPORT, json={"items": items, "dry_run": False, "confirm": "3"},
                       headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 3
    got = {i["title"]: i["content_type"] for i in docs(kb.client)["items"]}
    assert got == {"A": "note", "B": "article", "卡": "link"}


@PG_ONLY
def test_导入_link单块且条目校验与创建同口径(kb):
    kb.client.post(IMPORT, json={"items": [{"title": "卡", "content": "摘要",
                                            "type": "link", "url": "https://a.example"}],
                                 "dry_run": False, "confirm": "1"}, headers=AUTH)
    rows = raw(kb, 'SELECT text FROM "{s}".chunks ORDER BY ord')
    assert rows == [("卡\nhttps://a.example\n摘要",)]
    # 缺 url / 非法 url / image 一律 400（口径与创建端点同款）
    for item, error in (({"title": "x", "content": "y", "type": "link"}, "url_required"),
                        ({"title": "x", "content": "y", "type": "link",
                          "url": "ftp://a"}, "bad_url"),
                        ({"title": "x", "content": "y", "type": "image"}, "image_not_open"),
                        ({"title": "x", "content": "y", "type": "skill"},
                         "content_type_immutable")):
        r = kb.client.post(IMPORT, json={"items": [item]}, headers=AUTH)
        assert r.status_code == 400, item
        assert r.json()["detail"]["error"] == error


@PG_ONLY
def test_导出_每行带content_type(kb):
    create(kb.client, "手记一篇", "甲")
    create(kb.client, "链接一张", "摘要", content_type="link", url="https://a.example")
    seed_skill(kb)
    rows = [json.loads(line) for line in
            kb.client.get(EXPORT, headers=AUTH).text.splitlines() if line.strip()]
    got = {r["title"]: r["content_type"] for r in rows}
    assert got == {"手记一篇": "note", "链接一张": "link", "灌入器技能文档": "skill"}


@PG_ONLY
def test_检索结果带content_type(kb):
    """检索命中行要带得出类型（前端切片卡片渲染类型徽标靠它）。"""
    create(kb.client, "链接一张", "讲讲分页", content_type="link", url="https://a.example")
    item = next(i for i in search(kb.client, "分页")["items"])
    assert item["content_type"] == "link"
    assert item["snippet"].startswith("链接一张\nhttps://a.example")


# ---------------------------------------------------------------- 迁移形状 / 缺列防御

@PG_ONLY
def test_测试DDL与生产迁移同形状_含content_type列(kb):
    """测试库必须与生产**同形状**（含 NOT NULL + DEFAULT），否则测试是假绿：
    生产那根列没有 DEFAULT 时，旧灌入器版本的 INSERT 会直接炸。"""
    cols = raw(kb, "SELECT column_name, is_nullable, column_default"
                   " FROM information_schema.columns"
                   " WHERE table_schema='{s}' AND table_name='documents'"
                   " AND column_name='content_type'")
    assert cols == [("content_type", "NO", "'note'::text")]


@PG_ONLY
def test_迁移SQL_幂等且真能把老表迁起来(legacy_kb):
    """🔴 迁移形状探针：隔离 schema 里造一张**没有 content_type 的老表** →
    跑生产迁移 SQL → 列出现且存量行按来源回填（skill/skill_hk ⇒ skill，其余 ⇒ note）。
    再跑一遍 ⇒ 幂等不炸。"""
    assert not _has_ctype(legacy_kb)                       # 起点确实是老表
    with psycopg.connect(legacy_kb.dsn, autocommit=True, connect_timeout=30) as conn, \
            conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{legacy_kb.schema}".documents'
                    "(source_type, source_key, title) VALUES"
                    " ('skill','n/s/a.md','技能'),('skill_hk','n/s/b.md','技能手册'),"
                    " ('manual','manual:x','我手写的')")
        for stmt in MIGRATE_SQL:                           # 跑两遍：幂等
            cur.execute(stmt.format(s=legacy_kb.schema))
            cur.execute(stmt.format(s=legacy_kb.schema))
    assert _has_ctype(legacy_kb)
    got = raw(legacy_kb, 'SELECT title, content_type FROM "{s}".documents ORDER BY title')
    assert got == [("我手写的", "note"), ("技能", "skill"), ("技能手册", "skill")]


@PG_ONLY
def test_缺列_写端点人话报错不裸崩(legacy_kb):
    """🔴 防「主脑忘了先迁移」的窗口期：老库上新增/导入**不许**拿到 PG 的
    `column "content_type" does not exist` 裸 500，要给带迁移 SQL 的人话 500。"""
    for url, body in ((CREATE, {"confirm": True, "title": "t", "content": "c"}),
                      (IMPORT, {"items": [{"title": "t", "content": "c"}]})):
        r = legacy_kb.client.post(url, json=body, headers=AUTH)
        assert r.status_code == 500, (url, r.status_code)
        detail = r.json()["detail"]
        assert detail["error"] == "kb_schema_unknown"
        assert "content_type" in detail["message"] and "ALTER TABLE" in detail["message"]


@PG_ONLY
def test_缺列_读路径降级不炸(legacy_kb):
    """🔴 老库上只读端点照常跑：列表/检索出参**不带** content_type 键（缺省不假装有值），
    按类型过滤 400（筛不了就说筛不了），类型计数给空字典（前端不编数字）。"""
    with psycopg.connect(legacy_kb.dsn, autocommit=True, connect_timeout=30) as conn, \
            conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{legacy_kb.schema}".documents'
                    "(source_type, source_key, title, meta, content_hash) VALUES"
                    " ('manual','manual:abc','老库手记','{}'::jsonb,'sha:x')")
        cur.execute(f'INSERT INTO "{legacy_kb.schema}".chunks(document_id, ord, text)'
                    " VALUES (1, 0, '老库正文')")

    body = docs(legacy_kb.client)
    assert [i["title"] for i in body["items"]] == ["老库手记"]
    assert "content_type" not in body["items"][0]          # 键缺席 ≠ 假装是 note
    hits = search(legacy_kb.client, "老库正文")
    assert hits["items"] and "content_type" not in hits["items"][0]

    r = legacy_kb.client.get("/api/v1/kb/documents?content_type=note", headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "kb_schema_unknown"
    assert legacy_kb.client.get("/api/v1/kb/type-counts", headers=AUTH).json()["counts"] == {}


@PG_ONLY
def test_缺列_导出仍可用_content_type给空串(legacy_kb):
    """导出不写内容、老库上不该整条端瘫掉（只读端点的降级口径与 source_type 同款）。"""
    with psycopg.connect(legacy_kb.dsn, autocommit=True, connect_timeout=30) as conn, \
            conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{legacy_kb.schema}".documents'
                    "(source_type, source_key, title, meta, content_hash) VALUES"
                    " ('manual','manual:abc','老库手记','{}'::jsonb,'sha:x')")
    rows = [json.loads(line) for line in
            legacy_kb.client.get(EXPORT, headers=AUTH).text.splitlines() if line.strip()]
    assert [r["content_type"] for r in rows] == [""]
