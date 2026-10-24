"""检索命中打点（2026-10 ks_hit_tracking）—— 三后端 + API。

本单只做**数据地基**：检索命中时异步记事件、按天窗统计、过期清理。
升降级逻辑不在这里（下一单 core_tiering），所以本文件里**没有**任何
「够热就升常驻」的判断。

## 本文件锁死的三件事

  1. **检索结果零变化**：打点是纯侧写 —— 打点抛异常，检索照常返回原结果
     （fail-open 双向断言：真打点炸一次 + stub 炸一次）；
  2. **不阻塞 + 不重复**：默认执行器是后台线程（断言打点发生在别的线程），
     同 key 300 秒内只记一次（改判据时间源，不 sleep 5 分钟）；
  3. **统计口径**：窗口 / 阈值 / keys 白名单三口径 + 空表返回空 items 不报错，
     prune 删旧留新。

## 异步怎么测（gate 说明）

真实执行器是 daemon 后台线程，断言方式是**在线程里打标记**：
把 `record_hits` 换成会 `Event.set()` 的 stub，跑一次真检索，等 Event 超时窗口
（2s，测试结束即走），再比 `threading.get_ident()` 与主线程不同 ——
即「打点没发生在检索调用线程」。异常路径另用 stub 直接 `raise`，
断言 `search` 返回值与打点前逐字一致（fail-open）。
限频那条不改判据时间源也不 sleep：直接动实例上的 `_hit_seen` 时间戳
（往回拨 400 秒 ⇒ 仍在窗口内；拨到 10 分钟前 ⇒ 窗口已过），
这样测的是**真限频逻辑**，不是把间隔改小的假窗口。

SQLite 用例全 hermetic（tmp_path 临时库）；Redis 用假 client（不连真 Redis，
与 `test_list_by_tag_archived.py` 同一套接法）；PG 连
`~/.keepsake_pg_test.env` 里的 KEEPSAKE_PG_TEST_HOST（缺省 postgres.test.example）/keepsake_test（隔离 schema），
读不到凭据就 skip —— 与 `test_list_by_tag_archived.py` 同一套接法。
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from keepsake.core import base as storage_base
from keepsake.storage import RedisStorage
from keepsake.storage_sqlite import SqliteStorage

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST, ALLOWED_DBNAME = os.environ.get("KEEPSAKE_PG_TEST_HOST", "postgres.test.example"), "keepsake_test"
psycopg = pytest.importorskip("psycopg", reason="未装 psycopg")

CORPUS = ["回测策略命中率提升明显", "风控规则收紧审批流程", "量化组合再平衡节奏"]


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


DSN = _read_dsn()
_pg_ready = bool(DSN and ALLOWED_HOST in DSN and ALLOWED_DBNAME in DSN)


def _wait_for(fn, timeout: float = 2.0):
    """轮询等后台打点落库 —— 真线程是异步的（默认 2s 窗口，远大于实际毫秒级）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.02)
    return fn()


# ---------------------------------------------------------------------------
# 零、内存 fake storage：打点语义（三后端共用同一份，共用逻辑就该一份测）
# ---------------------------------------------------------------------------

class MemStorage:
    """只实现 `record_hits` 的内存替身（限频/异步/fail-open 与后端无关）。"""

    def __init__(self, boom: bool = False) -> None:
        self.rows: List[Dict[str, Any]] = []
        self.boom = boom
        self.thread_ids: List[int] = []

    def record_hits(self, keys: List[str]) -> int:
        self.thread_ids.append(threading.get_ident())
        if self.boom:
            raise RuntimeError("落库炸了（故意的）")
        now = datetime.now(timezone.utc).isoformat()
        self.rows.extend({"key": k, "ts": now} for k in keys)
        return len(keys)


def test_打点记进事件表():
    st = MemStorage()
    # 空 key 丢掉、同批重复去重 ⇒ 只放行 1 个
    assert storage_base.fire_hits(st, ["memory:frag:a", "memory:frag:a", ""]) == 1
    _wait_for(lambda: st.rows)
    assert [r["key"] for r in st.rows] == ["memory:frag:a"]


