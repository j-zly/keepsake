"""删除墓碑（tombstone）定向测试 —— 2026-11 ks_tombstone。

被测的语义（任务书「判据」段）：

  1. **CAS 删除留碑**：`cas_remove` 成功 ⇒ `ks_tombstone` 恰 1 行
     （key / 归属 / 删除时的 rev / 删除时刻），`memory_get` 回**墓碑档**文案
     （含删除时间与 rev），**不再**回「不存在」；
  2. **批删也留碑**：`delete_fragments_batch` 同样留痕；
  3. **复活 rev 接续**：同正文重 add ⇒ 复活**原 key**、rev = 原 rev + 1、
     墓碑**消失**（版本历史不断裂）；
  4. **从未存在过的 key** ⇒ 仍逐字回「记忆条目不存在」（**不塌成墓碑档**）；
  5. **他人墓碑不泄露**：墓碑归属 ≠ 调用方且调用方非 primary ⇒ 仍回「不存在」；
  6. **ensure_index 两路径**：缺表老库补齐 + 全新空库直接有表；
  7. **fail-closed**：墓碑写失败 ⇒ 整个删事务回滚（**宁可删不掉，不可删无痕**）。

本文件 **hermetic**：只用 tmp_path 里的临时 SQLite 文件 + 内存里的 provider，
不连 PG / Redis / 网络（Redis 只取「构造 + 读能力声明」，不建连）。
"""

from __future__ import annotations

import sqlite3
from typing import Any, List, Tuple

import pytest

from keepsake.adapters.hermes.provider import KeepsakeProvider
from keepsake.core.base import BackendUnsupportedError
from keepsake.core.redis import RedisStorage
from keepsake.core.sqlite import SqliteStorage
from keepsake.mcp_server.tools import ToolError, memory_capabilities, memory_get

TOMBSTONE_TABLE = "ks_tombstone"


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
    provider._agent_id = agent_id
    return provider


@pytest.fixture()
def provider(tmp_path):
    p = _make_provider(str(tmp_path / "tomb.db"))
    yield p
    p._storage.close()


def _tombstone_rows(storage: SqliteStorage) -> List[Tuple[str, str, str, str]]:
    """墓碑表的全部行（`(key, agent_id, rev, deleted_at)`）。"""
    with storage._lock:                       # noqa: SLF001 — 测试直读库
        return list(storage._db().execute(
            f"SELECT key, agent_id, rev, deleted_at FROM {TOMBSTONE_TABLE}"))


def _has_table(storage: SqliteStorage, name: str) -> bool:
    with storage._lock:                       # noqa: SLF001 — 测试直读库
        return storage._db().execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None


# ---------------------------------------------------------------------------
# ① CAS 删除 ⇒ 留一行墓碑 + memory_get 回墓碑档
# ---------------------------------------------------------------------------

def test_cas删除后墓碑表恰一行(provider):
    """删一条 ⇒ 墓碑表**恰一行**（不多不少）：key / 归属 / rev / 删除时刻。"""
    key = provider.add("墓碑测试 正文甲", caller_agent_id="agent-a")
    assert provider.remove(key, expected_rev="1") == {"ok": True, "removed": True,
                                                      "key": key}
    rows = _tombstone_rows(provider._storage)      # noqa: SLF001
    assert len(rows) == 1, rows
    t_key, t_agent, t_rev, t_at = rows[0]
    assert t_key == key
    assert t_agent == "agent-a", "归属取被删那条的 agent:<id>（唯一一份归属口径）"
    assert t_rev == "1", "墓碑记的是**删除时那一版**的 rev"
    assert t_at, "必须记删除时刻（否则「何时删的」查不到）"


def test_cas删除后get回墓碑档文案(provider):
    """行不在但墓碑在 ⇒ `memory_get` 回**墓碑档**（含删除时间与 rev）。"""
    key = provider.add("墓碑测试 正文乙", caller_agent_id="agent-a")
    provider.remove(key, expected_rev="1")
    with pytest.raises(ToolError) as exc:
        memory_get(provider, key, caller_agent_id="agent-a", caller_role="sub")
    msg = str(exc.value)
    assert "已于" in msg and "删除" in msg, msg
    assert "rev=1" in msg, f"墓碑档必须带上删除时的 rev：{msg}"
    assert "rev 从 2 接续" in msg, f"墓碑档必须说明复活后 rev 怎么走：{msg}"
    # 删除时刻（墓碑那一行的时间戳）必须出现在文案里
    deleted_at = _tombstone_rows(provider._storage)[0][3]   # noqa: SLF001
    assert deleted_at in msg, f"墓碑档必须带删除时刻 {deleted_at}：{msg}"


