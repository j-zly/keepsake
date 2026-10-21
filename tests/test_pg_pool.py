"""PG 连接池测试（2026-10 ks_pg_pool）—— **纯 mock，不连真库**。

覆盖四件事：

1. **池重建语义**：checkout 抛连接类异常 ⇒ 关掉旧池、另起一个，再借一次；
2. **池被借空不重建**：`PoolTimeout` 是并发太高，重建只会多开连接（把拥塞变雪崩）；
3. `_ro()` / `_tx()` **从池里借、用完还回**（不再有「每线程一条连接」那套手工活）；
4. 池参数与任务书给定的一致（`min_size=1` / `max_idle=60` / 健康探测开着）。

为什么用 mock：这一层要验的是**池的接线**（借/还/重建），不是 PG 的行为；
真库那侧由 `tests/test_storage_pg.py` 负责（没凭据就 skip）。
"""

from __future__ import annotations

import contextlib
from typing import Any, List, Optional

import pytest

from keepsake.core.pg import (
    POOL_CHECKOUT_TIMEOUT_S,
    POOL_MAX_IDLE_S,
    POOL_MAX_LIFETIME_S,
    POOL_MAX_SIZE,
    POOL_MIN_SIZE,
    PgStorage,
)


# --------------------------------------------------------------------------
# 假连接 / 假池
# --------------------------------------------------------------------------

class _FakeCursor:
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
    def __init__(self, fail_with: Optional[Exception] = None) -> None:
        self.fail_with = fail_with
        self.borrowed = 0
        self.returned = 0
        self.closed = False

    def getconn(self, timeout: Optional[float] = None) -> _FakeConn:
        if self.fail_with is not None:
            raise self.fail_with
        self.borrowed += 1
        return _FakeConn()

    def putconn(self, conn: Any) -> None:
        self.returned += 1

    def close(self) -> None:
        self.closed = True


def _storage() -> PgStorage:
    """一个**不连库**的 PgStorage 实例（dsn 随便写，池子被 mock 掉）。"""
    return PgStorage(dsn="postgresql://user:pw@127.0.0.1:1/none")


def _with_pool(st: PgStorage, pool: Any) -> PgStorage:
    """给实例塞一个假池（**连 conninfo 一起对齐** —— conninfo 变了池子会重建）。"""
    st._conn_pool = pool                # noqa: SLF001
    st._pool_conninfo = st._conninfo()  # noqa: SLF001
    return st


# --------------------------------------------------------------------------
# ① 池重建语义
# --------------------------------------------------------------------------

def test_checkout抛连接异常时重建池并重试成功(monkeypatch):
    """🔴 事故根因那一条：连接坏了不许「一直坏着」—— 重建池再借一次。"""
    class OperationalError(Exception):
        """按类名判定的假异常（`is_broken_connection_error` 只看类名）。"""

    st = _storage()
    pools: List[_FakePool] = []

    def _build(self: PgStorage) -> _FakePool:
        # 第一个池借不到（连接类异常）⇒ 重建后的第二个池正常
        pool = _FakePool(fail_with=None if pools else OperationalError("boom"))
        pools.append(pool)
        return pool

    monkeypatch.setattr(PgStorage, "_build_pool", _build)
    pool, conn = st._checkout()
    assert len(pools) == 2, "连接类异常后必须重建池"
    assert pools[0].closed is True, "旧池必须关掉（别留着坏连接继续用）"
    assert pool is pools[1]
    assert conn is not None


def test_池被借空不重建池(monkeypatch):
    """🔴 `PoolTimeout` = 并发太高（不是连接坏了）⇒ 重建只会多开连接。"""
    class PoolTimeout(Exception):
        """按类名判定的假异常。"""

    st = _storage()
    pools: List[_FakePool] = []

    def _build(self: PgStorage) -> _FakePool:
        pool = _FakePool(fail_with=PoolTimeout("couldn't get a connection after 30.00 sec"))
        pools.append(pool)
        return pool

    monkeypatch.setattr(PgStorage, "_build_pool", _build)
    with pytest.raises(PoolTimeout):
        st._checkout()
    assert len(pools) == 1, "池超时不该重建（重建 = 多开连接 = 雪崩）"


# --------------------------------------------------------------------------
# ② 借 / 还
# --------------------------------------------------------------------------

def test_ro从池里借连接并归还(monkeypatch):
    st = _storage()
    pool = _FakePool()
    _with_pool(st, pool)
    with st._ro() as cur:
        cur.execute("SELECT 1")
    assert pool.borrowed == 1 and pool.returned == 1


def test_tx从池里借连接并归还(monkeypatch):
    st = _storage()
    pool = _FakePool()
    _with_pool(st, pool)
    with st._tx() as cur:
        cur.execute("SELECT 1")
    assert pool.borrowed == 1 and pool.returned == 1


def test_异常时连接照样归还池子(monkeypatch):
    """🔴 异常路径不许把连接吞掉（吞了就等于泄漏一个池位）。"""
    st = _storage()
    pool = _FakePool()
    _with_pool(st, pool)
    with pytest.raises(RuntimeError):
        with st._ro() as cur:
            cur.execute("SELECT 1")
            raise RuntimeError("炸了")
    assert pool.returned == 1