def test_同key_300秒内不重复记():
    """限频 = 同 key 五分钟内只放行一次；把判据时间源拨到窗口外即恢复。"""
    st = MemStorage()
    assert storage_base.hits_pending(st, ["k1"], now=1000.0) == ["k1"]
    assert storage_base.hits_pending(st, ["k1"], now=1000.0 + 299.0) == []
    assert storage_base.hits_pending(st, ["k1"], now=1000.0 + 300.0) == ["k1"]
    assert storage_base.HIT_RATE_LIMIT_S == 300.0


def test_后台线程打点_线程标识与主线程不同():
    """默认执行器 = 后台 daemon 线程（断言用线程标识，不靠 sleep 猜）。"""
    st = MemStorage()
    done = threading.Event()
    real = st.record_hits

    def _wrapped(keys: List[str]) -> int:
        st.thread_ids.append(threading.get_ident())
        done.set()
        return real(keys)

    st.record_hits = _wrapped                # type: ignore[method-assign]
    storage_base.fire_hits(st, ["k1"])
    assert done.wait(2.0), "后台线程没跑起来"
    assert st.thread_ids[0] != threading.get_ident(), "打点跑在检索调用线程上了（热路径被阻塞）"
    assert [r["key"] for r in st.rows] == ["k1"]


def test_打点失败_fail_open_不抛():
    """打点落库抛异常 ⇒ `fire_hits` 照常返回（异常绝不许冒进检索路径）。"""
    st = MemStorage(boom=True)
    assert storage_base.fire_hits(st, ["k1"]) == 1     # 不抛
    time.sleep(0.05)                                     # 后台线程把异常吞成 debug
    assert st.rows == []


