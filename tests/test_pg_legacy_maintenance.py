"""PG **老库缺列自愈** 真库测试（202-10 ks_pcol）。

## 为什么必须有这个文件

`keepsake_test`（8.140.192.91）里 `ks_fragment` **已经有**
`level / consumed_by / consumed_at` 三列 —— 上一单在它上面跑过 `ensure_index`，
ALTER 已落库。所以既有 PG 真库用例**测不出**生产 180 的故障：

    consolidator: scan error: column "level" does not exist
    ① 合并 dry-run ⇒ {"scanned": 0, ...}    ← 扫描直接失败，不是扫不到
    ② 遗忘 dry-run ⇒ psycopg.errors.UndefinedColumn: column "level" does not exist

根因：`level` 是 Redis 侧一直存在的 hash 字段，**PG 表从未建过这一列**。
⇒ 回归防护必须**另造一个缺列的库**来跑，不能靠 91 的 public 表。

## 本文件覆盖两条 DDL 路径

1. **老库**（表在、三列缺）—— 对应生产 180。
2. **全新库**（表都不在）—— 对应首次部署。

第 2 条在修复前是**必挂**的：`_plan_schema_ddl` 对空库会同时排出
`CREATE TABLE ks_fragment (... level ...)` 与 `ALTER TABLE ... ADD COLUMN level`
⇒ 同事务内 `DuplicateColumn` ⇒ 整个迁移段回滚 ⇒ `ensure_index()` 返回 **False**
⇒ provider 初始化中止。修法见 `_maintenance_column_ddl` 的 docstring。

## 隔离与卫生

- 每个用例在**独立 schema** 里建库（DSN 追加 `?options=-csearch_path=...`），
  绝不碰 91 的 `public` 表。
- 用例结束 `DROP SCHEMA ... CASCADE`，残留复核 = 0（见 `_probe_schemas` teardown）。
- 凭据规则与 `test_storage_pg.py` 一致：DSN 只从
  `~/.keepsake_pg_test.env` 读；host/dbname 不符就整模块 skip。
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

from keepsake.consolidator import Consolidator
from keepsake.forgetter import Forgetter
from keepsake.storage_pg import MAINTENANCE_COLUMNS, PgStorage

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST = "8.140.192.91"
ALLOWED_DBNAME = "keepsake_test"

psycopg = pytest.importorskip("psycopg", reason="未装 psycopg（pip install 'keepsake-memory[postgres]'）")


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


DSN = _read_dsn()
if not (DSN and ALLOWED_HOST in DSN and ALLOWED_DBNAME in DSN):
    pytest.skip(f"无测试库凭据或指向非 {ALLOWED_HOST}/{ALLOWED_DBNAME}，跳过 PG 老库自愈测试",
                allow_module_level=True)

# 「老库」的表定义 = 当前 FRAGMENT_COLUMNS 全集，**刻意不含**三列维护字段。
# 与 commit 0aea992（PG 后端首版，production 180 上跑的就是它）的 ks_fragment DDL 一致。
_LEGACY_DDL = """
CREATE TABLE ks_fragment (
    key             text PRIMARY KEY,
    content         text NOT NULL DEFAULT '',
    tags            text NOT NULL DEFAULT '',
    category        text NOT NULL DEFAULT '',
    source          text NOT NULL DEFAULT '',
    created         text NOT NULL DEFAULT '',
    sentiment_score text NOT NULL DEFAULT '',
    sentiment_label text NOT NULL DEFAULT '',
    feedback_score  text NOT NULL DEFAULT '0',
    entities        text NOT NULL DEFAULT '',
    fragment_type   text NOT NULL DEFAULT '',
    valid_until     text NOT NULL DEFAULT '',
    is_archived     text NOT NULL DEFAULT '',
    superseded_by   text NOT NULL DEFAULT '',
    superseded_at   text NOT NULL DEFAULT '',
    corrected_at    text NOT NULL DEFAULT '',
    invalid_at      text NOT NULL DEFAULT ''
)
"""

_PROBE_SCHEMAS: list[str] = []


def _dsn_for(schema: str) -> str:
    """把 `search_path` 钉到本用例的隔离 schema（public 保留：pgvector 扩展装在那）。"""
    return DSN + f"?options=-csearch_path%3D{schema},public"


def _columns(dsn: str) -> set:
    with psycopg.connect(dsn, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'ks_fragment'"
        )
        return {r[0] for r in cur.fetchall()}


def _rowcount(dsn: str) -> int:
    with psycopg.connect(dsn, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ks_fragment")
        return cur.fetchone()[0]


@pytest.fixture
def probe(request):
    """造一个隔离 schema 的「老库」或「全新库」，跑完 DROP 掉。

    `legacy=True`  → 建 17 列的 ks_fragment，**三列维护字段缺失**（= 生产 180）
    `legacy=False` → schema 全空，连表都没有（= 首次部署）
    """
    legacy = getattr(request, "param", True)
    schema = f"ks_probe_{uuid.uuid4().hex[:12]}"
    _PROBE_SCHEMAS.append(schema)
    dsn = _dsn_for(schema)

    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    if legacy:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
            cur.execute(_LEGACY_DDL)

    # 先确认「缺列」这个前提真的成立，否则下面的断言是空转
    missing = [c for c in MAINTENANCE_COLUMNS if c not in _columns(dsn)]
    if legacy:
        assert missing == list(MAINTENANCE_COLUMNS), (
            f"老库前提不成立：三列本就在（missing={missing}），本用例失去意义")

    storage = PgStorage(dsn=dsn, connect_timeout=30, embed_dim=4)
    yield storage, dsn, missing

    storage.close()
    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    _PROBE_SCHEMAS.remove(schema)


def _teardown_guard():
    """模块级兜底：任何用例中途炸了也要把 probe schema 清干净（残留复核 = 0）。"""
    yield
    for schema in list(_PROBE_SCHEMAS):
        with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        _PROBE_SCHEMAS.remove(schema)


@pytest.fixture(scope="module", autouse=True)
def _no_leftover_probe_schemas():
    yield from _teardown_guard()


def _seed(dsn: str, n: int = 4) -> None:
    """写入 n 条**够老**、同话题的碎片（合并/遗忘两条路径都要能被扫到）。"""
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        for i in range(n):
            cur.execute(
                "INSERT INTO ks_fragment (key, content, tags, category, source, created,"
                " sentiment_score, sentiment_label, feedback_score, entities, fragment_type)"
                " VALUES (%s, %s, '', 'note', 'test', %s, '0', 'flat', '0', '', '')",
                (f"memory:frag:probe{i:04d}",
                 f"PostgreSQL 索引 优化 数据库 调优 第{i}条", old),
            )


def _channel():
    return {"base_url": "http://x", "model": "m", "api_key": "k", "source": "configured"}


# ===========================================================================
# 1. 老库缺列 → ensure_index 自愈
# ===========================================================================

@pytest.mark.parametrize("probe", [True], indirect=True)
def test_legacy_db_ensure_index_adds_missing_maintenance_columns(probe):
    """老库缺三列 ⇒ ensure_index() 补齐 ⇒ 返回 True。"""
    storage, dsn, missing = probe
    assert missing == list(MAINTENANCE_COLUMNS)

    assert storage.ensure_index() is True, "ensure_index 必须成功（生产上 provider 靠它启动）"

    have = _columns(dsn)
    assert not [c for c in MAINTENANCE_COLUMNS if c not in have], (
        f"ensure_index 后仍缺列：{[c for c in MAINTENANCE_COLUMNS if c not in have]}")


@pytest.mark.parametrize("probe", [False], indirect=True)
def test_fresh_db_ensure_index_does_not_collide_with_create_table(probe):
    """🔴 全新库：CREATE TABLE（含三列）与 ADD COLUMN 三列同批 ⇒ 不得 DuplicateColumn。

    修复前这条必红：`ensure_index()` 连续 3 次 DuplicateColumn 后返回 **False**。
    """
    storage, dsn, _ = probe
    assert _columns(dsn) == set(), "前提：全新库里连 ks_fragment 都没有"

    assert storage.ensure_index() is True, "全新库 ensure_index 必须成功"

    have = _columns(dsn)
    assert not [c for c in MAINTENANCE_COLUMNS if c not in have]


@pytest.mark.parametrize("probe", [True], indirect=True)
def test_legacy_db_ensure_index_is_idempotent(probe):
    """跑第二遍：不发多余 DDL、仍成功（IF NOT EXISTS 幂等）。"""
    storage, dsn, _ = probe
    assert storage.ensure_index() is True
    before = _columns(dsn)
    assert storage.ensure_index() is True
    assert _columns(dsn) == before


# ===========================================================================
# 2. 补列后维护路径可用 —— 不许再 UndefinedColumn
# ===========================================================================

@pytest.mark.parametrize("probe", [True], indirect=True)
def test_legacy_db_consolidate_dry_run_works_after_ensure_index(probe):
    """老库补列后：合并 dry-run 扫得到、报得出组数，且**前后行数完全一致**。"""
    storage, dsn, _ = probe
    assert storage.ensure_index() is True
    _seed(dsn, 4)
    before = _rowcount(dsn)

    stats = Consolidator(storage, min_group_size=2, max_age_hours=1,
                         channel=_channel()).consolidate(dry_run=True)

    assert stats["scanned"] >= 1, f"扫描失败（生产症状：scanned=0）: {stats}"
    assert stats["groups_found"] >= 1
    assert stats["merged"] == 0 and stats["dry_run"] is True
    assert _rowcount(dsn) == before, "dry-run 一个字都不许写"


@pytest.mark.parametrize("probe", [True], indirect=True)
def test_legacy_db_forget_dry_run_works_after_ensure_index(probe):
    """老库补列后：遗忘 dry-run 跑通、零删除。

    修复前这条抛 `psycopg.errors.UndefinedColumn: column "level" does not exist`
    —— forgetter 的 `get_fragments_batch` 选了 MAINTENANCE_COLUMNS。
    """
    storage, dsn, _ = probe
    assert storage.ensure_index() is True
    _seed(dsn, 3)
    before = _rowcount(dsn)

    stats = Forgetter(storage, max_age_days=30, dry_run=True).forget()

    assert stats["scanned"] >= 1
    assert stats["dry_run"] is True
    assert stats["deleted"] == 0
    assert _rowcount(dsn) == before, "dry-run 零删除"


# ===========================================================================
# 3. 真跑（仅限本用例自造数据）：语义与 Redis 侧对齐
# ===========================================================================

@pytest.mark.parametrize("probe", [True], indirect=True)
def test_legacy_db_consolidate_real_run_marks_consumed(probe, monkeypatch):
    """真跑：consumed_by 落库条数 == 组内条数、原行仍在（软删）、新增 consolidated 行。"""
    monkeypatch.setattr("keepsake.consolidator._call_llm",
                        lambda *a, **k: "合并后的高层知识条目")
    storage, dsn, _ = probe
    assert storage.ensure_index() is True
    _seed(dsn, 4)
    src_keys = [f"memory:frag:probe{i:04d}" for i in range(4)]

    stats = Consolidator(storage, min_group_size=2, max_age_hours=1,
                         channel=_channel()).consolidate()

    assert stats["merged"] == len(src_keys)
    with psycopg.connect(dsn, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT key, consumed_by, consumed_at, fragment_type FROM ks_fragment"
            " WHERE key = ANY(%s)", (src_keys,))
        rows = cur.fetchall()
        assert len(rows) == len(src_keys), "原料行必须仍在（软删，不是硬删）"
        consolidated_keys = {r[1] for r in rows}
        assert len(consolidated_keys) == 1, "一组只应产生一条 consolidated"
        for _key, consumed_by, consumed_at, frag_type in rows:
            assert frag_type == "consumed"
            assert consumed_by in consolidated_keys
            assert consumed_at, "consumed_at 必须落库"
        cur.execute(
            "SELECT key, fragment_type, level FROM ks_fragment"
            " WHERE key = ANY(%s)", (list(consolidated_keys),))
        new = cur.fetchall()
        assert len(new) == 1, "新增 consolidated 行数应 = 组数"
        assert new[0][1] == "consolidated"
        assert new[0][2] == "2", "首次合并 level 必须是 2"


@pytest.mark.parametrize("probe", [True], indirect=True)
def test_legacy_db_forget_real_run_deletes_only_rows(probe):
    """真跑：只删够格的，其余原样保留；时间线随之清理。"""
    storage, dsn, _ = probe
    assert storage.ensure_index() is True
    _seed(dsn, 3)
    before = _rowcount(dsn)

    stats = Forgetter(storage, max_age_days=30, dry_run=True).forget(force=True)

    assert stats["deleted"] == before, "自造的碎片全都够老够低价值 ⇒ 应全删"
    assert _rowcount(dsn) == 0