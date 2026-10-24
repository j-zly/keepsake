"""多代理记忆治理三闸（用户拍板）—— 本单新增行为的回归锁。

🔴 **本文件锁死的是三条语义**，不是某个实现细节：
  1. **非 primary 对常驻层只读（检索侧）**：子代理能召回 `core` 条目 + 自己域 +
     `shared`，但**召回不到别的 agent 的私有条目**（PG/Redis/SQLite 三后端同口径）。
     🔴 2026-10 ks_search_core_filter 追加一档：**带归属的** core 条目（`core,agent:x`）
     按归属判 —— 非 primary 召回不到别人的常驻条目（裸 `core` 不再是通行证）；
     **无归属的**老常驻条目仍可见（现状不变硬回归线）
  2. **写闸**：非 primary 写 `tags="core"` 落库必须是 `core_candidate`（审核前不进
     常驻层）；标签判据是逗号边界的完整标签相等（`core_x` 不算命中）；
     **primary 写 core 也不再直通**（2026-10 常驻层入口改革：落候选 + 提名日戳），
     只有人工显式直写（`core_direct=True`）才原样放行；其余标签原样保留
  3. **主脑检索噪音排除**：`retrieval.exclude_tags` 缺省排除 `core_candidate`，
     显式空串 = 不排除；`search()` / `prefetch()` 两个出口同一份实现

SQLite 用例全 hermetic（tmp_path 临时库，BM25-only 模式）；
PG 用例连 `~/.keepsake_pg_test.env` 里的 KEEPSAKE_PG_TEST_HOST（缺省 postgres.test.example）/keepsake_test（隔离 schema），
读不到凭据就 skip —— 与 tests/test_tag_filter_semantics.py 同一套接法。
Redis 用例需要真实 RediSearch，无基建 ⇒ skip（口径由 G2 静态闸覆盖）。
"""

from __future__ import annotations

import os
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


def test_non_primary_recalls_no_foreign_owned_core(tmp_path):
    """🔴 2026-10 ks_search_core_filter：**带归属的** core 条目对非 primary 零命中。

    旧口径（裸 `core` 人人放行）⇒ 子代理能搜到**别人的**常驻记忆（实测：一次搜索
    命中 10 条，其中 9 条是 `core,...,agent:main-brain`）。现在 core 与普通条目
    同一把尺子：按归属判（共享集里的代理除外）。

    「**无归属的**老常驻条目仍然可见」——见上面的 `test_non_primary_recalls_core`
    （现状不变硬回归线，两条一起才是完整口径）。
    """
    path = str(tmp_path / "owned_core.db")
    st = SqliteStorage(path=path, is_primary=True)     # 无 agent_id ⇒ 归属标签原样落库
    try:
        assert st.ensure_index() is True
        assert st.store(text=f"{QUERY} 甲", tags="core,agent:other",
                        category="probe", source="probe") is True
    finally:
        st.close()

    s = SqliteStorage(path=path, is_primary=True)
    try:
        assert s.ensure_index() is True
        who = sorted(r["content"][-1] for r in
                     s.search_bm25(QUERY, tag_filter="", agent_id="worker",
                                   is_primary=False))
        assert who == [], who
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
ALLOWED_HOST, ALLOWED_DBNAME = os.environ.get("KEEPSAKE_PG_TEST_HOST", "postgres.test.example"), "keepsake_test"
psycopg = pytest.importorskip("psycopg", reason="未装 psycopg")


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


DSN = _read_dsn()
_pg_ready = bool(DSN and ALLOWED_HOST in DSN and ALLOWED_DBNAME in DSN)
_pg_skip = pytest.mark.skipif(not _pg_ready,
                              reason="无测试库凭据，跳过 PG 真库")


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


def test_write_gate_primary_也不再直通常驻层():
    """🔴 2026-10 常驻层入口改革：**primary 写 core 也不再直通**。

    旧语义（primary 原样放行）作废：常驻层从此只有两个入口 —— 审核页人工批准、
    候选满观察期自动通过。写 core 意图 ⇒ 落普通记忆条目 + 候选标 + 提名日戳。

    提名日戳（`tier:auto-hot:<日>`）是这条**进入 7 天观察期**的凭据：没有它，
    班车规则二压根不会审它（子代理降级路径就不带这个戳，两条分支的差别就在这）。
    """
    st = _FakeStorage()
    _provider(st, is_primary=True).add("主脑写常驻记忆", tags="core,foo")
    tags = st.store_calls[0]["tags"]
    assert _has_tag(tags, CORE_CANDIDATE_TAG), tags
    assert not _has_tag(tags, "core"), tags
    assert _has_tag(tags, "foo"), "其余标签原样保留"
    assert any(t.startswith("tier:auto-hot:") for t in tags.split(",")), tags


def test_write_gate_非primary降级带提名日戳():
    """子代理写 core 落候选**且带提名日戳**（2026-10 ks_perm_scope）。

    🔴 此前子代理写的候选**没有提名日** ⇒ 班车规则二压根不审它（死候选），
    等于「子代理不能自荐常驻」。现在与主脑候选**同一条审核/班车链**：
    人工批准、热度自动过，一视同仁。
    """
    st = _FakeStorage()
    _provider(st, is_primary=False).add("子代理写常驻记忆", tags="core")
    tags = st.store_calls[0]["tags"]
    assert _has_tag(tags, CORE_CANDIDATE_TAG), tags
    assert any(t.startswith("tier:auto-hot:") for t in tags.split(",")), tags


