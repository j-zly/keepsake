"""并发 `ensure_index()` + 并发写的回归测试 —— 把线上偶发死锁固化成必测。

## 为什么必须有这个文件（事故背景）

2026-10 ks_pg_b2r2：批 2 的全量测试**偶发死锁**（460 条里偶发 1 条
`psycopg.errors.DeadlockDetected`）。PG 服务端日志把互相等待的两条语句都记下来了：

    Process A waits for RowExclusiveLock on ks_entity_timeline; blocked by B
    Process B waits for AccessExclusiveLock on ks_fragment; blocked by A
    A: INSERT INTO ks_entity_timeline (...) ON CONFLICT ... DO UPDATE
    B: ALTER TABLE ks_fragment ADD COLUMN IF NOT EXISTS content_tsv tsvector

根因：`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` **列已存在也要拿 ACCESS EXCLUSIVE**
（要先查目录、要落元数据），于是它与并发 DML 互等成环；更糟的是 ACCESS EXCLUSIVE
**挡住整张 ks_fragment 的读写**。生产上 provider、提炼 cron、迁移脚本各自构造存储、
各自调 `ensure_index()` ⇒ 这不是测试问题，是线上会踩的雷。

## 这里断言什么（分清「结构不变量」与「概率现象」）

死锁本身是**概率现象**（取决于两个后端的物理交错），写一个「必然死锁」的
回归测试只会变成另一个偶发失败的测试。所以这里锁的是**它成立的前提**：

  * `test_concurrent_ensure_index_and_writes_never_deadlock`
    20 轮「两个连接 barrier 对齐后同时 ensure_index() + 同时写」。
    断言：无死锁/无超时/无异常，两边的写入都真的落库。
    旧写法（无条件重发 DDL）下这 20 轮里 B 会拿 ACCESS EXCLUSIVE 挡写 ——
    配合 A 的 `store()` 写 ks_entity_timeline / ks_fragment，正是日志里那对互等。
  * `test_ensure_index_issues_no_ddl_when_schema_present`
    锁住**结构不变量本身**：schema 齐备时 `ensure_index()` **一条 DDL 都不发**。
    没有这条 DDL，就永远拿不到 ACCESS EXCLUSIVE，环形等待无从形成。

两条合起来 = 「根因被消灭」的证明；而重跑 20 轮的那条负责抓「挂死 / 锁泄漏 /
写丢失」这类回归。

## 凭据与库（同 test_storage_pg.py）

  * DSN 只从 `~/.keepsake_pg_test.env` 的 KEEPSAKE_PG_DSN 读
  * 读不到 / 没装 psycopg / 指向非 88 的 keepsake_test → skip，不 fail
  * 凭据绝不进代码、日志、断言输出
  * 测试结束清掉本次写入的全部行
"""

from __future__ import annotations

import inspect
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import List

import pytest

from keepsake import storage_pg
from keepsake.storage_pg import PgStorage

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST = "8.140.192.91"
ALLOWED_DBNAME = "keepsake_test"

ROUNDS = 20


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


psycopg = pytest.importorskip("psycopg", reason="未装 psycopg（pip install 'psycopg[binary]'）")
DSN = _read_dsn()
if not (DSN and ALLOWED_HOST in DSN and ALLOWED_DBNAME in DSN):
    pytest.skip(f"无测试库凭据或指向非 {ALLOWED_HOST}/{ALLOWED_DBNAME}，跳过 PG 并发测试",
                allow_module_level=True)


# =============================================================================
# 记录实际发出的 SQL 的探针（测试侧 shim，**不改生产代码**）
# =============================================================================


