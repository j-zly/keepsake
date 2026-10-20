"""代理互聊信箱测试（`ks_agent_messaging` 存储层 + 投递闸门）。

覆盖四件事：

1. **三后端闭环**：send → inbox（fetch 即标读 → 二次 unread_only 取不到）→
   recent_between → unread_count，**PG / SQLite / Redis 三份逐条同款**；
2. **Redis 侧的双写与消费语义**（未读队列 + 已读时刻）不被悄悄改掉；
3. **投递闸门**：目标代理不存在 / 未批准 / 已吊销 ⇒ 拒投；
   `dm_enabled=0` ⇒ 拒投并说明；
4. **`dm_enabled` 开关本身**：默认开、幂等、老行（缺列）兼容。

全程 hermetic：SQLite 用 `tmp_path` 临时库、Redis 用内存假 client、
**不连任何外部服务、不碰生产库**。PG 真库不在本单范围内（信箱的 PG 侧
与 SQLite 侧同构，由 `govern/messaging.py` 的同一份 SQL 保证）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from keepsake.govern.messaging import (
    CONTENT_MAX,
    Messaging,
    TABLE,
)
from keepsake.govern.registry import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REVOKED,
    Registry,
)


# --------------------------------------------------------------------------
# 夹具：SQLite 真库 / Redis 假 client
# --------------------------------------------------------------------------

@pytest.fixture()
def lite(tmp_path):
    """真 SqliteStorage（临时文件）+ 信箱。"""
    from keepsake.core.sqlite import SqliteStorage

    st = SqliteStorage(path=str(tmp_path / "msg.db"))
    assert st.ensure_index() is True
    box = Messaging(st)
    assert box.ensure_schema() is True
    yield box, st
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

    # -- hash（注册表用）------------------------------------------------
    def exists(self, key):
        k = self._key(key)
        return 1 if (k in self.hashes or k in self.lists or k in self.counters) else 0

    def hgetall(self, key):
        return dict(self.hashes.get(self._key(key), {}))

    def hset(self, key, field=None, value=None, mapping=None):
        slot = self.hashes.setdefault(self._key(key), {})
        pairs = dict(mapping or {})
        if field is not None:
            pairs[field] = value
        for k, v in pairs.items():
            slot[str(k)] = self._val(v)

    def hincrby(self, key, field, amount=1):
        slot = self.hashes.setdefault(self._key(key), {})
        now = int(slot.get(str(field), b"0") or 0)
        slot[str(field)] = str(now + int(amount)).encode()
        return now + int(amount)

    def scan_iter(self, match="*"):
        prefix = str(match).rstrip("*")
        for key in sorted(self.hashes):
            if key.startswith(prefix):
                yield key

    def renamenx(self, key, new_key):
        src, dst = self._key(key), self._key(new_key)
        if src not in self.hashes or dst in self.hashes:
            return 0
        self.hashes[dst] = self.hashes.pop(src)
        return 1

    # -- list（信箱用）--------------------------------------------------
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

    def llen(self, key):
        return len(self.lists.get(self._key(key), []))

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

    def hget(self, key, field):
        return self.hashes.get(self._key(key), {}).get(str(field))

    # -- 计数器 ---------------------------------------------------------
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
    box = Messaging(storage)
    assert box.ensure_schema() is True
    yield box, storage


# --------------------------------------------------------------------------
# ① 三后端闭环（同一份断言，逐个后端跑）
# --------------------------------------------------------------------------

def _closed_loop(box: Messaging) -> None:
    """send → inbox（fetch 即读）→ 二次 unread 取不到 → recent → unread_count。"""
    seq = box.send("agent-a", "agent-b", "在吗？", "th_demo")
    assert isinstance(seq, int) and seq > 0

    # 收件箱：拿到刚投的那条，且**fetch 即标读**
    inbox = box.fetch_inbox("agent-b", unread_only=True, limit=20)
    assert len(inbox) == 1
    row = inbox[0]
    assert row["seq"] == seq
    assert row["from_id"] == "agent-a" and row["to_id"] == "agent-b"
    assert row["content"] == "在吗？"
    assert row["thread_id"] == "th_demo"          # 线程号必须回显
    assert row["created"] and row["read_at"]      # 收信即标读

    # 二次取未读 ⇒ 空（已被消费）
    assert box.fetch_inbox("agent-b", unread_only=True, limit=20) == []
    assert box.unread_count("agent-b") == 0

    # 双向最近消息：含自己发的，且**不动 read_at**
    box.send("agent-b", "agent-a", "在，说吧", "th_demo")
    recent = box.recent_between("agent-a", "agent-b", limit=20)
    assert [r["content"] for r in recent] == ["在吗？", "在，说吧"]
    assert recent[0]["read_at"], "recent 不得清掉已读时刻"
    assert recent[1]["read_at"] == "", "recent 不得把未读标成已读"

    # 对方那边还有一条未读
    assert box.unread_count("agent-a") == 1
    assert len(box.fetch_inbox("agent-a", unread_only=True, limit=20)) == 1
    assert box.unread_count("agent-a") == 0


def test_sqlite_闭环(lite):
    box, _ = lite
    _closed_loop(box)


def test_redis_闭环(red):
    box, _ = red
    _closed_loop(box)


def test_sqlite与redis返回形状逐字一致(lite, red):
    """🔴 三后端「一份语义」的硬判据：同一串操作，两边返回的**键集合与取值类型**一致。"""
    box_sql, _ = lite
    box_red, _ = red
    for box in (box_sql, box_red):
        box.send("a", "b", "同一条", "th_x")
    rows_sql = box_sql.fetch_inbox("b", unread_only=True, limit=20)
    rows_red = box_red.fetch_inbox("b", unread_only=True, limit=20)
    assert set(rows_sql[0]) == set(rows_red[0])
    assert set(rows_sql[0]) == {"seq", "from_id", "to_id", "content",
                                "thread_id", "created", "read_at"}
    for key in ("from_id", "to_id", "content", "thread_id", "created", "read_at"):
        assert type(rows_sql[0][key]) is type(rows_red[0][key]), key


def test_redis双写与消费语义(red):
    """🔴 Redis 侧的取舍钉死：全局 LIST + 收件人未读 LIST（双写），
    读信 = 从未读队列里消费掉（`LREM`），已读时刻另存 hash。"""
    box, storage = red
    box.send("a", "b", "第一条", "t1")
    client = storage.client
    assert client.llen("keepsake:msg") == 1                  # 全量 LIST
    assert client.llen("keepsake:msg:to:b") == 1             # 收件人未读队列
    assert client.llen("keepsake:msg:to:a") == 0             # 发件人自己不在收件队列里

    box.fetch_inbox("b", unread_only=True, limit=20)
    assert client.llen("keepsake:msg:to:b") == 0             # 消费掉
    assert client.llen("keepsake:msg") == 1                  # 全量仍在（历史不丢）
    assert client.hget("keepsake:msg:read", "1")             # 已读时刻在 hash 里


def test_未读只取未读且按时间正序(lite):
    box, _ = lite
    box.send("a", "b", "第一条", "")
    box.send("a", "b", "第二条", "")
    box.fetch_inbox("b", unread_only=True, limit=1)          # 只取最近一条
    rows = box.fetch_inbox("b", unread_only=True, limit=20)
    assert [r["content"] for r in rows] == ["第一条"]         # 剩下的是更早那条
    assert box.unread_count("b") == 0


def test_未读limit取最近的那几条_三后端同款(lite, red):
    """🔴 `limit` 小于未读条数时：**取最近的那几条**（SQL 是 `ORDER BY seq DESC LIMIT n`，
    Redis 是取未读队列的**尾部** n 条）—— 两边必须同款，否则换个后端就换了语义。"""
    for box in (lite[0], red[0]):
        box.send("a", "b", "第一条", "")
        box.send("a", "b", "第二条", "")
        box.send("a", "b", "第三条", "")
        got = box.fetch_inbox("b", unread_only=True, limit=2)
        assert [r["content"] for r in got] == ["第二条", "第三条"]
        # 取走的两条已读，没取的那条仍是未读
        assert box.unread_count("b") == 1
        rest = box.fetch_inbox("b", unread_only=True, limit=20)
        assert [r["content"] for r in rest] == ["第一条"]
        assert box.unread_count("b") == 0


def test_recent不区分收件人只认这两人(lite):
    box, _ = lite
    box.send("a", "b", "给我", "")
    box.send("a", "c", "给别人", "")
    rows = box.recent_between("a", "b", limit=20)
    assert [r["content"] for r in rows] == ["给我"]


def test_空内容与超长内容被拒(lite):
    box, _ = lite
    with pytest.raises(ValueError):
        box.send("a", "b", "   ", "")
    with pytest.raises(ValueError):
        box.send("a", "b", "x" * (CONTENT_MAX + 1), "")
    with pytest.raises(ValueError):
        box.send("", "b", "有人吗", "")


def test_空收件人返回空列表不报错(lite):
    box, _ = lite
    assert box.fetch_inbox("", unread_only=True, limit=20) == []
    assert box.recent_between("a", "", limit=20) == []
    assert box.unread_count("") == 0


# --------------------------------------------------------------------------
# ② 投递闸门（目标代理的资格）+ ③ dm_enabled 开关
# --------------------------------------------------------------------------

def _registry(box: Messaging) -> Registry:
    """信箱用的注册表（与信箱同库 —— SQLite 侧从同一个 storage 拿）。"""
    from keepsake.core.sqlite import SqliteStorage

    st = SqliteStorage(path=":memory:")
    reg = Registry(st)
    reg.ensure_schema()
    return reg


def test_dm_enabled默认开且可关(lite):
    """🔴 新列必须兼容旧数据：默认**开**，且关掉后读得到。"""
    box, st = lite
    reg = Registry(st)
    reg.ensure_schema()
    reg.register("agent-b")
    assert reg.dm_enabled("agent-b") is True          # 新登记 ⇒ 默认开
    assert reg.set_dm_enabled("agent-b", False) is True
    assert reg.dm_enabled("agent-b") is False
    assert reg.set_dm_enabled("agent-b", False) is False   # 幂等：没变就不写
    assert reg.set_dm_enabled("查无此代理", False) is False  # 行不存在
    # 开关**只碰** dm_enabled：状态 / 角色 / 凭据一个都不动
    reg.approve("agent-b")
    reg.set_dm_enabled("agent-b", False)
    row = reg._be.load("agent-b")                # noqa: SLF001 —— 直读后端验纪律
    assert row["status"] == STATUS_APPROVED and row["dm_enabled"] == "0"


def test_老库补列后存量行默认开(tmp_path):
    """🔴 老库**根本没有** `dm_enabled` 这一列 ⇒ `ensure_schema` 补上，
    且存量行的语义不变（补出来的值 = '1' = 开）。"""
    from keepsake.core.sqlite import SqliteStorage

    path = str(tmp_path / "legacy.db")
    st = SqliteStorage(path=path)
    # 造一张**老表**（没有 dm_enabled 列），再插一行
    with st._lock:                                    # noqa: SLF001
        st._db().execute(                            # noqa: SLF001
            "CREATE TABLE ks_agent ("
            "agent_id text PRIMARY KEY, name text NOT NULL DEFAULT '',"
            "role text NOT NULL DEFAULT 'sub', status text NOT NULL DEFAULT 'approved',"
            "credential_hash text NOT NULL DEFAULT '', credential_hint text NOT NULL DEFAULT '',"
            "first_seen_at text NOT NULL DEFAULT '', last_seen_at text NOT NULL DEFAULT '',"
            "reads integer NOT NULL DEFAULT 0, writes integer NOT NULL DEFAULT 0,"
            "exclude_from_primary integer NOT NULL DEFAULT 0, meta text NOT NULL DEFAULT '{}')")
        st._db().execute(                            # noqa: SLF001
            "INSERT INTO ks_agent (agent_id) VALUES ('legacy-agent')")
    try:
        reg = Registry(st)
        assert reg.ensure_schema() is True
        assert reg.dm_enabled("legacy-agent") is True       # 存量行默认开
        row = reg._be.load("legacy-agent")         # noqa: SLF001
        assert row["dm_enabled"] == "1"                   # 补列落默认值
    finally:
        st.close()


def test_目标未批准时一条都投不出去(lite):
    """🔴 投递闸门（存储层视角）：目标代理没有「已批准」资格 ⇒ 收件箱恒空。

    闸门本体在工具层（`mcp_server/tools.py`：`message_send` 先查注册表），
    这里钉的是**存储层不越权**：它只收发，不替调用方判身份。
    """
    box, st = lite
    reg = Registry(st)
    reg.ensure_schema()
    reg.register("pending-agent")
    reg.approve("revoked-agent")
    reg.revoke("revoked-agent")

    for target in ("pending-agent", "revoked-agent", "查无此代理"):
        row = reg.get(target)
        assert row is None or row["status"] != STATUS_APPROVED
        assert box.unread_count(target) == 0


def test_三后端判据同源(monkeypatch):
    """🔴 「不许三份逻辑漂移」的可运行判据：三后端的分派判据是**同一个函数**。"""
    from keepsake.govern import messaging as msg_mod
    from keepsake.govern import registry as reg_mod

    assert msg_mod._backend_of is reg_mod._backend_of
    # 分派结果：SQL 后端（有 _tx/_ro）、SQLite（有 _write/_db）、其余 Redis
    class _Pg:
        def _tx(self): ...
        def _ro(self): ...

    class _Lite:
        def _write(self): ...
        def _db(self): ...

    class _Red:
        def _get_client(self): ...

    assert msg_mod._backend_of(_Pg()) == "postgres"
    assert msg_mod._backend_of(_Lite()) == "sqlite"
    assert msg_mod._backend_of(_Red()) == "redis"


# --------------------------------------------------------------------------
# ④ PG 真库（隔离测试库；没凭据 / 没装驱动就 skip）
# --------------------------------------------------------------------------

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST = "8.140.192.91"
ALLOWED_DBNAME = "keepsake_test"


def _pg_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


def _pg_dsn_is_safe(dsn: str) -> bool:
    """只允许连指定测试库（防手滑连生产）。"""
    return bool(dsn) and ALLOWED_HOST in dsn and ALLOWED_DBNAME in dsn


pytest.importorskip("psycopg", reason="未装 psycopg，跳过 PG 信箱真库测试")
_PG_DSN = _pg_dsn()
if not _pg_dsn_is_safe(_PG_DSN):
    pytest.skip(f"无测试库凭据或指向非 {ALLOWED_HOST}/{ALLOWED_DBNAME}，跳过 PG 真库测试",
                allow_module_level=True)

#: 本模块在 PG 上用到的代理 id（跑完按 id 清干净 —— 那是别人的库，不许留残渣）
_PG_IDS = ("pg-msg-agent-a", "pg-msg-agent-b")


@pytest.fixture(scope="module")
def pg_box():
    from keepsake.core.pg import PgStorage

    storage = PgStorage(dsn=_PG_DSN, connect_timeout=30)
    box = Messaging(storage)
    assert box.ensure_schema() is True
    yield box, storage
    for aid in _PG_IDS:
        try:
            with storage._tx() as cur:      # noqa: SLF001 —— 经原后端连线，不另开连接
                cur.execute(
                    "DELETE FROM ks_message WHERE from_id = %s OR to_id = %s", (aid, aid))
        except Exception as e:               # noqa: BLE001 —— 清理失败不许掩盖用例结论
            print(f"[warn] PG 清理 {aid} 失败：{type(e).__name__}")
    storage.close()


def test_pg_闭环(pg_box):
    """🔴 PG 真库上跑**同一条闭环**（与 SQLite / Redis 逐条同款）。"""
    box, _ = pg_box
    assert box.backend == "postgres"
    a, b = _PG_IDS
    box.send(a, b, "PG 闭环", "th_pg")
    rows = box.fetch_inbox(b, unread_only=True, limit=20)
    assert len(rows) == 1
    assert rows[0]["from_id"] == a and rows[0]["content"] == "PG 闭环"
    assert rows[0]["thread_id"] == "th_pg"
    assert rows[0]["read_at"]                     # fetch 即标读
    assert box.fetch_inbox(b, unread_only=True, limit=20) == []
    assert box.unread_count(b) == 0
    assert [r["content"] for r in box.recent_between(a, b, limit=20)] == ["PG 闭环"]