def test_限频表本身炸了也_fail_open(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("限频表炸了")
    monkeypatch.setattr(storage_base, "hits_pending", _boom)
    assert storage_base.fire_hits(MemStorage(), ["k1"]) == 0


def test_起线程失败也_fail_open(monkeypatch):
    monkeypatch.setattr(storage_base, "submit_hit_work", lambda fn: False)
    assert storage_base.fire_hits(MemStorage(), ["k1"]) == 1


# ---------------------------------------------------------------------------
# 一、SQLite 真库（hermetic）：检索 → 事件行 → stats → prune
# ---------------------------------------------------------------------------

@pytest.fixture()
def lite(tmp_path):
    s = SqliteStorage(path=str(tmp_path / "hits.db"), is_primary=True)
    assert s.ensure_index() is True
    for text in CORPUS:
        assert s.store(text=text, tags="memory,shared", category="demo") is True
    yield s
    s.close()


def _hit_rows(s: SqliteStorage) -> List[tuple]:
    with s._lock:                                          # noqa: SLF001
        return s._db().execute(                           # noqa: SLF001
            "SELECT key, ts FROM ks_hit_event").fetchall()


def _insert_hits(s: SqliteStorage, key: str, when: datetime, n: int = 1) -> None:
    """直插事件行（造历史数据用）—— 打点只写「现在」，历史 ts 只能造。"""
    with s._write() as cur:
        cur.executemany("INSERT INTO ks_hit_event (key, ts) VALUES (?, ?)",
                        [(key, (when + timedelta(seconds=i)).isoformat())
                         for i in range(n)])


def test_检索一次后事件表有行(lite):
    out = lite.search_bm25("回测")
    assert out, "前置：本用例要真检索到东西"
    _wait_for(lambda: _hit_rows(lite))
    rows = _hit_rows(lite)
    assert {k for k, _ in rows} == {f["_key"] for f in out}
    assert all(ts for _, ts in rows), "ts 不许为空（窗口统计全靠它）"


def test_300秒内二次检索不加行(lite):
    """不 sleep 5 分钟：把实例上的限频时间戳**拨回窗口内**再检索一次。"""
    first = lite.search_bm25("回测")
    assert first
    _wait_for(lambda: _hit_rows(lite))
    n0 = len(_hit_rows(lite))
    assert n0 > 0

    lite._hit_seen = {k: time.time() for k in lite._hit_seen}   # noqa: SLF001
    lite.search_bm25("回测")
    time.sleep(0.1)                                              # 给后台线程机会（不该有）
    assert len(_hit_rows(lite)) == n0, "300 秒窗口内不该重复记行"

    # 窗口已过 ⇒ 允许再记（同时证明限频不是「永久只记一次」）
    lite._hit_seen = {k: time.time() - 301 for k in lite._hit_seen}  # noqa: SLF001
    lite.search_bm25("回测")
    _wait_for(lambda: len(_hit_rows(lite)) > n0)
    assert len(_hit_rows(lite)) > n0


def test_打点炸了_检索结果不受影响(lite, monkeypatch):
    """fail-open 双向断言：打点落库抛异常 ⇒ 检索返回内容/顺序逐字不变。"""
    baseline = lite.search_bm25("回测")
    assert baseline

    def _boom(keys: List[str]) -> int:
        raise RuntimeError("打点炸了（故意的）")

    lite.record_hits = _boom            # type: ignore[method-assign]
    for k in getattr(lite, "_hit_seen", {}):
        lite._hit_seen[k] = time.time() - 301          # 绕开限频，逼它真去打点  # noqa: SLF001
    got = lite.search_bm25("回测")
    time.sleep(0.05)
    assert [f["_key"] for f in got] == [f["_key"] for f in baseline]
    assert [f.get("_bm25_score") for f in got] == [f.get("_bm25_score") for f in baseline]


def test_search_也打点(lite):
    lite.search("回测")
    _wait_for(lambda: _hit_rows(lite))
    assert _hit_rows(lite), "search() 入口同样要打点（上层走的多是它）"


def test_stats_窗口与阈值(lite):
    now = datetime.now(timezone.utc)
    hot_at = now - timedelta(days=1)
    _insert_hits(lite, "hot", hot_at, n=5)
    _insert_hits(lite, "warm", hot_at, n=4)
    _insert_hits(lite, "cold", now - timedelta(days=40), n=9)      # 窗口外

    items = lite.hits_stats(window_days=7, min_hits=5)
    assert [it["key"] for it in items] == ["hot"], items
    assert items[0]["hits"] == 5
    # last_hit = 窗口内最后一次命中（ISO8601），不许是窗口外那条
    last = datetime.fromisoformat(items[0]["last_hit"])
    assert hot_at <= last <= now, items[0]

    # 窗口放到 90 天 ⇒ 窗口外那条进来；阈值放到 1 ⇒ warm 也进来，且按 hits 降序
    items = lite.hits_stats(window_days=90, min_hits=1)
    assert [it["key"] for it in items] == ["cold", "hot", "warm"], items


def test_stats_keys_白名单不过阈值(lite):
    now = datetime.now(timezone.utc)
    _insert_hits(lite, "k1", now, n=1)
    _insert_hits(lite, "k2", now, n=3)
    items = lite.hits_stats(window_days=7, min_hits=5, keys=["k1", "k2", "missing"])
    # 给了 keys 就不套 min_hits（候选集已知，热度阈值归调用方判断）
    assert {it["key"]: it["hits"] for it in items} == {"k1": 1, "k2": 3}
    assert all(it["last_hit"] for it in items), "last_hit 不许为空串以外的形态"


def test_stats_空表返回空_items_不报错(lite):
    assert lite.hits_stats(window_days=7, min_hits=5) == []
    assert lite.record_hits([]) == 0


def test_prune_删旧留新(lite):
    now = datetime.now(timezone.utc)
    _insert_hits(lite, "old", now - timedelta(days=100), n=3)
    _insert_hits(lite, "new", now - timedelta(days=1), n=2)

    assert lite.prune_hit_events(days=90) == 3
    assert {k for k, _ in _hit_rows(lite)} == {"new"}
    assert lite.prune_hit_events(days=90) == 0


# ---------------------------------------------------------------------------
# 一之二、提名基数回填（`record_hits_repeat`）：同一个 key 补 N 条事件
# ---------------------------------------------------------------------------

def test_基数回填_同一个key补N条事件(lite):
    """基数回填要的是「同一个 key 落 N 行」—— `record_hits` 按 key 去重，
    一次调用同一个 key 只落一行，所以另开一个原语（不许拿 record_hits 顶替）。
    """
    assert lite.record_hits_repeat("memory:frag:hot", 3000) == 3000
    rows = [r for r in _hit_rows(lite) if r[0] == "memory:frag:hot"]
    assert len(rows) == 3000, "基数必须是 3000 条事件行（规则二数的是行数）"
    stats = lite.hits_stats(window_days=7, min_hits=1, keys=["memory:frag:hot"])
    assert stats and stats[0]["hits"] == 3000, stats


def test_基数回填_零条与空key不写(lite):
    assert lite.record_hits_repeat("memory:frag:x", 0) == 0
    assert lite.record_hits_repeat("", 100) == 0
    assert _hit_rows(lite) == []


# ---------------------------------------------------------------------------
# 一之三、窗口锚点 `since`：绝对时刻（分级班车复核「自提名日起」要用）
# ---------------------------------------------------------------------------

def test_stats_since_绝对锚点替代滚动窗(lite):
    """🔴 `since` = 窗口锚点：给了就从那一刻起算（`window_days` 作废）。

    这是分级班车复核口径「**自提名日起**累计命中」的存储层原语 ——
    滚动窗表达不了它（每条候选的挂龄不同，窗口长度得跟着走，还得靠挂钟对齐）。
    """
    now = datetime.now(timezone.utc)
    _insert_hits(lite, "old", now - timedelta(days=30), n=5)
    _insert_hits(lite, "new", now - timedelta(days=2), n=3)

    # 滚动 7 天窗：只数得到 2 天前那 3 条
    got = {it["key"]: it["hits"]
           for it in lite.hits_stats(window_days=7, keys=["old", "new"])}
    assert got == {"new": 3}, got

    # 锚点挪到 30 天前 ⇒ 8 条全数（「对历史命中不封顶」）
    got = {it["key"]: it["hits"]
           for it in lite.hits_stats(since=now - timedelta(days=30), keys=["old", "new"])}
    assert got == {"old": 5, "new": 3}, got

    # 锚点挪到 29 天前 ⇒ 30 天前那 5 条不在窗口内（起点就是锚点）
    got = {it["key"]: it["hits"]
           for it in lite.hits_stats(since=now - timedelta(days=29), keys=["old", "new"])}
    assert got == {"new": 3}, got


# ---------------------------------------------------------------------------
# 二、Redis：假 client（zset + hash 计数），不连真 Redis
# ---------------------------------------------------------------------------

class FakeHitsClient:
    """只实现命中打点用到的 Redis 命令（zadd GT / hincrby / hget / zrangebyscore…）。"""

    def __init__(self) -> None:
        self.zset: Dict[str, float] = {}
        self.counter: Dict[str, int] = {}

    def pipeline(self):
        client = self

        class P:
            def __init__(self):
                self.q: List[tuple] = []

            def zadd(self, key, mapping, **kw):
                self.q.append(("zadd", mapping, kw))

            def hincrby(self, key, field, amount):
                self.q.append(("hincrby", field, amount))

            def hget(self, key, field):
                self.q.append(("hget", field, None))

            def hdel(self, key, *fields):
                self.q.append(("hdel", tuple(fields), None))

            def zremrangebyscore(self, key, lo, hi):
                self.q.append(("zrem", (lo, hi), None))

            def execute(self):
                out = []
                for op, a, b in self.q:
                    if op == "zadd":
                        gt = (b or {}).get("gt")
                        for m, score in a.items():
                            cur = client.zset.get(m)
                            if gt and cur is not None and score <= cur:
                                continue          # GT：只在真的前移时才写
                            client.zset[m] = score
                    elif op == "hincrby":
                        client.counter[a] = client.counter.get(a, 0) + b
                    elif op == "hget":
                        v = client.counter.get(a)
                        out.append(str(v).encode() if v is not None else None)
                    elif op == "hdel":
                        for f in a:
                            client.counter.pop(f, None)
                    elif op == "zrem":
                        _lo, hi = a
                        hi_f = float(hi[1:]) if hi.startswith("(") else float(hi)
                        for m in [m for m, s in client.zset.items() if s < hi_f]:
                            client.zset.pop(m, None)
                self.q = []
                return out

        return P()

    def zrangebyscore(self, key, lo, hi, withscores=False):
        lo_f = float(lo) if lo != "-inf" else float("-inf")
        if hi == "+inf":
            hi_f = float("inf")
        elif hi.startswith("("):                          # 排他上界（prune 用）
            hi_f = float(hi[1:])
        else:
            hi_f = float(hi)
        members = [(m.encode(), s) for m, s in sorted(self.zset.items())
                   if lo_f <= s <= hi_f]
        return members if withscores else [m for m, _ in members]


@pytest.fixture()
def rstore(monkeypatch):
    client = FakeHitsClient()
    monkeypatch.setattr(RedisStorage, "_get_client", lambda self: client)
    s = object.__new__(RedisStorage)
    s._hit_seen = {}                     # noqa: SLF001
    return s, client


def test_redis_记点与统计(rstore):
    s, client = rstore
    assert s.record_hits(["a", "a", "b"]) == 2
    assert client.counter == {"a": 1, "b": 1}
    assert set(client.zset) == {"a", "b"}

    for _ in range(4):
        s.record_hits(["a"])
    items = s.hits_stats(window_days=7, min_hits=5)
    assert [(it["key"], it["hits"]) for it in items] == [("a", 5)]
    assert items[0]["last_hit"], "last_hit 必须来自 zset 的 score"


def test_redis_基数回填_一次HINCRBY补N次(rstore):
    """Redis 侧命中口径是「hash 累计计数」，所以补 N 次 = 一次 HINCRBY N
    （与行表侧「补 N 行」同义，且只一次往返）。"""
    s, client = rstore
    assert s.record_hits_repeat("a", 3000) == 3000
    assert client.counter == {"a": 3000}
    assert "a" in client.zset, "zset 的最近时刻也要刷（窗口筛只看它）"
    items = s.hits_stats(window_days=7, min_hits=1, keys=["a"])
    assert items and items[0]["hits"] == 3000, items
    assert s.record_hits_repeat("a", 0) == 0
    assert s.record_hits_repeat("", 10) == 0


def test_redis_since_绝对锚点筛掉更早的key(rstore):
    """Redis 侧 `since` 同样改窗口起点（zset 的最近时刻早于锚点 ⇒ 不在窗口内）。

    🔴 已知口径差（与本模块其它 Redis 用例同款）：计数取自 hash 的**累计**次数，
    `since` 只筛「最近一次命中在不在窗口内」。
    """
    s, client = rstore
    client.zset["old"] = time.time() - 40 * 86400
    client.zset["new"] = time.time() - 2 * 86400
    client.counter["old"] = 5
    client.counter["new"] = 3
    anchor = datetime.now(timezone.utc) - timedelta(days=30)
    got = {it["key"]: it["hits"]
           for it in s.hits_stats(since=anchor, keys=["old", "new"])}
    assert got == {"new": 3}, got


def test_redis_白名单与阈值(rstore):
    s, client = rstore
    s.record_hits(["a"])
    s.record_hits(["b"])
    s.record_hits(["b"])
    assert s.hits_stats(window_days=7, min_hits=5) == []          # 都没到阈值
    assert {it["key"]: it["hits"]
            for it in s.hits_stats(window_days=7, keys=["a", "b"])} == {"a": 1, "b": 2}
    assert s.hits_stats(window_days=7, keys=["missing"]) == []


def test_redis_窗口过滤(rstore):
    s, client = rstore
    s.record_hits(["old"])
    client.zset["old"] = time.time() - 40 * 86400
    s.record_hits(["new"])
    assert [it["key"] for it in s.hits_stats(window_days=7, min_hits=1)] == ["new"]


def test_redis_prune_删旧留新(rstore):
    s, client = rstore
    s.record_hits(["old"])
    client.zset["old"] = time.time() - 100 * 86400
    s.record_hits(["new"])

    assert s.prune_hit_events(days=90) == 1
    assert set(client.zset) == {"new"}
    assert "old" not in client.counter, "计数 hash 的字段要跟着清，否则计数永不回收"
    assert s.prune_hit_events(days=90) == 0


def test_redis_不可达返回0(monkeypatch):
    monkeypatch.setattr(RedisStorage, "_get_client", lambda self: None)
    s = object.__new__(RedisStorage)
    assert s.record_hits(["a"]) == 0
    assert s.hits_stats() == []
    assert s.prune_hit_events() == 0


# ---------------------------------------------------------------------------
# 三、PG 真库（无凭据则 skip）：表 + 统计 + prune
# ---------------------------------------------------------------------------

@pytest.fixture()
def pgs():
    if not _pg_ready:
        pytest.skip("无测试库凭据，跳过 PG 真库")
    from keepsake.storage_pg import PgStorage

    schema = f"ks_hits_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    pg = PgStorage(dsn=DSN + f"?options=-csearch_path%3D{schema},public",
                   connect_timeout=30, embed_dim=4, is_primary=True)
    assert pg.ensure_index() is True
    pg._hit_seen = {}                                  # noqa: SLF001
    yield pg
    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    pg.close()