class _RecordingCursor:
    """包一层真游标：记录每条 execute 的 SQL，其余属性原样转发。

    顺带在 `SET LOCAL lock_timeout` 生效的**同一事务内**读一次 `SHOW lock_timeout` ——
    迁移段一提交 SET LOCAL 就被还原了，事后在外面查只会看到 0，测不到真实取值。
    """

    def __init__(self, cur, log: List[str], settings: List[str], params_log: List[tuple] = None):
        self._cur = cur
        self._log = log
        self._settings = settings
        # 排序探针要用：多行 INSERT 的**行序在参数里**（SQL 文本里只有一串 %s），
        # 只记 SQL 记不到「真正发出去的行序」。
        self._params_log = params_log if params_log is not None else []

    def execute(self, query, params=None, **kw):
        sql = " ".join(query.split())
        self._log.append(sql)
        if params is not None and self._params_log is not None:
            self._params_log.append((sql, list(params)))
        if params is None:
            out = self._cur.execute(query, **kw)
        else:
            out = self._cur.execute(query, params, **kw)
        head = sql.lower()
        if head.startswith("set local") and "lock_timeout" in head:
            self._cur.execute("SHOW lock_timeout")
            self._settings.append(self._cur.fetchone()[0])
        return out

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _RecordingPg(PgStorage):
    """PgStorage + SQL 记录。`_tx` 是本模块唯一的写入口，包它就够了。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sql_log: List[str] = []
        self.lock_timeout_seen: List[str] = []
        self.params_log: List[tuple] = []

    @contextmanager
    def _tx(self):
        with super()._tx() as cur:
            yield _RecordingCursor(cur, self.sql_log, self.lock_timeout_seen, self.params_log)


def _ddl_of(sql_log: List[str]) -> List[str]:
    """从 SQL 日志里挑出 DDL（ALTER / CREATE / DROP）。"""
    heads = ("alter ", "create ", "drop ")
    return [s for s in sql_log if s.lower().startswith(heads)]


# =============================================================================
# fixture
# =============================================================================

_AUX_TABLES = ("ks_hot_topic", "ks_hot_topic_seen", "ks_attention", "ks_entity_cooc")
_written_keys: List[str] = []


def _aux_snapshot(conn) -> dict:
    """辅助表的行主键快照（用于「只删自己新写的」）。"""
    snap = {}
    with conn.cursor() as cur:
        for t in _AUX_TABLES:
            cur.execute(f"SELECT * FROM {t}")
            snap[t] = {tuple(r) for r in cur.fetchall()}
    return snap


def _aux_cleanup(conn, snap: dict) -> None:
    """删掉快照里没有的行 —— 本次测试自己写进去的那些。"""
    cols = {
        "ks_hot_topic": ("scope", "topic"),
        "ks_hot_topic_seen": ("topic",),
        "ks_attention": ("scope", "topic"),
        "ks_entity_cooc": ("pair",),
    }
    with conn.cursor() as cur:
        for t, keycols in cols.items():
            cur.execute(f"SELECT * FROM {t}")
            before = snap.get(t, set())
            # 排序：DELETE 逐行拿行锁，同款「行序=加锁顺序」，清理也别自己撞自己
            new = sorted(tuple(r) for r in cur.fetchall() if tuple(r) not in before)
            if new:
                where = " AND ".join(f"{c} = %s" for c in keycols)
                cur.executemany(f"DELETE FROM {t} WHERE {where}",
                                [tuple(r[:len(keycols)]) for r in new])
    conn.commit()


@pytest.fixture(autouse=True)
def _clean_state():
    """每个测试前后清掉进程级记忆，并清掉本测试写入的行。"""
    storage_pg._SCHEMA_READY.clear()          # noqa: SLF001 — 测试需强制走真实迁移段
    conn = psycopg.connect(DSN)
    conn.commit()
    snap = _aux_snapshot(conn)
    yield
    try:
        if _written_keys:
            with conn.cursor() as cur:
                for k in _written_keys:
                    cur.execute("DELETE FROM ks_entity_timeline WHERE frag_key = %s "
                                "OR frag_key LIKE %s", (k, f"{k}:%"))
                    cur.execute("DELETE FROM ks_fragment WHERE key = %s OR key LIKE %s",
                                (k, f"{k}:%"))
            conn.commit()
            _written_keys.clear()
        _aux_cleanup(conn, snap)
    finally:
        storage_pg._SCHEMA_READY.clear()      # noqa: SLF001
        conn.close()


@pytest.fixture(scope="module")
def warm():
    """先确保 schema 齐备（后面的并发轮次跑在「稳态」上）。"""
    s = PgStorage(dsn=DSN, connect_timeout=30)
    assert s.ensure_index() is True
    yield s
    s.close()


# =============================================================================
# ① 并发回归：两个连接同时 ensure_index + 同时写
# =============================================================================


def test_concurrent_ensure_index_and_writes_never_deadlock(warm):
    """20 轮「provider 与 cron 同时到」：无死锁、无超时、两边写入都成功。

    两条连接**全程复用**（真实服务就是这样：连接池常驻，不是每轮重连）。
    每轮清掉进程级记忆 ⇒ 两个线程每轮都真的走一遍迁移段核对与 DDL 决策。
    """
    marker = uuid.uuid4().hex[:8]
    # 两个 worker = 两个独立连接（模拟 provider 进程与提炼 cron 各自一个实例）
    storages = [PgStorage(dsn=DSN, connect_timeout=30) for _ in (0, 1)]
    errors: List[str] = []
    per_round: List[dict] = []

    def worker(idx: int, st: PgStorage, barrier: threading.Barrier, rnd: int, out: dict):
        try:
            barrier.wait()                       # 对齐：两边同时冲进迁移段
            idx_ok = st.ensure_index()
            text = f"并发回归记忆 worker{idx} 轮次{rnd} 标记 {marker} 部署流程由张三负责"
            st_ok = st.store(text, tags="shared")
            out[idx] = (idx_ok, st_ok, st._fragment_key_for(text))  # noqa: SLF001
        except Exception as e:                   # noqa: BLE001 — 失败要留证据
            errors.append(f"round{rnd}/worker{idx}: {type(e).__name__}: {e}")
            out[idx] = (False, False, None)

    t0 = time.time()
    for rnd in range(ROUNDS):
        storage_pg._SCHEMA_READY.clear()          # noqa: SLF001 — 强制走真实迁移段
        out: dict = {}
        barrier = threading.Barrier(2, timeout=60)
        threads = [threading.Thread(target=worker, args=(i, storages[i], barrier, rnd, out))
                   for i in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert not any(t.is_alive() for t in threads), f"round{rnd} 有 worker 卡死未返回"
        per_round.append(out)
    elapsed = time.time() - t0

    for st in storages:
        st.close()

    assert not errors, f"并发轮次出现异常（死锁/超时都会落到这里）: {errors[:5]}"
    for rnd, out in enumerate(per_round):
        assert len(out) == 2, f"round{rnd} 两个 worker 都应有结果，实际 {len(out)}"
        for idx, (idx_ok, st_ok, key) in sorted(out.items()):
            assert idx_ok is True, f"round{rnd} worker{idx} 的 ensure_index 未成功"
            assert st_ok is True, f"round{rnd} worker{idx} 的写入未成功"
            _written_keys.append(key)

    # 两边的写入都必须真的落库（不只是 API 返回 True）
    with warm._ro() as cur:                       # noqa: SLF001 — 真读回来看
        for rnd, out in enumerate(per_round):
            for idx, (_, _, key) in sorted(out.items()):
                cur.execute("SELECT content FROM ks_fragment WHERE key = %s", (key,))
                row = cur.fetchone()
                assert row is not None, f"round{rnd} worker{idx} 写入的碎片查不到：{key}"
                assert marker in row[0]

    deadlock = sum(1 for e in errors if "DeadlockDetected" in e or "deadlock" in e.lower())
    timeout = sum(1 for e in errors if "timeout" in e.lower() or "Canceled" in e)
    both_ok = 1 if all(len(o) == 2 and all(r[1] for r in o.values())
                       for o in per_round) else 0
    print(f"\nCONC_ROUNDS={ROUNDS} CONC_DEADLOCK={deadlock} CONC_TIMEOUT={timeout} "
          f"CONC_BOTH_WRITES_OK={both_ok} CONC_WRITES={len(_written_keys)} "
          f"CONC_ELAPSED_S={elapsed:.1f}")
    assert deadlock == 0
    assert timeout == 0
    assert both_ok == 1
    assert len(_written_keys) == 2 * ROUNDS


# =============================================================================
# ② DDL 收敛：schema 齐备时一条 DDL 都不发
# =============================================================================


def test_ensure_index_issues_no_ddl_when_schema_present(warm):
    """① 首轮缺 GIN 索引 ⇒ 只补那一条；② 列/索引都在 ⇒ 0 条 DDL；③ 同进程二次 ⇒ 0 条 SQL。"""
    # ---- ① 造一个「真缺」：只删 GIN 索引（派生对象，删了不丢任何数据）----
    with warm._tx() as cur:                       # noqa: SLF001
        cur.execute("DROP INDEX IF EXISTS idx_ks_fragment_content_tsv")
    assert _index_exists(warm, "idx_ks_fragment_content_tsv") is False

    first = _RecordingPg(dsn=DSN, connect_timeout=30)
    try:
        storage_pg._SCHEMA_READY.clear()          # noqa: SLF001
        assert first.ensure_index() is True
        first_ddl = _ddl_of(first.sql_log)
        first_sql = len(first.sql_log)
        lock_timeout_rt = first.lock_timeout_seen[-1]
    finally:
        first.close()

    # 首轮**只补缺的那个索引**：1 条 CREATE、0 条 ALTER —— 那个列明明已经在了。
    assert first_ddl == ["CREATE INDEX IF NOT EXISTS idx_ks_fragment_content_tsv "
                         "ON ks_fragment USING GIN (content_tsv)"], first_ddl
    assert not any(s.upper().startswith("ALTER") for s in first_ddl), \
        "content_tsv 列已存在，绝不能再发 ALTER —— 这正是线上死锁的那条语句"
    assert _index_exists(warm, "idx_ks_fragment_content_tsv") is True

    # ---- ② schema 齐备、清掉进程记忆后再调 ⇒ 有查询、**零 DDL** ----
    second = _RecordingPg(dsn=DSN, connect_timeout=30)
    try:
        storage_pg._SCHEMA_READY.clear()          # noqa: SLF001 — 强制真的去核对
        assert second.ensure_index() is True
        second_ddl = _ddl_of(second.sql_log)
        second_sql = len(second.sql_log)
    finally:
        second.close()
    assert second_ddl == [], f"schema 齐备时仍发了 DDL: {second_ddl}"

    # ---- ③ 同进程第二次调用（进程级记忆命中）⇒ **一条 SQL 都不发** ----
    third = _RecordingPg(dsn=DSN, connect_timeout=30)
    try:
        assert third.ensure_index() is True      # 首次：走真实迁移段，写进进程级记忆
        third.sql_log.clear()
        assert third.ensure_index() is True      # 二次：同一目标库 + 同一 dim ⇒ 记忆命中
        third_sql = len(third.sql_log)
    finally:
        third.close()
    assert third_sql == 0, f"同进程第二次调用仍发了 SQL: {third.sql_log}"

    # ---- ④ lock_timeout / advisory lock 的设置代码与运行时取值 ----
    src = inspect.getsource(storage_pg.PgStorage._migrate_schema)
    code_lines = [ln.strip() for ln in src.splitlines()
                  if "lock_timeout" in ln or "advisory" in ln]

    print("\n--- DDL 收敛实测 ---")
    print(f"① 首轮（缺 GIN 索引）DDL {len(first_ddl)} 条 / 总 SQL {first_sql} 条:")
    for s in first_ddl:
        print(f"     {s}")
    print(f"② 列与索引都在、清进程记忆后再调：DDL {len(second_ddl)} 条（应为 0）"
          f" / 总 SQL {second_sql} 条（只读目录核对）")
    print(f"③ 同进程第二次调用：SQL {third_sql} 条（应为 0）")
    print("④ 并发保护设置代码（storage_pg.PgStorage._migrate_schema）：")
    for ln in code_lines:
        print(f"     {ln}")
    print(f"   迁移事务内运行时 SHOW lock_timeout = {lock_timeout_rt}"
          f"（常量 SCHEMA_LOCK_TIMEOUT_MS={storage_pg.SCHEMA_LOCK_TIMEOUT_MS}；"
          f"SET LOCAL 随事务结束还原，不污染后续读写）")
    print(f"   advisory lock key = {storage_pg.SCHEMA_ADVISORY_LOCK_KEY}"
          f"（常量 SCHEMA_ADVISORY_LOCK_KEY，见 test_lock_contention 实测持有数）")
    print(f"   重试上限 SCHEMA_RETRY_ATTEMPTS = {storage_pg.SCHEMA_RETRY_ATTEMPTS}"
          f"，退避基数 {storage_pg.SCHEMA_RETRY_BACKOFF_S}s（0.2/0.4s）")

    print(f"\nDDL_FIRST={len(first_ddl)} DDL_WHEN_EXISTS={len(second_ddl)} "
          f"SECOND_CALL_SQL={third_sql} LOCK_TIMEOUT={lock_timeout_rt} "
          f"ADVISORY_KEY={storage_pg.SCHEMA_ADVISORY_LOCK_KEY}")


def _index_exists(storage, name: str) -> bool:
    with storage._ro() as cur:                    # noqa: SLF001
        cur.execute("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() "
                    "AND indexname = %s", (name,))
        return cur.fetchone() is not None


# =============================================================================
# ③ 负向：拿不到锁 ⇒ 必须**明确失败**，不许静默成功，也不许无限等
# =============================================================================


def test_lock_contention_returns_false_within_bounded_time(warm, monkeypatch):
    """把 lock_timeout 人为压到极小值制造抢锁失败 ⇒ 有限重试后返回 False。"""
    assert storage_pg.SCHEMA_RETRY_ATTEMPTS <= 5, "重试上限必须有限（不许无限重试）"

    blocker = psycopg.connect(DSN)
    blocker.autocommit = True
    try:
        with blocker.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)",
                        (storage_pg.SCHEMA_ADVISORY_LOCK_KEY,))
            # 顺带取证：这把锁确实落在服务端 pg_locks 里（不是「以为加上了」）
            cur.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
            advisory_held = cur.fetchone()[0]
            # 人为把等待时间压到 1ms：另一个连接再来必然抢不到锁
            monkeypatch.setattr(storage_pg, "SCHEMA_LOCK_TIMEOUT_MS", 1)
            storage_pg._SCHEMA_READY.clear()      # noqa: SLF001 — 绕开进程级记忆
            t0 = time.time()
            other = PgStorage(dsn=DSN, connect_timeout=30)
            try:
                got = other.ensure_index()
            finally:
                other.close()
            elapsed = time.time() - t0
            cur.execute("SELECT pg_advisory_unlock(%s)",
                        (storage_pg.SCHEMA_ADVISORY_LOCK_KEY,))
            cur.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
            advisory_after = cur.fetchone()[0]
    finally:
        blocker.close()

    assert advisory_held >= 1, "服务端 pg_locks 里看不到 advisory 锁 —— 串行化没生效"
    assert advisory_after == 0, "解锁后锁还在 —— 会把后来的迁移段永久堵死"
    assert got is False, (
        f"抢不到锁时必须明确返回 False，实际 {got!r} —— "
        "静默成功 = 以为迁移过了，其实 schema 可能根本没建"
    )
    # 有限重试：退避 0.2+0.4s 至少要走完 ⇒ 不可能瞬间返回；又绝不能挂死
    assert elapsed >= 0.55, f"重试退避没走完（{elapsed:.2f}s），重试逻辑可能没生效"
    assert elapsed < 30, f"抢锁失败后挂死 {elapsed:.1f}s —— 必须有硬上限"

    # 记忆**不能**被写进去（否则下一个调用会拿到假的 True）
    assert not storage_pg._SCHEMA_READY, "失败的迁移不得写入进程级记忆"
    print(f"\nLOCK_CONTENTION ok returned={got} elapsed={elapsed:.2f}s "
          f"attempts={storage_pg.SCHEMA_RETRY_ATTEMPTS} "
          f"advisory_locks_held={advisory_held} after_unlock={advisory_after} "
          f"key={storage_pg.SCHEMA_ADVISORY_LOCK_KEY}")
# =============================================================================
# ④ 唯一索引冲突型死锁（ks_pg_b2r3）：行序 = 加锁顺序
# =============================================================================

# 事故原文（主脑复跑抓到，非推测）：
#
#   DeadlockDetected: deadlock detected
#   DETAIL:  Process A waits for ShareLock on transaction 7581; blocked by process B
#            Process B waits for ShareLock on transaction 7580; blocked by process A
#   CONTEXT:  while inserting index tuple (0,86) in relation "ks_hot_topic"
#
# 「ShareLock on transaction」+「插索引元组」= 唯一索引冲突型死锁：两个事务按
# **不同顺序**插入同一批唯一键行，各自握着对方要的那把行锁 ⇒ 成环。
# （PG 侧同时段 0 次 lock timeout ⇒ 这不是锁超时，是真死锁。）

#: 六个热词。真实场景里 provider 与提炼 cron 抽到的就是同一批词，只是顺序不同。
_HOT_WORDS = ("回滚", "灰度", "上线", "监控", "限流", "部署")
_ASC = sorted(_HOT_WORDS)
_DESC = sorted(_HOT_WORDS, reverse=True)

#: 线程号 → 词序。用**线程本地**而不是 monkeypatch 的模块全局：两个 worker 都要
#: 打同一个桩，若各自 setattr 就会互相覆盖（谁后设谁赢，两边拿到的词序相同），
#: 恰好把要复现的「顺序不同」抹掉 —— 那样测试会假绿。
_ORDER_BY_THREAD: dict = {}


def _threaded_keywords(text, max_keywords=5):
    """`extract_keywords` 的确定性替身：按调用线程给出词序。

    为什么打桩而不用真函数：`store()` 内部调 `extract_keywords(text)`，真实顺序
    取决于 jieba 命中次序，**没法**让同一个文本在两个线程里抽到不同顺序。
    而事故的真正变量就是「两个进程拿到同一批词、顺序不同」—— 直接把顺序做成
    变量，比赌 jieba 稳定复现更忠实。
    """
    return list(_ORDER_BY_THREAD[threading.get_ident()])


def test_concurrent_store_with_reversed_input_never_deadlocks(warm, monkeypatch):
    """两个连接并发 `store()`，**一边词序正序、一边倒序**（且由 set 构造），跑 20 轮。

    修复前：两边对 `ks_hot_topic`/`ks_attention`/`ks_hot_topic_seen` 的行序相反
    ⇒ 按不同顺序抢同一批唯一键行锁 ⇒ 成环死锁。
    修复后：`_insert_many` 统一排序 ⇒ 两边发出的行序逐字相同 ⇒ 加锁顺序一致。
    """
    monkeypatch.setattr(storage_pg, "extract_keywords", _threaded_keywords)
    marker = uuid.uuid4().hex[:8]
    orders = [_ASC, _DESC]
    storages = [PgStorage(dsn=DSN, connect_timeout=30) for _ in (0, 1)]
    errors: List[str] = []
    per_round: List[dict] = []
    barrier = threading.Barrier(2, timeout=60)

    def worker(idx: int, st: PgStorage, rnd: int, out: dict):
        # 用 set 构造输入：真实调用方给的就是集合；这里还顺带复现
        # 「不同进程里同一个 set 的哈希迭代顺序不同」这件事本身。
        _ORDER_BY_THREAD[threading.get_ident()] = list(set(orders[idx]))
        try:
            barrier.wait()                       # 对齐：两边同时冲进事务
            # tags 也故意反序：一边的 tags 正序拼，另一边倒序拼
            tag_order = [f"t{orders[idx][i % len(orders[idx])]}" for i in range(3)]
            if idx == 1:
                tag_order.reverse()
            text = f"并发乱序记忆 worker{idx} 轮次{rnd} 标记 {marker} " + " ".join(orders[idx])
            ok = st.store(text, tags=",".join(tag_order))
            out[idx] = (ok, st._fragment_key_for(text))   # noqa: SLF001
        except Exception as e:                   # noqa: BLE001 — 失败要留证据
            errors.append(f"round{rnd}/worker{idx}: {type(e).__name__}: {e}")
            out[idx] = (False, None)
        finally:
            _ORDER_BY_THREAD.pop(threading.get_ident(), None)

    t0 = time.time()
    round_log: List[str] = []
    for rnd in range(ROUNDS):
        err_mark = len(errors)
        out: dict = {}
        barrier.reset()
        threads = [threading.Thread(target=worker, args=(i, storages[i], rnd, out))
                   for i in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        hung = any(t.is_alive() for t in threads)
        # 逐轮留痕：死锁 / 超时 / 写失败 / 卡死，逐项显式记 0 或 1（取证用）
        new_errs = errors[err_mark:]
        dl = sum(1 for e in new_errs if "DeadlockDetected" in e or "deadlock" in e.lower())
        to = sum(1 for e in new_errs if "timeout" in e.lower() or "Canceled" in e)
        wrote = sum(1 for r in out.values() if r[0])
        round_log.append(f"round{rnd:02d} deadlock={dl} timeout={to} hung={int(hung)} "
                         f"writes_ok={wrote}/2")
        assert not hung, f"round{rnd} 有 worker 卡死未返回"
        per_round.append(out)
    elapsed = time.time() - t0

    for st in storages:
        st.close()

    deadlock = sum(1 for e in errors if "DeadlockDetected" in e or "deadlock" in e.lower())
    timeout = sum(1 for e in errors if "timeout" in e.lower() or "Canceled" in e)
    assert not errors, f"并发轮次出现异常（死锁/超时都会落到这里）: {errors[:5]}"
    assert deadlock == 0
    assert timeout == 0

    # 两边的写入都必须真的落库（不只是 API 返回 True）
    with warm._ro() as cur:                       # noqa: SLF001
        for rnd, out in enumerate(per_round):
            assert len(out) == 2
            for idx, (ok, key) in sorted(out.items()):
                assert ok is True, f"round{rnd} worker{idx} 的写入未成功"
                cur.execute("SELECT content FROM ks_fragment WHERE key = %s", (key,))
                row = cur.fetchone()
                assert row is not None, f"round{rnd} worker{idx} 写入的碎片查不到：{key}"
                assert marker in row[0]
                _written_keys.append(key)

    both_ok = 1 if all(len(o) == 2 and all(r[0] for r in o.values())
                       for o in per_round) else 0
    assert both_ok == 1
    print(f"\n--- 逐轮记录（{ROUNDS} 轮 × 2 连接并发 store）---")
    for ln in round_log:
        print("  " + ln)
    print(f"\nCONC_ROUNDS={ROUNDS} CONC_DEADLOCK={deadlock} CONC_TIMEOUT={timeout} "
          f"CONC_BOTH_WRITES_OK={both_ok} CONC_WRITES={len(_written_keys)} "
          f"CONC_ELAPSED_S={elapsed:.1f} CONC_ORDERS=asc/desc")


# =============================================================================
# ⑤ 排序探针：真正发给 PG 的行序 = 排序后的顺序（证明 ① 生效，而非碰巧没撞上）
# =============================================================================


def _emitted_rows(params_log: List[tuple], table: str) -> List[tuple]:
    """从 SQL 探针里还原「发给 PG 的行」。

    `_insert_many` 发的是一条 `INSERT INTO <表> (...) VALUES (%s,...),(%s,...)`，
    **行序全部体现在参数里**（SQL 文本只是一串 %s），所以只能从参数还原；
    这也正是这条探针存在的原因：只看 SQL 文本看不出顺序，也证明不了排序生效。
    """
    out: List[tuple] = []
    for sql, params in params_log:
        head = sql.upper()
        if not head.startswith(f"INSERT INTO {table.upper()} "):
            continue
        values_sql = sql[head.index("VALUES") + len("VALUES"):]
        ncols = values_sql[values_sql.index("("):].split(")")[0].count("%s")
        out.extend(tuple(params[i:i + ncols]) for i in range(0, len(params), ncols))
    return out


def _key_cols(rows: List[tuple]) -> List[tuple]:
    """只取唯一键列（前两列 scope/topic）—— 加锁顺序只由唯一键决定。"""
    return [(r[0], r[1]) for r in rows]


def test_multirow_insert_sorts_rows_before_sending(warm, monkeypatch):
    """同一批 key，三种输入序（正序/倒序/set）⇒ **发出序必须逐字相同且有序**。"""
    monkeypatch.setattr(storage_pg, "extract_keywords", _threaded_keywords)
    rec = _RecordingPg(dsn=DSN, connect_timeout=30)
    emitted: List[List[tuple]] = []
    inputs: List[List[str]] = []
    try:
        for words in (_ASC, _DESC, list(set(_HOT_WORDS))):
            _ORDER_BY_THREAD[threading.get_ident()] = list(words)
            rec.params_log.clear()
            text = f"排序探针 {' '.join(words)} {uuid.uuid4().hex[:8]}"
            assert rec.store(text) is True
            key = rec._fragment_key_for(text)      # noqa: SLF001
            _written_keys.append(key)
            inputs.append(list(words))
            emitted.append(_emitted_rows(rec.params_log, "ks_hot_topic"))
    finally:
        _ORDER_BY_THREAD.pop(threading.get_ident(), None)
        rec.close()

    asc_rows, desc_rows, set_rows = emitted
    # ---- 前提：三种输入确实不是同一个序（否则断言就是空转）----
    assert inputs[0] != inputs[1] and inputs[2] != inputs[0], f"输入序没区分开：{inputs}"
    for label, rows in (("asc", asc_rows), ("desc", desc_rows), ("set", set_rows)):
        assert len(rows) == len(_ASC) * len(storage_pg._TOPIC_SCOPES), (label, len(rows))
        # (scope, topic) 是该表唯一键 ⇒ 发出行序必须按它升序
        keyed = _key_cols(rows)
        assert keyed == sorted(keyed), f"{label} 输入发出的行序未排序：{keyed}"
    # 三种输入发出**完全相同的唯一键序** ⇒ 加锁顺序与调用方给的顺序彻底解耦。
    # （只比唯一键列：score/expire_ts 是每次调用现算的时间戳，本来就各不相同。）
    assert _key_cols(desc_rows) == _key_cols(asc_rows), \
        "倒序输入发出的行序与正序输入不同 ⇒ 排序没生效"
    assert _key_cols(set_rows) == _key_cols(asc_rows), \
        "set 输入发出的行序与正序输入不同 ⇒ 排序没生效"

    print("\n--- 排序探针（同一批热词，三种输入序）---")
    print(f"输入序 asc  : {inputs[0]}")
    print(f"输入序 desc : {inputs[1]}")
    print(f"输入序 set  : {inputs[2]}")
    print(f"发出序(三者逐字相同) : {_key_cols(asc_rows)}")
    src = inspect.getsource(storage_pg.PgStorage._insert_many)
    sort_lines = [ln.strip() for ln in src.splitlines() if "sorted(rows)" in ln]
    print(f"排序单点    : storage_pg.PgStorage._insert_many → {sort_lines}")
    assert len(sort_lines) == 1, "排序必须只有一处（多行写的唯一出口）"


# =============================================================================
# ⑥ 第二道防线：有界死锁重试（单点、必须重跑整个事务、超限明确失败）
# =============================================================================


class _FlakyPg(PgStorage):
    """在多行写那一步人为抛 `DeadlockDetected`，模拟真死锁的「事务已被 abort」。

    为什么注入在 `_insert_many` 之前而不是事务中间：真死锁一旦发生，服务端已经
    把事务 abort 了，客户端**没有任何半截状态可以续** —— 这正是
    `with_deadlock_retry` docstring 里「只能整体重跑」那个论断的由来。
    `tx_count` 数的是**整个事务**的进入次数，重试一次就必须 +1。
    """

    def __init__(self, *a, fail_times: int = 1, **kw):
        super().__init__(*a, **kw)
        self.fail_times = fail_times
        self.tx_count = 0
        self.insert_attempts: List[str] = []

    @contextmanager
    def _tx(self):
        self.tx_count += 1
        with super()._tx() as cur:
            yield cur

    def _insert_many(self, cur, table, columns, rows, on_conflict):
        self.insert_attempts.append(table)
        if len(self.insert_attempts) <= self.fail_times:
            raise psycopg.errors.DeadlockDetected(
                "injected: while inserting index tuple in relation ks_hot_topic")
        super()._insert_many(cur, table, columns, rows, on_conflict)


def test_deadlock_retry_reruns_the_whole_transaction(warm):
    """第一次撞死锁 ⇒ 重跑**整个**事务 ⇒ 第二次成功，数据完整落库。"""
    st = _FlakyPg(dsn=DSN, connect_timeout=30, fail_times=1)
    text = f"重试回归 {uuid.uuid4().hex[:8]} " + " ".join(_ASC)
    key = st._fragment_key_for(text)             # noqa: SLF001
    try:
        assert st.store(text) is True
    finally:
        st.close()
    _written_keys.append(key)

    assert st.tx_count == 2, (
        f"期望整个事务被重跑一次（tx_count=2），实际 {st.tx_count} —— "
        "只续跑半截事务 = 拿一个已 abort 的事务继续写，必丢数据")
    # 真的落库了（不只是返回 True）
    with warm._ro() as cur:                       # noqa: SLF001
        cur.execute("SELECT content FROM ks_fragment WHERE key = %s", (key,))
        row = cur.fetchone()
    assert row is not None, "重试成功后数据没落库"
    assert text in row[0]
    print(f"\nRETRY_RUN attempts={st.tx_count} insert_attempts={st.insert_attempts} "
          f"最终落库=ok（上界 {storage_pg.DEADLOCK_RETRY_ATTEMPTS}）")


def test_deadlock_retry_is_bounded_and_fails_loudly(warm):
    """一直撞死锁 ⇒ 跑满上限（3 次事务）后**原样抛出**，既不无限重试也不静默成功。"""
    assert storage_pg.DEADLOCK_RETRY_ATTEMPTS == 3, "重试上限必须是有界的 3 次"
    st = _FlakyPg(dsn=DSN, connect_timeout=30, fail_times=99)
    text = f"重试上限 {uuid.uuid4().hex[:8]} " + " ".join(_ASC)
    key = st._fragment_key_for(text)             # noqa: SLF001
    t0 = time.time()
    try:
        with pytest.raises(psycopg.errors.DeadlockDetected):
            st.store(text)
    finally:
        st.close()
    elapsed = time.time() - t0

    assert st.tx_count == storage_pg.DEADLOCK_RETRY_ATTEMPTS, \
        f"期望上限 {storage_pg.DEADLOCK_RETRY_ATTEMPTS} 次事务，实际 {st.tx_count}"
    # 退避确实走了（0.2 与 0.4 各带 0.5~1.5 倍抖动 ⇒ 合计 ∈ [0.3, 0.9]s）
    assert 0.25 <= elapsed < 15, f"退避序列不对或挂死：{elapsed:.2f}s"
    # 超限不许静默成功：库里绝不能有这条碎片
    with warm._ro() as cur:                       # noqa: SLF001
        cur.execute("SELECT 1 FROM ks_fragment WHERE key = %s", (key,))
        assert cur.fetchone() is None, "超限后却静默写入了 —— 吞异常等于假装成功"

    # 单点证明：全模块的 `for attempt in range(1,` 只有两处 —— 迁移段 + 写事务重试
    loops = [ln.strip() for ln in inspect.getsource(storage_pg).splitlines()
             if "for attempt in range(1," in ln]
    assert len(loops) == 2, f"重试循环应恰好 2 处（迁移段 + 写事务重试），实际 {len(loops)}"
    print(f"\nRETRY_LIMIT attempts={st.tx_count} elapsed={elapsed:.2f}s "
          f"attempts_max={storage_pg.DEADLOCK_RETRY_ATTEMPTS} "
          f"backoff={storage_pg.DEADLOCK_RETRY_BACKOFF_S} "
          f"single_place={storage_pg.with_deadlock_retry.__name__}")
