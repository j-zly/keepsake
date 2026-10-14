"""`list_by_tag` 必须排除已归档条目 —— 三后端同一口径（回归锁）。

🔴 **背景（主脑真机复现的真 bug）**：三后端 `list_by_tag` 都不看 `is_archived` ——
PG/SQLite 的 WHERE 只来自 `tag_match_sql`，Redis 的 HMGET 压根没取这根字段。
后果是控制台候选列表（`api/routers/fragments.py`）把已归档条目照样吐出来，
且波及全部调用方：`api/routers/core.py`（常驻页）、`ops.py`（合并计数）、
`mcp_server/factory.py`（list_core）、`adapters/hermes/provider.py:717`
（**归档一条 core 仍每轮注入 system_prompt_block**）。

本文件锁死三件事：
  1. 归档（`is_archived='1'` + `valid_until=now`，与 `archive_fragment` 同款）
     之后 `list_by_tag` 不再返回它；
  2. **防误杀**：历史上从未写过该列 / 值为空串的行必须照常返回
     （判据是 `= '1'` 精确相等，不是 `= ''`）；
  3. 本单**只**加 `is_archived` 判据 —— `valid_until` / `superseded_by`
     版本化语义没被卷进来（`valid_until` 有值但未归档的行照样返回）。

SQLite 用例全 hermetic（tmp_path 临时库）；Redis 用假 client（不连真 Redis，
与 `test_core_tier.py` 同一套 fake）；PG 连 `~/.keepsake_pg_test.env` 里的
8.140.192.91/keepsake_test（隔离 schema），读不到凭据就 skip ——
与 `test_tag_filter_semantics.py` 同一套接法。
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

PROBE_TAG = "coretest"
CORPUS = ["甲", "乙", "丙"]


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


DSN = _read_dsn()
_pg_ready = bool(DSN and ALLOWED_HOST in DSN and ALLOWED_DBNAME in DSN)


def _archive(store, key: str) -> None:
    """走**存储层**的归档原语 —— 与 `api/routers/ops.py:archive_fragment` 同款。"""
    assert store.update_fragment_fields(
        key, {"is_archived": "1", "valid_until": "2026-01-01T00:00:00+00:00"}
    ) is True, f"归档写入失败：{key}"


def _raw_update(store, sql: str) -> None:
    """绕过白名单直接改列 —— 用来造「历史上从没写过该列」的历史行形态。"""
    with store._lock:  # noqa: SLF001
        store._db().execute(sql)  # noqa: SLF001
        store._db().commit()  # noqa: SLF001


# ---------------------------------------------------------------------------
# 一、SQLite 真库（hermetic）
# ---------------------------------------------------------------------------

@pytest.fixture()
def lite(tmp_path):
    s = SqliteStorage(path=str(tmp_path / "ks.db"), is_primary=True)
    assert s.ensure_index() is True
    for suffix in CORPUS:
        assert s.store(text=f"常驻条目{suffix}", tags=f"{PROBE_TAG},memory",
                       category="core", fragment_type="core") is True
    yield s
    s.close()


def _contents(store, tag=PROBE_TAG):
    """只取条目末字（甲/乙/丙）比对 —— 绕开排序与全文，比的是「谁还在列表里」。"""
    return [f["content"][-1] for f in store.list_by_tag(tag)]


def test_archived_excluded_from_list_by_tag(lite):
    """归档后 `list_by_tag` 不再返回它 —— 这是 provider 每轮注入的判据。"""
    assert _contents(lite) == CORPUS

    victim = next(f["key"] for f in lite.list_by_tag(PROBE_TAG) if f["content"].endswith("甲"))
    _archive(lite, victim)

    got = _contents(lite)
    assert "甲" not in got, got
    assert got == ["乙", "丙"], got


def test_never_archived_rows_still_listed(lite):
    """🔴 防误杀：`is_archived` 为空串（历史行 / 补列前的旧行）的条目照常返回。

    判据必须是 `!= '1'` 而不是 `= ''` —— 后者会连从未写过这根列的行一起误杀。
    """
    _raw_update(lite, "UPDATE ks_fragment SET is_archived = '' WHERE content LIKE '%甲'")
    with lite._lock:  # noqa: SLF001
        col = lite._db().execute(  # noqa: SLF001
            "SELECT is_archived FROM ks_fragment WHERE content LIKE '%甲'"
        ).fetchone()[0]
    assert col == "", col

    assert _contents(lite) == CORPUS, "空串形态被判成归档了 = 误杀历史行"


def test_content_hash_versioning_untouched(lite):
    """同内容再存一次：旧版被标 `is_archived='1'` 且下线，新版照常返回。

    反向断言 `valid_until` 没被卷进判据：一条 valid_until 有值但未归档的行
    （历史库可能长这样）必须仍然返回 —— 本单只加 `is_archived`。
    """
    assert lite.store(text="常驻条目甲", tags=f"{PROBE_TAG},memory",
                      category="core", fragment_type="core") is True
    got = _contents(lite)
    assert got.count("甲") == 1, f"旧版应被排除、新版应在：{got}"
    assert len([r for r in lite.list_by_tag(PROBE_TAG)
                if r["key"].count(":") == 3]) == 1, "活的那份仍应带版本后缀 key"

    _raw_update(
        lite,
        "UPDATE ks_fragment SET is_archived = '', "
        "valid_until = '2026-01-01T00:00:00+00:00' WHERE content LIKE '%乙'",
    )
    assert "乙" in _contents(lite), "valid_until 不该参与判据（本单只判 is_archived）"


# ---------------------------------------------------------------------------
# 二、Redis：假 client（不连真 Redis），锁 HMGET 多取的那根字段 + 判据
# ---------------------------------------------------------------------------

def test_redis_archived_excluded_and_legacy_pass(monkeypatch):
    from keepsake.storage import RedisStorage

    hashes = {
        "memory:frag:a": {"content": b"core-A", "tags": b"core"},
        "memory:frag:b": {"content": b"core-B", "tags": b"core", "is_archived": b"1"},
        # 历史形态：压根没这根字段 ⇒ HMGET 返回 None ⇒ 放行
        "memory:frag:c": {"content": b"core-C", "tags": b"core"},
        # 未归档但显式写空串 ⇒ 放行（不能写 `== ""` 判归档）
        "memory:frag:d": {"content": b"core-D", "tags": b"core", "is_archived": b""},
    }
    keys = [k.encode() for k in hashes]

    class FakeClient:
        def scan(self, cursor=0, match=None, count=None):
            return (0, keys)

        def pipeline(self):
            class P:
                def __init__(self):
                    self.q = []

                def hmget(self, key, *fields):
                    self.q.append((key, fields))

                def execute(self):
                    out = [[hashes.get(k, {}).get(f) for f in fs] for k, fs in self.q]
                    self.q = []
                    return out
            return P()

    monkeypatch.setattr(RedisStorage, "_get_client", lambda self: FakeClient())
    rows = RedisStorage.list_by_tag(object.__new__(RedisStorage), "core")
    contents = sorted(r["content"] for r in rows)

    assert contents == ["core-A", "core-C", "core-D"], contents
    assert "core-B" not in contents, "归档条目仍在 list_by_tag 结果里"


# ---------------------------------------------------------------------------
# 三、PG 真库（无凭据则 skip）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _pg_ready, reason="无 8.140.192.91/keepsake_test 凭据，跳过 PG 真库")
def test_pg_archived_excluded_and_legacy_pass():
    from keepsake.storage_pg import PgStorage

    schema = f"ks_lbtag_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    pg = PgStorage(dsn=DSN + f"?options=-csearch_path%3D{schema},public",
                   connect_timeout=30, embed_dim=4, is_primary=True)
    try:
        assert pg.ensure_index() is True
        for suffix in CORPUS:
            assert pg.store(text=f"常驻条目{suffix}", tags=f"{PROBE_TAG},memory",
                            category="core", fragment_type="core") is True

        assert sorted(f["content"][-1] for f in pg.list_by_tag(PROBE_TAG)) == sorted(CORPUS)

        victim = next(f["key"] for f in pg.list_by_tag(PROBE_TAG)
                      if f["content"].endswith("甲"))
        _archive(pg, victim)
        assert "甲" not in [f["content"][-1] for f in pg.list_by_tag(PROBE_TAG)]

        # 历史行形态（空串）不许被误杀
        with pg._tx() as cur:  # noqa: SLF001
            cur.execute("UPDATE ks_fragment SET is_archived = '' WHERE content LIKE '%乙'")
        assert sorted(f["content"][-1] for f in pg.list_by_tag(PROBE_TAG)) == sorted(["乙", "丙"])
    finally:
        pg.close()
        with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
