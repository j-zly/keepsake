"""代理互聊信箱（`ks_message`）—— 代理之间**直接传话**的存储层。

2026-10 ks_agent_messaging：此前代理之间传话只能由人转述（「你去问下 X」）。
本模块给出一条**不经人**的通路：已批准的代理把消息投进对方的信箱，对方收信。

## 三后端一份语义

* PG / SQLite：`ks_message` 行表，DDL 由 **本模块唯一定义**
  （`KS_MESSAGE_DDL_PG` / `KS_MESSAGE_DDL_SQLITE`），两个后端各 import 自己的那一份
  —— 建表语句复制两份必然漂，而漂的后果是「信箱在换后端时静默消失」。
* Redis：`keepsake:msg` LIST（全部消息，元素是 JSON）+ 每收件人
  `keepsake:msg:to:<agent>` LIST（**该收件人的未读队列**），发送时**双写**。

对外形状（`fetch_inbox` / `recent_between` 的返回）三后端逐字段相同：
`{seq, from_id, to_id, content, thread_id, created, read_at}`，时刻一律 **ISO 字符串**
（PG 的 `timestamptz` 读出来是 datetime ⇒ 规范化时转 ISO；SQLite/Redis 本来就是字符串）。

## 🔴 三条语义纪律

1. **只信 agent_id**：本模块的 API 一律由调用方传 `agent_id`（发件人身份由**凭据
   反查**得来，见 `mcp_server/tools.py`），**不认任何调用侧自带的 from**。
2. **fetch 即标读**：`fetch_inbox` 收走的行当场标 `read_at`（收信 = 消费）。
   `recent_between` **不动** `read_at`（看历史不该把对方的消息吃掉）。
3. **老数据兼容**：Redis 侧没有 `read_at` 列这回事（已读状态存在 hash 里），
   PG/SQLite 的 `read_at` 允许 NULL（= 未读）。

## Redis 侧的取舍（说清楚，别让人以为它与 SQL 等价）

* **未读队列 = 每收件人那个 LIST 本身**（发送双写进去，读信 `LREM` 消费掉）。
  ⇒ `unread_count` = `LLEN`（O(1)），代价是**已读消息不在队列里**，
  「按未读筛历史」这种查询在 Redis 侧要走全表扫（`recent_between` 就是这么做的）。
* **已读时刻存在 hash** `keepsake:msg:read`（`seq → read_at`）——
  Redis 的 LIST 元素不能就地改字段（要 `LSET` 按下标改，O(n) 且易错），
  用一个 hash 记已读时刻是**更省事也更稳**的写法。
* `keepsake:msg` 是**只增不减**的全量 LIST（与 SQL 表同款）⇒ `recent_between`
  在 Redis 侧是 O(全量) 扫。信箱量级是「代理之间几十条对话」，可接受；
  真要上量再换 ZSET（按 seq 排序 + `ZRANGE` 窗口）。

`ponytail:` 现在这三套（全局 LIST + 每收件人 LIST + 已读 hash）够用；
升级路径 = 把 `keepsake:msg` 换成 ZSET（`ZRANGE by score` 取最近 N 条），
已读状态那套不动。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from .events import publish_dm
from .registry import (
    _SqlBase,
    _backend_of,
    _text,
)

logger = logging.getLogger("keepsake.govern.messaging")

# --------------------------------------------------------------------------
# 常量（口径单点：别处引用这里的名字，不要复制字面量）
# --------------------------------------------------------------------------

TABLE = "ks_message"
INDEX = "idx_ks_message_to_read"

#: 列 / 字段名（SQL 行与 Redis JSON 共用同一份名字 ⇒ 规范化只有一处）
MESSAGE_FIELDS: Tuple[str, ...] = (
    "seq", "from_id", "to_id", "content", "thread_id", "created", "read_at",
)

#: 单条消息的正文上限（超了报错，不静默截断 —— 截断的信会让人读错话）。
CONTENT_MAX = 4000

#: 收件箱 / 历史各自的默认条数与硬上限（防一次拉爆上下文）。
DEFAULT_LIMIT = 20
LIMIT_MAX = 200

#: 建表失败后的**重试冷却**（秒）—— 见 `Messaging.ensure_schema`：
#: 未读计数寄生在检索热路径上，DDL 失败时不能每轮都重试建表。
SCHEMA_RETRY_COOLDOWN_S = 60.0

#: 🔴 `ks_message` 的 DDL **唯一定义处**（本模块）。PG 与 SQLite 各一份 ——
#: 两边**方言不同**（`BIGSERIAL` / `timestamptz` vs `INTEGER PRIMARY KEY` /
#: ISO 文本时刻），但**列名、语义、索引完全同构**。
KS_MESSAGE_DDL_PG = """
CREATE TABLE IF NOT EXISTS ks_message (
    seq       BIGSERIAL PRIMARY KEY,
    from_id   text NOT NULL DEFAULT '',
    to_id     text NOT NULL DEFAULT '',
    content   text NOT NULL DEFAULT '',
    thread_id text NOT NULL DEFAULT '',
    created   timestamptz NOT NULL DEFAULT now(),
    read_at   timestamptz NULL
)
"""

KS_MESSAGE_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS ks_message (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id   text NOT NULL DEFAULT '',
    to_id     text NOT NULL DEFAULT '',
    content   text NOT NULL DEFAULT '',
    thread_id text NOT NULL DEFAULT '',
    created   text NOT NULL DEFAULT '',
    read_at   text NULL
)
"""

