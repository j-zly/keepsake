"""per-agent 注入预算（`ks_agent.core_budget_chars`）—— 预算下沉注册表的回归锁。

锁死的是**语义**，不是某个实现细节：

1. **各读各的**：A 配了预算 ⇒ **只有 A** 的 core 按它截断，B 完全不受影响；
2. **NULL 回退全局**：没配（NULL）的一律用全局值 —— 「新增字段不许改存量语义」，
   老库补列落 NULL ⇒ 存量行一个都没被改过；
3. **改完立刻生效**：读路径**不缓存**这一列（照 `share_memory` 的纪律）；
4. **脏值当「没配」**：0 / 负数 / 非整数一律回落全局（回落 0 = 常驻层全被截掉）；
5. **share_memory 既有语义不动**：两个开关各走各的列，互不干扰。

全程 hermetic：`tmp_path` 里的临时 SQLite + 真 `ks_agent` 表，`KEEPSAKE_CONFIG`
指向临时文件（全局兜底值可控），不连 Redis / PG / 网络 / 生产库。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from keepsake.govern.registry import Registry

#: 全局兜底预算（写进临时 config.json）—— 「没配 per-agent 预算」时用它。
GLOBAL_BUDGET = 5000

#: 每条常驻条目的正文长度（够短 ⇒ 预算 100 时只装得下一条）。
ITEM_CHARS = 60

#: 三条常驻条目的正文（末字用来认身份：甲 / 乙 / 丙）。
CORE_ITEMS: Tuple[str, ...] = ("甲", "乙", "丙")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture()
def cfg_path(tmp_path, monkeypatch) -> Path:
    """**指文件**的临时 config 路径（全局兜底预算从这儿读）。

    🔴 必须指文件：不指就会回落到 `~/.config/keepsake/config.json` ——
    真机上那份文件是存在的，测试会悄悄读到真配置（假绿）。
    """
    from keepsake.adapters.hermes import provider as prov_mod

    p = tmp_path / "config.json"
    p.write_text(json.dumps({"retrieval": {"core_char_budget": GLOBAL_BUDGET}}),
                 encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(p))
    prov_mod._CORE_BUDGET_CACHE.clear()      # noqa: SLF001
    return p


@pytest.fixture()
def env(tmp_path, cfg_path):
    """一套注册表（main-brain / agent-a / agent-b）+ 三条常驻条目。"""
    from keepsake.core.sqlite import SqliteStorage

    path = str(tmp_path / "budget.db")
    seed = SqliteStorage(path=path)
    assert seed.ensure_index() is True
    reg = Registry(seed)
    reg.ensure_schema()
    for aid in ("main-brain", "agent-a", "agent-b"):
        reg.register(aid, aid)
        reg.approve(aid)
    reg.set_role("main-brain", "primary")
    # 归属标签要原样落库 ⇒ 用**没有 agent_id 的**存储写（存储层 store() 会把
    # `agent:` 标签改写成自己的归属）。这里写的是**没有归属**的老数据 ——
    # 它对谁都可见（core 层可见性过滤的那一档）。
    for suffix in CORE_ITEMS:
        text = f"{suffix}" + "内" * (ITEM_CHARS - 1)
        assert seed.store(text=text, tags="core", category="probe", source="probe") is True
    seed.close()
    _reset_core_cache()
    yield reg, path
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


def _block(path: str, reg: Registry, agent_id: str, *, is_primary: bool = False,
           caller_agent_id: str = "", caller_role: str = "") -> str:
    """取某个读取方的 core 注入块（临时 provider，用完即关）。"""
    p, st = _provider(path, reg, agent_id=agent_id, is_primary=is_primary)
    try:
        return p._core_block(caller_agent_id, caller_role)      # noqa: SLF001
    finally:
        st.close()


def _suffixes(text: str) -> set:
    """注入块里出现的条目末字集合（甲 / 乙 / 丙）。"""
    return {c for c in text if c in CORE_ITEMS}


# ---------------------------------------------------------------------------
# ① 各读各的：A 配了预算，B 不受影响
# ---------------------------------------------------------------------------

def test_代理预算只截自己的core(env):
    """🔴 A 配了预算 ⇒ **只有 A** 的 core 被截；B 照旧走全局。"""
    reg, path = env
    assert reg.set_core_budget_chars("agent-a", 100) is True

    block_a = _block(path, reg, "agent-a")
    assert "core 层已截断" in block_a, block_a
    assert "本次注入 1 条" in block_a, block_a
    assert _suffixes(block_a) == {"甲"}, _suffixes(block_a)

    # B 没配预算 ⇒ 走全局（5000，装得下三条）⇒ 一条都不截
    block_b = _block(path, reg, "agent-b")
    assert "core 层已截断" not in block_b, block_b
    assert _suffixes(block_b) == {"甲", "乙", "丙"}, _suffixes(block_b)


def test_主脑进程按自己那一行的预算(env):
    """主脑进程（caller_agent_id 空 ⇒ 本进程自己）取**它自己那行**的预算。"""
    reg, path = env
    assert reg.set_core_budget_chars("main-brain", 100) is True
    block = _block(path, reg, "main-brain", is_primary=True)
    assert "本次注入 1 条" in block, block
    # 子代理没配 ⇒ 不受主脑那条影响
    assert "core 层已截断" not in _block(path, reg, "agent-b")


def test_远端调用方按它那一行的预算(env):
    """MCP 远端调用方（凭据反查出的身份）：按**它**的预算，不按进程自己的。"""
    reg, path = env
    assert reg.set_core_budget_chars("agent-a", 100) is True
    # 进程自己是主脑（全局装得下三条），但调用方是 agent-a（预算 100）
    block = _block(path, reg, "main-brain", is_primary=True,
                   caller_agent_id="agent-a", caller_role="sub")
    assert "本次注入 1 条" in block, block


# ---------------------------------------------------------------------------
# ② NULL 回退全局
# ---------------------------------------------------------------------------

def test_没配预算就回退全局(env):
    """🔴 NULL = 沿用全局（老行一个都没被改过）。"""
    reg, path = env
    assert reg.core_budget_chars("agent-a") is None
    block = _block(path, reg, "agent-a")
    assert "core 层已截断" not in block, block
    assert _suffixes(block) == {"甲", "乙", "丙"}


def test_全局也放不下时按全局截(env, monkeypatch):
    """全局兜底值也生效（per-agent 没配时，截断阈值 = 全局）。"""
    from keepsake.adapters.hermes import provider as prov_mod

    reg, path = env
    p = cfg_path = Path(prov_mod.config_path())
    p.write_text(json.dumps({"retrieval": {"core_char_budget": 100}}),
                 encoding="utf-8")
    prov_mod._CORE_BUDGET_CACHE.clear()          # noqa: SLF001
    try:
        block = _block(path, reg, "agent-a")
        assert "本次注入 1 条" in block, block
    finally:
        prov_mod._CORE_BUDGET_CACHE.clear()      # noqa: SLF001


# ---------------------------------------------------------------------------
# ③ 改完立刻生效（读路径不缓存这一列）
# ---------------------------------------------------------------------------

def test_改预算立刻生效(env):
    """🔴 改完立刻能查到（读路径不缓存这一列 —— 照 share_memory 的纪律）。"""
    reg, path = env
    p, st = _provider(path, reg, agent_id="agent-a")
    try:
        assert "core 层已截断" not in p._core_block()      # noqa: SLF001
        reg.set_core_budget_chars("agent-a", 100)
        assert "本次注入 1 条" in p._core_block()          # noqa: SLF001
        reg.set_core_budget_chars("agent-a", None)
        assert "core 层已截断" not in p._core_block()      # noqa: SLF001
    finally:
        st.close()


# ---------------------------------------------------------------------------
# ④ 注册表层：读写 / 幂等 / 脏值
# ---------------------------------------------------------------------------

def test_注册表读写与幂等(env):
    reg, _ = env
    assert reg.core_budget_chars("agent-a") is None
    assert reg.set_core_budget_chars("agent-a", 100) is True
    assert reg.core_budget_chars("agent-a") == 100
    assert reg.set_core_budget_chars("agent-a", 100) is False        # 没变
    assert reg.set_core_budget_chars("agent-a", None) is True       # 清空
    assert reg.core_budget_chars("agent-a") is None


def test_查无此代理(env):
    reg, _ = env
    assert reg.core_budget_chars("查无此代理") is None
    assert reg.set_core_budget_chars("查无此代理", 100) is False


def test_改预算不碰状态角色与凭据(env):
    reg, _ = env
    before = reg.get("agent-a")
    reg.set_core_budget_chars("agent-a", 100)
    after = reg.get("agent-a")
    assert after["status"] == before["status"] == "approved"
    assert after["role"] == before["role"]
    assert after["credential_hint"] == before["credential_hint"]


def test_脏值当没配(env):
    """🔴 0 / 负数 / 非整数一律当「没配」⇒ 回落全局（回落 0 = 全被截掉，更糟）。"""
    reg, path = env
    from keepsake.core.sqlite import SqliteStorage

    st = SqliteStorage(path=path)
    try:
        for bad in (0, -5, "abc"):
            st._db().execute(              # noqa: SLF001 —— 直写脏数据
                "UPDATE ks_agent SET core_budget_chars = ? WHERE agent_id = ?",
                (bad, "agent-a"))
            st._db().commit()              # noqa: SLF001
            assert reg.core_budget_chars("agent-a") is None, bad
    finally:
        st.close()


def test_老库没有这一列按没配处理(tmp_path):
    """🔴 老库（`ks_agent` 里**没有** `core_budget_chars` 列）补列后，存量行一律
    「没配」—— 「新增字段必须兼容旧数据」的那一档。
    """
    import sqlite3

    from keepsake.core.sqlite import SqliteStorage

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE ks_agent ("
        "agent_id text PRIMARY KEY, name text NOT NULL DEFAULT '', "
        "role text NOT NULL DEFAULT 'sub', status text NOT NULL DEFAULT 'pending', "
        "credential_hash text NOT NULL DEFAULT '', credential_hint text NOT NULL DEFAULT '', "
        "first_seen_at text NOT NULL DEFAULT '', last_seen_at text NOT NULL DEFAULT '', "
        "reads integer NOT NULL DEFAULT 0, writes integer NOT NULL DEFAULT 0, "
        "exclude_from_primary integer NOT NULL DEFAULT 0, "
        "meta text NOT NULL DEFAULT '{}', dm_enabled text NOT NULL DEFAULT '1', "
        "share_memory text NOT NULL DEFAULT '0')"
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
        assert reg.core_budget_chars("old-row") is None
        assert reg.get("old-row")["core_budget_chars"] is None
    finally:
        st.close()


def test_redis后端同一份语义():
    """🔴 三后端一份语义：Redis hash 上的 `core_budget_chars` 字段与 SQL 后端同款。"""
    from test_agent_registry import FakeRedisStorage   # 复用既有假 client，不复制一份

    st = FakeRedisStorage()
    reg = Registry(st)
    reg.ensure_schema()
    reg.register("agent-a", "agent-a")
    reg.approve("agent-a")
    assert reg.core_budget_chars("agent-a") is None        # 缺省 = 没配
    assert reg.set_core_budget_chars("agent-a", 100) is True
    assert reg.core_budget_chars("agent-a") == 100
    assert reg.get("agent-a")["core_budget_chars"] == 100
    assert reg.set_core_budget_chars("agent-a", 100) is False    # 幂等
    assert reg.set_core_budget_chars("agent-a", None) is True
    assert reg.core_budget_chars("agent-a") is None


# ---------------------------------------------------------------------------
# ⑤ share_memory 既有语义回归不动
# ---------------------------------------------------------------------------

def test_预算与共享开关各走各的列(env):
    """🔴 两个开关互不干扰：改预算不动 `share_memory`，反之亦然。"""
    reg, _ = env
    assert reg.set_share_memory("agent-a", True) is True
    assert reg.set_core_budget_chars("agent-a", 100) is True
    row = reg.get("agent-a")
    assert row["share_memory"] is True
    assert row["core_budget_chars"] == 100
    assert reg.set_share_memory("agent-a", False) is True
    assert reg.core_budget_chars("agent-a") == 100


def test_共享可见性在有预算时照样成立(env):
    """有预算**不改变**可见性：没共享的代理的 core 照样看不见。"""
    reg, path = env
    reg.set_core_budget_chars("agent-b", 100)
    p, st = _provider(path, reg, agent_id="agent-b")
    try:
        frags = p._core_fragments(caller_agent_id="agent-b", caller_role="sub")  # noqa: SLF001
        assert _suffixes("".join(str(f.get("content") or "") for f in frags)) == {"甲", "乙", "丙"}
    finally:
        st.close()
