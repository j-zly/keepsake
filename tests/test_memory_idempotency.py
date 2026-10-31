"""写请求幂等键（idempotency_key）定向测试 —— 2026-10 ks_idem。

治的病：MCP 调用 300s 超时（客户端**没收到响应**）⇒ 客户端重试 ⇒ 正文稍有差异
⇒ 按内容哈希落**第二条同题条目**（zly-hermes #135 实锤）。

本文件锁死的语义：

  1. **同键两次写** ⇒ 第二次 `duplicated=True`、同一个 key、**库内条目数不涨**；
  2. **作用域 = 归属代理** ⇒ A 代理的键命中不了 B 代理的条目；
  3. **不同键** ⇒ 两条（各写各的）；
  4. **TTL 7 天** ⇒ 过期登记视为未命中（重写），且那行被顺手删掉；
  5. **Redis 后端** ⇒ 带键**显式报错**（绝不静默降级成「照写不误」）；
  6. **空键** ⇒ 行为逐字不变（返回值里也不多 `duplicated` 字段）。

## PG 侧的覆盖方式（这台机器没有 PG 测试库）

  * 「**PG 代码路径跑在真 SQLite 上**」的方言垫片：跑的是 `PgStorage` 那份
    实现发出的**同一批 SQL**，垫片只做两处最小改写（`%s` → `?`、
    `ON CONFLICT (cols) DO NOTHING` → SQLite 允许的 `ON CONFLICT DO NOTHING`），
    其余**一字不改**；
  * 真库用例（读不到 `~/.keepsake_pg_test.env` 则 skip）—— 与
    `tests/test_hit_tracking.py` 同一套接法。

本文件 **hermetic**：只用 tmp_path 里的临时 SQLite 文件 + 内存里的 provider，
不连生产库 / Redis / 网络。
"""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from keepsake.adapters.hermes.provider import KeepsakeProvider
from keepsake.core.base import BackendUnsupportedError, IDEM_TTL_DAYS
from keepsake.core.redis import RedisStorage
from keepsake.core.sqlite import SqliteStorage

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST = os.environ.get("KEEPSAKE_PG_TEST_HOST", "postgres.test.example")
ALLOWED_DBNAME = "keepsake_test"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _make_provider(path: str, *, agent_id: str = "", is_primary: bool = True
                   ) -> KeepsakeProvider:
    """provider + 真 SqliteStorage（tmp 文件库，不连任何外部服务）。"""
    storage = SqliteStorage(path=path, agent_id=agent_id,
                            is_primary=is_primary, final_limit=10)
    assert storage.ensure_index() is True
    provider = KeepsakeProvider()
    provider._storage = storage
    return provider


@pytest.fixture()
def provider(tmp_path):
    p = _make_provider(str(tmp_path / "idem.db"))
    yield p
    p._storage.close()


def _frag_count(storage: SqliteStorage) -> int:
    """库内碎片条数（判「有没有真的多落一条」）。"""
    with storage._lock:                       # noqa: SLF001 — 测试直读库
        return int(storage._db().execute(
            "SELECT COUNT(*) FROM ks_fragment").fetchone()[0])


def _idem_rows(storage: SqliteStorage) -> List[Tuple[str, str, str, str]]:
    with storage._lock:                       # noqa: SLF001 — 测试直读库
        return list(storage._db().execute(
            "SELECT owner, idem_key, frag_key, created FROM ks_idem_event"))


def _seed_expired_idem(storage: SqliteStorage, owner: str, idem_key: str,
                       frag_key: str) -> None:
    """手工塞一条**已过期**的幂等登记（TTL 边界之外 ⇒ 判重时必须视为未命中）。"""
    expired = "2020-01-01T00:00:00.000000+00:00"
    with storage._lock:                       # noqa: SLF001 — 测试直写库
        storage._db().execute(
            "INSERT OR REPLACE INTO ks_idem_event (owner, idem_key, frag_key, created) "
            "VALUES (?, ?, ?, ?)", (owner, idem_key, frag_key, expired))