def test_删后行确实不在(provider):
    """墓碑**不替代**正文行：删完 `provider.get` 仍是 None（行真的没了）。"""
    key = provider.add("墓碑测试 正文丙", caller_agent_id="agent-a")
    provider.remove(key, expected_rev="1")
    assert provider.get(key) is None


# ---------------------------------------------------------------------------
# ② 批删（delete_fragments_batch）同样留碑
# ---------------------------------------------------------------------------

def test_批删后同样留碑(provider):
    """另一条删路（`delete_fragments_batch`）也必须留痕 —— 两条删路同语义。"""
    storage = provider._storage                # noqa: SLF001
    key = provider.add("墓碑测试 正文丁", caller_agent_id="agent-a")
    assert storage.delete_fragments_batch([key]) == 1
    rows = _tombstone_rows(storage)
    assert len(rows) == 1, rows
    assert rows[0][0] == key
    assert rows[0][2] == "1"
    with pytest.raises(ToolError) as exc:
        memory_get(provider, key, caller_agent_id="agent-a", caller_role="sub")
    assert "已于" in str(exc.value), str(exc.value)


def test_批删不存在的key不留碑(provider):
    """删一个压根不存在的 key ⇒ 不留墓碑（墓碑记的是「删过」，不是「没删成」）。"""
    storage = provider._storage                # noqa: SLF001
    assert storage.delete_fragments_batch(["memory:frag:根本没存在过"]) == 0
    assert _tombstone_rows(storage) == []


# ---------------------------------------------------------------------------
# ③ 复活：同正文重 add ⇒ 原 key 复活、rev 接续、墓碑消失
# ---------------------------------------------------------------------------

def test_同正文重add_复活原key且rev接续(provider):
    """🔴 复活语义的核心：**rev 不再归 1**，而是从墓碑接续（版本历史不断裂）。"""
    text = "墓碑测试 复活正文戊"
    key = provider.add(text, caller_agent_id="agent-a")
    assert provider.get(key)["rev"] == "1"
    provider.remove(key, expected_rev="1")

    key2 = provider.add(text, caller_agent_id="agent-a")
    assert key2 == key, "同正文重 add 复活的是**原 key**（key = 内容哈希）"
    assert provider.get(key2)["rev"] == "2", "rev 必须从墓碑接续（1 → 2），不归 1"
    assert _tombstone_rows(provider._storage) == [], "复活后墓碑必须消失"  # noqa: SLF001


def test_连续复活rev一路接续(provider):
    """删 → 复活 → 再删 → 再复活 ⇒ rev 一路 `next_rev`（1 → 2 → 3）。"""
    text = "墓碑测试 连续复活正文己"
    key = provider.add(text, caller_agent_id="agent-a")
    provider.remove(key, expected_rev="1")
    assert provider.add(text, caller_agent_id="agent-a") == key
    assert provider.get(key)["rev"] == "2"
    provider.remove(key, expected_rev="2")
    assert provider.add(text, caller_agent_id="agent-a") == key
    assert provider.get(key)["rev"] == "3"


def test_无墓碑的写入rev仍归1(provider):
    """无墓碑 ⇒ 行为**逐字不变**（新写的条目 rev 仍是 '1'）。"""
    key = provider.add("墓碑测试 全新正文庚", caller_agent_id="agent-a")
    assert provider.get(key)["rev"] == "1"


# ---------------------------------------------------------------------------
# ④ 从未存在过的 key ⇒ 仍逐字回「不存在」（**不塌成墓碑档**）
# ---------------------------------------------------------------------------

