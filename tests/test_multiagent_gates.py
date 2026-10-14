"""多代理记忆治理三闸（用户拍板）—— 本单新增行为的回归锁。

🔴 **本文件锁死的是三条语义**，不是某个实现细节：
  1. **非 primary 对常驻层只读（检索侧）**：子代理能召回 `core` 条目 + 自己域 +
     `shared`，但**召回不到别的 agent 的私有条目**（PG/Redis/SQLite 三后端同口径）
  2. **写闸**：非 primary 写 `tags="core"` 落库必须是 `core_candidate`（审核前不进
     常驻层）；标签判据是逗号边界的完整标签相等（`core_x` 不算命中）；
     primary 写 core 原样放行；其余标签原样保留
  3. **主脑检索噪音排除**：`retrieval.exclude_tags` 缺省排除 `core_candidate`，
     显式空串 = 不排除；`search()` / `prefetch()` 两个出口同一份实现

SQLite 用例全 hermetic（tmp_path 临时库，BM25-only 模式）；
PG 用例连 `~/.keepsake_pg_test.env` 里的 8.140.192.91/keepsake_test（隔离 schema），
读不到凭据就 skip —— 与 tests/test_tag_filter_semantics.py 同一套接法。
Redis 用例需要真实 RediSearch，无基建 ⇒ skip（口径由 G2 静态闸覆盖）。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from keepsake import CORE_CANDIDATE_TAG, KeepsakeProvider, _has_tag
from keepsake.storage_sqlite import SqliteStorage

# 一条能被 BM25 命中的公共查询词（jieba 分词后三个后端都能切出实词）
QUERY = "回滚 流程 校验"
# (正文唯一后缀, 落库 tags)
CORPUS = [
    ("甲", "core"),              # 常驻层：子代理要能召回
    ("乙", "shared"),            # 共享层：本来就能召回
    ("丙", "agent:worker"),      # 自己域：本来就能召回
    ("丁", "agent:other"),       # 别人的私有：必须召回不到
]


# ---------------------------------------------------------------------------
# 一、非主检索放开 core（sqlite 真库端到端 + PG 真库）
# ---------------------------------------------------------------------------

def _seed_lite(path: str) -> None:
    """一份含 4 条记忆条目的临时库（core/shared/自己/别人各一条）。"""
    st = SqliteStorage(path=path, is_primary=True)
    try:
        assert st.ensure_index() is True
        for suffix, tags in CORPUS:
            assert st.store(text=f"{QUERY} {suffix}", tags=tags,
                            category="probe", source="probe") is True
    finally:
        st.close()


@pytest.fixture()
def lite_seeded(tmp_path):
    path = str(tmp_path / "seed.db")
    _seed_lite(path)
    yield path


def test_non_primary_recalls_core(lite_seeded):
    """非 primary 能召回 core / shared / 自己域 —— 但仅此三类（反向：见隔离用例）。"""
    s = SqliteStorage(path=lite_seeded, is_primary=True)
    try:
        assert s.ensure_index() is True
        who = sorted(r["content"][-1] for r in
                     s.search_bm25(QUERY, tag_filter="", agent_id="worker",
                                   is_primary=False))
        assert who == ["丙", "乙", "甲"], who
    finally:
        s.close()


def test_non_primary_still_isolated_from_other_agent(lite_seeded):
    """反向：core 放开后**别的 agent 私有条目依然召不回来**（隔离不能被放开冲掉）。"""
    s = SqliteStorage(path=lite_seeded, is_primary=True)
    try:
        assert s.ensure_index() is True
        who = sorted(r["content"][-1] for r in
                     s.search_bm25(QUERY, tag_filter="", agent_id="worker",
                                   is_primary=False))
        assert "丁" not in who, who
    finally:
        s.close()


def test_primary_branch_unchanged_by_core_opening(lite_seeded):
    """正向+反向：primary 分支逐字未动（能看到全库，含别人的私有条目）。"""
    s = SqliteStorage(path=lite_seeded, is_primary=True)
    try:
        assert s.ensure_index() is True
        who = sorted(r["content"][-1] for r in s.search_bm25(QUERY, tag_filter=""))
        assert who == ["丁", "丙", "乙", "甲"], who
    finally:
        s.close()


# PG 真库：同一份语料同一套断言（无凭据则 skip）
ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST, ALLOWED_DBNAME = "8.140.192.91", "keepsake_test"
psycopg = pytest.importorskip("psycopg", reason="未装 psycopg")


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


DSN = _read_dsn()
_pg_ready = bool(DSN and ALLOWED_HOST in DSN and ALLOWED_DBNAME in DSN)
_pg_skip = pytest.mark.skipif(not _pg_ready,
                              reason="无 8.140.192.91/keepsake_test 凭据，跳过 PG 真库")


@pytest.fixture()
def pg_seeded(lite_seeded):
    """PG 隔离 schema 里灌同一份语料（与 sqlite 同一断言口径）。"""
    schema = f"ks_mag_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    dsn = DSN + f"?options=-csearch_path%3D{schema},public"
    from keepsake.storage_pg import PgStorage

    pg = PgStorage(dsn=dsn, connect_timeout=30, embed_dim=4, is_primary=True)
    assert pg.ensure_index() is True
    for suffix, tags in CORPUS:
        assert pg.store(text=f"{QUERY} {suffix}", tags=tags,
                        category="probe", source="probe") is True
    yield pg
    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA "{schema}" CASCADE')


@_pg_skip
def test_non_primary_recalls_core_pg(pg_seeded):
    who = sorted(r["content"][-1] for r in
                 pg_seeded.search_bm25(QUERY, tag_filter="", agent_id="worker",
                                       is_primary=False))
    assert who == ["丙", "乙", "甲"], who


@_pg_skip
def test_non_primary_still_isolated_from_other_agent_pg(pg_seeded):
    who = sorted(r["content"][-1] for r in
                 pg_seeded.search_bm25(QUERY, tag_filter="", agent_id="worker",
                                       is_primary=False))
    assert "丁" not in who, who


# Redis：无 RediSearch 基建 ⇒ skip（口径由 G2 静态闸 + sqlite/PG 行为双验覆盖）
@pytest.mark.skip(reason="无 Redis+RediSearch 测试基建：三后端口径由 sqlite/PG 行为"
                          "用例 + G2 静态闸（redis.py 的 core filter_clause）覆盖")
def test_non_primary_recalls_core_redis():
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# 二、写闸：非 primary 写 core 自动降级为候选
# ---------------------------------------------------------------------------

class _FakeStorage:
    """只记 store 调用的最小 fake（写闸用例不碰真库）。"""

    def __init__(self):
        self.store_calls = []

    def store(self, text, tags="", category="", source="", fragment_type="", **_kw):
        self.store_calls.append({"text": text, "tags": tags})
        return True


def _provider(storage, *, is_primary: bool) -> KeepsakeProvider:
    p = object.__new__(KeepsakeProvider)
    p._config = {}
    p._storage = storage
    p._tag_filter = ""
    p._is_primary = is_primary
    p._exclude_tags = ()
    return p


def test_write_gate_downgrades_core_tag():
    """非 primary 写 `core` ⇒ 落库 `core_candidate`，且不含裸 core。"""
    st = _FakeStorage()
    key = _provider(st, is_primary=False).add("写一条常驻记忆", tags="core")
    assert key, "add 应返回 key"
    tags = st.store_calls[0]["tags"]
    assert _has_tag(tags, CORE_CANDIDATE_TAG), tags
    assert not _has_tag(tags, "core"), tags


def test_write_gate_keeps_other_tags():
    """降级只动 core 一个标签：`foo` 原样保留、顺序不变。"""
    st = _FakeStorage()
    _provider(st, is_primary=False).add("写一条带 foo 的记忆", tags="core,foo")
    tags = st.store_calls[0]["tags"]
    assert _has_tag(tags, CORE_CANDIDATE_TAG) and _has_tag(tags, "foo"), tags
    assert not _has_tag(tags, "core"), tags
    assert tags.split(",")[1] == "foo", tags

    # 反向：`core_x` 不算 core 命中（逗号边界的完整标签相等）⇒ 不降级
    st2 = _FakeStorage()
    _provider(st2, is_primary=False).add("写一条像 core 的记忆", tags="core_x")
    assert st2.store_calls[0]["tags"] == "core_x", st2.store_calls[0]


def test_write_gate_primary_untouched():
    """primary 写 core 原样落库；非 core 标签两边都原样。"""
    st = _FakeStorage()
    _provider(st, is_primary=True).add("主脑写常驻记忆", tags="core,foo")
    assert st.store_calls[0]["tags"] == "core,foo", st.store_calls[0]

    st2 = _FakeStorage()
    _provider(st2, is_primary=False).add("子代理写普通记忆", tags="foo,bar")
    assert st2.store_calls[0]["tags"] == "foo,bar", st2.store_calls[0]


# ---------------------------------------------------------------------------
# 三、主脑检索噪音排除（retrieval.exclude_tags）
# ---------------------------------------------------------------------------

def _resolve_exclude(inline: dict) -> tuple:
    """走真实的配置合并路径（与 initialize 同口径），返回解析后的排除标签。"""
    return tuple(KeepsakeProvider._resolve_config({"agent_id": "primary", **inline})
                 ["exclude_tags"])


def test_exclude_tags_default_filters_candidate(tmp_path, monkeypatch):
    """缺省配置：search 不返回 `core_candidate` 条目，shared 的照常返回。"""
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "none.json"))
    exclude = _resolve_exclude({})
    assert exclude == (CORE_CANDIDATE_TAG,), exclude

    path = str(tmp_path / "excl.db")
    st = SqliteStorage(path=path, is_primary=True)
    try:
        assert st.ensure_index() is True
        st.store(text=f"{QUERY} 候选", tags=CORE_CANDIDATE_TAG, category="probe", source="probe")
        st.store(text=f"{QUERY} 正式", tags="shared", category="probe", source="probe")
        p = object.__new__(KeepsakeProvider)
        p._config, p._storage, p._tag_filter = {}, st, ""
        p._is_primary = True
        p._exclude_tags = exclude
        hits = p.search(QUERY)
        assert [h["content"] for h in hits] == [f"{QUERY} 正式"], hits
    finally:
        st.close()


def test_exclude_tags_empty_disables_filter(tmp_path, monkeypatch):
    """`exclude_tags=""` 显式关闸：候选条目照常返回。"""
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "none.json"))
    exclude = _resolve_exclude({"retrieval": {"exclude_tags": ""}})
    assert exclude == (), exclude

    path = str(tmp_path / "noexcl.db")
    st = SqliteStorage(path=path, is_primary=True)
    try:
        assert st.ensure_index() is True
        st.store(text=f"{QUERY} 候选", tags=CORE_CANDIDATE_TAG, category="probe", source="probe")
        p = object.__new__(KeepsakeProvider)
        p._config, p._storage, p._tag_filter = {}, st, ""
        p._is_primary = True
        p._exclude_tags = exclude
        assert len(p.search(QUERY)) == 1, p.search(QUERY)
    finally:
        st.close()


def test_prefetch_uses_same_exclusion_helper(tmp_path, monkeypatch):
    """反向：prefetch 出口与 search 共用 `_filter_excluded`（漏一处就是故障）。"""
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "none.json"))
    st = SqliteStorage(path=str(tmp_path / "pf.db"), is_primary=True)
    try:
        assert st.ensure_index() is True
        st.store(text=f"{QUERY} 候选", tags=CORE_CANDIDATE_TAG, category="probe", source="probe")
        st.store(text=f"{QUERY} 正式", tags="shared", category="probe", source="probe")
        p = object.__new__(KeepsakeProvider)
        p._config, p._storage, p._tag_filter = {}, st, ""
        p._is_primary = True
        p._exclude_tags = _resolve_exclude({})
        p._core_cache_ttl = 60.0
        out = p.prefetch(QUERY)
        assert "候选" not in out and "正式" in out, out
    finally:
        st.close()