# ---------------------------------------------------------------------------
# ① 存储原语（SQLite 真库）
# ---------------------------------------------------------------------------

def test_同键登记两次_第二次命中且指向首次落库(provider):
    """同 `(owner, idem_key)` 登记两次 ⇒ 第二次命中**首次**那条（不被覆盖）。"""
    st = provider._storage                   # noqa: SLF001
    assert st.idem_record("agent-a", "K1", "memory:frag:first") is True
    assert st.idem_record("agent-a", "K1", "memory:frag:second") is True
    hit = st.idem_lookup("agent-a", "K1")
    assert hit is not None
    assert hit["frag_key"] == "memory:frag:first", hit


def test_未命中返回None(provider):
    st = provider._storage                   # noqa: SLF001
    assert st.idem_lookup("agent-a", "没登记过的键") is None


def test_跨owner同键互不命中(provider):
    """🔴 作用域 = 归属代理：A 的键**命中不了** B 的登记。"""
    st = provider._storage                   # noqa: SLF001
    assert st.idem_record("agent-a", "K1", "memory:frag:a") is True
    assert st.idem_lookup("agent-b", "K1") is None
    assert st.idem_lookup("agent-a", "K1")["frag_key"] == "memory:frag:a"


def test_不同键两条(provider):
    st = provider._storage                   # noqa: SLF001
    assert st.idem_record("agent-a", "K1", "memory:frag:one") is True
    assert st.idem_record("agent-a", "K2", "memory:frag:two") is True
    assert st.idem_lookup("agent-a", "K1")["frag_key"] == "memory:frag:one"
    assert st.idem_lookup("agent-a", "K2")["frag_key"] == "memory:frag:two"


def test_过期登记视为未命中且那行被删(provider):
    """TTL 到期 ⇒ 判重**视为未命中**，并顺手把过期行删掉。"""
    st = provider._storage                   # noqa: SLF001
    _seed_expired_idem(st, "agent-a", "K-old", "memory:frag:旧")
    assert st.idem_lookup("agent-a", "K-old") is None
    assert _idem_rows(st) == [], "过期行必须被顺手删掉"


def test_未过期登记不因惰性清理被删(provider):
    """惰性清理的删除条件带 `created < 截止` ⇒ 没过期的行**不许**被删。"""
    st = provider._storage                   # noqa: SLF001
    assert st.idem_record("agent-a", "K1", "memory:frag:alive") is True
    assert st.idem_lookup("agent-a", "K1") is not None
    assert len(_idem_rows(st)) == 1


def test_idem_purge删过期留新(provider):
    st = provider._storage                   # noqa: SLF001
    _seed_expired_idem(st, "agent-a", "K-old", "memory:frag:旧")
    assert st.idem_record("agent-a", "K-new", "memory:frag:新") is True
    # 截止点夹在两行之间 ⇒ 只删过期那行（"2020" 行 < 截止 < "2026" 行）
    assert st.idem_purge("2025-01-01T00:00:00.000000+00:00") == 1
    assert [r[1] for r in _idem_rows(st)] == ["K-new"]
    assert st.idem_purge("2025-01-01T00:00:00.000000+00:00") == 0


def test_空键不登记_空owner是合法作用域(provider):
    """空 `idem_key` ⇒ 不登记（没有键谈不上幂等）。

    空 `owner` 是**合法作用域**（=「无归属」那一类）：作用域必须与落库打标
    同一把尺 —— 没有归属的条目，打标也就没有归属。
    """
    st = provider._storage                   # noqa: SLF001
    assert st.idem_record("agent-a", "", "memory:frag:x") is False
    assert st.idem_record("agent-a", "K1", "") is False
    assert st.idem_lookup("agent-a", "") is None
    assert st.idem_record("", "K1", "memory:frag:x") is True
    assert st.idem_lookup("", "K1")["frag_key"] == "memory:frag:x"
    assert st.idem_lookup("agent-a", "K1") is None, "空 owner 与有 owner 互不命中"


