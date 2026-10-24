"""待办（`ks_todo`）存储层测试 —— 三后端一份语义（2026-10 ks_todo）。

覆盖任务书判据：

1. **状态三态**（open / done / obsolete）：改状态、非法态被拒、done 记完成时刻；
2. **权限矩阵**：`can_access` 口径 = owner ∪ creator，primary 全量；
3. **可见性**：非 primary 只见 owner=自己 ∪ creator=自己（被指派者必见自己的活）；
4. **到期口径单点**：`due_state` = none / soon / overdue（基准日 = UTC 今天）；
5. **三后端一致**：PG / SQLite / Redis **同一份断言**逐个跑；
6. **老数据兼容**：缺字段的行不炸，缺省值回落。

全程 hermetic：SQLite 用 `tmp_path` 临时库、Redis 用内存假 client、
PG 只连白名单里的测试库（`KEEPSAKE_PG_TEST_HOST（缺省 postgres.test.example）/keepsake_test`）且用例自带清理。
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from keepsake.govern.todo import (
    DEFAULT_LIMIT,
    DUE_SOON_DAYS,
    OBSOLETE_SUGGEST_DAYS,
    SCOPE_ALL,
    SCOPE_DONE,
    SCOPE_OPEN,
    STATUS_DONE,
    STATUS_OBSOLETE,
    STATUS_OPEN,
    TABLE,
    TodoError,
    TodoStore,
    can_access,
    due_state,
    overdue_days,
    parse_due,
    visible_rows,
)


# --------------------------------------------------------------------------
# 夹具：SQLite 真库 / Redis 假 client / PG 真库
# --------------------------------------------------------------------------

@pytest.fixture()
def lite(tmp_path):
    """真 SqliteStorage（临时文件）+ 待办存储。"""
    from keepsake.core.sqlite import SqliteStorage

    st = SqliteStorage(path=str(tmp_path / "todo.db"))
    assert st.ensure_index() is True
    store = TodoStore(st)
    assert store.ensure_schema() is True
    yield store, st
    st.close()


class FakeRedisClient:
    """内存假 Redis：hash + list + 计数器（键值存 bytes，模拟 decode_responses=False）。"""

    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, bytes]] = {}
        self.lists: Dict[str, List[bytes]] = {}
        self.counters: Dict[str, int] = {}

    @staticmethod
    def _key(key: Any) -> str:
        return key.decode() if isinstance(key, (bytes, bytearray)) else str(key)

    @staticmethod
    def _val(value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        return str(value).encode()

    def hgetall(self, key):
        return dict(self.hashes.get(self._key(key), {}))

    def hset(self, key, field=None, value=None, mapping=None):
        slot = self.hashes.setdefault(self._key(key), {})
        pairs = dict(mapping or {})
        if field is not None:
            pairs[field] = value
        for k, v in pairs.items():
            slot[str(k)] = self._val(v)

    def rpush(self, key, *values):
        slot = self.lists.setdefault(self._key(key), [])
        for v in values:
            slot.append(self._val(v))
        return len(slot)

    def lrange(self, key, start, stop):
        """与真 Redis 同款：负数下标**相对末尾**，越界夹到边界内。"""
        slot = list(self.lists.get(self._key(key), []))
        n = len(slot)
        if n == 0:
            return []
        start = n + start if start < 0 else start
        stop = n + stop if stop < 0 else stop
        start = max(0, min(start, n))
        stop = max(-1, min(stop, n - 1))
        if stop < start:
            return []
        return slot[start:stop + 1]

    def lrem(self, key, count, value):
        slot = self.lists.get(self._key(key))
        if not slot:
            return 0
        target = self._val(value)
        removed = 0
        keep: List[bytes] = []
        for item in slot:
            if item == target and (count == 0 or removed < abs(count)):
                removed += 1
                continue
            keep.append(item)
        self.lists[self._key(key)] = keep
        return removed

    def delete(self, key):
        k = self._key(key)
        existed = k in self.hashes
        self.hashes.pop(k, None)
        return 1 if existed else 0

    def incr(self, key):
        k = self._key(key)
        self.counters[k] = self.counters.get(k, 0) + 1
        return self.counters[k]


class FakeRedisStorage:
    """Redis 后端替身（只有 `_get_client`，正是 `_backend_of` 认 Redis 的判据）。"""

    def __init__(self) -> None:
        self.client = FakeRedisClient()

    def _get_client(self):
        return self.client


@pytest.fixture()
def red():
    storage = FakeRedisStorage()
    store = TodoStore(storage)
    assert store.ensure_schema() is True
    yield store, storage


# --------------------------------------------------------------------------
# ① 三后端闭环（同一份断言，逐个后端跑）
# --------------------------------------------------------------------------

def _closed_loop(store: TodoStore, owner: str = "worker-a") -> None:
    """加一条 → 读 → 改状态 → 计数 → 删 —— 三后端逐字段同款。

    🔴 计数与列表**按 owner 收窄**：PG 真库是**共享库**（别的用例也在里面写），
    不按 owner 收窄的话，计数会被别人的行污染。
    """
    tid = store.add("把回滚手册补齐", "缺第 3 章", owner=owner, creator="worker-b",
                    due="2026-12-31")
    assert isinstance(tid, int) and tid > 0

    row = store.get(tid)
    assert row is not None
    assert row["title"] == "把回滚手册补齐"
    assert row["detail"] == "缺第 3 章"
    assert row["owner"] == owner and row["creator"] == "worker-b"
    assert row["status"] == STATUS_OPEN
    assert row["due"] == "2026-12-31"
    assert row["created"] and row["updated"]
    assert row["done_at"] == ""
    assert row["due_state"] in ("none", "soon", "overdue")

    # 状态机：open → done（记完成时刻）→ obsolete
    assert store.set_status(tid, STATUS_DONE) is True
    done_row = store.get(tid)
    assert done_row["status"] == STATUS_DONE
    assert done_row["done_at"], "标完成必须记完成时刻"
    assert store.set_status(tid, STATUS_OBSOLETE) is True
    assert store.get(tid)["status"] == STATUS_OBSOLETE

    # 计数（按 owner 收窄：PG 是共享库）
    summary = store.summary(owner=owner)
    assert summary["total"] == 1
    assert summary["obsolete"] == 1
    assert summary["open"] == 0 and summary["done"] == 0

    # 列表（按状态筛）
    rows, total = store.list_rows(owner=owner, status=STATUS_OBSOLETE)
    assert total == 1 and len(rows) == 1
    rows, total = store.list_rows(owner=owner, status=STATUS_OPEN)
    assert total == 0 and rows == []

    # 删
    assert store.delete(tid) is True
    assert store.get(tid) is None
    assert store.delete(tid) is False


def test_sqlite_闭环(lite):
    store, _ = lite
    _closed_loop(store)


def test_redis_闭环(red):
    store, _ = red
    _closed_loop(store)


def test_sqlite与redis返回形状逐字一致(lite, red):
    """🔴 三后端「一份语义」的硬判据：同一串操作，两边返回的**键集合**一致。"""
    store_sql, _ = lite
    store_red, _ = red
    for store in (store_sql, store_red):
        store.add("同一条", "正文", owner="a", creator="a", due="")
    row_sql = store_sql.get(1)
    row_red = store_red.get(1)
    assert set(row_sql) == set(row_red)
    for key in ("title", "detail", "owner", "creator", "status", "due",
                "created", "updated", "done_at", "due_state"):
        assert type(row_sql[key]) is type(row_red[key]), key


# --------------------------------------------------------------------------
# ② 状态三态与输入校验
# --------------------------------------------------------------------------

def test_状态三态_非法态被拒(lite):
    """🔴 状态只有三个 —— 库里不该出现第四态（DDL 里的 CHECK 是最后一道兜底）。"""
    store, _ = lite
    tid = store.add("一件事", owner="a", creator="a")
    with pytest.raises(TodoError) as exc:
        store.set_status(tid, "半吊子")
    assert exc.value.code == "invalid_status"
    assert store.get(tid)["status"] == STATUS_OPEN, "被拒的改状态不许落库"


def test_标题空与超长被拒(lite):
    store, _ = lite
    with pytest.raises(TodoError):
        store.add("", owner="a", creator="a")
    with pytest.raises(TodoError):
        store.add("x" * 201, owner="a", creator="a")
    with pytest.raises(TodoError):
        store.add("标题", detail="y" * 4001, owner="a", creator="a")


def test_到期日非法被拒_不猜(lite):
    """🔴 猜错一个到期日 = 有人漏了事却没人知道 ⇒ 非法日期直接报错。"""
    store, _ = lite
    with pytest.raises(TodoError) as exc:
        store.add("一件事", owner="a", creator="a", due="下周三")
    assert exc.value.code == "invalid_due"


def test_到期日归一(lite):
    store, _ = lite
    tid = store.add("一件事", owner="a", creator="a", due="2026-11-03T08:30:00+00:00")
    assert store.get(tid)["due"] == "2026-11-03"


# --------------------------------------------------------------------------
# ③ 到期口径单点
# --------------------------------------------------------------------------

def test_due_state_三档():
    """🔴 **唯一**的到期口径：none / soon / overdue。"""
    today = date(2026, 10, 22)
    assert due_state("", today) == "none"
    assert due_state(None, today) == "none"
    assert due_state("2026-10-22", today) == "soon", "今天到期今天还来得及"
    assert due_state("2026-10-25", today) == "soon", f"3 天内（{DUE_SOON_DAYS} 天）算临期"
    assert due_state("2026-10-26", today) == "none", "第 4 天不算临期"
    assert due_state("2026-10-21", today) == "overdue"
    assert due_state("不是日期", today) == "none", "判不出日期就不编一个"


def test_overdue_days():
    today = date(2026, 10, 22)
    assert overdue_days("2026-10-15", today) == 7
    assert overdue_days("2026-10-22", today) == 0
    assert overdue_days("", today) == 0


def test_过期计数只算开放的(lite):
    """🔴 销掉的待办不算「过期」—— 过期是活儿没干完。"""
    store, _ = lite
    today = datetime.now(timezone.utc).date()
    old = (today - timedelta(days=10)).isoformat()
    tid = store.add("欠很久的一件", owner="a", creator="a", due=old)
    assert store.summary()["overdue"] == 1
    store.set_status(tid, STATUS_DONE)
    assert store.summary()["overdue"] == 0
    assert store.summary()["done"] == 1


def test_建议废弃_过期满七天(lite):
    """🔴 「建议废弃」只提名：过期满 N 天还开着的才列出来。"""
    store, _ = lite
    today = datetime.now(timezone.utc).date()
    old = (today - timedelta(days=OBSOLETE_SUGGEST_DAYS)).isoformat()
    fresh = (today - timedelta(days=2)).isoformat()
    store.add("欠很久", owner="a", creator="a", due=old)
    store.add("才欠两天", owner="a", creator="a", due=fresh)
    suggestions = store.obsolete_suggestions()
    assert len(suggestions) == 1
    assert suggestions[0]["title"] == "欠很久"
    assert suggestions[0]["overdue_days"] == OBSOLETE_SUGGEST_DAYS
    # 标完成 ⇒ 不再提名（**标对那一条**：列表按 id 倒序，别拿错行）
    rows, _ = store.list_rows(status=STATUS_OPEN)
    target = next(r for r in rows if r["title"] == "欠很久")
    store.set_status(target["id"], STATUS_DONE)
    assert store.obsolete_suggestions() == []


# --------------------------------------------------------------------------
# ④ 权限矩阵与可见性
# --------------------------------------------------------------------------

def test_可见性_非primary只看owner或creator是自己的():
    """🔴 判据在 `can_access` 一处：owner ∪ creator，primary 全量。"""
    row = {"owner": "worker-a", "creator": "worker-b"}
    assert can_access(row, "worker-a", False) is True, "被指派者必见自己的活"
    assert can_access(row, "worker-b", False) is True, "记录人看得见自己记的"
    assert can_access(row, "worker-c", False) is False
    assert can_access(row, "worker-c", True) is True, "primary 全量"
    assert can_access(None, "worker-a", False) is False


def test_可见性过滤():
    rows = [
        {"id": 1, "owner": "a", "creator": "b"},
        {"id": 2, "owner": "c", "creator": "c"},
        {"id": 3, "owner": "a", "creator": "a"},
    ]
    assert [r["id"] for r in visible_rows(rows, "a", False)] == [1, 3]
    assert [r["id"] for r in visible_rows(rows, "a", True)] == [1, 2, 3]


def test_列表按可见人筛(lite):
    """🔴 非 primary 的可见范围在**存储查询**里就收窄（owner ∪ creator）。"""
    store, _ = lite
    store.add("派给我的", owner="worker-a", creator="manager")
    store.add("我记的", owner="manager", creator="worker-a")
    store.add("别人的", owner="worker-c", creator="worker-c")
    rows, total = store.list_rows(visible_to="worker-a")
    assert total == 2
    assert {r["title"] for r in rows} == {"派给我的", "我记的"}
    rows, total = store.list_rows()
    assert total == 3


def test_按owner筛与分页(lite):
    store, _ = lite
    for i in range(5):
        store.add(f"第 {i} 条", owner="a" if i % 2 else "b", creator="a")
    rows, total = store.list_rows(owner="a")
    assert total == 2
    rows, total = store.list_rows(owner="a", page=1, size=1)
    assert total == 2 and len(rows) == 1
    rows, total = store.list_rows(owner="a", page=2, size=1)
    assert total == 2 and len(rows) == 1
    rows, total = store.list_rows(owner="b")
    assert total == 3


def test_scope快捷档(lite):
    store, _ = lite
    tid = store.add("一条", owner="a", creator="a")
    store.set_status(tid, STATUS_DONE)
    rows, _ = store.list_rows(scope=SCOPE_OPEN)
    assert rows == []
    rows, _ = store.list_rows(scope=SCOPE_DONE)
    assert len(rows) == 1
    rows, _ = store.list_rows(scope=SCOPE_ALL)
    assert len(rows) == 1


def test_非法scope被拒(lite):
    store, _ = lite
    with pytest.raises(TodoError) as exc:
        store.list_rows(scope="随便")
    assert exc.value.code == "invalid_scope"


# --------------------------------------------------------------------------
# ⑤ 老数据兼容 + DDL 单点
# --------------------------------------------------------------------------

def test_DDL唯一定义处_两后端列名同构():
    """🔴 PG 与 SQLite 的 DDL 各一份，但**列名与语义必须同构**（漂了就静默少一列）。"""
    from keepsake.govern.todo import KS_TODO_DDL_PG, KS_TODO_DDL_SQLITE

    def _cols(ddl: str) -> set:
        body = ddl.split("(", 1)[1].rsplit(")", 1)[0]
        out = set()
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("CONSTRAINT"):
                continue
            out.add(line.split()[0])
        return out

    assert _cols(KS_TODO_DDL_PG) == _cols(KS_TODO_DDL_SQLITE)
    assert {"id", "title", "detail", "owner", "creator", "status", "due",
            "created", "updated", "done_at"} <= _cols(KS_TODO_DDL_PG)


def test_老行缺字段不炸(lite):
    """🔴 老数据兼容：缺列的行读出来是缺省值，不抛。"""
    store, st = lite
    # 手工插一行「老数据」：只给 id/title/owner/creator/status
    st._write()  # noqa: SLF001 —— 先确保连接可用
    with st._write() as cur:   # noqa: SLF001
        cur.execute(
            f"INSERT INTO {TABLE} (title, owner, creator, status) VALUES (?,?,?,?)",
            ("老条目", "a", "a", "open"))
    rows, total = store.list_rows()
    assert total == 1
    row = rows[0]
    assert row["detail"] == "" and row["due"] == ""
    assert row["done_at"] == "" and row["due_state"] == "none"


# --------------------------------------------------------------------------
# ⑥ PG 真库（三后端一致的最后一块）
# --------------------------------------------------------------------------

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

PG_ONLY = pytest.mark.skipif(not _pg_ready,
                             reason=f"无 {ALLOWED_HOST}/{ALLOWED_DBNAME} 凭据，跳过 PG 真库")


@pytest.fixture(scope="module")
def pg_store():
    """PG 真库上的待办存储（**只连白名单里的测试库**）；用例用唯一 owner 隔离。"""
    from keepsake.core.pg import PgStorage

    storage = PgStorage(dsn=DSN, connect_timeout=30)
    store = TodoStore(storage)
    assert store.ensure_schema() is True
    yield store
    try:
        storage.close()
    except Exception:       # noqa: BLE001 —— 收尾不该让用例红
        pass


@PG_ONLY
def test_pg_闭环与三后端一致(pg_store):
    """🔴 PG 侧与 SQLite / Redis **同一份断言**（三后端一份语义）。"""
    owner = f"todo-test-{uuid.uuid4().hex[:8]}"
    try:
        _closed_loop(pg_store, owner)
        # 隔离性：这个 owner 名下**一条都不该有**（闭环里那条已删）
        rows, total = pg_store.list_rows(owner=owner)
        assert total == 0
    finally:
        # 清理：只删本次 owner 名下的行（那是别人的库，整表 TRUNCATE 不许干）
        rows, _ = pg_store.list_rows(owner=owner)
        for row in rows:
            pg_store.delete(int(row["id"]))