def test_从未存在的key仍回不存在(provider):
    """逐字回归：从来没有过的 key ⇒ 仍是「记忆条目不存在：{key}」。"""
    missing = "memory:frag:0123456789ab"
    with pytest.raises(ToolError) as exc:
        memory_get(provider, missing, caller_agent_id="agent-a", caller_role="sub")
    assert str(exc.value) == f"记忆条目不存在：{missing}", str(exc.value)


def test_从未存在过的key删不掉也不留碑(provider):
    """删一个从未存在的 key ⇒ 不留墓碑（别把「没删成」也记成「删过」）。"""
    storage = provider._storage                # noqa: SLF001
    out = provider.remove("memory:frag:0123456789ab", expected_rev="1")
    assert out.get("error") == "not_found", out
    assert _tombstone_rows(storage) == []


# ---------------------------------------------------------------------------
# ⑤ 他人墓碑 ⇒ 仍回「不存在」（**不泄露别人删过什么**）
# ---------------------------------------------------------------------------

def test_他人墓碑对非primary调用方不可见(provider):
    """🔴 防泄露：A 的条目被删后，**B** 拿 key 来问 ⇒ 只回「不存在」。"""
    key = provider.add("墓碑测试 归属正文辛", caller_agent_id="agent-a")
    provider.remove(key, expected_rev="1")

    with pytest.raises(ToolError) as exc:
        memory_get(provider, key, caller_agent_id="agent-b", caller_role="sub")
    msg = str(exc.value)
    assert msg == f"记忆条目不存在：{key}", msg
    assert "已于" not in msg, f"别人的墓碑不许回碑文：{msg}"


def test_他人墓碑对primary可见(provider):
    """primary 全量可见（= 主脑现状逐字不变）：能查到墓碑档。"""
    key = provider.add("墓碑测试 归属正文壬", caller_agent_id="agent-a")
    provider.remove(key, expected_rev="1")
    with pytest.raises(ToolError) as exc:
        memory_get(provider, key, caller_agent_id="agent-p", caller_role="primary")
    assert "已于" in str(exc.value), str(exc.value)


def test_无归属墓碑人人可见(provider):
    """无归属的老条目（墓碑 agent_id 为空）⇒ 谁都能查到墓碑档。"""
    storage = provider._storage                # noqa: SLF001
    key = provider.add("墓碑测试 无归属正文癸", caller_agent_id="")
    provider.remove(key, expected_rev="1")
    assert _tombstone_rows(storage)[0][1] == "", "无归属条目的墓碑归属也为空"
    with pytest.raises(ToolError) as exc:
        memory_get(provider, key, caller_agent_id="agent-b", caller_role="sub")
    assert "已于" in str(exc.value), str(exc.value)


# ---------------------------------------------------------------------------
# ⑥ ensure_index 两路径：缺表老库补齐 + 全新空库直接有表
# ---------------------------------------------------------------------------

def test_全新空库建表后直接有墓碑表(tmp_path):
    """**全新空库**：`ensure_index()` 一次成功 ⇒ 墓碑表直接在。"""
    path = str(tmp_path / "brand_new.db")
    storage = SqliteStorage(path=path, agent_id="agent-a")
    assert storage.ensure_index() is True
    try:
        assert _has_table(storage, TOMBSTONE_TABLE), "新库必须直接有墓碑表"
    finally:
        storage.close()


def test_缺表老库跑ensure_index补齐(tmp_path):
    """**缺表老库**（本次上线前建的库）：`ensure_index()` 把墓碑表补上。"""
    path = str(tmp_path / "legacy.db")
    storage = SqliteStorage(path=path, agent_id="agent-a")
    assert storage.ensure_index() is True
    try:
        with storage._lock:                    # noqa: SLF001 — 模拟老库（表不存在）
            storage._db().execute(f"DROP TABLE {TOMBSTONE_TABLE}")
        assert not _has_table(storage, TOMBSTONE_TABLE)

        assert storage.ensure_index() is True, "ensure_index 必须自愈补表"
        assert _has_table(storage, TOMBSTONE_TABLE), "墓碑表必须被补齐"
    finally:
        storage.close()


