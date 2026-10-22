"""待办（`ks_todo`）—— 待办一等公民的存储层（2026-10 ks_todo）。

待办此前一直用 `kind:pending` 记忆条目凑合：盘点 12 条里 9 条烂账
（做完没销、过期没人知）。本模块给它一个正经的去处 —— **状态机 + 归属 + 到期日**。

## 三后端一份语义

* PG / SQLite：`ks_todo` 行表，DDL 由 **本模块唯一定义**
  （`KS_TODO_DDL_PG` / `KS_TODO_DDL_SQLITE`），两个后端各 import 自己的那一份
  —— 建表语句复制两份必然漂，而漂的后果是「待办在换后端时静默消失」。
* Redis：`keepsake:todo:<id>` hash（一条待办一个 hash）+ `keepsake:todo:ids`
  LIST（全部待办的 id，插入序）+ `keepsake:todo:seq` 计数器（发号）。

对外形状逐字段相同：`{id, title, detail, owner, creator, status, due,
created, updated, done_at, due_state}`，时刻一律 **ISO 字符串**。

## 🔴 四条语义纪律

1. **状态三态**：`open` / `done` / `obsolete`（DDL 里有 CHECK 兜底）——
   「已废弃」与「已完成」是**两件事**：一个是干完了，一个是这条作废了。
2. **归属两列**：`owner`（谁该干这件事）+ `creator`（谁记下的这件事）。
   可见性 = `owner = 自己 ∪ creator = 自己`（协作台账：被指派者必见自己的活），
   primary 全量。
3. **到期口径单点**：`due_state()` 是**唯一**判「临期 / 过期」的地方
   （none / soon / overdue，基准日 = UTC 今天）—— 前端只翻译它，不自己算日期。
4. **老数据兼容**：`due` / `detail` / `done_at` 都可空，老行缺字段一律回落缺省值。

`ponytail:` Redis 侧是 hash + 全量 LIST（O(全量) 扫），待办量级是「几十条」，
够用；升级路径 = 把 LIST 换成 ZSET（`ZRANGE by score` 取最近 N 条）。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .registry import (
    _SqlBase,
    _backend_of,
    _text,
)

logger = logging.getLogger("keepsake.govern.todo")

# --------------------------------------------------------------------------
# 常量（口径单点：别处引用这里的名字，不要复制字面量）
# --------------------------------------------------------------------------

TABLE = "ks_todo"
INDEX = "idx_ks_todo_owner_status"

#: 列 / 字段名（SQL 行与 Redis hash 共用同一份名字 ⇒ 规范化只有一处）
TODO_FIELDS: Tuple[str, ...] = (
    "id", "title", "detail", "owner", "creator", "status", "due",
    "created", "updated", "done_at",
)

#: 状态三态
STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_OBSOLETE = "obsolete"
STATUSES: Tuple[str, ...] = (STATUS_OPEN, STATUS_DONE, STATUS_OBSOLETE)

#: 列表快捷档（`scope`）
SCOPE_OPEN = "open"
SCOPE_DONE = "done"
SCOPE_ALL = "all"
SCOPES: Tuple[str, ...] = (SCOPE_OPEN, SCOPE_DONE, SCOPE_ALL)

#: 临期窗口（天）：到期日在今天起 3 天内 = 临期（橙），过了今天 = 过期（红）。
DUE_SOON_DAYS = 3

#: 「建议废弃」的门槛：过期满 N 天仍未销 ⇒ 维护页提名（**只提名，不改状态**）。
OBSOLETE_SUGGEST_DAYS = 7

#: 标题 / 正文上限（超了报错，不静默截断 —— 截掉的待办会让人漏事）。
TITLE_MAX = 200
DETAIL_MAX = 4000

#: 列表默认条数与硬上限。
DEFAULT_LIMIT = 50
LIMIT_MAX = 200

#: 建表失败后的**重试冷却**（秒）—— 同一款纪律见 `govern/messaging.py`。
SCHEMA_RETRY_COOLDOWN_S = 60.0

#: Redis 键
REDIS_KEY_GLOBAL = "keepsake:todo:ids"
REDIS_KEY_ITEM = "keepsake:todo:"
REDIS_KEY_SEQ = "keepsake:todo:seq"

#: 🔴 `ks_todo` 的 DDL **唯一定义处**（本模块）。PG 与 SQLite 各一份 ——
#: 两边**方言不同**（`BIGSERIAL` / `timestamptz` vs `INTEGER AUTOINCREMENT` /
#: ISO 文本时刻），但**列名、语义、索引完全同构**。
KS_TODO_DDL_PG = """
CREATE TABLE IF NOT EXISTS ks_todo (
    id       BIGSERIAL PRIMARY KEY,
    title    text NOT NULL DEFAULT '',
    detail   text NOT NULL DEFAULT '',
    owner    text NOT NULL DEFAULT '',
    creator  text NOT NULL DEFAULT '',
    status   text NOT NULL DEFAULT 'open',
    due      text NOT NULL DEFAULT '',
    created  timestamptz NOT NULL DEFAULT now(),
    updated  timestamptz NOT NULL DEFAULT now(),
    done_at  timestamptz NULL,
    CONSTRAINT ks_todo_status_chk CHECK (status IN ('open','done','obsolete'))
)
"""

KS_TODO_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS ks_todo (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    title    text NOT NULL DEFAULT '',
    detail   text NOT NULL DEFAULT '',
    owner    text NOT NULL DEFAULT '',
    creator  text NOT NULL DEFAULT '',
    status   text NOT NULL DEFAULT 'open',
    due      text NOT NULL DEFAULT '',
    created  text NOT NULL DEFAULT '',
    updated  text NOT NULL DEFAULT '',
    done_at  text NULL,
    CONSTRAINT ks_todo_status_chk CHECK (status IN ('open','done','obsolete'))
)
"""

