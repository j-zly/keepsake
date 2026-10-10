"""tag 过滤语义回归 —— 「逗号分隔 + 标签精确相等」三后端同一套。

🔴 **本文件是回归，不是新功能**：修之前 `_search_filter_sql` 两后端都写的是
`instr('|'||tags||'|', '|tag|')`（PG 是 `strpos`），而库里 `tags` 落库形态是
**逗号串**（`a,b`）⇒ 竖线包裹的针脚永远扎不进去 ⇒ `tag_filter` 非空时召回恒 0，
非主脑的 `agent:` 隔离也恒 0（两后端实测，见 /tmp/ks_tag_pre.txt）。

本文件锁死三件事：
  1. tag 命中是**完整标签相等**，不是子串包含（`a` 不得命中 `ab`）
  2. 分隔符按**真实形态（逗号）**处理，且逗号两侧空格容错（`c, d` 里的 `d` 能查到）
  3. 多 tag_filter 是**并集**（OR），不是「拼成一个相邻子串」

PG 用例只连 `~/.keepsake_pg_test.env` 里的 8.140.192.91/keepsake_test（隔离 schema），
读不到凭据就 skip；SQLite 用例全 hermetic（tmp_path 临时库）。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from keepsake.storage_sqlite import SqliteStorage

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST, ALLOWED_DBNAME = "8.140.192.91", "keepsake_test"
psycopg = pytest.importorskip("psycopg", reason="未装 psycopg")

QUERY = "部署 回滚 流程"
# (正文唯一后缀, 传入的 tags, 落库后是否原样改成带空格形态)
CORPUS = [
    ("甲", "a,b", False),      # 单条里带 a
    ("乙", "ab", False),       # 子串陷阱：a 不得命中它
    ("丙", "c,d", True),       # 迁移行：tags 原样是 "c, d"
    ("丁", "x", False),        # 另建 worker 实例写 ⇒ 自动注入 agent:worker
]


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


DSN = _read_dsn()
_pg_ready = bool(DSN and ALLOWED_HOST in DSN and ALLOWED_DBNAME in DSN)


# ---------------------------------------------------------------------------
# 一、SQL 片段层：两后端生成同一份「逗号边界」表达式
# ---------------------------------------------------------------------------

def _bare(cls):
    """不建连的裸实例：`_search_filter_sql` 只读 `_agent_id` / `_is_primary`。"""
    obj = object.__new__(cls)
    obj._agent_id, obj._is_primary = "", True
    return obj


def test_filter_sql_uses_comma_boundaries_not_pipes():
    """两个后端的 tag 片段都必须是逗号包裹的针脚，且不再出现竖线包裹。"""
    from keepsake.storage_pg import PgStorage
    from keepsake.storage_sqlite import SqliteStorage as _S

    sql_pg, params_pg = PgStorage._search_filter_sql(_bare(PgStorage), "a,c", "", True)
    assert "|tags" not in sql_pg and "'|'" not in sql_pg, sql_pg
    assert params_pg == [",a,", ",c,"], params_pg
    # 多 tag 是 OR（并集），不是拼成 ",a,c," 这种相邻子串
    assert sql_pg.count("%s") == 2 and " OR " in sql_pg, sql_pg

    sql_lite, params_lite = _S._search_filter_sql(_bare(_S), "a,c", "", True)
    assert "|tags" not in sql_lite and "'|'" not in sql_lite, sql_lite
    assert params_lite == [",a,", ",c,"], params_lite
    assert sql_lite.count("?") == 2 and " OR " in sql_lite, sql_lite


def test_clean_tag_still_scrubs_separators():
    """针脚里的分隔符/空格必须先被清掉，否则会自己造出边界。"""
    from keepsake.storage_pg import PgStorage
    from keepsake.storage_sqlite import SqliteStorage as _S

    for cls in (PgStorage, _S):
        assert cls._clean_tag(" a|b,c{} ") == "abc", cls.__name__
        assert cls._clean_tag("agent:worker") == "agent:worker", cls.__name__


# ---------------------------------------------------------------------------
# 二、SQLite 后端端到端（hermetic）
# ---------------------------------------------------------------------------

@pytest.fixture()
def lite(tmp_path):
    s = SqliteStorage(path=str(tmp_path / "ks.db"), is_primary=True)
    assert s.ensure_index() is True
    yield s
    s.close()


@pytest.fixture()
def lite_seeded(tmp_path):
    """一份含 4 条多标签碎片的库（其中一条用 worker 实例写 ⇒ 带 agent:worker）。"""
    path = str(tmp_path / "seed.db")
    main = SqliteStorage(path=path, is_primary=True)
    worker = SqliteStorage(path=path, agent_id="worker", is_primary=True)
    try:
        assert main.ensure_index() is True
        assert worker.ensure_index() is True
        for suffix, tags, spaced in CORPUS:
            st = worker if suffix == "丁" else main
            st.store(text=f"{QUERY} {suffix}", tags=tags, category="probe", source="probe")
            if spaced:
                with main._lock:  # noqa: SLF001
                    main._db().execute(  # noqa: SLF001
                        "UPDATE ks_fragment SET tags = ? WHERE content = ?",
                        ("c, d", f"{QUERY} {suffix}"))
                    main._db().commit()
    finally:
        main.close()
        worker.close()
    yield path


def _who(store: SqliteStorage, tag_filter: str, agent_id="", is_primary=True):
    rows = store.search_bm25(QUERY, tag_filter=tag_filter, agent_id=agent_id,
                             is_primary=is_primary)
    return sorted(r["content"][-1] for r in rows), sorted(r["_key"] for r in rows)


def test_sqlite_tag_filter_recalls_and_no_false_positive(lite_seeded):
    """单标签能召回；带子串的 ab 不得被误召回。"""
    s = SqliteStorage(path=lite_seeded, is_primary=True)
    try:
        assert s.ensure_index() is True
        assert set(_who(s, "")[0]) == {"甲", "乙", "丙", "丁"}
        who, _ = _who(s, "a")
        assert who == ["甲"], who
    finally:
        s.close()


def test_sqlite_multi_tag_filter_is_union(lite_seeded):
    """`a,c` 是并集：甲 + 丙。"""
    s = SqliteStorage(path=lite_seeded, is_primary=True)
    try:
        assert s.ensure_index() is True
        who, _ = _who(s, "a,c")
        assert who == ["丙", "甲"], who
    finally:
        s.close()


def test_sqlite_tag_filter_tolerates_spaces_around_comma(lite_seeded):
    """tags 原样落库是 `c, d` 时，c 和 d 都要能查到。"""
    s = SqliteStorage(path=lite_seeded, is_primary=True)
    try:
        assert s.ensure_index() is True
        assert _who(s, "c")[0] == ["丙"]
        assert _who(s, "d")[0] == ["丙"]
    finally:
        s.close()


def test_sqlite_agent_isolation_clause_matches_own_tag(lite_seeded):
    """非主脑按 `agent:worker` 隔离：自己写的能查到。"""
    s = SqliteStorage(path=lite_seeded, is_primary=True)
    try:
        assert s.ensure_index() is True
        who, _ = _who(s, "", agent_id="worker", is_primary=False)
        assert who == ["丁"], who
        # 别的 agent 查不到任何东西
        assert _who(s, "", agent_id="other", is_primary=False)[0] == []
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 三、PG 真库 + 与 SQLite 的 key 集合一致性（无凭据则 skip）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _pg_ready, reason="无 8.140.192.91/keepsake_test 凭据，跳过 PG 真库")
def test_pg_matches_sqlite_on_same_corpus(lite_seeded):
    from keepsake.storage_pg import PgStorage

    schema = f"ks_tagt_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    dsn = DSN + f"?options=-csearch_path%3D{schema},public"
    pg = PgStorage(dsn=dsn, connect_timeout=30, embed_dim=4, is_primary=True)
    try:
        assert pg.ensure_index() is True
        worker = PgStorage(dsn=dsn, connect_timeout=30, embed_dim=4,
                           agent_id="worker", is_primary=True)
        try:
            for suffix, tags, spaced in CORPUS:
                st = worker if suffix == "丁" else pg
                st.store(text=f"{QUERY} {suffix}", tags=tags, category="probe", source="probe")
                if spaced:
                    with pg._tx() as cur:  # noqa: SLF001
                        cur.execute("UPDATE ks_fragment SET tags = %s WHERE content = %s",
                                    ("c, d", f"{QUERY} {suffix}"))
        finally:
            worker.close()

        lite = SqliteStorage(path=lite_seeded, is_primary=True)
        try:
            assert lite.ensure_index() is True
            for tag_filter, agent_id, is_primary in (("a", "", True),
                                                     ("a,c", "", True),
                                                     ("c", "", True),
                                                     ("d", "", True),
                                                     ("", "worker", False)):
                pg_keys = sorted(r["_key"] for r in pg.search_bm25(
                    QUERY, tag_filter=tag_filter, agent_id=agent_id, is_primary=is_primary))
                lt_keys = sorted(r["_key"] for r in lite.search_bm25(
                    QUERY, tag_filter=tag_filter, agent_id=agent_id, is_primary=is_primary))
                assert pg_keys, f"PG 侧 tag_filter={tag_filter!r} 召回 0 条"
                assert pg_keys == lt_keys, (tag_filter, agent_id, pg_keys, lt_keys)
        finally:
            lite.close()
    finally:
        pg.close()
        with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