def test_缺表老库补齐后删除照常留碑(tmp_path):
    """老库补齐后 ⇒ 删除留碑照常（不留痕的老库不许「删得掉却查不到」）。"""
    path = str(tmp_path / "legacy2.db")
    p = _make_provider(path, agent_id="agent-a", is_primary=False)
    storage = p._storage                      # noqa: SLF001
    try:
        with storage._lock:                    # noqa: SLF001
            storage._db().execute(f"DROP TABLE {TOMBSTONE_TABLE}")
        key = p.add("墓碑测试 老库正文子", caller_agent_id="agent-a")
        assert storage.delete_fragments_batch([key]) == 1
        rows = _tombstone_rows(storage)
        assert len(rows) == 1 and rows[0][0] == key, rows
    finally:
        storage.close()


# ---------------------------------------------------------------------------
# ⑦ fail-closed：留碑失败 ⇒ 删不掉（**宁可删不掉，不可删无痕**）
# ---------------------------------------------------------------------------

def test_留碑失败则批删回滚_行还在(provider, monkeypatch):
    """🔴 fail-closed：墓碑写失败 ⇒ 整个删事务回滚 ⇒ 条目**还在**。"""
    storage = provider._storage                # noqa: SLF001
    key = provider.add("墓碑测试 回滚正文丑", caller_agent_id="agent-a")

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("模拟墓碑表写失败")

    monkeypatch.setattr(storage, "_write_tombstone", boom)
    assert storage.delete_fragments_batch([key]) == 0, "留痕失败不许删成"
    assert storage.get_fragment(key) is not None, "行必须还在（事务回滚了）"
    assert _tombstone_rows(storage) == []


def test_留碑失败则cas删除抛错_行还在(provider, monkeypatch):
    """同款 fail-closed 落在 CAS 删除上（异常原样上抛 ⇒ 事务回滚）。"""
    storage = provider._storage                # noqa: SLF001
    key = provider.add("墓碑测试 回滚正文寅", caller_agent_id="agent-a")

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("模拟墓碑表写失败")

    monkeypatch.setattr(storage, "_write_tombstone", boom)
    with pytest.raises(sqlite3.OperationalError):
        storage.cas_remove(key, "1")
    assert storage.get_fragment(key) is not None, "行必须还在（事务回滚了）"


# ---------------------------------------------------------------------------
# ⑧ 能力声明：PG / SQLite = True；Redis = False（**不留静默**）
# ---------------------------------------------------------------------------

def test_能力声明_sqlite为true(provider):
    caps = memory_capabilities(provider)
    assert caps["tombstone"] is True, caps


def test_能力声明_redis为false():
    """Redis 后端删除不留痕 ⇒ 能力声明**如实说 False**（不假装「删了可查证」）。"""
    p = KeepsakeProvider()
    p._storage = RedisStorage()
    caps = memory_capabilities(p)
    assert caps["tombstone"] is False, caps


def test_redis后端查墓碑显式拒(provider):
    """Redis 后端查墓碑 ⇒ 存储层**显式拒**（backend_unsupported），不静默假装。"""
    with pytest.raises(BackendUnsupportedError) as excinfo:
        RedisStorage().tombstone_lookup("memory:frag:任意")
    assert "backend_unsupported" in str(excinfo.value)


def test_redis后端删除显式拒():
    """🔴 Redis 后端删除**不留痕** ⇒ `provider.remove` **显式拒**（不留静默）。

    宁可删不掉，不可删无痕 —— 删完再也查不到「这条删过」的历史，
    调用方却以为删干净了。
    """
    p = KeepsakeProvider()
    p._storage = RedisStorage()
    with pytest.raises(BackendUnsupportedError) as excinfo:
        p.remove("memory:frag:任意")
    assert "backend_unsupported" in str(excinfo.value)
    assert "tombstone" in str(excinfo.value), str(excinfo.value)


def test_存储层三后端都声明墓碑方法():
    """三个后端都必须**有** `tombstone_lookup` 这个方法（签名逐字对齐）。

    🔴 漏实现的后端会让调用方拿到 AttributeError（而不是一句人话）⇒
    这里钉住「方法存在 + 参数名一致」。
    """
    import inspect

    from keepsake.core.pg import PgStorage

    for cls in (PgStorage, SqliteStorage, RedisStorage):
        fn = getattr(cls, "tombstone_lookup", None)
        assert callable(fn), f"{cls.__name__}.tombstone_lookup 缺失"
        params = list(inspect.signature(fn).parameters.values())[1:]
        assert params[0].name == "key", (cls.__name__, fn)
    assert PgStorage.supports_tombstone is True
    assert SqliteStorage.supports_tombstone is True
    assert RedisStorage.supports_tombstone is False