def test_write_gate_人工显式直写放行():
    """人工显式直写（`core_direct=True`，MCP `memory_add` 显式 core）⇒ 原样放行。

    那是「人在批」的等价动作（控制台 promote / CoreView 新增同理）；
    自动管线与通用协议路径不带这个标志 ⇒ 走候选。
    """
    st = _FakeStorage()
    _provider(st, is_primary=True).add("人写的常驻记忆", tags="core,foo",
                                        core_direct=True)
    assert st.store_calls[0]["tags"] == "core,foo", st.store_calls[0]

    # 非 primary 仍然降级：显式直写**不能**绕过治理（子代理对常驻层只读）
    st2 = _FakeStorage()
    _provider(st2, is_primary=False).add("子代理想直写", tags="core",
                                         core_direct=True)
    assert _has_tag(st2.store_calls[0]["tags"], CORE_CANDIDATE_TAG), st2.store_calls[0]


def test_write_gate_显式直写只对primary凭据生效():
    """🔴 2026-10 ks_share_tighten：显式直写**只对 primary 生效**（凭据说了算）。

    远端调用方（MCP）的身份由**凭据反查**得出 —— 一个外部代理带着 core 标签
    进来，显式直写**不生效**：照常降级成候选（**带提名日戳** ⇒ 与主脑候选
    同一条审核链，人工批准才进常驻层）。
    """
    st = _FakeStorage()
    _provider(st, is_primary=True).add("外部代理想直写", tags="core",
                                        core_direct=True, caller_role="sub")
    tags = st.store_calls[0]["tags"]
    assert _has_tag(tags, CORE_CANDIDATE_TAG), tags
    assert not _has_tag(tags, "core"), tags
    assert any(t.startswith("tier:auto-hot:") for t in tags.split(",")), tags

    # 判不出角色（空串）⇒ 按 sub（fail-closed）
    st2 = _FakeStorage()
    _provider(st2, is_primary=True).add("角色未知的调用方", tags="core",
                                        core_direct=True, caller_role="")
    assert _has_tag(st2.store_calls[0]["tags"], CORE_CANDIDATE_TAG), st2.store_calls[0]


def test_write_gate_primary_非core标签原样():
    """非 core 标签两边都原样（写闸只认完整标签 `core`）。"""
    st2 = _FakeStorage()
    _provider(st2, is_primary=False).add("子代理写普通记忆", tags="foo,bar")
    assert st2.store_calls[0]["tags"] == "foo,bar", st2.store_calls[0]


def test_write_gate_primary_写core_落候选并补召回基数(tmp_path, monkeypatch):
    """真 SQLite：primary 写 core ⇒ 落**普通条目**（候选标）+ 补 3000 条召回基数。

    三条断言各钉一件事：① 不进常驻层（常驻层是注入源，候选不进）；
    ② 候选标在（审核页看得到）；③ 基数在（7 天窗内被数到的命中 ≥3000 ——
    候选要撑满 7 天观察期，光靠自然召回打点攒不够）。
    """
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "no-config.json"))
    st = SqliteStorage(path=str(tmp_path / "gate.db"), is_primary=True)
    try:
        assert st.ensure_index() is True
        key = _provider(st, is_primary=True).add("主脑写一条常驻记忆", tags="core")
        assert key, "add 应返回 key"

        row = st.get_fragment(key) or {}
        assert _has_tag(row.get("tags", ""), CORE_CANDIDATE_TAG), row
        assert not _has_tag(row.get("tags", ""), "core"), row
        assert st.list_by_tag("core") == [], "候选不进常驻层"

        hits = st.hits_stats(window_days=7, min_hits=1, keys=[key])
        assert hits and hits[0]["hits"] >= 3000, hits
    finally:
        st.close()


def test_write_gate_同内容重写_基数不补在归档旧条上(tmp_path, monkeypatch):
    """同内容重写 ⇒ 存储层**版本化**（新条走 `key:epoch`，旧条归档）。

    这时 provider 手算的 key 指向的是**归档旧条** ⇒ 基数补在它身上 = 给一条
    归档条目凭空加热（还会被班车当成热门条目提名进审核队列）。所以重复内容
    不再补第二份基数。
    """
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "no-config.json"))
    st = SqliteStorage(path=str(tmp_path / "dup.db"), is_primary=True)
    try:
        assert st.ensure_index() is True
        p = _provider(st, is_primary=True)
        key = p.add("同一条常驻记忆", tags="core")
        assert key
        p.add("同一条常驻记忆", tags="core")          # 同内容 ⇒ 版本化
        with st._lock:                                 # noqa: SLF001
            rows = st._db().execute(                  # noqa: SLF001
                "SELECT key, count(*) FROM ks_hit_event GROUP BY key").fetchall()
        total = sum(int(c) for k, c in rows if str(k).startswith(key))
        assert total == 3000, f"重复内容不该再补一份基数：{rows}"
    finally:
        st.close()


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