def _pg_rows(pg) -> List[tuple]:
    with pg._ro() as cur:                              # noqa: SLF001
        cur.execute("SELECT key, ts FROM ks_hit_event")
        return cur.fetchall()


def test_pg_检索一次后事件表有行(pgs):
    assert pgs.store(text="回测策略命中率提升明显", tags="memory,shared") is True
    out = pgs.search_bm25("回测")
    assert out, "前置：本用例要真检索到东西"
    _wait_for(lambda: _pg_rows(pgs))
    assert {k for k, _ in _pg_rows(pgs)} == {f["_key"] for f in out}


def test_pg_stats_窗口阈值白名单(pgs):
    # 同 key 的多次命中 = 多次 record_hits（限频在上游 fire_hits，不在这层）
    for _ in range(5):
        assert pgs.record_hits(["hot"]) == 1
    for _ in range(4):
        pgs.record_hits(["warm"])
    for _ in range(9):
        pgs.record_hits(["old"])
    now = datetime.now(timezone.utc)
    with pgs._tx() as cur:                             # noqa: SLF001
        cur.execute("UPDATE ks_hit_event SET ts = %s WHERE key = %s",
                    (now - timedelta(days=40), "old"))

    items = pgs.hits_stats(window_days=7, min_hits=5)
    assert [(it["key"], it["hits"]) for it in items] == [("hot", 5)], items
    last = datetime.fromisoformat(items[0]["last_hit"])
    assert now - timedelta(minutes=5) <= last <= now, items[0]

    got = {it["key"]: it["hits"]
           for it in pgs.hits_stats(window_days=7, keys=["warm", "hot", "nope"])}
    assert got == {"hot": 5, "warm": 4}
    assert pgs.hits_stats(window_days=7, min_hits=99) == []


