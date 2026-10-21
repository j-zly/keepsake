"""记忆共享开关（per-agent `share_memory`）—— 可见性过滤器的回归锁。

锁死的是**语义**，不是某个实现细节：

1. **primary 检索 = 全量**（现状不变，硬回归线）：开关上线前后主脑看到的
   一模一样；
2. **共享开 ⇒ 进别人的检索可见范围**：A 打开共享，B 检索得到 A 的记忆；
3. **共享关（缺省）⇒ 只有自己 + 主脑看得见**：B 检索不到 C 的记忆；
4. **按 key 直取也要过归属校验**：B 直取 C 的条目 ⇒ **not-found 语义**
   （存在性本身也不泄露）；
5. **切换即时生效**：改完立刻能查到（过滤器**不缓存**）；
6. **缺省是「关」**：新登记的代理 / 老库里没有这一列的行，都按关处理
   （「新增字段不许改存量语义」）。

全程 hermetic：`tmp_path` 里的临时 SQLite + 真 `ks_agent` 表，不连
Redis / PG / 网络 / 生产库，也不碰主脑的真实 config。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from keepsake.core.shared import fragment_key_for
from keepsake.govern.registry import STATUS_APPROVED, Registry
from keepsake.govern.visibility import (
    fragment_owner,
    shared_agent_ids,
    visible_agent_ids,
    visible_core_fragments,
)

#: 一条能被 BM25 命中的公共查询词（jieba 分词后能切出实词）
QUERY = "回滚 流程 校验"

#: 语料：(正文后缀, 落库 tags)
CORPUS: Tuple[Tuple[str, str], ...] = (
    ("甲", "agent:agent-a"),     # A 的私有记忆
    ("乙", "agent:agent-b"),     # B 自己的
    ("丙", "agent:agent-c"),     # C 的私有记忆（谁都不共享）
    ("丁", "core"),              # 常驻层（primary 写的）
    ("戊", "shared"),            # 共享层（本来就能召回）
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path):
    """一套注册表 + 一份种好语料的 SQLite 库。

    语料用**独立于 provider 的存储**写（那个存储没有 `agent_id` ⇒ 不注入归属
    标签），这样 `agent:agent-a` 这类归属标签能原样落库。
    """
    from keepsake.core.sqlite import SqliteStorage

    path = str(tmp_path / "share.db")
    seed = SqliteStorage(path=path)
    assert seed.ensure_index() is True
    reg = Registry(seed)
    reg.ensure_schema()
    for aid in ("primary", "agent-a", "agent-b", "agent-c"):
        reg.register(aid, aid)
        reg.approve(aid)
    reg.set_role("primary", "primary")

    keys: Dict[str, str] = {}
    for suffix, tags in CORPUS:
        text = f"{QUERY} {suffix}"
        assert seed.store(text=text, tags=tags, category="probe", source="probe") is True
        keys[suffix] = fragment_key_for(text)      # store() 的 key = 内容 hash
    seed.close()
    _reset_core_cache()
    yield reg, path, keys
    _reset_core_cache()


def _reset_core_cache() -> None:
    """清掉 core 层的**进程级**缓存（它跨用例存活，不清会串味）。"""
    from keepsake.adapters.hermes import provider as prov_mod

    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})      # noqa: SLF001


def _provider(path: str, reg: Registry, *, agent_id: str = "", is_primary: bool = False):
    """一个**已装配注册表**的 provider（不跑完整 initialize：那条要连 Redis/embedding）。"""
    from keepsake.adapters.hermes.provider import KeepsakeProvider
    from keepsake.core.sqlite import SqliteStorage

    st = SqliteStorage(path=path, agent_id=agent_id, is_primary=is_primary)
    assert st.ensure_index() is True
    p = KeepsakeProvider()
    p._storage = st
    p._registry = reg
    p._agent_id = agent_id
    p._is_primary = is_primary
    return p, st


def _suffixes(hits: List[Dict[str, Any]]) -> set:
    """命中正文的后缀集合（甲/乙/丙/丁/戊）。"""
    return {str(h.get("content", ""))[-1] for h in hits}


# ---------------------------------------------------------------------------
# ① primary 检索 = 全量（现状不变，硬回归线）
# ---------------------------------------------------------------------------

def test_primary检索仍是全量(env):
    """🔴 开关上线前后主脑看到的必须一模一样：别人的私有条目照样看得见。"""
    reg, path, _ = env
    p, st = _provider(path, reg, agent_id="primary", is_primary=True)
    try:
        who = _suffixes(p.search(QUERY, limit=20))
        assert who == {"甲", "乙", "丙", "丁", "戊"}, who
    finally:
        st.close()


def test_primary按key直取照样取得到(env):
    reg, path, keys = env
    p, st = _provider(path, reg, agent_id="primary", is_primary=True)
    try:
        assert p.get(keys["丙"]) is not None
    finally:
        st.close()


# ---------------------------------------------------------------------------
# ② 共享开 ⇒ 进别人的检索可见范围；共享关 ⇒ 看不见
# ---------------------------------------------------------------------------

def test_共享关时子代理B看不见A的记忆(env):
    reg, path, _ = env
    assert reg.share_memory("agent-a") is False        # 缺省就是关
    p, st = _provider(path, reg, agent_id="agent-b", is_primary=False)
    try:
        who = _suffixes(p.search(QUERY, limit=20))
        assert "甲" not in who, who
        assert "丙" not in who, who
        assert "乙" in who and "丁" in who and "戊" in who, who
    finally:
        st.close()


def test_共享打开后B看得见A的记忆(env):
    reg, path, _ = env
    assert reg.set_share_memory("agent-a", True) is True
    p, st = _provider(path, reg, agent_id="agent-b", is_primary=False)
    try:
        who = _suffixes(p.search(QUERY, limit=20))
        assert "甲" in who, who
        assert "丙" not in who, who     # C 没开共享 ⇒ 仍然看不见
    finally:
        st.close()


def test_远端调用方身份按它算(env):
    """MCP 远端调用方（凭据反查出来的身份）：可见范围按**它**算，不按存储自己算。"""
    reg, path, _ = env
    reg.set_share_memory("agent-a", True)
    p, st = _provider(path, reg, agent_id="primary", is_primary=True)
    try:
        who = _suffixes(p.search(QUERY, limit=20, caller_agent_id="agent-b",
                                 caller_role="sub"))
        assert "甲" in who, who
        assert "丙" not in who, who
    finally:
        st.close()


def test_远端调用方是primary时全量可见(env):
    reg, path, _ = env
    p, st = _provider(path, reg, agent_id="agent-b", is_primary=False)
    try:
        who = _suffixes(p.search(QUERY, limit=20, caller_agent_id="primary",
                                 caller_role="primary"))
        assert who == {"甲", "乙", "丙", "丁", "戊"}, who
    finally:
        st.close()


# ---------------------------------------------------------------------------
# ③ 按 key 直取：未共享代理的条目 ⇒ not-found
# ---------------------------------------------------------------------------

def test_直取未共享代理的条目返回not_found(env):
    reg, path, keys = env
    p, st = _provider(path, reg, agent_id="agent-b", is_primary=False)
    try:
        assert p.get(keys["丙"]) is None          # C 没共享 ⇒ 存在性也不泄露
    finally:
        st.close()


def test_直取共享代理的条目取得到(env):
    reg, path, keys = env
    reg.set_share_memory("agent-a", True)
    p, st = _provider(path, reg, agent_id="agent-b", is_primary=False)
    try:
        assert p.get(keys["甲"]) is not None
    finally:
        st.close()


def test_直取无归属条目人人可见(env):
    """老数据 / 共享层的条目没有归属标签 ⇒ 人人可见（不许把老数据一起关掉）。"""
    reg, path, keys = env
    p, st = _provider(path, reg, agent_id="agent-b", is_primary=False)
    try:
        assert p.get(keys["戊"]) is not None
    finally:
        st.close()


# ---------------------------------------------------------------------------
# ④ 切换即时生效（过滤器不缓存）
# ---------------------------------------------------------------------------

def test_开关切换即时生效(env):
    reg, path, _ = env
    p, st = _provider(path, reg, agent_id="agent-b", is_primary=False)
    try:
        assert "甲" not in _suffixes(p.search(QUERY, limit=20))
        reg.set_share_memory("agent-a", True)
        assert "甲" in _suffixes(p.search(QUERY, limit=20))     # 立刻可见
        reg.set_share_memory("agent-a", False)
        assert "甲" not in _suffixes(p.search(QUERY, limit=20))   # 立刻收回
    finally:
        st.close()


def test_未批准的代理不共享(env):
    """共享集只认 `status=approved`：pending / revoked 的代理不进共享范围。"""
    reg, path, _ = env
    reg.set_share_memory("agent-c", True)
    reg.revoke("agent-c")
    assert "agent-c" not in shared_agent_ids(reg)


# ---------------------------------------------------------------------------
# ⑤ 过滤器口径本身（单点）
# ---------------------------------------------------------------------------

def test_过滤器_primary不过滤_子代理是共享集加自己(env):
    reg, path, _ = env
    reg.set_share_memory("agent-a", True)
    assert visible_agent_ids("primary", reg) is None
    assert visible_agent_ids("agent-b", reg) == {"agent-b", "agent-a"}
    assert visible_agent_ids("agent-c", reg) == {"agent-c", "agent-a"}


def test_过滤器_没有注册表时不过滤(env):
    reg, path, _ = env
    assert visible_agent_ids("agent-b", None) is None


def test_归属标签按逗号边界取(env):
    assert fragment_owner("a,agent:win,b") == "win"
    assert fragment_owner("agent:win-desktop") == "win-desktop"
    assert fragment_owner("agentx:win") == ""
    assert fragment_owner("") == ""


def test_core按同口径过滤_非primary只留primary与已共享的(env):
    reg, path, _ = env
    reg.set_share_memory("agent-a", True)
    frags = [
        {"key": "k1", "content": "c", "tags": "core,agent:primary"},
        {"key": "k2", "content": "c", "tags": "core,agent:agent-a"},
        {"key": "k3", "content": "c", "tags": "core,agent:agent-c"},
        {"key": "k4", "content": "c", "tags": "core"},          # 老数据：没有归属
    ]
    keys = [f["key"] for f in visible_core_fragments(frags, reg, "agent-b")]
    assert keys == ["k1", "k2", "k4"], keys
    # primary 全量可见（现状逐字不变）
    assert len(visible_core_fragments(frags, reg, "primary")) == 4


def test_core资源在provider侧同口径(env):
    """provider 的 core 层（非 primary 调用方）只给 primary 的 + 已共享代理的 core。"""
    reg, path, _ = env
    reg.set_share_memory("agent-a", True)
    p, st = _provider(path, reg, agent_id="agent-b", is_primary=False)
    try:
        # 归属标签要原样落库 ⇒ 用**没有 agent_id 的**那个存储写
        # （存储层 store() 会把 agent: 标签改写成自己的归属）。
        from keepsake.core.sqlite import SqliteStorage

        writer = SqliteStorage(path=path)
        try:
            writer.store(text="primary 的常驻", tags="core,agent:primary")
            writer.store(text="C 的常驻", tags="core,agent:agent-c")
        finally:
            writer.close()
        frags = p._core_fragments(caller_agent_id="agent-b", caller_role="sub")
        got = sorted(str(f.get("content")) for f in frags)
        # primary 的 core 可见；C 的 core 不可见；**没有归属的老数据**继续可见
        assert got == sorted(["primary 的常驻", f"{QUERY} 丁"]), got
    finally:
        st.close()


# ---------------------------------------------------------------------------
# ⑥ 注册表层：缺省「关」+ 老行兼容
# ---------------------------------------------------------------------------

def test_新登记的代理缺省不共享(env):
    reg, path, _ = env
    reg.register("agent-new", "agent-new")
    assert reg.share_memory("agent-new") is False
    assert reg.get("agent-new")["share_memory"] is False


def test_查无此代理按不共享(env):
    reg, path, _ = env
    assert reg.share_memory("查无此代理") is False
    assert reg.set_share_memory("查无此代理", True) is False


def test_开关幂等且报是否变化(env):
    reg, path, _ = env
    assert reg.set_share_memory("agent-a", True) is True
    assert reg.set_share_memory("agent-a", True) is False     # 没变
    assert reg.set_share_memory("agent-a", False) is True


def test_开关不碰状态角色与凭据(env):
    reg, path, _ = env
    before = reg.get("agent-a")
    reg.set_share_memory("agent-a", True)
    after = reg.get("agent-a")
    assert after["status"] == before["status"] == STATUS_APPROVED
    assert after["role"] == before["role"]
    assert after["credential_hash" if "credential_hash" in after else "credential_hint"] == \
        before["credential_hint"]


def test_redis后端同一份语义():
    """🔴 三后端一份语义：Redis hash 上的 `share_memory` 字段与 SQL 后端同款。"""
    from test_agent_registry import FakeRedisStorage   # 复用既有假 client，不复制一份

    st = FakeRedisStorage()
    reg = Registry(st)
    reg.ensure_schema()
    reg.register("agent-a", "agent-a")
    reg.approve("agent-a")
    assert reg.share_memory("agent-a") is False          # 缺省就是关
    assert reg.set_share_memory("agent-a", True) is True
    assert reg.share_memory("agent-a") is True
    assert reg.get("agent-a")["share_memory"] is True
    assert reg.set_share_memory("agent-a", True) is False    # 幂等


# ---------------------------------------------------------------------------
# ⑦ PG 真库：查询期标签过滤同一套口径（无凭据就 skip）
# ---------------------------------------------------------------------------

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
def pg_seeded():
    """PG 隔离 schema 里灌同一份语料（与 sqlite 同一断言口径）。"""
    schema = f"ks_sm_{uuid.uuid4().hex[:12]}"
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
def test_pg真库_共享集进查询期标签过滤(pg_seeded):
    """🔴 共享集是**查询期**的标签过滤（不是事后筛）：B 检索时看得见 A，看不见 C。"""
    who = {str(r["content"])[-1] for r in
           pg_seeded.search_bm25(QUERY, tag_filter="", agent_id="agent-b",
                                 is_primary=False, visible_agents=["agent-a"])}
    # 甲（A 的私有，靠共享集进来）/ 乙（自己）/ 丁（core）/ 戊（shared）都看得见；
    # 丙（C 的私有，没共享）看不见 —— core 与 shared 本来就对非 primary 可见。
    assert who == {"甲", "乙", "丁", "戊"}, who
    assert "丙" not in who, who


@_pg_skip
def test_pg真库_不给共享集时与开关上线前一致(pg_seeded):
    """共享集为空 ⇒ 可见集合与开关上线前**逐字相同**（现状不变回归线）。"""
    who = {str(r["content"])[-1] for r in
           pg_seeded.search_bm25(QUERY, tag_filter="", agent_id="agent-b",
                                 is_primary=False)}
    assert who == {"乙", "丁", "戊"}, who


def test_老库没有这一列按关处理(tmp_path):
    """🔴 老库（`ks_agent` 里**没有** `share_memory` 列）补列后，存量行一律当**关**。

    这是「新增字段必须兼容旧数据」的那一档：补列落 `DEFAULT '0'` ⇒ 谁也没被
    共享出去过，存量行的语义一个字没变。
    """
    import sqlite3

    from keepsake.core.sqlite import SqliteStorage

    path = str(tmp_path / "legacy.db")
    # 造一张**开关上线前**的 ks_agent（没有 share_memory 列）
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE ks_agent ("
        "agent_id text PRIMARY KEY, name text NOT NULL DEFAULT '', "
        "role text NOT NULL DEFAULT 'sub', status text NOT NULL DEFAULT 'pending', "
        "credential_hash text NOT NULL DEFAULT '', credential_hint text NOT NULL DEFAULT '', "
        "first_seen_at text NOT NULL DEFAULT '', last_seen_at text NOT NULL DEFAULT '', "
        "reads integer NOT NULL DEFAULT 0, writes integer NOT NULL DEFAULT 0, "
        "exclude_from_primary integer NOT NULL DEFAULT 0, "
        "meta text NOT NULL DEFAULT '{}', dm_enabled text NOT NULL DEFAULT '1')"
    )
    conn.execute("INSERT INTO ks_agent (agent_id, name, role, status) "
                 "VALUES ('old-row', 'old-row', 'sub', 'approved')")
    conn.commit()
    conn.close()

    st = SqliteStorage(path=path)
    assert st.ensure_index() is True
    reg = Registry(st)
    assert reg.ensure_schema() is True          # 补列
    try:
        assert reg.share_memory("old-row") is False
        assert reg.get("old-row")["share_memory"] is False
    finally:
        st.close()