def test_TTL常量是七天():
    assert IDEM_TTL_DAYS == 7


# ---------------------------------------------------------------------------
# ② PG 代码路径（真 SQL 语义跑在 SQLite 上 —— 方言垫片）
# ---------------------------------------------------------------------------

_ON_CONFLICT_RE = re.compile(r"ON CONFLICT\s*\([^)]*\)\s*DO NOTHING", re.I)


def _to_sqlite_sql(sql: str) -> str:
    """垫片的**全部**改写（只有两处，其余一字不改）。"""
    return _ON_CONFLICT_RE.sub("ON CONFLICT DO NOTHING", sql.replace("%s", "?"))


class _ShimCursor:
    """假 PG 游标 → 真 SQLite 游标（顺手记下**原始** SQL 供断言）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._last: Optional[sqlite3.Cursor] = None
        self.raw_sql: List[str] = []
        self.rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        self.raw_sql.append(" ".join(sql.split()))
        self._last = self._conn.execute(_to_sqlite_sql(sql), tuple(params or ()))
        self.rowcount = self._last.rowcount

    def fetchone(self) -> Any:
        return self._last.fetchone() if self._last is not None else None


@pytest.fixture()
def pg_on_sqlite():
    """PgStorage 的幂等实现 + 真 SQLite 执行器（PG 代码路径 / 真 SQL 语义）。"""
    from keepsake.core.pg import PgStorage

    pg = PgStorage(dsn="postgresql://user@localhost/keepsake")
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("""
        CREATE TABLE ks_idem_event (
            owner    text NOT NULL,
            idem_key text NOT NULL,
            frag_key text NOT NULL,
            created  text NOT NULL,
            PRIMARY KEY (owner, idem_key)
        )
    """)
    cursor_holder: Dict[str, _ShimCursor] = {}

    @contextmanager
    def _tx():
        cur = _ShimCursor(conn)
        cursor_holder["cur"] = cur
        yield cur

    pg._tx = _tx                            # noqa: SLF001 — 换掉真连库那层
    pg._shim_conn = conn                   # noqa: SLF001 — 测试自用
    pg._shim_cursor = cursor_holder        # noqa: SLF001 — 测试自用
    yield pg
    conn.close()


def test_pg_同键两次登记_第二次命中首次(pg_on_sqlite):
    pg = pg_on_sqlite
    assert pg.idem_record("agent-a", "K1", "memory:frag:first") is True
    assert pg.idem_record("agent-a", "K1", "memory:frag:second") is True
    hit = pg.idem_lookup("agent-a", "K1")
    assert hit is not None and hit["frag_key"] == "memory:frag:first", hit


def test_pg_未命中回None(pg_on_sqlite):
    assert pg_on_sqlite.idem_lookup("agent-a", "没登记过") is None


def test_pg_跨owner同键互不命中(pg_on_sqlite):
    pg = pg_on_sqlite
    pg.idem_record("agent-a", "K1", "memory:frag:a")
    assert pg.idem_lookup("agent-b", "K1") is None
    assert pg.idem_lookup("agent-a", "K1")["frag_key"] == "memory:frag:a"


def test_pg_过期登记视为未命中(pg_on_sqlite):
    pg = pg_on_sqlite
    pg._shim_conn.execute(                  # noqa: SLF001 — 手工塞过期行
        "INSERT INTO ks_idem_event (owner, idem_key, frag_key, created) "
        "VALUES (?, ?, ?, ?)",
        ("agent-a", "K-old", "memory:frag:旧", "2020-01-01T00:00:00.000000+00:00"))
    assert pg.idem_lookup("agent-a", "K-old") is None
    left = pg._shim_conn.execute(           # noqa: SLF001 — 过期行应被顺手删掉
        "SELECT COUNT(*) FROM ks_idem_event").fetchone()[0]
    assert left == 0, "过期行必须被顺手删掉"


def test_pg_idem_purge删过期留新(pg_on_sqlite):
    pg = pg_on_sqlite
    pg._shim_conn.execute(                  # noqa: SLF001
        "INSERT INTO ks_idem_event (owner, idem_key, frag_key, created) "
        "VALUES (?, ?, ?, ?)",
        ("agent-a", "K-old", "memory:frag:旧", "2020-01-01T00:00:00.000000+00:00"))
    pg.idem_record("agent-a", "K-new", "memory:frag:新")
    assert pg.idem_purge("2025-01-01T00:00:00.000000+00:00") == 1
    assert pg.idem_purge("2025-01-01T00:00:00.000000+00:00") == 0


def test_pg_发的SQL形状对(pg_on_sqlite):
    """钉住 PG 那几条语句的关键子句（垫片只改方言，不改语义）。"""
    pg = pg_on_sqlite
    pg.idem_lookup("agent-a", "K1")
    sqls = pg._shim_cursor["cur"].raw_sql  # noqa: SLF001
    assert any("DELETE FROM ks_idem_event" in s for s in sqls), sqls
    assert any("SELECT frag_key, created FROM ks_idem_event" in s for s in sqls), sqls
    pg.idem_record("agent-a", "K1", "memory:frag:x")
    ins = [s for s in pg._shim_cursor["cur"].raw_sql if "INSERT INTO ks_idem_event" in s]
    assert ins and "ON CONFLICT (owner, idem_key) DO NOTHING" in ins[0], ins


def test_pg_三后端都声明幂等原语且签名一致():
    """PG / SQLite 真实现；Redis 显式抛「不支持」—— 但**方法必须在**。

    🔴 漏实现的后端会让调用方拿到 AttributeError（而不是一句人话）⇒
    这里钉住「方法存在 + 签名一致」。
    """
    import inspect

    from keepsake.core.pg import PgStorage

    for cls in (PgStorage, SqliteStorage, RedisStorage):
        for name in ("idem_lookup", "idem_record"):
            fn = getattr(cls, name, None)
            assert callable(fn), f"{cls.__name__}.{name} 缺失"
            params = list(inspect.signature(fn).parameters.values())[1:]
            assert params[0].name == "owner", (cls.__name__, name)
            assert params[1].name == "idem_key", (cls.__name__, name)
        purge = getattr(cls, "idem_purge", None)
        assert callable(purge), f"{cls.__name__}.idem_purge 缺失"
        assert list(inspect.signature(purge).parameters.values())[1].name \
            == "older_than_iso", cls.__name__
    assert PgStorage.supports_idempotency is True
    assert SqliteStorage.supports_idempotency is True
    assert RedisStorage.supports_idempotency is False


# ---------------------------------------------------------------------------
# ③ Redis 后端：显式不支持（**禁止静默降级**）
#
# ⚠️ 空键路径**禁用 `RedisStorage` 直调**：本机 Redis 可达时那次调用会**真的
# 写进真实 redis**（不可达时又只是「写失败」假象通过）⇒ 两种机器表现不一致。
# 空键路径改用 sqlite 夹具（本文件既有的 `provider` 夹具）—— 用例意图与 Redis 无关。
# ---------------------------------------------------------------------------

def test_redis后端幂等原语抛backend_unsupported():
    """Redis 不做幂等登记 ⇒ **明确抛错**，绝不静默忽略 idempotency_key。"""
    storage = RedisStorage()
    with pytest.raises(RuntimeError) as excinfo:
        storage.idem_lookup("agent-a", "K1")
    assert "backend_unsupported" in str(excinfo.value)
    with pytest.raises(RuntimeError) as excinfo:
        storage.idem_record("agent-a", "K1", "memory:frag:x")
    assert "backend_unsupported" in str(excinfo.value)
    with pytest.raises(RuntimeError) as excinfo:
        storage.idem_purge("2999-01-01T00:00:00.000000+00:00")
    assert "backend_unsupported" in str(excinfo.value)


def test_redis后端带幂等键在MCP层明确报错():
    """MCP 层：带了幂等键 + 后端不支持 ⇒ **ToolError 人话**（不静默降级）。"""
    from keepsake.mcp_server.tools import ToolError, memory_add

    p = KeepsakeProvider()
    p._storage = RedisStorage()
    with pytest.raises(ToolError) as exc:
        memory_add(p, "Redis 后端不该写进去", "",
                   caller_agent_id="agent-a", caller_role="sub",
                   idempotency_key="K1")
    assert "backend_unsupported" in str(exc.value)
    assert "idempotency_key" in str(exc.value)


def test_空幂等键不进幂等分支_返回形状不变(provider):
    """空键 ⇒ **不进幂等分支**：不抛错、返回值里**不多 `duplicated` 字段**。

    🔴 这里用 **sqlite 夹具**（不是 `RedisStorage` 直调）：意图是「空键不碰幂等
    语义」，与后端无关；而 `RedisStorage` 直调会**写进真实 redis**（本机可达时）
    或只假象通过「写失败」（本机不可达时）—— 两台机器表现不一致。
    """
    from keepsake.mcp_server.tools import memory_add

    out = memory_add(provider, "空键路径 正文甲", "tag",
                     caller_agent_id="agent-a", caller_role="sub")
    assert "duplicated" not in out, out
    assert out["key"], out
    assert out["rev"], out


# ---------------------------------------------------------------------------
# ④ MCP 面：memory_add 带幂等键
# ---------------------------------------------------------------------------

def test_mcp_带键首次写duplicated_false(provider):
    from keepsake.mcp_server.tools import memory_add

    out = memory_add(provider, "幂等测试 正文甲", "tag1",
                     caller_agent_id="agent-a", caller_role="sub",
                     idempotency_key="K1")
    assert out["duplicated"] is False, out
    assert out["key"], out
    assert out["rev"], out


def test_mcp_同键再调_duplicated_true且条目数不涨(provider):
    """🔴 本单的核心断言：同键重试**不落第二条**。"""
    from keepsake.mcp_server.tools import memory_add

    st = provider._storage                   # noqa: SLF001
    first = memory_add(provider, "幂等测试 正文甲", "tag1",
                        caller_agent_id="agent-a", caller_role="sub",
                        idempotency_key="K1")
    n0 = _frag_count(st)
    second = memory_add(provider, "幂等测试 正文甲", "tag1",
                         caller_agent_id="agent-a", caller_role="sub",
                         idempotency_key="K1")
    assert second["duplicated"] is True, second
    assert second["key"] == first["key"], (first, second)
    assert second["rev"] == first["rev"], (first, second)
    assert _frag_count(st) == n0, "同键重试**不许**多落一条"


def test_mcp_同键但正文不同_仍不落第二条(provider):
    """🔴 正是 zly-hermes 的病：超时重试时正文**稍有差异**。

    幂等键命中 ⇒ **按首次落库的结果回**，不写第二次（哪怕正文不同）。
    """
    from keepsake.mcp_server.tools import memory_add

    st = provider._storage                   # noqa: SLF001
    first = memory_add(provider, "幂等测试 正文甲", "tag1",
                        caller_agent_id="agent-a", caller_role="sub",
                        idempotency_key="K1")
    n0 = _frag_count(st)
    second = memory_add(provider, "幂等测试 正文甲（重试时多了几个字）", "tag1",
                         caller_agent_id="agent-a", caller_role="sub",
                         idempotency_key="K1")
    assert second["duplicated"] is True, second
    assert second["key"] == first["key"], (first, second)
    assert _frag_count(st) == n0, "同键重试不许多落一条"


def test_mcp_不同键两条(provider):
    from keepsake.mcp_server.tools import memory_add

    st = provider._storage                   # noqa: SLF001
    a = memory_add(provider, "幂等测试 正文甲", "", caller_agent_id="agent-a",
                    caller_role="sub", idempotency_key="K1")
    b = memory_add(provider, "幂等测试 正文乙", "", caller_agent_id="agent-a",
                    caller_role="sub", idempotency_key="K2")
    assert a["duplicated"] is False and b["duplicated"] is False
    assert a["key"] != b["key"], (a, b)
    assert _frag_count(st) == 2


def test_mcp_跨owner同键互不命中(provider):
    """🔴 作用域 = 归属代理：A 的键命中不了 B 的登记。"""
    from keepsake.mcp_server.tools import memory_add

    st = provider._storage                   # noqa: SLF001
    a = memory_add(provider, "幂等测试 归属甲", "", caller_agent_id="agent-a",
                    caller_role="sub", idempotency_key="K1")
    b = memory_add(provider, "幂等测试 归属乙", "", caller_agent_id="agent-b",
                    caller_role="sub", idempotency_key="K1")
    assert a["duplicated"] is False, a
    assert b["duplicated"] is False, "B 的键命中不了 A 的登记"
    assert a["key"] != b["key"], (a, b)
    owners = {r[0] for r in _idem_rows(st)}
    assert owners == {"agent-a", "agent-b"}, owners


def test_mcp_过期登记视为未命中_重写(provider):
    """TTL 到期 ⇒ 视为未命中 ⇒ 照常重写（并把登记刷新）。"""
    from keepsake.mcp_server.tools import memory_add

    st = provider._storage                   # noqa: SLF001
    first = memory_add(provider, "幂等测试 过期甲", "", caller_agent_id="agent-a",
                        caller_role="sub", idempotency_key="K1")
    # 把登记改成过期（手工改库，模拟「7 天前登记、今天才重试」）
    with st._lock:                           # noqa: SLF001 — 测试直改库
        st._db().execute(
            "UPDATE ks_idem_event SET created = ? WHERE idem_key = ?",
            ("2020-01-01T00:00:00.000000+00:00", "K1"))
    again = memory_add(provider, "幂等测试 过期甲", "", caller_agent_id="agent-a",
                        caller_role="sub", idempotency_key="K1")
    assert again["duplicated"] is False, "过期登记必须视为未命中"
    rows = _idem_rows(st)
    assert len(rows) == 1 and rows[0][1] == "K1", rows
    assert rows[0][3] != "2020-01-01T00:00:00.000000+00:00", "登记应被刷新"
    assert first["key"] and again["key"]


def test_mcp_登记的那条被删_视为未命中重写(provider):
    """登记指向的碎片已被删 ⇒ 视为未命中 ⇒ 照常重写（不留悬空登记）。"""
    from keepsake.mcp_server.tools import memory_add

    st = provider._storage                   # noqa: SLF001
    first = memory_add(provider, "幂等测试 被删甲", "", caller_agent_id="agent-a",
                        caller_role="sub", idempotency_key="K1")
    st.delete_fragments_batch([first["key"]])
    again = memory_add(provider, "幂等测试 被删甲", "", caller_agent_id="agent-a",
                        caller_role="sub", idempotency_key="K1")
    assert again["duplicated"] is False, "登记的碎片没了 ⇒ 视为未命中"
    assert again["key"]


def test_mcp_空键行为逐字不变(provider):
    """🔴 空键 ⇒ 返回值**不多 `duplicated` 字段**，也不碰幂等表。"""
    from keepsake.mcp_server.tools import memory_add

    st = provider._storage                   # noqa: SLF001
    out = memory_add(provider, "幂等测试 空键甲", "tag1",
                     caller_agent_id="agent-a", caller_role="sub")
    assert set(out) == {"key", "tags", "rev"}, out
    assert out["tags"] == "tag1", out
    assert _idem_rows(st) == [], "空键不许写幂等表"


def test_mcp_空键两次照旧各写各的(provider):
    """空键 ⇒ 没有判重 ⇒ 两次写就是两次（既有行为逐字不变）。"""
    from keepsake.mcp_server.tools import memory_add

    st = provider._storage                   # noqa: SLF001
    out1 = memory_add(provider, "幂等测试 空键乙", "", caller_agent_id="agent-a",
                       caller_role="sub")
    out2 = memory_add(provider, "幂等测试 空键乙", "", caller_agent_id="agent-a",
                       caller_role="sub")
    assert "duplicated" not in out1 and "duplicated" not in out2
    assert _frag_count(st) == 2, "空键不判重（既有行为）"


def test_mcp_能力声明带idempotent_write(provider):
    """能力面暴露 `idempotent_write`（调用方据此决定能不能带幂等键）。"""
    from keepsake.mcp_server.tools import memory_capabilities

    caps = memory_capabilities(provider)
    assert caps["idempotent_write"] is True, caps


def test_redis后端能力声明idempotent_write为False():
    from keepsake.mcp_server.tools import memory_capabilities

    p = KeepsakeProvider()
    p._storage = RedisStorage()
    caps = memory_capabilities(p)
    assert caps["idempotent_write"] is False, caps


def test_provider_幂等owner按实际打标判定(provider):
    """owner = 凭据反查出的调用方；调用方为空 ⇒ 进程默认身份。"""
    p = _make_provider(str(Path(provider._storage._path)), agent_id="main")  # noqa: SLF001
    try:
        assert p.idem_owner("agent-a") == "agent-a"
        assert p.idem_owner("") == "main", "空调用方 ⇒ 进程默认身份"
    finally:
        p._storage.close()


# ---------------------------------------------------------------------------
# ⑤ PG 真库（无凭据则 skip）—— 与 SQLite 侧同一套语义
# ---------------------------------------------------------------------------

def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


_DSN = _read_dsn()
_pg_ready = bool(_DSN) and ALLOWED_HOST in _DSN and ALLOWED_DBNAME in _DSN


@pytest.fixture()
def pgs():
    if not _pg_ready:
        pytest.skip("无测试库凭据，跳过 PG 真库")
    psycopg = pytest.importorskip("psycopg", reason="未装 psycopg")
    from keepsake.core.pg import PgStorage

    schema = f"ks_idem_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_DSN, autocommit=True, connect_timeout=30) as conn, \
            conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    pg = PgStorage(dsn=_DSN + f"?options=-csearch_path%3D{schema},public",
                   connect_timeout=30, embed_dim=4, is_primary=True)
    assert pg.ensure_index() is True
    yield pg
    with psycopg.connect(_DSN, autocommit=True, connect_timeout=30) as conn, \
            conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    pg.close()


def test_pg真库_同键两次登记第二次命中首次(pgs):
    assert pgs.idem_record("agent-a", "K1", "memory:frag:first") is True
    assert pgs.idem_record("agent-a", "K1", "memory:frag:second") is True
    hit = pgs.idem_lookup("agent-a", "K1")
    assert hit is not None and hit["frag_key"] == "memory:frag:first", hit


def test_pg真库_跨owner同键互不命中(pgs):
    pgs.idem_record("agent-a", "K1", "memory:frag:a")
    assert pgs.idem_lookup("agent-b", "K1") is None


def test_pg真库_过期登记视为未命中(pgs):
    with pgs._tx() as cur:                  # noqa: SLF001
        cur.execute(
            "INSERT INTO ks_idem_event (owner, idem_key, frag_key, created) "
            "VALUES (%s, %s, %s, %s)",
            ("agent-a", "K-old", "memory:frag:旧", "2020-01-01T00:00:00.000000+00:00"))
    assert pgs.idem_lookup("agent-a", "K-old") is None
    with pgs._ro() as cur:                  # noqa: SLF001
        cur.execute("SELECT COUNT(*) FROM ks_idem_event")
        assert cur.fetchone()[0] == 0, "过期行必须被顺手删掉"


def test_pg真库_idem_purge删过期留新(pgs):
    with pgs._tx() as cur:                  # noqa: SLF001
        cur.execute(
            "INSERT INTO ks_idem_event (owner, idem_key, frag_key, created) "
            "VALUES (%s, %s, %s, %s)",
            ("agent-a", "K-old", "memory:frag:旧", "2020-01-01T00:00:00.000000+00:00"))
    pgs.idem_record("agent-a", "K-new", "memory:frag:新")
    assert pgs.idem_purge("2999-01-01T00:00:00.000000+00:00") == 1
