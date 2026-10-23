"""PG 连接池**进程级共享**测试（2026-10 pg_pool_singleton）—— **纯假池，不连真库**。

## 为什么要有这一组用例（主脑 2026-10-23 实测取证）

Hermes 网关**每起一个会话就实例化一次 KeepsakeProvider** → 各自
`storage_from_config` 建一个 PgStorage → **各带一个自己的 ConnectionPool**
（min1/max8）。会话销毁时 `provider.shutdown()` 未必被宿主调用 ⇒ 池与连接
就滞留。实测：网关进程（uptime 6.5h）对 180:5432 挂了 95 条连接（73 条纯
idle>1h），180 的 `max_connections=100` 被打满 ⇒ kb 读写全阻。

根治只有一个办法：**同一 DSN 全进程只有一个池**，谁用都借它。本组用例钉住
这条不变量：

  a) 同 conninfo 的两个 PgStorage 实例 ⇒ `_get_pool()` 拿到**同一个池对象**；
  b) 实例 A `close()` 后 ⇒ **共享池不关**，实例 B 仍能借到可用连接跑 `SELECT 1`；
  c) 不同 conninfo ⇒ 不同池（注册表的键就是 conninfo）；
  d) 多线程 barrier 并发 `_get_pool()` ⇒ 注册表里该 key 只对应**一个**池
     （建池在注册表锁内做 ⇒ 不会并发出两个池 = 两倍连接）。

外加一条重建语义：`_rebuild_pool()` 把注册表同 key 换成新池、旧池照旧关闭。

## 为什么全程用假池

这一层要验的是**池的接线**（谁拿到哪个池、谁负责关），不是 PG 的行为：
真库那侧由 `tests/test_storage_pg.py` 负责（没凭据就 skip）。
`_build_pool` 按既有测试的惯例 monkeypatch 掉 ⇒ 不 import psycopg、不连 180。
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any, List, Optional

import pytest

from keepsake.core import pg
from keepsake.core.pg import PgStorage


# --------------------------------------------------------------------------
# 假连接 / 假池
# --------------------------------------------------------------------------

class _FakeCursor:
    """只记 SQL 的假游标 —— `fetchone()` 恒返 `(1,)`（够跑 `SELECT 1`）。"""

    def __init__(self) -> None:
        self.stmts: List[Any] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.stmts.append((sql, params))

    def fetchone(self) -> tuple:
        return (1,)

    def fetchall(self) -> list:
        return [(1,)]


class _FakeConn:
    def __init__(self) -> None:
        self.cur = _FakeCursor()
        self.commits = 0

    def cursor(self) -> _FakeCursor:
        return self.cur

    def commit(self) -> None:
        self.commits += 1

    def transaction(self) -> Any:
        return contextlib.nullcontext()

    def close(self) -> None:
        pass


class _FakePool:
    """假池：记借还次数，`close()` 后借连接会抛（像真池关掉那样）。"""

    def __init__(self) -> None:
        self.borrowed = 0
        self.returned = 0
        self.closed = False
        self.conns: List[_FakeConn] = []

    def getconn(self, timeout: Optional[float] = None) -> _FakeConn:
        if self.closed:
            raise RuntimeError("pool is closed")
        self.borrowed += 1
        conn = _FakeConn()
        self.conns.append(conn)
        return conn

    def putconn(self, conn: Any) -> None:
        self.returned += 1

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------
# fixtures / 辅助
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_shared_registry():
    """每个用例前后**还原**共享注册表。

    注册表是进程级全局态（这正是本单要的东西），用例之间必须隔离 ——
    否则前一个用例登记的池会被后一个用例捡走，表现为看不懂的假红/假绿。
    还原（而不是只清空）是为了不破坏别的模块留下的条目。
    """
    pools, srcs = dict(pg._SHARED_POOLS), dict(pg._SHARED_POOL_SRC)  # noqa: SLF001
    pg._SHARED_POOLS.clear()      # noqa: SLF001
    pg._SHARED_POOL_SRC.clear()   # noqa: SLF001
    yield
    pg._SHARED_POOLS.clear()      # noqa: SLF001
    pg._SHARED_POOLS.update(pools)   # noqa: SLF001
    pg._SHARED_POOL_SRC.clear()       # noqa: SLF001
    pg._SHARED_POOL_SRC.update(srcs)  # noqa: SLF001


def _storage(dsn: str) -> PgStorage:
    """一个**不连库**的 PgStorage 实例（池子被下面的 monkeypatch 掉）。"""
    return PgStorage(dsn=dsn)


def _patch_build_pool(monkeypatch: pytest.MonkeyPatch) -> List[_FakePool]:
    """把 `_build_pool` 换成假池（既有测试的同一惯例），返回建出来的池列表。"""
    built: List[_FakePool] = []

    def _build(self: PgStorage) -> _FakePool:
        pool = _FakePool()
        built.append(pool)
        return pool

    monkeypatch.setattr(PgStorage, "_build_pool", _build)
    return built


# --------------------------------------------------------------------------
# ① 同 DSN ⇒ 同一个池对象
# --------------------------------------------------------------------------

def test_同conninfo的两个实例拿到同一个池(monkeypatch):
    """🔴 本单的核心不变量：同一 DSN 全进程只有一个池（不是「每个实例一个」）。"""
    built = _patch_build_pool(monkeypatch)
    dsn = "postgresql://user:pw@127.0.0.1:1/singleton"

    a = _storage(dsn)
    b = _storage(dsn)

    pool_a = a._get_pool()   # noqa: SLF001
    pool_b = b._get_pool()   # noqa: SLF001

    assert pool_a is pool_b, "同 DSN 的两个实例必须拿到**同一个**池对象"
    assert len(built) == 1, f"同 DSN 只许建一个池（实际建了 {len(built)} 个）"
    assert pg._SHARED_POOLS.get(a._conninfo()) is pool_a   # noqa: SLF001


# --------------------------------------------------------------------------
# ② 实例 close() 不关共享池
# --------------------------------------------------------------------------

def test_实例关闭后共享池仍可借连接执行SELECT1(monkeypatch):
    """🔴 网关每会话一个 provider ⇒ close() 是**高频**动作。

    若 close() 把共享池关了，别的实例的连接就被误伤（会话一关，满库读写全断）。
    """
    _patch_build_pool(monkeypatch)
    dsn = "postgresql://user:pw@127.0.0.1:1/close-semantics"

    a = _storage(dsn)
    b = _storage(dsn)
    pool = a._get_pool()   # noqa: SLF001

    a.close()

    assert pool.closed is False, "共享池不随单个实例 close 而关闭（别的实例还在用）"

    # 另一个实例仍能从同一个池借到可用连接，并真的跑一条 SELECT 1
    with b._ro() as cur:    # noqa: SLF001
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)
    assert pool.borrowed == 1 and pool.returned == 1


def test_私有池仍会被close关掉(monkeypatch):
    """🔴 反面：**本实例私有**的池（没登记在共享注册表里的）照旧关掉。

    共享池不关 ≠ 所有池都不关 —— 私有池留着就是泄漏。
    """
    _patch_build_pool(monkeypatch)
    st = _storage("postgresql://user:pw@127.0.0.1:1/private-pool")

    private = _FakePool()
    st._conn_pool = private          # noqa: SLF001
    st._pool_conninfo = st._conninfo()   # noqa: SLF001

    st.close()

    assert private.closed is True
    st.close()                        # 可重复调用


# --------------------------------------------------------------------------
# ③ 不同 conninfo ⇒ 不同池
# --------------------------------------------------------------------------

def test_不同conninfo拿到不同池(monkeypatch):
    """🔴 注册表的键就是 conninfo —— 键错了就会串库（拿到别处的连接）。"""
    _patch_build_pool(monkeypatch)
    a = _storage("postgresql://user:pw@127.0.0.1:1/db-a")
    b = _storage("postgresql://user:pw@127.0.0.1:1/db-b")

    pool_a = a._get_pool()   # noqa: SLF001
    pool_b = b._get_pool()   # noqa: SLF001

    assert pool_a is not pool_b
    assert pg._SHARED_POOLS.get(a._conninfo()) is pool_a   # noqa: SLF001
    assert pg._SHARED_POOLS.get(b._conninfo()) is pool_b   # noqa: SLF001


# --------------------------------------------------------------------------
# ④ 并发首次取池 ⇒ 只建一个
# --------------------------------------------------------------------------

def test_多线程并发取池注册表只对应一个池(monkeypatch):
    """🔴 并发首次取池 ⇒ **注册表里该 key 只对应一个池**（本单的判据）。

    实现上：建池**不占**注册表锁（建池要探库，几秒起步，占着锁会把别的 DSN
    一起拖住），所以并发窗口里可能各自建了多份 —— **先登记的赢**，其余当场
    关掉 ⇒ 所有人收敛到同一个池，注册表里也只有那一个。
    """
    built = _patch_build_pool(monkeypatch)
    dsn = "postgresql://user:pw@127.0.0.1:1/concurrent"

    n = 8
    barrier = threading.Barrier(n)
    pools: List[Any] = []
    lock = threading.Lock()

    def _worker() -> None:
        st = _storage(dsn)
        barrier.wait()                 # 一起冲进 `_get_pool`
        pool = st._get_pool()          # noqa: SLF001
        with lock:
            pools.append(pool)

    threads = [threading.Thread(target=_worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len({id(p) for p in pools}) == 1, "所有线程必须拿到同一个池对象"
    assert pg._SHARED_POOLS.get(dsn) is pools[0]   # noqa: SLF001
    # 多建出来的那几份必须**当场关掉**（不然就是一批白开的连接）
    for pool in built:
        if pool is not pools[0]:
            assert pool.closed is True, "并发多建出来的池必须当场关掉"


# --------------------------------------------------------------------------
# ⑤ 重建语义：注册表同 key 换新池、旧池照旧关闭
# --------------------------------------------------------------------------

def test_重建池替换注册表并关闭旧池(monkeypatch):
    """🔴 坏池重建：注册表里同 key 换成新池，旧池照旧关闭（别留着坏连接）。"""
    built = _patch_build_pool(monkeypatch)
    st = _storage("postgresql://user:pw@127.0.0.1:1/rebuild")

    first = st._get_pool()   # noqa: SLF001
    second = st._rebuild_pool()   # noqa: SLF001

    assert second is not first
    assert first.closed is True, "旧池必须关掉"
    assert len(built) == 2
    assert pg._SHARED_POOLS.get(st._conninfo()) is second   # noqa: SLF001


def test_重建池时别的实例已换过则复用现成的(monkeypatch):
    """🔴 别的实例已经换过池 ⇒ **直接用现成的**，不再建第二个。

    否则两个实例会互相把对方的池换掉 ⇒ 来回重建 ⇒ 连接数翻倍。
    """
    built = _patch_build_pool(monkeypatch)
    dsn = "postgresql://user:pw@127.0.0.1:1/rebuild-shared"

    a = _storage(dsn)
    b = _storage(dsn)
    pool = a._get_pool()   # noqa: SLF001
    assert b._get_pool() is pool   # noqa: SLF001 —— 共享同一个池

    # b 遇到坏连接 ⇒ 重建（注册表换成新池，旧池关闭）
    new_pool = b._rebuild_pool()   # noqa: SLF001
    assert new_pool is not pool
    assert pool.closed is True

    # a 这时再重建：注册表里已经是 b 换的那个 ⇒ 复用，不新建
    reused = a._rebuild_pool()   # noqa: SLF001
    assert reused is new_pool
    assert len(built) == 2, f"不该再多建一个池（实际建了 {len(built)} 个）"