def test_close关掉连接池():
    st = _storage()
    pool = _FakePool()
    _with_pool(st, pool)
    st.close()
    assert pool.closed is True
    st.close()                                # 可重复调用


def test_conninfo变了池子重建(monkeypatch):
    """🔴 conninfo 变了（评测器追加 `default_transaction_read_only=on` 那一套）
    ⇒ 池子必须重建 —— 不重建就等于「改了配置没生效」。"""
    st = _storage()
    first = _FakePool()
    _with_pool(st, first)
    assert st._get_pool() is first          # noqa: SLF001 —— conninfo 没变 ⇒ 复用

    st._dsn = st._dsn + " options='-c default_transaction_read_only=on'"  # noqa: SLF001
    built: List[_FakePool] = []

    def _build(self: PgStorage) -> _FakePool:
        pool = _FakePool()
        built.append(pool)
        return pool

    monkeypatch.setattr(PgStorage, "_build_pool", _build)
    pool = st._get_pool()                    # noqa: SLF001
    assert pool is not first
    assert first.closed is True, "旧池必须关掉"
    assert len(built) == 1


# --------------------------------------------------------------------------
# ③ 池参数（与任务书给定的一致）
# --------------------------------------------------------------------------

def test_池参数与任务书一致(monkeypatch):
    """🔴 池参数是**有口径的**：min_size=1、max_idle=60、健康探测开着。"""
    captured: dict = {}

    class _FakeCtor:
        @staticmethod
        def check_connection(conn: Any) -> None:
            return None

        def __init__(self, conninfo: str, **kwargs: Any) -> None:
            captured["conninfo"] = conninfo
            captured.update(kwargs)

        def open(self) -> None:
            captured["opened"] = True

    import psycopg
    import psycopg_pool
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _FakeCtor)
    # 建池前的那次「探一下」也要 mock（真连会连不上）
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: _FakeConn())
    st = _storage()
    pool = st._build_pool()
    assert captured["min_size"] == POOL_MIN_SIZE == 1
    assert captured["max_size"] == POOL_MAX_SIZE
    assert captured["max_idle"] == POOL_MAX_IDLE_S == 60.0
    assert captured["max_lifetime"] == POOL_MAX_LIFETIME_S
    assert captured["check"] is _FakeCtor.check_connection
    assert captured["kwargs"] == {"connect_timeout": st._connect_timeout}  # noqa: SLF001
    assert captured["opened"] is True
    assert pool is not None
    assert POOL_CHECKOUT_TIMEOUT_S > 0


def test_坏DSN立刻报连接类异常而不是等池超时(monkeypatch):
    """🔴 「DSN 写错了」必须**立刻**报错（连接类异常），不能变成「每次调用等 30 秒」。

    建池前那次探测就是干这个的：池子会在后台重试连库，没有探测的话客户端
    要等到 checkout 超时才拿到 `PoolTimeout`（那会把配置错误伪装成「PG 抖动」）。
    """
    import psycopg

    class OperationalError(Exception):
        """按类名判定的假异常。"""

    def _boom(*a: Any, **kw: Any) -> Any:
        raise OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", _boom)
    st = _storage()
    with pytest.raises(OperationalError):
        st._build_pool()          # noqa: SLF001
    assert st._conn_pool is None  # noqa: SLF001 —— 建不起来就不留半个池子


# --------------------------------------------------------------------------
# ④ 写路径不许读未定义属性（2026-10 ks_pg_tls_fix）
# --------------------------------------------------------------------------

def test_hits_基数回填不读未定义属性_没有_tls():
    """🔴 `record_hits_repeat` 原来读 `self._tls.in_worker` —— **PgStorage 全类
    没有 `_tls`**（从 Redis 抄来的残留）⇒ 真库上恒抛
    `AttributeError: 'PgStorage' object has no attribute '_tls'`，被调用方吞掉
    ⇒ 提名基数回填失效、自动提名永远攒不满基数。

    本用例**不连库**（假池 + 假游标）⇒ 没 PG 的 CI 上也能钉住这条线。
    """
    class _KeepConnPool(_FakePool):
        """把借出去的连接留下来，好断言真的发过 INSERT。"""

        def __init__(self) -> None:
            super().__init__()
            self.conns: List[_FakeConn] = []

        def getconn(self, timeout: Optional[float] = None) -> _FakeConn:
            conn = super().getconn(timeout=timeout)
            self.conns.append(conn)
            return conn

    st = _storage()
    pool = _KeepConnPool()
    _with_pool(st, pool)
    assert st.record_hits_repeat("memory:frag:x", 3) == 3
    stmts = pool.conns[0].cur.stmts
    assert len(stmts) == 1, "3 行 = 一条多行 INSERT（不许逐条 insert）"
    sql, params = stmts[0]
    assert sql.startswith("INSERT INTO ks_hit_event (key, ts)"), sql
    assert len(params) == 6, "3 行 × 2 列 = 6 个绑定参数"