# ---------------------------------------------------------------------------
# ⑨ MCP 删除面：墓碑档**只影响 memory_get**，删除面仍回「条目不存在」
# ---------------------------------------------------------------------------

def test_mcp删除已删条目仍回条目不存在(provider):
    """🔴 墓碑档**只加在 `memory_get`**：删除面（「这条没了」）的口径不变。"""
    from keepsake.mcp_server.tools import memory_remove

    key = provider.add("墓碑测试 删除面正文", caller_agent_id="agent-a")
    provider.remove(key, expected_rev="1")
    with pytest.raises(ToolError) as exc:
        memory_remove(provider, key, True, expected_rev="1",
                      caller_agent_id="agent-a", caller_role="sub")
    assert "条目不存在" in str(exc.value), str(exc.value)


# ---------------------------------------------------------------------------
# ⑩ PG 代码路径（真 SQL 语义跑在 SQLite 上 —— 方言垫片，与 ks_idem 同款）
# ---------------------------------------------------------------------------

class _ShimCursor:
    """假 PG 游标 → 真 SQLite 游标（顺手记下**原始** SQL 供断言）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._last = None

    def execute(self, sql: str, params: Any = None) -> None:
        self._last = self._conn.execute(sql.replace("%s", "?"), tuple(params or ()))

    def fetchone(self) -> Any:
        return self._last.fetchone() if self._last is not None else None

    def fetchall(self) -> Any:
        return self._last.fetchall() if self._last is not None else []


@pytest.fixture()
def pg_on_sqlite():
    """PgStorage 的墓碑实现 + 真 SQLite 执行器（PG 代码路径 / 真 SQL 语义）。"""
    from contextlib import contextmanager

    from keepsake.core.pg import PgStorage

    pg = PgStorage(dsn="postgresql://user@localhost/keepsake")
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("""
        CREATE TABLE ks_tombstone (
            key        TEXT PRIMARY KEY,
            agent_id   TEXT NOT NULL DEFAULT '',
            rev        TEXT NOT NULL DEFAULT '1',
            deleted_at TEXT NOT NULL DEFAULT ''
        )
    """)

    @contextmanager
    def _ro():
        yield _ShimCursor(conn)

    pg._ro = _ro                              # noqa: SLF001 — 换掉真连库那层
    pg._shim_conn = conn                      # noqa: SLF001 — 测试自用
    yield pg
    conn.close()


def test_pg_墓碑读写语义一致(pg_on_sqlite):
    """PG 那份实现的 SQL 语义与 SQLite 侧**逐条同款**（方言垫片只改 %s → ?）。"""
    pg = pg_on_sqlite
    assert pg.tombstone_lookup("memory:frag:x") is None
    cur = _ShimCursor(pg._shim_conn)          # noqa: SLF001 — 测试自用
    pg._write_tombstone(cur, "memory:frag:x", "3", "agent:agent-x,shared")
    hit = pg.tombstone_lookup("memory:frag:x")
    assert hit is not None, hit
    assert hit["key"] == "memory:frag:x"
    assert hit["agent_id"] == "agent-x", hit
    assert hit["rev"] == "3", hit
    assert hit["deleted_at"], hit


def test_pg_墓碑表在ensure_index的建表清单里():
    """PG 的 `ensure_index` 必须会建 `ks_tombstone`（否则 PG 侧删路径留不了碑）。"""
    from keepsake.core.pg import _DDL_TABLES

    names = {name for name, _sql in _DDL_TABLES}
    assert TOMBSTONE_TABLE in names, sorted(names)


def test_sqlite_墓碑表在ensure_index的建表清单里():
    """SQLite 的 `ensure_index` 必须会建 `ks_tombstone`。"""
    from keepsake.core.sqlite import _DDL_TABLES

    names = {name for name, _sql in _DDL_TABLES}
    assert TOMBSTONE_TABLE in names, sorted(names)