#: 索引（`owner` + `status` 复合：可见性按 owner 筛，状态按 status 筛）
KS_TODO_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS {INDEX} ON {TABLE} (owner, status)"
)


class TodoError(Exception):
    """待办层的可读错误（`code` 供前端映射，`message` 给人看）。

    🔴 继承 Exception 而不是 ValueError：调用方（MCP / API）要**分开**处理
    「输入不合法」与「后端不可达」两种失败。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# 纯函数：校验 / 归一 / 到期判定（口径单点，前后端都照它说话）
# --------------------------------------------------------------------------

def check_status(status: Any) -> str:
    """状态三态校验：非法值 ⇒ `TodoError`（不许悄悄落一个第四态）。"""
    value = str(status or "").strip()
    if value not in STATUSES:
        raise TodoError("invalid_status",
                        f"status 只能是 {'/'.join(STATUSES)}，收到：{value or '(空)'}")
    return value


def check_title(title: Any) -> str:
    """标题：非空 + ≤200 字（超了报错，不截断）。"""
    value = str(title or "").strip()
    if not value:
        raise TodoError("invalid_title", "标题不能为空")
    if len(value) > TITLE_MAX:
        raise TodoError("invalid_title",
                        f"标题超长（{len(value)} > {TITLE_MAX} 字符）")
    return value


def check_detail(detail: Any) -> str:
    """正文：可空，但 ≤4000 字（超了报错，不截断）。"""
    value = str(detail or "").strip()
    if len(value) > DETAIL_MAX:
        raise TodoError("invalid_detail",
                        f"正文超长（{len(value)} > {DETAIL_MAX} 字符）")
    return value


def parse_due(raw: Any) -> str:
    """到期日归一：`''` / `None` ⇒ 空串；`'YYYY-MM-DD'` 或 ISO 时刻 ⇒ 日期部分。

    🔴 非法日期**报错**（不猜）：猜错一个到期日 = 有人漏了事却没人知道。
    """
    value = str(raw or "").strip()
    if not value:
        return ""
    head = value[:10]
    try:
        date.fromisoformat(head)
    except ValueError:
        raise TodoError("invalid_due",
                        f"到期日必须是 ISO 日期（YYYY-MM-DD），收到：{value}") from None
    return head


def _today() -> date:
    """基准日 = UTC 今天（**单点**：到期判定全走它）。"""
    return datetime.now(timezone.utc).date()


def due_state(due: Any, today: Optional[date] = None) -> str:
    """到期态：`none` / `soon` / `overdue`。

    🔴 **唯一**的到期口径（全站只有这一处算日期）：

    * `overdue` —— 到期日**早于**今天（昨天到期就是过期了）；
    * `soon` —— 到期日在今天起 `DUE_SOON_DAYS` 天内（**含今天**：今天到期
      今天还来得及，不算过期）；
    * `none` —— 没到期日，或到期日还远。
    """
    head = str(due or "").strip()[:10]
    if not head:
        return "none"
    try:
        day = date.fromisoformat(head)
    except ValueError:
        return "none"
    base = today or _today()
    if day < base:
        return "overdue"
    if day <= base + timedelta(days=DUE_SOON_DAYS):
        return "soon"
    return "none"


def overdue_days(due: Any, today: Optional[date] = None) -> int:
    """过期天数（没到期 / 没过期 ⇒ 0）。「建议废弃」按它筛（≥7 天）。"""
    head = str(due or "").strip()[:10]
    if not head:
        return 0
    try:
        day = date.fromisoformat(head)
    except ValueError:
        return 0
    base = today or _today()
    return max(0, (base - day).days)


def can_access(row: Optional[Dict[str, Any]], caller: str, is_primary: bool) -> bool:
    """可见性 / 可改性：**同一把尺子**（owner ∪ creator，primary 全量）。

    🔴 「看得见」与「能改」在这里是同一组 —— 待办不是共享记忆：
    一条待办要么跟你有关（你该干 / 你记的），要么跟你没关系。
    """
    if is_primary:
        return True
    if not row:
        return False
    who = str(caller or "").strip()
    if not who:
        return False
    return who in (str(row.get("owner") or ""), str(row.get("creator") or ""))


def visible_rows(rows: List[Dict[str, Any]], caller: str,
                 is_primary: bool) -> List[Dict[str, Any]]:
    """按可见性过滤一批行（primary 全量，其余 owner ∪ creator）。"""
    if is_primary:
        return list(rows or [])
    who = str(caller or "").strip()
    return [r for r in (rows or [])
            if who and who in (str(r.get("owner") or ""), str(r.get("creator") or ""))]


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
    """一行（SQL tuple→dict 或 Redis hash→dict）→ 规范 dict（缺项补缺省）。"""
    row: Dict[str, Any] = {f: raw.get(f) for f in TODO_FIELDS}
    try:
        row["id"] = int(row.get("id") or 0)
    except (TypeError, ValueError):
        row["id"] = 0
    for f in ("title", "detail", "owner", "creator", "status", "due"):
        row[f] = str(row.get(f) or "")
    if row["status"] not in STATUSES:
        row["status"] = STATUS_OPEN
    for f in ("created", "updated", "done_at"):
        row[f] = _iso(row.get(f))
    # 🔴 到期态在**存储层**算好往外给（前端只翻译，不自己算日期）。
    row["due_state"] = due_state(row["due"])
    return row


def _clamp_limit(size: Any) -> int:
    """条数上限（1..LIMIT_MAX）；非法值按默认算 —— 不因为一个参数把库拉爆。"""
    try:
        n = int(size)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(n, LIMIT_MAX))


def _clamp_page(page: Any) -> int:
    """页码下限 1（非法值按 1 算）。"""
    try:
        return max(1, int(page))
    except (TypeError, ValueError):
        return 1


def _scope_status(status: str, scope: str) -> str:
    """`status`（精确）优先于 `scope`（快捷档）；两者都空 ⇒ 全部状态。"""
    exact = str(status or "").strip()
    if exact:
        return check_status(exact)
    sc = str(scope or "").strip() or SCOPE_ALL
    if sc not in SCOPES:
        raise TodoError("invalid_scope",
                        f"scope 只能是 {'/'.join(SCOPES)}，收到：{sc}")
    if sc == SCOPE_ALL:
        return ""
    return sc


# --------------------------------------------------------------------------
# 后端适配：只有「怎么存取」不同，语义全部在 TodoStore 里
# --------------------------------------------------------------------------

class _TodoSqlBackend(_SqlBase):
    """PG / SQLite：一张行表。

    🔴 读/写原语**直接继承** `govern.registry._SqlBase`（占位符、事务入口）——
    与注册表、信箱同源，不复制第二份。
    """

    def ensure_schema(self) -> bool:
        """建表 + 建索引（**幂等**，`CREATE ... IF NOT EXISTS`）。"""
        if not super().ensure_schema():
            return False
        ddl = KS_TODO_DDL_PG if self.kind == "postgres" else KS_TODO_DDL_SQLITE

        def _fn(cur: Any) -> None:
            cur.execute(ddl)
            cur.execute(KS_TODO_INDEX_SQL)

        try:
            self.write(_fn)
        except Exception as e:      # noqa: BLE001 —— 已存在（并发建表）不算失败
            logger.warning("keepsake: 待办建表失败（%s: %s）", type(e).__name__, e)
            return False
        return True

    # -- 时刻：PG 收 datetime、SQLite 收 ISO 文本 ------------------------
    def _now_value(self) -> Any:
        return datetime.now(timezone.utc) if self.kind == "postgres" else _now_iso()

    # -- 写 --------------------------------------------------------------
    def add(self, title: str, detail: str, owner: str, creator: str,
            due: str) -> int:
        """插一条待办，返回它的 `id`。"""
        now = self._now_value()
        cols = "title, detail, owner, creator, status, due, created, updated"
        params = (title, detail, owner, creator, STATUS_OPEN, due, now, now)
        marks = ", ".join(self.ph for _ in range(8))
        if self.kind == "postgres":
            def _pg(cur: Any) -> int:
                cur.execute(
                    f"INSERT INTO {TABLE} ({cols}) VALUES ({marks}) RETURNING id",
                    params)
                row = cur.fetchone()
                return int(row[0]) if row else 0

            return int(self.write(_pg))

        def _lite(cur: Any) -> int:
            cur.execute(f"INSERT INTO {TABLE} ({cols}) VALUES ({marks})", params)
            return int(cur.lastrowid or 0)

        return int(self.write(_lite))

    def set_status(self, todo_id: int, status: str) -> bool:
        """改状态（`done` ⇒ 记 `done_at`；回到 `open` ⇒ 清掉）。返回是否命中。"""
        now = self._now_value()
        done_at = now if status == STATUS_DONE else None
        if self.kind == "postgres":
            def _pg(cur: Any) -> bool:
                cur.execute(
                    f"UPDATE {TABLE} SET status = {self.ph}, updated = {self.ph}, "
                    f"done_at = {self.ph} WHERE id = {self.ph} RETURNING id",
                    (status, now, done_at, todo_id))
                return cur.fetchone() is not None

            return bool(self.write(_pg))

        def _lite(cur: Any) -> bool:
            cur.execute(
                f"UPDATE {TABLE} SET status = {self.ph}, updated = {self.ph}, "
                f"done_at = {self.ph} WHERE id = {self.ph}",
                (status, now, done_at, todo_id))
            return int(cur.rowcount or 0) > 0

        return bool(self.write(_lite))

    def delete(self, todo_id: int) -> bool:
        """删一条待办。返回是否命中。"""
        def _fn(cur: Any) -> bool:
            cur.execute(f"DELETE FROM {TABLE} WHERE id = {self.ph}", (todo_id,))
            return int(cur.rowcount or 0) > 0

        return bool(self.write(_fn))

    # -- 读 --------------------------------------------------------------
    def _select(self, where: str, params: Tuple[Any, ...],
                limit: int, offset: int) -> List[Dict[str, Any]]:
        cols = ", ".join(TODO_FIELDS)
        rows = self.read(
            f"SELECT {cols} FROM {TABLE}{where} "
            f"ORDER BY id DESC LIMIT {self.ph} OFFSET {self.ph}",
            (*params, limit, offset),
        )
        return [_norm_row(dict(zip(TODO_FIELDS, r))) for r in rows]

    def _where(self, owner: str, status: str,
               visible_to: str = "") -> Tuple[str, Tuple[Any, ...]]:
        clauses: List[str] = []
        params: List[Any] = []
        if owner:
            clauses.append(f"owner = {self.ph}")
            params.append(owner)
        if visible_to:
            # 协作台账口径：**owner 是自己 ∪ creator 是自己**（被指派者必见自己的活）
            clauses.append(f"(owner = {self.ph} OR creator = {self.ph})")
            params.extend([visible_to, visible_to])
        if status:
            clauses.append(f"status = {self.ph}")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, tuple(params)

    def list_rows(self, owner: str, status: str, page: int, size: int,
                  visible_to: str = "") -> Tuple[List[Dict[str, Any]], int]:
        """分页列表（按 id 倒序：新的在前）+ 总条数。"""
        where, params = self._where(owner, status, visible_to)
        total_rows = self.read(
            f"SELECT COUNT(*) FROM {TABLE}{where}", params)
        total = int(total_rows[0][0]) if total_rows and total_rows[0] else 0
        rows = self._select(where, params, size, (page - 1) * size)
        return rows, total

    def get(self, todo_id: int) -> Optional[Dict[str, Any]]:
        cols = ", ".join(TODO_FIELDS)
        rows = self.read(
            f"SELECT {cols} FROM {TABLE} WHERE id = {self.ph}", (todo_id,))
        return _norm_row(dict(zip(TODO_FIELDS, rows[0]))) if rows else None

    def all_rows(self, owner: str = "", visible_to: str = "") -> List[Dict[str, Any]]:
        """全量行（Redis 侧同款语义；owner 非空 ⇒ 只看那个 owner）。"""
        where, params = self._where(owner, "", visible_to)
        cols = ", ".join(TODO_FIELDS)
        rows = self.read(f"SELECT {cols} FROM {TABLE}{where} ORDER BY id ASC", params)
        return [_norm_row(dict(zip(TODO_FIELDS, r))) for r in rows]


class _RedisBackend:
    """Redis：每条待办一个 hash + 全量 id LIST + 发号计数器。

    🔴 **取舍说清楚**（别让人以为它与 SQL 等价）：LIST 元素不能就地改字段
    ⇒ 改状态是「改那个 id 的 hash」（O(1)）；但「按 owner / 状态筛」只能
    **全量扫**（O(全量)）。待办量级是「几十条」，够用。
    `ponytail:` 升级路径 = 把 LIST 换成 ZSET（`ZRANGE by score` 取最近 N 条）。
    """

    kind = "redis"

    def __init__(self, storage: Any) -> None:
        self._st = storage

    @staticmethod
    def item_key(todo_id: Any) -> str:
        return f"{REDIS_KEY_ITEM}{todo_id}"

    def _client(self) -> Any:
        client = self._st._get_client()      # noqa: SLF001 —— 复用既有连线
        if client is None:
            raise RuntimeError("todo: Redis 不可达（_get_client 返回 None）")
        return client

    def ensure_schema(self) -> bool:
        return True          # hash / LIST 形态没有 schema

    def add(self, title: str, detail: str, owner: str, creator: str,
            due: str) -> int:
        client = self._client()
        todo_id = int(client.incr(REDIS_KEY_SEQ))
        now = _now_iso()
        row = {
            "id": str(todo_id), "title": title, "detail": detail,
            "owner": owner, "creator": creator, "status": STATUS_OPEN,
            "due": due, "created": now, "updated": now, "done_at": "",
        }
        client.hset(self.item_key(todo_id), mapping=row)
        client.rpush(REDIS_KEY_GLOBAL, str(todo_id))
        return todo_id

    def _raw(self, todo_id: int) -> Dict[str, Any]:
        client = self._client()
        raw = client.hgetall(self.item_key(todo_id)) or {}
        out: Dict[str, Any] = {}
        for k, v in (raw or {}).items():
            key = _text(k)
            val = _text(v)
            out[key] = val
        return out

    def get(self, todo_id: int) -> Optional[Dict[str, Any]]:
        raw = self._raw(todo_id)
        return _norm_row(raw) if raw else None

    def set_status(self, todo_id: int, status: str) -> bool:
        raw = self._raw(todo_id)
        if not raw:
            return False
        client = self._client()
        now = _now_iso()
        client.hset(self.item_key(todo_id), "status", status)
        client.hset(self.item_key(todo_id), "updated", now)
        client.hset(self.item_key(todo_id), "done_at",
                    now if status == STATUS_DONE else "")
        return True

    def delete(self, todo_id: int) -> bool:
        client = self._client()
        raw = self._raw(todo_id)
        if not raw:
            return False
        client.delete(self.item_key(todo_id))
        client.lrem(REDIS_KEY_GLOBAL, 1, str(todo_id))
        return True

    def all_rows(self, owner: str = "", visible_to: str = "") -> List[Dict[str, Any]]:
        client = self._client()
        ids = [_text(i) for i in client.lrange(REDIS_KEY_GLOBAL, 0, -1)]
        out: List[Dict[str, Any]] = []
        for raw_id in ids:
            try:
                todo_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            row = self.get(todo_id)
            if not row:
                continue
            if owner and row.get("owner") != owner:
                continue
            if visible_to and visible_to not in (row.get("owner") or "",
                                                row.get("creator") or ""):
                continue
            out.append(row)
        return out

    def list_rows(self, owner: str, status: str, page: int, size: int,
                  visible_to: str = "") -> Tuple[List[Dict[str, Any]], int]:
        rows = self.all_rows(owner, visible_to)
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: int(r.get("id") or 0), reverse=True)
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total


# --------------------------------------------------------------------------
# 对外：一份语义，三后端
# --------------------------------------------------------------------------

class TodoStore:
    """待办存储。`TodoStore(storage)` 一份，三后端同一语义。

    **不自拼连接、不另开进程**：与 `Registry` / `Messaging` 同款 —— PG / SQLite
    走存储后端自己的事务游标，Redis 走 `_get_client()` 的既有连线。
    """

    def __init__(self, storage: Any) -> None:
        kind = _backend_of(storage)
        self.backend = kind
        self._be: Any = (_TodoSqlBackend(storage, kind) if kind != "redis"
                         else _RedisBackend(storage))
        self._ensured = False
        self._ensure_lock = threading.Lock()
        #: 建表失败后的**重试冷却**截止时刻（单调钟）。
        self._fail_until = 0.0

    # ---- 生命周期 ----------------------------------------------------
    def ensure_schema(self) -> bool:
        """确保 `ks_todo` 在（幂等；**成功 True / 失败 False**）。

        🔴 **失败后有冷却**（60s 内不重试 DDL）：建表 DDL 失败时每次都重试，
        等于每轮发一遍 DDL（PG 上建索引会拿锁、挡写）。
        """
        with self._ensure_lock:
            if self._ensured:
                return True
            if time.monotonic() < self._fail_until:
                return False
            try:
                ok = bool(self._be.ensure_schema())
            except Exception as e:      # noqa: BLE001 —— 返回 False，不抛
                logger.warning("keepsake: 待办 ensure_schema 失败（%s: %s）",
                               type(e).__name__, e)
                ok = False
            if ok:
                self._ensured = True
            else:
                self._fail_until = time.monotonic() + SCHEMA_RETRY_COOLDOWN_S
            return ok

    def _ready(self) -> None:
        """第一次用之前把表建好（失败 ⇒ 抛，让调用方知道「待办没写进去」）。"""
        if not self.ensure_schema():
            raise RuntimeError("待办 schema 未就绪（ensure_schema 失败）—— 待办未落库")

    # ---- 写 -----------------------------------------------------------
    def add(self, title: str, detail: str = "", owner: str = "",
            creator: str = "", due: str = "") -> int:
        """新增一条待办，返回 `id`。

        🔴 入参**先校验后落库**（标题/正文/到期日/状态四道闸）——
        库里不该出现「状态是第四态」或「到期日是乱的」的行。
        """
        t = check_title(title)
        d = check_detail(detail)
        dd = parse_due(due)
        self._ready()
        return int(self._be.add(t, d, str(owner or "").strip(),
                                 str(creator or "").strip(), dd))

    def set_status(self, todo_id: int, status: str) -> bool:
        """改状态。命中返回 True（不存在 ⇒ False）。"""
        st = check_status(status)
        self._ready()
        return bool(self._be.set_status(int(todo_id), st))

    def delete(self, todo_id: int) -> bool:
        """删一条待办。命中返回 True。"""
        self._ready()
        return bool(self._be.delete(int(todo_id)))

    # ---- 读 -----------------------------------------------------------
    def get(self, todo_id: int) -> Optional[Dict[str, Any]]:
        """取一条待办（不存在 ⇒ None）。"""
        self._ready()
        return self._be.get(int(todo_id))

    def list_rows(self, owner: str = "", status: str = "", scope: str = SCOPE_ALL,
                  page: int = 1, size: int = DEFAULT_LIMIT, visible_to: str = ""
                  ) -> Tuple[List[Dict[str, Any]], int]:
        """分页列表：`{"items": [...], "total": n}` 的两个半截。

        `status`（精确三态）优先于 `scope`（快捷档 open/done/all）；都空 ⇒ 全部状态。
        `visible_to` 非空 ⇒ 只给**看得见**的（owner = 它 ∪ creator = 它）。
        """
        st = _scope_status(status, scope)
        self._ready()
        return self._be.list_rows(str(owner or "").strip(), st,
                                  _clamp_page(page), _clamp_limit(size),
                                  str(visible_to or "").strip())

    def all_rows(self, owner: str = "", visible_to: str = "") -> List[Dict[str, Any]]:
        """全量行（升序 id）—— 可见性过滤与计数走这一份。"""
        self._ready()
        return list(self._be.all_rows(str(owner or "").strip(),
                                      str(visible_to or "").strip()))

    def summary(self, owner: str = "") -> Dict[str, Any]:
        """计数口径：`{open, done, obsolete, total, overdue}`。

        🔴 `overdue` = **开放且已过期**的条数（销掉的不算 —— 过期是活儿没干完，
        干完了就没什么好急的）。
        """
        rows = self.all_rows(owner)
        counts = {s: 0 for s in STATUSES}
        overdue = 0
        for row in rows:
            st = str(row.get("status") or STATUS_OPEN)
            if st in counts:
                counts[st] += 1
            if st == STATUS_OPEN and row.get("due_state") == "overdue":
                overdue += 1
        return {
            "open": counts[STATUS_OPEN],
            "done": counts[STATUS_DONE],
            "obsolete": counts[STATUS_OBSOLETE],
            "total": len(rows),
            "overdue": overdue,
        }

    def obsolete_suggestions(self, days: int = OBSOLETE_SUGGEST_DAYS,
                             today: Optional[date] = None,
                             owner: str = "") -> List[Dict[str, Any]]:
        """「建议废弃」提名清单（**只提名，不改状态**）。

        判据：**仍开放** + 到期日已过满 `days` 天（默认 7 天）。按到期日升序 ——
        欠得最久的排最前。
        """
        try:
            need = max(1, int(days))
        except (TypeError, ValueError):
            need = OBSOLETE_SUGGEST_DAYS
        base = today or _today()
        out: List[Dict[str, Any]] = []
        for row in self.all_rows(owner):
            if str(row.get("status") or "") != STATUS_OPEN:
                continue
            days_over = overdue_days(row.get("due"), base)
            if days_over < need:
                continue
            item = dict(row)
            item["overdue_days"] = days_over
            out.append(item)
        out.sort(key=lambda r: (str(r.get("due") or ""), -int(r.get("id") or 0)))
        return out


__all__ = [
    "TodoStore", "TodoError", "TODO_FIELDS", "TABLE", "INDEX",
    "STATUS_OPEN", "STATUS_DONE", "STATUS_OBSOLETE", "STATUSES",
    "SCOPE_OPEN", "SCOPE_DONE", "SCOPE_ALL", "SCOPES",
    "DUE_SOON_DAYS", "OBSOLETE_SUGGEST_DAYS", "TITLE_MAX", "DETAIL_MAX",
    "DEFAULT_LIMIT", "LIMIT_MAX", "SCHEMA_RETRY_COOLDOWN_S",
    "KS_TODO_DDL_PG", "KS_TODO_DDL_SQLITE", "KS_TODO_INDEX_SQL",
    "REDIS_KEY_GLOBAL", "REDIS_KEY_ITEM", "REDIS_KEY_SEQ",
    "due_state", "overdue_days", "parse_due", "check_status", "check_title",
    "check_detail", "can_access", "visible_rows",
]
