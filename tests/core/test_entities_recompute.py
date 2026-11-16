"""改正文 ⇒ `entities`（正文的派生抽取物）必须跟着重算（2026-11 content_update_entities_recompute）。

生产缺陷：改正文后 `entities` 列留着**旧正文**的抽取物，而检索索引是三路拼接
（正文 + entities + tags —— SQLite 的 `_fts_tokens` / PG 的
`content_tsv = A(content) || B(entities+tags)`）⇒ 旧正文的实体词**永久**留在
BM25 索引里：控制台显示「改成功了」，检索却召回旧内容，而且不会自愈。

本文件钉死三件事：
  1. SQLite（**真后端**，临时库）：改正文后 `entities` 列 = 新正文的抽取物，
     旧实体词既不在列里、也搜不到（新词仍搜得到 —— 防「索引整个空了」的假绿）；
  2. 显式传 `entities` 时**以显式值为准**（不重抽）；
  3. PG：真库不可达时走**打桩游标**，断言同一事务里 UPDATE 的 `content_tsv`
     参数用的是**新 entities**，且用的是 `base.entities_for_content` 那个
     **同一个函数对象**（两后端同一份抽取口径）。

抽取口径钉在 `base.entities_for_content`（→ `domain.splitter.extract_entities`，
写入路径 `store()` 用的同一个函数）；本文件里凡「期望值」都从它算，不写死字面量。
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

from keepsake.core.base import entities_for_content
from keepsake.core.sqlite import SqliteStorage

# 正文里带一个 ASCII 专名：实体抽取对 ASCII 词走正则（不经 jieba），⇒
# **任何环境**（有无 jieba 领域词典）都能稳定抽出 "KUBERNETES" ⇒ 本文件的
# 「旧实体词」断言在任何机器上都不是空断言。
OLD = "回测框架用阿里云服务器 Kubernetes"
NEW = "熔断降级限流方案 Sentinel"
OLD_ENTITY = "KUBERNETES"
KEY = "memory:frag:ent1"
TAGS = "ops,ents"


# ---------------------------------------------------------------- SQLite（真后端）

@pytest.fixture()
def store(tmp_path, monkeypatch):
    """真 SqliteStorage（临时文件，is_primary=True —— 与控制台主视角同款）。

    🔴 注入空同义词映射（monkeypatch `_load_synonym_map` 返回 {}）：夹具不许
    读到生产同义词表，否则「旧词搜不到」会被同义词扩展污染成假红/假绿。
    """
    s = SqliteStorage(path=str(tmp_path / "ents.db"), embedder=None, embed_dim=8,
                      is_primary=True)
    assert s.ensure_index() is not False
    monkeypatch.setattr(s, "_load_synonym_map", lambda: {})
    assert s.write_fragments_batch([
        {"key": KEY, "content": OLD, "tags": TAGS,
         "created": "2026-01-01T00:00:00+00:00"},
    ]) == 1
    return s


def _hits(storage, q: str) -> List[str]:
    return [str(r.get("key") or r.get("_key")) for r in storage.search(q)]


def test_sqlite_写入时entities按正文抽取(store):
    """前置：写路径（`store()` 同款抽取）已经把 OLD 的实体落库了。"""
    ents = store.get_fragment(KEY)["entities"]
    assert OLD_ENTITY in ents, f"旧正文实体没抽出来，断言会变成空断言：{ents!r}"


def test_sqlite_改正文后entities列等于新正文抽取物(store):
    """🔴 改正文后 `entities` 列必须 = **新正文**的抽取物（旧抽取物不许留着）。"""
    assert store.update_fragment_content(KEY, NEW) is True

    row = store.get_fragment(KEY)
    assert row["content"] == NEW
    assert row["entities"] == entities_for_content(NEW), \
        f"entities 列不是新正文的抽取物：{row['entities']!r}"
    assert OLD_ENTITY not in row["entities"], "旧正文的实体词还留在 entities 列里"


def test_sqlite_改正文后旧实体词搜不到新词搜得到(store):
    """🔴 双向断言：只断「新词搜得到」会放过「索引整个空了」的假绿。

    旧实体词（只在**旧 entities 列**里出现过）改正文后必须搜不到 —— 这正是
    生产缺陷「旧正文的实体词永久残留 BM25 索引」的可观测面。
    """
    assert store.update_fragment_content(KEY, NEW) is True

    assert KEY not in _hits(store, OLD_ENTITY), "旧实体词还能搜到 = 索引留着旧 entities"
    assert KEY not in _hits(store, "Kubernetes"), "旧实体词还能搜到 = 索引留着旧 entities"
    assert KEY in _hits(store, "Sentinel"), "新词搜不到 = 索引没跟着改"
    assert KEY in _hits(store, "熔断"), "新词搜不到 = 索引没跟着改"


def test_sqlite_显式传entities以显式值为准不被重抽(store):
    """调用方**显式**传 entities ⇒ 以显式值为准（**不重抽**）。"""
    assert store.update_fragment_content(KEY, NEW, entities="显式实体") is True

    row = store.get_fragment(KEY)
    assert row["entities"] == "显式实体", row["entities"]
    assert OLD_ENTITY not in row["entities"]
    # 显式值同样进索引（召回面 = 正文 + entities + tags 三路）
    assert KEY in _hits(store, "显式实体")


def test_sqlite_维护面原语不动entities列(store):
    """🔴 `update_fragment_fields`（维护面原语）的契约**零改变**：
    它只改点名的列，entities 列不跟着动（改正文那条路才重抽）。"""
    assert store.update_fragment_fields(KEY, {"tags": "ops,ents,tag2"}) is True

    row = store.get_fragment(KEY)
    assert row["entities"] == entities_for_content(OLD), \
        "维护面原语顺手重抽了 entities —— 契约被改坏了"


# ---------------------------------------------------------------- PG（打桩游标）

class _FakeCursor:
    """只记 SQL 与参数的假游标（PG 真库不可达时的打桩件）。"""

    def __init__(self, tag_row: Tuple[str, ...]) -> None:
        self._rows: List[Tuple[str, ...]] = [tag_row]
        self.executed: List[Tuple[str, List[Any]]] = []
        self.rowcount = 1

    def execute(self, sql: str, params: Optional[List[Any]] = None) -> None:
        self.executed.append((" ".join(sql.split()), list(params or [])))

    def fetchone(self) -> Optional[Tuple[str, ...]]:
        return self._rows.pop(0) if self._rows else None


class _FakeTx:
    def __init__(self, cur: _FakeCursor) -> None:
        self.cur = cur

    def __enter__(self) -> _FakeCursor:
        return self.cur

    def __exit__(self, *exc: Any) -> bool:
        return False


def _pg_stub(content: str = NEW):
    """构造一个**不触网**的 PgStorage（构造不连库），把事务/回读换成打桩件。"""
    from keepsake.core.pg import PgStorage

    pg = PgStorage(dsn="postgresql://nobody@127.0.0.1:1/none", connect_timeout=1,
                   embed_dim=4, is_primary=True)
    cur = _FakeCursor((TAGS,))
    pg._tx = lambda: _FakeTx(cur)          # noqa: SLF001 —— 打桩件
    pg._snap_drop = lambda *groups: None   # noqa: SLF001 —— 快照失效不需要真库
    pg.get_fragment = lambda key: {"content": content}   # noqa: SLF001 —— 回读校验
    return pg, cur


def _update_sql(cur: _FakeCursor) -> Tuple[str, List[Any]]:
    for sql, params in cur.executed:
        if sql.startswith("UPDATE ks_fragment"):
            return sql, params
    raise AssertionError(f"没有发出 UPDATE：{cur.executed!r}")


def test_pg_改正文时entities按新正文重算并进content_tsv():
    """🔴 PG 侧同款判据：同一事务里 UPDATE 的 `content_tsv` 参数必须用**新 entities**。

    参数顺序 = `UPDATE ... SET content, content_tsv, entities [, tags] WHERE key`：
    `content_tsv` 的三个参数（`tsv_expr_params`）正是 A(正文) + B(entities+tags)
    的词元 ⇒ 断言它们等于「用新 entities 算出来的那一组」，就等于钉死了
    「B 权重按旧 entities 算 ⇒ 旧词永久残留」这条缺陷。
    """
    from keepsake.core.pg import tsv_expr_params

    pg, cur = _pg_stub()
    assert pg.update_fragment_content(KEY, NEW) is True

    sql, params = _update_sql(cur)
    assert "entities = %s" in sql, sql
    new_ents = entities_for_content(NEW)
    assert new_ents, "新正文没抽出实体 —— 断言会变成空断言"
    assert params == ([NEW] + list(tsv_expr_params(NEW, new_ents, TAGS))
                      + [new_ents, KEY]), params
    assert OLD_ENTITY not in new_ents


def test_pg_显式传entities以显式值为准不被重抽():
    """显式传 entities ⇒ 写进列的、算进 B 权重的都是**显式值**（不重抽）。"""
    from keepsake.core.pg import tsv_expr_params

    pg, cur = _pg_stub()
    assert pg.update_fragment_content(KEY, NEW, entities="显式实体") is True

    _sql, params = _update_sql(cur)
    assert params == ([NEW] + list(tsv_expr_params(NEW, "显式实体", TAGS))
                      + ["显式实体", KEY]), params
    assert entities_for_content(NEW) not in params, "显式传值时仍被重抽覆盖"


def test_pg与base用同一份抽取函数():
    """🔴 两后端同一份抽取口径（**同一个函数对象**，不是各抄一份）。"""
    from keepsake.core import base as base_mod
    from keepsake.core import pg as pg_mod

    assert pg_mod.entities_for_content is base_mod.entities_for_content


# ---------------------------------------------------------------- PG（真库，可达时）

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST, ALLOWED_DBNAME = os.environ.get(
    "KEEPSAKE_PG_TEST_HOST", "postgres.test.example"), "keepsake_test"


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


DSN = _read_dsn()
_pg_ready = bool(DSN and ALLOWED_HOST in DSN and ALLOWED_DBNAME in DSN)


@pytest.mark.skipif(not _pg_ready, reason="无测试库凭据，跳过 PG 真库")
def test_pg真库_改正文后entities列与索引同步():
    """PG 真库同款判据（凭据不可达时 skip —— 与 `tests/api/test_memory_crud.py` 同一套接法）。"""
    psycopg = pytest.importorskip("psycopg", reason="未装 psycopg")
    from keepsake.core.pg import PgStorage

    schema = f"ks_ents_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    pg = PgStorage(dsn=DSN + f"?options=-csearch_path%3D{schema},public",
                   connect_timeout=30, embed_dim=4, is_primary=True)
    try:
        assert pg.ensure_index() is True
        assert pg.store(text=OLD, tags="entstest", category="manual",
                        source="console", fragment_type="memory") is True
        key = pg._fragment_key_for(OLD)  # noqa: SLF001 —— 与 store() 同一算法
        assert OLD_ENTITY in pg.get_fragment(key)["entities"]

        assert pg.update_fragment_content(key, NEW, tags="entstest") is True

        assert pg.get_fragment(key)["entities"] == entities_for_content(NEW), \
            "PG 侧 entities 列没按新正文重算"
        assert pg.search_bm25(OLD_ENTITY, tag_filter="entstest") == [], \
            "旧实体词还能搜到 = content_tsv 留着旧 entities"
        assert key in [r["_key"] for r in pg.search_bm25("Sentinel", tag_filter="entstest")], \
            "新词搜不到 = content_tsv 没重算"
    finally:
        pg.close()
        with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