def test_pg_since_绝对锚点(pgs):
    """PG 侧 `since` 走**绝对时刻参数**（不是 `now() - interval` 表达式）——
    锚点是调用方给的时刻（提名日），不受数据库挂钟与注入时钟的漂移影响。
    """
    for _ in range(5):
        assert pgs.record_hits(["old"]) == 1
    now = datetime.now(timezone.utc)
    with pgs._tx() as cur:                             # noqa: SLF001
        cur.execute("UPDATE ks_hit_event SET ts = %s WHERE key = %s",
                    (now - timedelta(days=30), "old"))
    got = {it["key"]: it["hits"]
           for it in pgs.hits_stats(since=now - timedelta(days=30), keys=["old"])}
    assert got == {"old": 5}, got
    # 锚点挪到 29 天前 ⇒ 30 天前那 5 条不在窗口内
    got = {it["key"]: it["hits"]
           for it in pgs.hits_stats(since=now - timedelta(days=29), keys=["old"])}
    assert got == {}, got


def test_pg_prune_删旧留新(pgs):
    now = datetime.now(timezone.utc)
    for _ in range(3):
        pgs.record_hits(["old"])
    pgs.record_hits(["new"])
    with pgs._tx() as cur:                             # noqa: SLF001
        cur.execute("UPDATE ks_hit_event SET ts = %s WHERE key = %s",
                    (now - timedelta(days=100), "old"))
    assert pgs.prune_hit_events(days=90) == 3
    assert {k for k, _ in _pg_rows(pgs)} == {"new"}
    assert pgs.prune_hit_events(days=90) == 0