#: 收件箱索引（`(to_id, read_at)` —— 「某人还有几条没读」正是这个形状的查询）。
KS_MESSAGE_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS {INDEX} ON {TABLE} (to_id, read_at)"
)

# --------------------------------------------------------------------------
# Redis 侧的 key 布局（与本库其余 key 同命名空间 `keepsake:*`）
# --------------------------------------------------------------------------

#: 全部消息（只增不减，与 SQL 表同款）
REDIS_KEY_GLOBAL = "keepsake:msg"
#: 每收件人的**未读队列**（发送双写、读信消费）
REDIS_KEY_INBOX = "keepsake:msg:to:"
#: seq 计数器（`INCR`）
REDIS_KEY_SEQ = "keepsake:msg:seq"
#: 已读时刻（`seq → read_at`）
REDIS_KEY_READ = "keepsake:msg:read"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: Any) -> str:
    """时刻 → ISO 字符串（PG 的 `timestamptz` 读出来是 datetime；其余是字符串）。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _norm_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    """一行（SQL tuple→dict 或 Redis JSON→dict）→ 规范 dict（缺项补缺省）。"""
    row: Dict[str, Any] = {f: raw.get(f) for f in MESSAGE_FIELDS}
    try:
        row["seq"] = int(row.get("seq") or 0)
    except (TypeError, ValueError):
        row["seq"] = 0
    for f in ("from_id", "to_id", "content", "thread_id"):
        row[f] = str(row.get(f) or "")
    row["created"] = _iso(row.get("created"))
    row["read_at"] = _iso(row.get("read_at"))
    return row


def _clamp_limit(limit: Any) -> int:
    """条数上限（1..LIMIT_MAX）；非法值按默认算 —— 不因为一个参数把库拉爆。"""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(n, LIMIT_MAX))


def _clamp_page(page: Any) -> int:
    """页码下限 1（非法值按 1 算 —— 负页码/空串都不该把查询打成怪东西）。"""
    try:
        return max(1, int(page))
    except (TypeError, ValueError):
        return 1


def _like_escape(needle: str) -> str:
    """转义 LIKE 里的通配符（`%` / `_` / 转义符本身）。

    🔴 不转义的话，用户搜一个 `100%` 会匹配到**全部**消息 —— 一个搜索词
    悄悄变成「全库倒出来」，那不是搜索，那是意外的全量导出。
    转义符取 `!`（反斜杠在 PG 侧还要看 `standard_conforming_strings`，
    `!` 没这层讲究）。
    """
    return needle.replace("!", "!!").replace("%", "!%").replace("_", "!_")


# --------------------------------------------------------------------------
# 后端适配：只有「怎么存取」不同，语义全部在 Messaging 里
# --------------------------------------------------------------------------

class _MsgSqlBackend(_SqlBase):
    """PG / SQLite：一张行表。

    🔴 读/写原语**直接继承** `govern.registry._SqlBase`（占位符、事务入口、
    「先查后补」补列那一套）—— 与注册表同源，不复制第二份。
    """

    def ensure_schema(self) -> bool:
        """建表 + 建索引（**幂等**）。

        🔴 本模块**自己发 DDL**（与注册表不同）：`ks_message` 的表不在
        `core/pg.py` / `core/sqlite.py` 的建表清单里，而那两处的 DDL 清单
        不在本单的改动范围内 —— 复制一份建表语句进 core 只会多一处漂移源。
        `CREATE ... IF NOT EXISTS` ⇒ 重复调用不炸。
        """
        if not super().ensure_schema():
            return False
        ddl = KS_MESSAGE_DDL_PG if self.kind == "postgres" else KS_MESSAGE_DDL_SQLITE

        def _fn(cur: Any) -> None:
            cur.execute(ddl)
            cur.execute(KS_MESSAGE_INDEX_SQL)

        try:
            self.write(_fn)
        except Exception as e:      # noqa: BLE001 —— 已存在（并发建表）不算失败
            logger.warning("keepsake: 信箱建表失败（%s: %s）", type(e).__name__, e)
            return False
        return True

    # -- 时刻：PG 收 datetime、SQLite 收 ISO 文本 ------------------------
    def _now_value(self) -> Any:
        return datetime.now(timezone.utc) if self.kind == "postgres" else _now_iso()

    # -- 写入 ----------------------------------------------------------
    def send(self, from_id: str, to_id: str, content: str, thread_id: str) -> int:
        """投一条消息，返回它的 `seq`。"""
        created = self._now_value()
        cols = "from_id, to_id, content, thread_id, created"
        marks = ", ".join(self.ph for _ in range(5))
        params = (from_id, to_id, content, thread_id, created)
        if self.kind == "postgres":
            def _pg(cur: Any) -> int:
                cur.execute(
                    f"INSERT INTO {TABLE} ({cols}) VALUES ({marks}) RETURNING seq", params)
                row = cur.fetchone()
                return int(row[0]) if row else 0

            return int(self.write(_pg))

        def _lite(cur: Any) -> int:
            cur.execute(f"INSERT INTO {TABLE} ({cols}) VALUES ({marks})", params)
            return int(cur.lastrowid or 0)

        return int(self.write(_lite))

    # -- 读 -------------------------------------------------------------
    def _select(self, where: str, params: Tuple[Any, ...], limit: int) -> List[Dict[str, Any]]:
        cols = ", ".join(MESSAGE_FIELDS)
        rows = self.read(
            f"SELECT {cols} FROM {TABLE} WHERE {where} ORDER BY seq DESC LIMIT {self.ph}",
            (*params, limit),
        )
        out = [_norm_row(dict(zip(MESSAGE_FIELDS, r))) for r in rows]
        out.reverse()          # 一律按 seq **升序**返回（先旧后新）
        return out

    def fetch_inbox(self, agent_id: str, unread_only: bool, limit: int) -> List[Dict[str, Any]]:
        """收件箱（**fetch 即标读**）。返回按 seq 升序。"""
        where = "to_id = " + self.ph
        params: Tuple[Any, ...] = (agent_id,)
        if unread_only:
            where += " AND read_at IS NULL"
        rows = self._select(where, params, limit)
        if rows:
            at = _iso(self._mark_read([r["seq"] for r in rows]))
            # 返回行上的 read_at 同步补上（库里已标读，返回值不许说「还没读」）
            for row in rows:
                row["read_at"] = at
        return rows

    def recent_between(self, a: str, b: str, limit: int) -> List[Dict[str, Any]]:
        """与 `b` 的**双向**最近消息（**不动** `read_at`）。返回按 seq 升序。"""
        ph = self.ph
        where = (f"(from_id = {ph} AND to_id = {ph}) OR "
                 f"(from_id = {ph} AND to_id = {ph})")
        return self._select(where, (a, b, b, a), limit)

    def unread_summary(self, agent_id: str, sender_limit: int = 0) -> Dict[str, Any]:
        """未读私信的**计数 + 发件人**（**不取正文** —— 提醒行只要这两样）。

        `sender_limit=0` ⇒ 只计数（一条 count 查询；检索顺路带未读数用的就是这一种）。
        """
        where = f"to_id = {self.ph} AND read_at IS NULL"
        rows = self.read(f"SELECT COUNT(*) FROM {TABLE} WHERE {where}", (agent_id,))
        count = int(rows[0][0]) if rows and rows[0] else 0
        senders: List[str] = []
        if count > 0 and sender_limit > 0:
            srows = self.read(
                f"SELECT from_id FROM {TABLE} WHERE {where} "
                f"GROUP BY from_id ORDER BY MIN(seq), from_id LIMIT {int(sender_limit)}",
                (agent_id,),
            )
            senders = [str(r[0]) for r in srows if r and r[0]]
        return {"count": count, "senders": senders}

    def unread_snapshot(self, agent_id: str) -> Dict[str, Any]:
        """未读私信的**水位快照**：条数 + 最大 seq + 最近一个发件人（**不取正文**）。

        SSE 重连追帧用的就是这一份（见 `mcp_server/events.py`）：
        客户端刚连上就立刻知道「我有多少条没读、最新那条是谁发的」。
        """
        where = f"to_id = {self.ph} AND read_at IS NULL"
        rows = self.read(
            f"SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM {TABLE} WHERE {where}",
            (agent_id,))
        count = int(rows[0][0]) if rows and rows[0] else 0
        max_seq = int(rows[0][1]) if rows and rows[0] and rows[0][1] is not None else 0
        last_from = ""
        if count > 0:
            frows = self.read(
                f"SELECT from_id FROM {TABLE} WHERE {where} ORDER BY seq DESC LIMIT 1",
                (agent_id,))
            last_from = str(frows[0][0]) if frows and frows[0] else ""
        return {"count": count, "max_seq": max_seq, "last_from": last_from}

    def unread_by_agent(self) -> Dict[str, int]:
        """**每个收件人**的未读条数（`{agent_id: count}`）—— 控制台「按收件代理分组」用。"""
        rows = self.read(
            f"SELECT to_id, COUNT(*) FROM {TABLE} WHERE read_at IS NULL GROUP BY to_id")
        return {str(r[0]): int(r[1]) for r in rows if r and r[0]}

    # -- 治理面只读列表 -------------------------------------------------
    def list_messages(self, agent: str, thread: str, q: str,
                      page: int, size: int) -> Tuple[List[Dict[str, Any]], int]:
        """全库消息的一页（按 seq **倒序** = 新的在前）+ 过滤后的总数。

        🔴 **只读**：本方法**一个字段都不改**（尤其不动 `read_at`）——
        看历史不该把对方的消息吃掉（与 `recent_between` 同纪律）。

        过滤三件：代理（发件**或**收件都算）、线程（`thread_id`）、
        正文关键词（大小写不敏感子串，通配符已转义）。
        """
        where: List[str] = []
        params: List[Any] = []
        if agent:
            where.append(f"(from_id = {self.ph} OR to_id = {self.ph})")
            params += [agent, agent]
        if thread:
            where.append(f"thread_id = {self.ph}")
            params.append(thread)
        if q:
            # 🔴 `lower()` 两边都落：SQLite 的 LIKE 默认不区分大小写、PG 的区分 ——
            # 不统一的话，同一个搜索词在两个后端上结果不一样。
            where.append(f"lower(content) LIKE {self.ph} ESCAPE '!'")
            params.append(f"%{_like_escape(q.lower())}%")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total_rows = self.read(f"SELECT COUNT(*) FROM {TABLE}{clause}", tuple(params))
        total = int(total_rows[0][0]) if total_rows and total_rows[0] else 0
        cols = ", ".join(MESSAGE_FIELDS)
        rows = self.read(
            f"SELECT {cols} FROM {TABLE}{clause} "
            f"ORDER BY seq DESC LIMIT {self.ph} OFFSET {self.ph}",
            (*params, size, (page - 1) * size),
        )
        return [_norm_row(dict(zip(MESSAGE_FIELDS, r))) for r in rows], total

    # -- 标读 -----------------------------------------------------------
    def _mark_read(self, seqs: List[int]) -> Any:
        """把若干条消息标成已读（`read_at` = 此刻），返回那个时刻。"""
        if not seqs:
            return None
        marks = ", ".join(self.ph for _ in seqs)
        at = self._now_value()
        self.write(lambda cur: cur.execute(
            f"UPDATE {TABLE} SET read_at = {self.ph} WHERE seq IN ({marks})",
            (at, *seqs),
        ))
        return at


class _RedisBackend:
    """Redis：全局 LIST + 每收件人未读 LIST + 已读 hash（取舍见模块 docstring）。"""

    kind = "redis"

    def __init__(self, storage: Any) -> None:
        self._st = storage

    @staticmethod
    def inbox_key(agent_id: str) -> str:
        return f"{REDIS_KEY_INBOX}{agent_id}"

    def _client(self) -> Any:
        client = self._st._get_client()      # noqa: SLF001 —— 复用既有连线
        if client is None:
            raise RuntimeError("messaging: Redis 不可达（_get_client 返回 None）")
        return client

    def ensure_schema(self) -> bool:
        return True          # LIST / hash 形态没有 schema

    def send(self, from_id: str, to_id: str, content: str, thread_id: str) -> int:
        client = self._client()
        seq = int(client.incr(REDIS_KEY_SEQ))
        msg = {
            "seq": seq, "from_id": from_id, "to_id": to_id, "content": content,
            "thread_id": thread_id, "created": _now_iso(), "read_at": "",
        }
        blob = json.dumps(msg, ensure_ascii=False)
        client.rpush(REDIS_KEY_GLOBAL, blob)        # 双写：全量 + 收件人未读队列
        client.rpush(self.inbox_key(to_id), blob)
        return seq

    def _all_blobs(self) -> List[str]:
        return [_text(b) for b in self._client().lrange(REDIS_KEY_GLOBAL, 0, -1)]

    def _mark_read(self, agent_id: str, blobs: List[str]) -> None:
        """标读：记已读时刻（hash）+ 从收件人未读队列里消费掉（`LREM`）。"""
        if not blobs:
            return
        client = self._client()
        at = _now_iso()
        for blob in blobs:
            try:
                seq = int(json.loads(blob).get("seq") or 0)
            except (TypeError, ValueError):
                continue
            if seq:
                client.hset(REDIS_KEY_READ, str(seq), at)
            client.lrem(self.inbox_key(agent_id), 1, blob)

    def _fill_read_at(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """把已读时刻从 hash 里补回行里（LIST 元素不能就地改字段，见模块 docstring）。"""
        if not rows:
            return rows
        client = self._client()
        for row in rows:
            if row.get("read_at"):
                continue
            raw = client.hget(REDIS_KEY_READ, str(row.get("seq")))
            at = _text(raw)
            if at:
                row["read_at"] = at
        return rows

    def fetch_inbox(self, agent_id: str, unread_only: bool, limit: int) -> List[Dict[str, Any]]:
        client = self._client()
        if unread_only:
            # 未读队列的**尾部**才是最近的（与 SQL 的 `ORDER BY seq DESC LIMIT n` 同口径）
            blobs = [_text(b) for b in client.lrange(self.inbox_key(agent_id), -limit, -1)]
        else:
            mine = [b for b in self._all_blobs()
                    if str(json.loads(b).get("to_id") or "") == agent_id]
            blobs = mine[-limit:]
        rows = [_norm_row(json.loads(b)) for b in blobs]
        rows.sort(key=lambda r: r["seq"])
        self._mark_read(agent_id, blobs)
        return self._fill_read_at(rows)

    def recent_between(self, a: str, b: str, limit: int) -> List[Dict[str, Any]]:
        picked: List[Dict[str, Any]] = []
        for blob in self._all_blobs():
            try:
                row = _norm_row(json.loads(blob))
            except (TypeError, ValueError):
                continue
            if ((row["from_id"] == a and row["to_id"] == b)
                    or (row["from_id"] == b and row["to_id"] == a)):
                picked.append(row)
        picked.sort(key=lambda r: r["seq"])
        return self._fill_read_at(picked[-limit:])

    def unread_summary(self, agent_id: str, sender_limit: int = 0) -> Dict[str, Any]:
        client = self._client()
        count = int(client.llen(self.inbox_key(agent_id)) or 0)
        senders: List[str] = []
        if count > 0 and sender_limit > 0:
            seen = set()
            for blob in client.lrange(self.inbox_key(agent_id), 0, -1):
                try:
                    fid = str(json.loads(_text(blob)).get("from_id") or "")
                except (TypeError, ValueError):
                    continue
                if fid and fid not in seen:
                    seen.add(fid)
                    senders.append(fid)
                    if len(senders) >= sender_limit:
                        break
        return {"count": count, "senders": senders}

    def unread_snapshot(self, agent_id: str) -> Dict[str, Any]:
        """未读私信的水位快照（条数 + 最大 seq + 最近发件人）。

        Redis 侧的**未读队列本身就是未读集合**（见模块 docstring 的取舍），
        所以条数 = `LLEN`，最近那条 = 队尾（`LINDEX -1`）。
        """
        client = self._client()
        key = self.inbox_key(agent_id)
        count = int(client.llen(key) or 0)
        max_seq = 0
        last_from = ""
        if count > 0:
            try:
                row = _norm_row(json.loads(_text(client.lindex(key, -1))))
                max_seq = int(row.get("seq") or 0)
                last_from = str(row.get("from_id") or "")
            except (TypeError, ValueError):
                pass
        return {"count": count, "max_seq": max_seq, "last_from": last_from}

    def unread_by_agent(self) -> Dict[str, int]:
        """每个收件人的未读条数（`{agent_id: count}`）—— 控制台「按收件代理分组」用。"""
        client = self._client()
        out: Dict[str, int] = {}
        for key in client.scan_iter(match=f"{REDIS_KEY_INBOX}*"):
            name = _text(key)
            if not name.startswith(REDIS_KEY_INBOX):
                continue
            agent_id = name[len(REDIS_KEY_INBOX):]
            if agent_id:
                out[agent_id] = int(client.llen(key) or 0)
        return out

    # -- 治理面只读列表 -------------------------------------------------
    def list_messages(self, agent: str, thread: str, q: str,
                      page: int, size: int) -> Tuple[List[Dict[str, Any]], int]:
        """全库消息的一页（按 seq **倒序** = 新的在前）+ 过滤后的总数。

        🔴 **只读**：不动 `read_at`（Redis 侧已读时刻在 hash 里，本方法只读不写）。

        `ponytail:` Redis 侧是 O(全量) 扫 —— 与 `recent_between` 同一取舍
        （全量 LIST 是只增不减的，没有索引可走）。信箱量级是「代理之间几十条
        对话」，可接受；真要上量就把 `keepsake:msg` 换成 ZSET
        （`ZRANGE by score` 取最近 N 条），已读状态那套不动。
        """
        needle = (q or "").strip().lower()
        picked: List[Dict[str, Any]] = []
        for blob in self._all_blobs():
            try:
                row = _norm_row(json.loads(blob))
            except (TypeError, ValueError):
                continue
            if agent and row["from_id"] != agent and row["to_id"] != agent:
                continue
            if thread and row["thread_id"] != thread:
                continue
            if needle and needle not in row["content"].lower():
                continue
            picked.append(row)
        picked.sort(key=lambda r: r["seq"], reverse=True)
        total = len(picked)
        start = (page - 1) * size
        return self._fill_read_at(picked[start:start + size]), total


# --------------------------------------------------------------------------
# 信箱本体
# --------------------------------------------------------------------------

class Messaging:
    """代理互聊信箱。`Messaging(storage)` 一份，三后端同一语义。

    **不自拼连接、不另开进程**：与 `Registry` 同款 —— PG / SQLite 走存储后端
    自己的事务游标，Redis 走 `_get_client()` 的既有连线。
    """

    def __init__(self, storage: Any) -> None:
        kind = _backend_of(storage)
        self.backend = kind
        self._be: Any = (_MsgSqlBackend(storage, kind) if kind != "redis"
                         else _RedisBackend(storage))
        self._ensured = False
        self._ensure_lock = threading.Lock()
        #: 建表失败后的**重试冷却**截止时刻（单调钟）。见 `ensure_schema`。
        self._fail_until = 0.0

    # ---- 生命周期 ----------------------------------------------------
    def ensure_schema(self) -> bool:
        """确保 `ks_message` 在（幂等；**成功 True / 失败 False**）。

        🔴 **失败后有冷却**（60s 内不重试 DDL）：2026-10 ks_dm_hint 之后
        信箱的未读计数被**寄生在检索热路径**上（每轮都走）—— 建表 DDL 失败
        时若每次都重试，等于每轮发一遍 DDL（PG 上建索引会拿锁、挡写）。
        冷却期内的调用直接回 False（=「schema 未就绪」），不碰库。
        """
        with self._ensure_lock:
            if self._ensured:
                return True
            if time.monotonic() < self._fail_until:
                return False
            try:
                ok = bool(self._be.ensure_schema())
            except Exception as e:      # noqa: BLE001 —— 返回 False，不抛
                logger.warning("keepsake: 信箱 ensure_schema 失败（%s: %s）",
                               type(e).__name__, e)
                ok = False
            if ok:
                self._ensured = True
            else:
                self._fail_until = time.monotonic() + SCHEMA_RETRY_COOLDOWN_S
            return ok

    def _ready(self) -> None:
        """第一次用之前把表建好（失败 ⇒ 抛，让调用方知道「消息没投出去」）。"""
        if not self.ensure_schema():
            raise RuntimeError("信箱 schema 未就绪（ensure_schema 失败）—— 消息未投递")

    # ---- 写 -----------------------------------------------------------
    def send(self, from_id: str, to_id: str, content: str, thread_id: str = "") -> int:
        """投一条消息，返回 `seq`。

        🔴 `from_id` 一律由**调用方从凭据反查后**传入 —— 本模块不认任何
        「调用侧自带的 from」（那正是这套治理要拔掉的东西）。
        """
        sender = (from_id or "").strip()
        target = (to_id or "").strip()
        body = (content or "").strip()
        if not sender or not target:
            raise ValueError("from_id / to_id 不能为空")
        if not body:
            raise ValueError("content 不能为空")
        if len(body) > CONTENT_MAX:
            raise ValueError(f"content 超长（{len(body)} > {CONTENT_MAX} 字符）")
        self._ready()
        seq = int(self._be.send(sender, target, body, (thread_id or "").strip()))
        # ---- 投递钩子（2026-10 ks_dm_sse）：落库 COMMIT 成功后顺原流推一帧元数据 ----
        # 🔴 **只推元数据**（seq / from / unread），正文永远走信箱本体；
        #   **没有挂着连接 = 静默丢弃**（不重试、不轰炸）—— 补偿在下一层
        #   （SSE 重连追帧 + unread_dm 水位），不在这一层做。
        #   推送失败**不许**把已经落库的消息说成没投出去（消息已经送到了）。
        try:
            publish_dm(target, seq=seq, from_id=sender,
                       unread=self.unread_count(target))
        except Exception as e:      # noqa: BLE001 —— 推送失败不影响已落库的消息
            logger.warning("keepsake: 私信推送失败（消息已落库，不重试）：%s: %s",
                           type(e).__name__, e)
        return seq

    # ---- 读 -----------------------------------------------------------
    def fetch_inbox(self, agent_id: str, unread_only: bool = True,
                    limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
        """收件箱（**fetch 即标读**）：返回按 seq 升序的行。"""
        aid = (agent_id or "").strip()
        if not aid:
            return []
        self._ready()
        return list(self._be.fetch_inbox(aid, bool(unread_only), _clamp_limit(limit)))

    def recent_between(self, a: str, b: str, limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
        """与 `b` 的**双向**最近消息（含自己发的；**不动** `read_at`）。"""
        left = (a or "").strip()
        right = (b or "").strip()
        if not left or not right:
            return []
        self._ready()
        return list(self._be.recent_between(left, right, _clamp_limit(limit)))

    def unread_summary(self, agent_id: str, sender_limit: int = 0) -> Dict[str, Any]:
        """未读私信的**计数 + 发件人**（不取正文）—— 提醒行的**唯一口径**。

        🔴 **服务端（检索顺路带未读数）与插件（注入 📩 提醒行）走的是同一份**：
        两处各写一条 SQL 必然漂（漂了就是「面板说 3 条、注入说 1 条」）。
        `sender_limit=0` ⇒ 只计数（一条 count 查询）。
        """
        aid = (agent_id or "").strip()
        if not aid:
            return {"count": 0, "senders": []}
        self._ready()
        return self._be.unread_summary(aid, max(0, int(sender_limit)))

    def unread_count(self, agent_id: str) -> int:
        """还有几条没读（收件箱里未读的消息条数）。"""
        return int(self.unread_summary(agent_id).get("count") or 0)

    def unread_snapshot(self, agent_id: str) -> Dict[str, Any]:
        """未读私信的水位快照：`{count, max_seq, last_from}`（**不取正文**）。

        SSE **重连追帧**用的就是这一份 —— 客户端刚连上就立刻知道
        「我有多少条没读、最新那条是谁发的」（帧里同样只有元数据）。
        """
        aid = (agent_id or "").strip()
        if not aid:
            return {"count": 0, "max_seq": 0, "last_from": ""}
        self._ready()
        return dict(self._be.unread_snapshot(aid))

    def unread_by_agent(self) -> Dict[str, int]:
        """**每个收件人**的未读条数（`{agent_id: count}`）—— 控制台「按收件代理分组」用。"""
        self._ready()
        return dict(self._be.unread_by_agent())

    # ---- 治理面只读列表（控制台「消息中心」用）------------------------
    def list_messages(self, agent: str = "", thread: str = "", q: str = "",
                      page: int = 1, size: int = DEFAULT_LIMIT) -> Dict[str, Any]:
        """全库消息列表（**只读**）：`{"items": [...], "total": n}`。

        * `agent` 非空 ⇒ 只看**与该代理有关**的消息（发件或收件都算）；
        * `thread` 非空 ⇒ 只看该线程（`thread_id`）；
        * `q` 非空 ⇒ 正文关键词（大小写不敏感子串）；
        * 按 **seq 倒序**（新的在前）分页，`page` 从 1 起，`size` 上限 `LIMIT_MAX`。

        🔴 **只读**：不改任何字段（尤其不动 `read_at`）—— 看历史不该把对方
        的消息吃掉（与 `recent_between` 同纪律）。三后端语义逐字段一致。
        """
        self._ready()
        rows, total = self._be.list_messages(
            (agent or "").strip(), (thread or "").strip(), (q or "").strip(),
            _clamp_page(page), _clamp_limit(size),
        )
        return {"items": list(rows), "total": int(total)}


__all__ = [
    "Messaging", "MESSAGE_FIELDS", "CONTENT_MAX", "DEFAULT_LIMIT", "LIMIT_MAX",
    "TABLE", "INDEX", "REDIS_KEY_GLOBAL", "REDIS_KEY_INBOX",
    "KS_MESSAGE_DDL_PG", "KS_MESSAGE_DDL_SQLITE", "SCHEMA_RETRY_COOLDOWN_S",
]