def test_pg_hits_基数回填_同一个key补N条事件(pgs):
    """🔴 2026-10 ks_pg_tls_fix：`record_hits_repeat` 原来读 `self._tls.in_worker`
    —— PgStorage 全类**没有** `_tls`（从 Redis 抄来的残留）⇒ 真库上恒抛
    `AttributeError: 'PgStorage' object has no attribute '_tls'`，被调用方吞掉
    ⇒ 提名基数回填失效（自动提名永远攒不满基数）。

    本用例钉住两件事：返回条数 = N，且**真的落 N 行** `ks_hit_event`。
    """
    assert pgs.record_hits_repeat("memory:frag:pgbackfill", 3000) == 3000
    rows = [r for r in _pg_rows(pgs) if r[0] == "memory:frag:pgbackfill"]
    assert len(rows) == 3000, "基数回填必须真的落 3000 行事件（规则二数的是行数）"
    items = pgs.hits_stats(window_days=7, min_hits=1, keys=["memory:frag:pgbackfill"])
    assert items and items[0]["hits"] == 3000, items


# ---------------------------------------------------------------------------
# 四、API：只读 + 认证 + 出参形状
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from keepsake.api.app import create_app
    from keepsake.api.deps import get_storage

    class HitsFakeStorage:
        def __init__(self) -> None:
            self.calls: List[Dict[str, Any]] = []

        def hits_stats(self, window_days=7, min_hits=5, keys=None, limit=500):
            self.calls.append({"window_days": window_days, "min_hits": min_hits,
                               "keys": keys, "limit": limit})
            if keys is not None:
                return [{"key": keys[0], "hits": 9, "last_hit": "2026-10-15T00:00:00+00:00"}]
            return [
                {"key": "memory:frag:a", "hits": 7, "last_hit": "2026-10-15T00:00:00+00:00"},
                {"key": "memory:frag:b", "hits": 5, "last_hit": "2026-10-14T00:00:00+00:00"},
            ]

    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    app = create_app()
    storage = HitsFakeStorage()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as c:
        c.hits_storage = storage            # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()


TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_api_未登录_401(client):
    r = client.get("/api/v1/hits/stats")
    assert r.status_code == 401


def test_api_错token_401(client):
    r = client.get("/api/v1/hits/stats",
                   headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def test_api_形状与参数透传(client):
    r = client.get("/api/v1/hits/stats", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 7 and body["min_hits"] == 5
    assert [it["key"] for it in body["items"]] == ["memory:frag:a", "memory:frag:b"]
    assert body["items"][0]["hits"] == 7
    assert client.hits_storage.calls[-1] == {         # noqa: B018
        "window_days": 7, "min_hits": 5, "keys": None, "limit": 500,
    }


def test_api_参数与白名单(client):
    r = client.get("/api/v1/hits/stats?window_days=30&min_hits=2&keys=memory%3Afrag%3Ax",
                   headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 30 and body["min_hits"] == 2
    assert [it["key"] for it in body["items"]] == ["memory:frag:x"]
    assert client.hits_storage.calls[-1]["keys"] == ["memory:frag:x"]  # noqa: B018


def test_api_参数越界_422(client):
    """信任边界上的输入校验：window_days=0 / min_hits=0 一律 422，不进存储层。"""
    for qs in ("window_days=0", "window_days=99999", "min_hits=0"):
        r = client.get(f"/api/v1/hits/stats?{qs}", headers=AUTH)
        assert r.status_code == 422, qs


def test_api_数据源异常_500_不吞成空(monkeypatch):
    """打点/统计后端炸了 ⇒ 500，绝不装成「空 items 成功」。

    空表与「遥测挂了」必须可区分：下一单 core_tiering 会拿它当升降级信号，
    误判成空 = 把整批常驻层当冷的。

    （Starlette 的 ServerErrorMiddleware 发完 500 响应**仍会把异常抛给 ASGI
    服务器**，所以要关掉 raise_server_exceptions —— 与 test_api_readonly 同款。）
    """
    from fastapi.testclient import TestClient

    from keepsake.api.app import create_app
    from keepsake.api.deps import get_storage

    class BoomStorage:
        """数据源异常替身 —— 用来验「不许吞异常装成功」。"""

        def hits_stats(self, *a, **kw):
            raise RuntimeError("后端不可达")

    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: BoomStorage()
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/v1/hits/stats", headers=AUTH)
    app.dependency_overrides.clear()
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "internal_error"
