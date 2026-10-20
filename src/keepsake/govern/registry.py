"""代理注册表（`ks_agent`）—— 身份、角色、凭据、上下文的**唯一真相源**。

## 为什么要有它

修前每个代理**自报身份**：`is_primary` 来自各代理自己的 config
（`adapters/hermes/provider._resolve_config`），`agent_id` 缺省还能被兜底成
`mcp`。伪造一份 config = 冒充主代理 = 写闸形同虚设。本模块把这两件事收口：

| 事项 | 修前 | 修后 |
|---|---|---|
| 新代理接入 | 直接可用（自报即可） | `pending`，读写全拒，管理员批准才放行 |
| 谁是 primary | 各代理 config 自报 | 注册表角色，**全库唯一** |
| 进不进主脑上下文 | 全局 `retrieval.exclude_tags` | 每个代理身上的 `exclude_from_primary` |
| 凭据 | 无 | 批准时生成一次 `ksk_…`，库里只存 sha256 |

## 三后端一份语义

* PG / SQLite：`ks_agent` 行表，DDL 由 **本模块唯一定义**（`KS_AGENT_DDL`），
  两后端各自 `import` 它进自己的 `ensure_index` 建表机制 —— 建表语句复制两份
  必然漂，而漂的后果是「注册表在一侧永远不存在」。
* Redis：`keepsake:agent:<id>` hash（同库惯例，见 `core/redis.py` 的
  `keepsake:*` 各 key 常量），字段名与行表的列名一一对应。

对外形状（`list_agents()` / `get()` 的返回）三后端逐字段相同 —— 否则「换了
后端治理就失效」。

## 🔴 `set_role` 的「唯一 primary」怎么保证

PG / SQLite 是**真事务**（SQL 后端自带事务边界），两条 UPDATE 在同一事务内，
并发下也只有一个赢家。Redis **没有跨 key 事务**（本模块刻意不引 WATCH/MULTI：
那要求客户端支持 pipeline 语义，而本库对 Redis 的用法一律走简单命令 + 现有
假 client 测试也按简单命令编排）。取舍：改成**「先把全库 primary 降成 sub，
再把这一个提上来」的顺序写**（`REDIS_SET_ROLE_MODE = "demote-all-then-promote"`，
见 `set_role` 的 docstring）。后果要说清：并发两个进程同时置 primary 时，
可能出现**两个都是 sub**（先降的赢了、后者又把自己的降掉了）—— 但绝不可能
出现**两个 primary**。对治理来说「暂时没人是主脑」（多拦一次 core 写入、
fail-closed 可接受）远比「两个主脑同时写常驻」安全，方向选对即可。
`ponytail:` 真要严格唯一可换 Redis 的 `WATCH/MULTI/EXEC` 乐观事务
（代价：客户端要支持 pipeline 语义，本库的 Redis 假 client 测试按简单命令编排）。

## 🔴 凭据纪律

* 生成用 `secrets.token_hex`（32 hex ⇒ `ksk_` + 32 字符），**库里只存 sha256**；
* 明文**只**在 `approve()` 的返回值里出现一次，控制台只在批准响应里回显；
* `verify()` 失败的消息只说「凭据无效」，**绝不回显期望值 / 收到的值**；
* 本模块**不写任何日志**，也不把凭据交给日志器（唯一的出口是 approve 返回值）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("keepsake.govern.registry")

# --------------------------------------------------------------------------
# 常量（口径单点：别处引用这里的名字，不要复制字面量）
# --------------------------------------------------------------------------

ROLE_PRIMARY = "primary"
ROLE_SUB = "sub"
ROLES: Tuple[str, ...] = (ROLE_PRIMARY, ROLE_SUB)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REVOKED = "revoked"
STATUSES: Tuple[str, ...] = (STATUS_PENDING, STATUS_APPROVED, STATUS_REVOKED)

#: 凭据形态：`ksk_` + 32 hex。它出现在代码里时**只是前缀常量**，
#: 永远不是一个真串（G4 静态闸会拦 `ksk_` 后跟 20+ 位十六进制的行）。
CREDENTIAL_PREFIX = "ksk_"
_CREDENTIAL_HEX_LEN = 32

#: `credential_hint` 取凭据前 8 位，供控制台比对「我手上这条对不对」。
#: 🔴 只够人眼认，绝不足以被拿来当凭据用（8 位 hex = 32 bit，且它本来就是公开的）。
CREDENTIAL_HINT_LEN = 8

#: 迁移器认的标签前缀（`retrieval.exclude_tags` 里的 `agent:<id>` 条目）
AGENT_TAG_PREFIX = "agent:"

#: 迁移时**故意忽略**的值：`core_candidate` 的默认排除由审核引擎语义承担
#: （见 `core/tiering.py` 模块 docstring），不迁进注册表。
IGNORED_EXCLUDE_VALUES = ("core_candidate",)

#: 已退役的配置键（迁移器只读、**不改用户 config 文件**）
RETIRED_EXCLUDE_KEY = "retrieval.exclude_tags"

#: `touch()` 的限频：同一代理 60s 内只写库一次（活动计数是遥测，不是账本）
TOUCH_MIN_INTERVAL_S = 60.0

TABLE = "ks_agent"

#: Redis 侧 key 前缀（`keepsake:agent:<id>`，与本库其余 key 同命名空间）
REDIS_KEY_PREFIX = "keepsake:agent:"

#: Redis `set_role` 的唯一性方案（写进 `set_role` 的日志与返回，便于事后审计）
REDIS_SET_ROLE_MODE = "demote-all-then-promote"

#: 列 / 字段名（SQL 行与 Redis hash 共用同一份名字 ⇒ 规范化只有一处）
AGENT_FIELDS: Tuple[str, ...] = (
    "agent_id", "name", "role", "status", "credential_hash", "credential_hint",
    "first_seen_at", "last_seen_at", "reads", "writes", "exclude_from_primary",
    "meta",
)

#: 计数与布尔列（SQL 里是 integer，Redis hash 里存字符串 —— 规范化时统一成 int/bool）
_INT_FIELDS = ("reads", "writes")
_FLAG_FIELDS = ("exclude_from_primary",)

# 🔴 **`ks_agent` DDL 的唯一定义处**（`core/pg.py` 与 `core/sqlite.py` 各自
# import 它进自己的建表机制）。列全用 `text`（时刻按本库惯用的 ISO 字符串存，
# 与 `ks_fragment.created` / `ks_hit_event.ts` 同口径），所以**两个后端逐字
# 共用这一段**；计数器用 `integer`（SQLite 的 INTEGER / PG 的 integer 同名同义）。
KS_AGENT_DDL = """
CREATE TABLE IF NOT EXISTS ks_agent (
    agent_id             text PRIMARY KEY,
    name                 text NOT NULL DEFAULT '',
    role                 text NOT NULL DEFAULT 'sub',
    status               text NOT NULL DEFAULT 'pending',
    credential_hash      text NOT NULL DEFAULT '',
    credential_hint      text NOT NULL DEFAULT '',
    first_seen_at        text NOT NULL DEFAULT '',
    last_seen_at         text NOT NULL DEFAULT '',
    reads                integer NOT NULL DEFAULT 0,
    writes               integer NOT NULL DEFAULT 0,
    exclude_from_primary integer NOT NULL DEFAULT 0,
    meta                 text NOT NULL DEFAULT '{}'
)
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_credential() -> str:
    """生成一条一次性凭据（`ksk_` + 32 hex）。**明文只许流回 approve 的返回值**。"""
    return f"{CREDENTIAL_PREFIX}{secrets.token_hex(_CREDENTIAL_HEX_LEN // 2)}"


def hash_credential(credential: str) -> str:
    """凭据 → sha256 十六进制（库里存的就是它，比对用常数时间）。"""
    return hashlib.sha256((credential or "").encode("utf-8")).hexdigest()


def credential_hint(credential: str) -> str:
    """凭据前 8 位（面板比对用；不是凭据，也不能拿来当凭据）。"""
    return (credential or "")[:CREDENTIAL_HINT_LEN]


# --------------------------------------------------------------------------
# 行规范化：三后端 → 同一份 dict（对外形状只在这里定一次）
# --------------------------------------------------------------------------

def _norm(raw: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """一行（SQL tuple→dict 或 Redis hash→dict）→ 规范 dict；缺列补缺省。"""
    if raw is None:
        return None
    row: Dict[str, Any] = {f: raw.get(f) for f in AGENT_FIELDS}
    row["agent_id"] = str(row["agent_id"] or "")
    if not row["agent_id"]:
        return None
    for f in ("name", "role", "status", "credential_hash", "credential_hint",
              "first_seen_at", "last_seen_at"):
        row[f] = str(row.get(f) or "")
    for f in _INT_FIELDS:
        try:
            row[f] = int(row.get(f) or 0)
        except (TypeError, ValueError):
            row[f] = 0
    for f in _FLAG_FIELDS:
        row[f] = str(row.get(f) or "0").strip() not in ("", "0", "false", "False")
    meta = row.get("meta")
    if isinstance(meta, dict):
        row["meta"] = meta
    else:
        try:
            parsed = json.loads(str(meta or "{}"))
        except (TypeError, ValueError):
            parsed = {}
        row["meta"] = parsed if isinstance(parsed, dict) else {}
    # 脏数据不许悄悄当权威：角色/状态只认枚举内的值
    if row["role"] not in ROLES:
        row["role"] = ROLE_SUB
    if row["status"] not in STATUSES:
        row["status"] = STATUS_PENDING
    return row


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    """对外形状（控制台 GET 与判定方共用的字段集）。

    🔴 **不含 `credential_hash`**：注册表的对外读面永远不回显凭据摘要，
    更不含明文（明文只在 approve 的一次性响应里）。
    """
    return {
        "agent_id": row["agent_id"],
        "name": row["name"],
        "role": row["role"],
        "status": row["status"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "reads": row["reads"],
        "writes": row["writes"],
        "exclude_from_primary": row["exclude_from_primary"],
        "credential_hint": row["credential_hint"],
        "meta": row["meta"],
    }


# --------------------------------------------------------------------------
# 后端适配：只有「怎么存取」不同，语义全部在 Registry 里
# --------------------------------------------------------------------------

def _backend_of(storage: Any) -> str:
    """按能力判后端（不认类名 —— 测试的替身与真存储同形）。

    * 有 `_tx` / `_ro`（psycopg 事务游标）→ postgres
    * 有 `_write` / `_db`（sqlite3）      → sqlite
    * 其余（只有 `_get_client`）           → redis
    """
    if callable(getattr(storage, "_tx", None)) and callable(getattr(storage, "_ro", None)):
        return "postgres"
    if callable(getattr(storage, "_write", None)) and callable(getattr(storage, "_db", None)):
        return "sqlite"
    return "redis"


class _SqlBackend:
    """PG / SQLite 共用：SQL 只差**占位符**与事务入口，其余逐字相同。"""

    def __init__(self, storage: Any, kind: str) -> None:
        self._st = storage
        self.kind = kind
        self.ph = "%s" if kind == "postgres" else "?"

    # -- 原语 ----------------------------------------------------------
    def ensure_schema(self) -> bool:
        """建表走**各后端自己的 ensure 机制**（本模块不自发 DDL）。"""
        return bool(self._st.ensure_index())

    def read(self, sql: str, params: Tuple[Any, ...] = ()) -> List[tuple]:
        if self.kind == "postgres":
            with self._st._ro() as cur:      # noqa: SLF001 —— 经原后端连线，不另开连接
                cur.execute(sql, params)
                return list(cur.fetchall())
        with self._st._lock:                  # noqa: SLF001
            cur = self._st._db().cursor()     # noqa: SLF001
            cur.execute(sql, params)
            return list(cur.fetchall())

    def write(self, fn: Any) -> Any:
        """在一个事务里跑 `fn(cur)`（PG 走 `_tx`，SQLite 走 `_write` 的 BEGIN IMMEDIATE）。"""
        ctx = self._st._tx() if self.kind == "postgres" else self._st._write()  # noqa: SLF001
        with ctx as cur:
            return fn(cur)

    # -- ks_agent 专用 --------------------------------------------------
    def load(self, agent_id: str) -> Optional[Dict[str, Any]]:
        rows = self.read(
            f"SELECT {', '.join(AGENT_FIELDS)} FROM {TABLE} WHERE agent_id = {self.ph}",
            (agent_id,),
        )
        return _norm(dict(zip(AGENT_FIELDS, rows[0]))) if rows else None

    def all_rows(self) -> List[Dict[str, Any]]:
        rows = self.read(f"SELECT {', '.join(AGENT_FIELDS)} FROM {TABLE}")
        return [r for r in (_norm(dict(zip(AGENT_FIELDS, row))) for row in rows) if r]

    def insert(self, row: Dict[str, Any]) -> None:
        """插入一行（**仅当不存在**；`ON CONFLICT DO NOTHING` 是幂等 register 的落点）。"""
        cols = ", ".join(AGENT_FIELDS)
        marks = ", ".join(self.ph for _ in AGENT_FIELDS)
        self.write(lambda cur: cur.execute(
            f"INSERT INTO {TABLE} ({cols}) VALUES ({marks}) ON CONFLICT (agent_id) DO NOTHING",
            tuple(_sql_value(f, row[f]) for f in AGENT_FIELDS),
        ))

    def update(self, agent_id: str, fields: Dict[str, Any]) -> None:
        if not fields:
            return
        sets = ", ".join(f"{f} = {self.ph}" for f in fields)
        self.write(lambda cur: cur.execute(
            f"UPDATE {TABLE} SET {sets} WHERE agent_id = {self.ph}",
            (*(_sql_value(f, v) for f, v in fields.items()), agent_id),
        ))

    def set_role_unique(self, agent_id: str, role: str) -> None:
        """全库唯一 primary —— **两条 UPDATE 在同一事务内**（并发只有一个赢家）。"""
        def _fn(cur: Any) -> None:
            cur.execute(
                f"UPDATE {TABLE} SET role = {self.ph} "
                f"WHERE role = {self.ph} AND agent_id <> {self.ph}",
                (ROLE_SUB, ROLE_PRIMARY, agent_id),
            )
            cur.execute(
                f"UPDATE {TABLE} SET role = {self.ph} WHERE agent_id = {self.ph}",
                (role, agent_id),
            )

        self.write(_fn)

    def bump(self, agent_id: str, kind: str) -> None:
        self.write(lambda cur: cur.execute(
            f"UPDATE {TABLE} SET {kind} = COALESCE({kind}, 0) + 1, last_seen_at = {self.ph} "
            f"WHERE agent_id = {self.ph}",
            (_now_iso(), agent_id),
        ))

    def rename_id(self, old_id: str, new_id: str) -> bool:
        """换主键：`agent_id` 从 old 改成 new，**其余列一个都不动**。

        🔴 凭据哈希原样保留 ⇒ 已发出去的凭据继续有效（换 id 不需要客户端重配）。

        主键冲突（并发下新 id 被别人抢走）⇒ 换名失败（False），**绝不覆盖** ——
        覆盖会把别人的行洗白，还可能留下两行同凭据。
        """
        def _fn(cur: Any) -> bool:
            cur.execute(
                f"UPDATE {TABLE} SET agent_id = {self.ph} WHERE agent_id = {self.ph}",
                (new_id, old_id),
            )
            return cur.rowcount > 0

        try:
            return bool(self.write(_fn))
        except Exception:
            # 只有「新 id 已经存在」才当失败吞掉；其它异常照旧上抛（不把故障说成冲突）
            if self.load(new_id):
                logger.warning("keepsake: 换 agent_id 撞上并发占用：%s", new_id)
                return False
            raise


class _RedisBackend:
    """Redis：`keepsake:agent:<id>` hash。字段名与 `AGENT_FIELDS` 同名。"""

    kind = "redis"

    def __init__(self, storage: Any) -> None:
        self._st = storage

    @staticmethod
    def key(agent_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}{agent_id}"

    def _client(self) -> Any:
        client = self._st._get_client()      # noqa: SLF001 —— 复用既有连线（不可达返回 None）
        if client is None:
            raise RuntimeError("registry: Redis 不可达（_get_client 返回 None）")
        return client

    def _decode(self, raw: Dict[Any, Any]) -> Dict[str, Any]:
        return {_text(k): _text(v) for k, v in (raw or {}).items()}

    def ensure_schema(self) -> bool:
        return True          # hash 形态没有 schema；Redis 不可达也不在这里拦（读时自然报错）

    def load(self, agent_id: str) -> Optional[Dict[str, Any]]:
        raw = self._client().hgetall(self.key(agent_id))
        return _norm(self._decode(raw)) if raw else None

    def all_rows(self) -> List[Dict[str, Any]]:
        client = self._client()
        out: List[Dict[str, Any]] = []
        for k in client.scan_iter(match=f"{REDIS_KEY_PREFIX}*"):
            raw = client.hgetall(_text(k))
            row = _norm(self._decode(raw))
            if row:
                out.append(row)
        return out

    def insert(self, row: Dict[str, Any]) -> None:
        client = self._client()
        if client.exists(self.key(row["agent_id"])):
            return                       # 幂等：已存在就一个字段都不动（不碰 status）
        client.hset(self.key(row["agent_id"]),
                    mapping={f: _hash_field(row[f]) for f in AGENT_FIELDS})

    def update(self, agent_id: str, fields: Dict[str, Any]) -> None:
        if not fields:
            return
        self._client().hset(self.key(agent_id),
                            mapping={f: _hash_field(v) for f, v in fields.items()})

    def set_role_unique(self, agent_id: str, role: str) -> None:
        """「先降全库、再提一个」的**顺序写**（Redis 无跨 key 事务，取舍见模块 docstring）。

        🟡 并发下可能两个都是 sub，但绝不可能两个都是 primary —— 治理要的是后者绝不许发生。
        """
        client = self._client()
        for row in self.all_rows():
            if row["role"] == ROLE_PRIMARY and row["agent_id"] != agent_id:
                client.hset(self.key(row["agent_id"]), "role", ROLE_SUB)
        client.hset(self.key(agent_id), "role", role)

    def bump(self, agent_id: str, kind: str) -> None:
        client = self._client()
        client.hincrby(self.key(agent_id), kind, 1)
        client.hset(self.key(agent_id), "last_seen_at", _now_iso())

    def rename_id(self, old_id: str, new_id: str) -> bool:
        """换主键：`keepsake:agent:<old>` 这个 hash 整个搬到 `<new>` 下。

        🔴 **用 Redis 原生的 `RENAMENX`**（原子：新 key 已存在就返回 0，不覆盖）：
        「HGETALL → HSET 新键 → DEL 旧键」这种三步写法，中间崩一次就会留下
        **两行同凭据**（新键已写、旧键未删）—— 那正是本模块要防的形态；
        `RENAMENX` 一步到位，key 布局与 hash 字段一个字节都不改。

        `RENAMENX` 只搬 key，不改 hash **字段** ⇒ 搬完补一次 `agent_id` 字段
        （读面是从字段取 id 的，字段不补就会读出旧 id）。
        """
        client = self._client()
        if not client.exists(self.key(old_id)):
            return False
        if not client.renamenx(self.key(old_id), self.key(new_id)):
            return False          # 新 id 已被占用 ⇒ 换名失败，绝不覆盖
        client.hset(self.key(new_id), "agent_id", new_id)
        return True


def _text(value: Any) -> str:
    """bytes/str → str（Redis 客户端 `decode_responses=False`，读出来是 bytes）。"""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return "" if value is None else str(value)


def _hash_field(value: Any) -> Any:
    """写进 Redis hash 的值形态（int / bool / dict 统一成字符串，读时 `_norm` 再转回来）。"""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _sql_value(field: str, value: Any) -> Any:
    """写进 `ks_agent` 行表的列值（`meta` 落 JSON 文本；其余都是标量）。"""
    if field == "meta":
        return json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False)
    if isinstance(value, bool):
        return 1 if value else 0
    return value


# --------------------------------------------------------------------------
# 注册表本体
# --------------------------------------------------------------------------

class Registry:
    """代理注册表。`Registry(storage)` 一份，三后端同一语义（同步、经后端原连线）。

    **不自拼连接、不另开进程**：PG / SQLite 走存储后端自己的事务游标，Redis 走
    `_get_client()` 的既有连线 —— 这样「控制台进程改了角色、主脑进程下一次写闸
    就收敛」不需要任何跨进程协议。
    """

    def __init__(self, storage: Any) -> None:
        kind = _backend_of(storage)
        self.backend = kind
        self._be: Any = _SqlBackend(storage, kind) if kind != "redis" else _RedisBackend(storage)
        # `touch()` 的限频表按实例存（进程内遥测，重启归零是正常的）
        self._last_touch: Dict[str, float] = {}
        self._touch_lock = threading.Lock()

    # ---- 生命周期 ----------------------------------------------------
    def ensure_schema(self) -> bool:
        """确保 `ks_agent` 在（SQL 后端经**自己的** ensure 机制建表；Redis 无 schema）。"""
        try:
            return self._be.ensure_schema()
        except Exception as e:      # noqa: BLE001 —— 返回 False，不抛（调用方决定降级）
            logger.warning("keepsake: 注册表 ensure_schema 失败（%s: %s）",
                           type(e).__name__, e)
            return False

    # ---- 读 ----------------------------------------------------------
    def list_agents(self) -> List[Dict[str, Any]]:
        """全部代理（按 agent_id 排序；空库返回 `[]`，不是 None）。"""
        rows = self._be.all_rows()
        return [_public(r) for r in sorted(rows, key=lambda r: r["agent_id"])]

    def get(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """单个代理（对外形状）；不存在返回 None。"""
        row = self._be.load((agent_id or "").strip())
        return _public(row) if row else None

    def pending_count(self) -> int:
        return sum(1 for r in self._be.all_rows() if r["status"] == STATUS_PENDING)

    def role_of(self, agent_id: str) -> str:
        """角色（`primary` / `sub`）；查不到一律 sub（fail-closed 同款）。"""
        row = self._be.load((agent_id or "").strip())
        return row["role"] if row else ROLE_SUB

    # ---- 登记 --------------------------------------------------------
    def register(self, agent_id: str, name: str = "") -> Dict[str, Any]:
        """登记一个代理（**幂等**）：新代理 = `pending`；已有的一律不动。

        刻意不碰已有行的任何字段（尤其 status / role）：进程每轮启动都 register，
        若这里会刷新 status，管理员的一次吊销就会被下次启动悄悄洗白。
        """
        aid = (agent_id or "").strip()
        if not aid:
            raise ValueError("agent_id 不能为空")
        now = _now_iso()
        self._be.insert({
            "agent_id": aid,
            "name": name or aid,
            "role": ROLE_SUB,
            "status": STATUS_PENDING,
            "credential_hash": "",
            "credential_hint": "",
            "first_seen_at": now,
            "last_seen_at": now,
            "reads": 0,
            "writes": 0,
            "exclude_from_primary": False,
            "meta": {},
        })
        return self.get(aid) or {}

    # ---- 批准 / 吊销 / 角色 / 上下文开关 -------------------------------
    def approve(self, agent_id: str) -> Optional[str]:
        """批准并**生成一条一次性凭据**，返回明文；代理不存在返回 None。

        明文**只在这里出现一次**（返回值 = 控制台的响应体），库里只落 sha256。
        重新 approve 会换一条新凭据（旧的随即失效）—— 补发凭据走的就是这条。
        """
        aid = (agent_id or "").strip()
        if not self._be.load(aid):
            return None
        credential = new_credential()
        self._be.update(aid, {
            "status": STATUS_APPROVED,
            "credential_hash": hash_credential(credential),
            "credential_hint": credential_hint(credential),
        })
        return credential

    def revoke(self, agent_id: str) -> bool:
        """吊销（随时可做）：`status=revoked` 且**清掉凭据哈希**（旧凭据立即作废）。"""
        aid = (agent_id or "").strip()
        if not self._be.load(aid):
            return False
        self._be.update(aid, {"status": STATUS_REVOKED,
                              "credential_hash": "", "credential_hint": ""})
        return True

    def set_role(self, agent_id: str, role: str) -> List[str]:
        """设角色；置 primary 时**其余 primary 自动降 sub**。返回被降级者 id 列表。

        未知角色 ⇒ `ValueError`（调用方 400，不静默当 sub 写进去）。
        """
        aid = (agent_id or "").strip()
        want = (role or "").strip().lower()
        if want not in ROLES:
            raise ValueError(f"role 只能是 {ROLES}，收到 {role!r}")
        if not self._be.load(aid):
            raise KeyError(aid)
        demoted = [r["agent_id"] for r in self._be.all_rows()
                   if r["role"] == ROLE_PRIMARY and r["agent_id"] != aid]
        self._be.set_role_unique(aid, want)
        return demoted if want == ROLE_PRIMARY else []

    def set_exclude(self, agent_id: str, exclude: bool) -> bool:
        """「这个代理的条目进不进主脑上下文」开关。返回**值是否真的变了**。"""
        aid = (agent_id or "").strip()
        row = self._be.load(aid)
        if not row:
            return False
        flag = bool(exclude)
        if row["exclude_from_primary"] == flag:
            return False
        self._be.update(aid, {"exclude_from_primary": flag})
        return True

    def rename(self, agent_id: str, name: str) -> bool:
        """改**备注名**（只改 `name` 一个字段）。返回**值是否真的变了**。

        🔴 `status` / `role` / `credential_hash` / `credential_hint` / `meta`
        **一律不碰**：改名是给人看的标签，不是治理动作 —— 它既不该放行一个
        pending 代理，也不该作废一条已发的凭据。行不存在 ⇒ False（调用方 404）。
        """
        aid = (agent_id or "").strip()
        row = self._be.load(aid)
        if not row:
            return False
        new_name = (name or "").strip()
        if row["name"] == new_name:
            return False
        self._be.update(aid, {"name": new_name})
        return True

    # ---- 改 id（换主键 + 归属级联） ----------------------------------
    def set_id(self, old_id: str, new_id: str) -> bool:
        """改 `agent_id`（换主键）：**整行**搬到新 id 下，旧 id 消失。

        🔴 **唯一性判断是本方法的第一职责**：行不存在 / 新 id 已存在 / 新旧同串
        ⇒ 一律返回 False（**不抛**，路由层据此给区分文案）。路由层的预检只算
        体验，**最终以这里为准** —— 并发下不会出现两个代理占同一个 id，
        更不会留下两行同凭据。

        🔴 **凭据原样搬**：`credential_hash` / `credential_hint` 一个都不动 ⇒
        已发出去的凭据继续有效（http 客户端身份由凭据反查 `verify_any`，
        换 id 不需要客户端重配）。

        🔴 **记忆归属不在本方法里搬**：碎片的归属是 `tags` 里的 `agent:<id>`
        那个词，搬它要走存储层的 `reassign_agent`（还要重算 BM25 索引），
        由路由层在换名成功后接着调 —— 两件事分开做，失败时能分清是哪一步坏了。
        """
        old = (old_id or "").strip()
        new = (new_id or "").strip()
        if not old or not new or old == new:
            return False
        if not self._be.load(old):
            return False
        if self._be.load(new):
            return False
        return self._be.rename_id(old, new)

    # ---- 凭据校验 ----------------------------------------------------
    def verify(self, agent_id: str, credential: str) -> Dict[str, Any]:
        """凭据校验 → `{ok, role, status, reason}`。**pending / revoked 一律拒**。

        🔴 失败消息**不回显任何凭据内容**（收到的、期望的都不行）—— 只给
        「未批准 / 已吊销 / 凭据无效」这三种人话。
        """
        aid = (agent_id or "").strip()
        row = self._be.load(aid) if aid else None
        if not row:
            return {"ok": False, "role": ROLE_SUB, "status": "unknown",
                    "reason": "unknown_agent"}
        if row["status"] != STATUS_APPROVED:
            return {"ok": False, "role": row["role"], "status": row["status"],
                    "reason": "not_approved"}
        got = hash_credential(credential or "")
        if not row["credential_hash"] or not hmac.compare_digest(got, row["credential_hash"]):
            return {"ok": False, "role": row["role"], "status": row["status"],
                    "reason": "bad_credential"}
        return {"ok": True, "role": row["role"], "status": row["status"], "reason": ""}

    def verify_local(self, agent_id: str) -> Dict[str, Any]:
        """**本机文件通道**（主脑进程内）：不经凭据，只认 `meta.bootstrap='local'` 且已批准。

        这条通道只给「provider 与存储同机同进程」的主脑自己（`bootstrap_local`
        写的那一行）。MCP / 远端客户端**一律没有**这个待遇：它们走 `verify()`。
        """
        row = self._be.load((agent_id or "").strip())
        if not row:
            return {"ok": False, "role": ROLE_SUB, "status": "unknown", "reason": "unknown_agent"}
        local = row["meta"].get("bootstrap") == "local"
        if not local:
            return {"ok": False, "role": row["role"], "status": row["status"],
                    "reason": "not_local"}
        if row["status"] != STATUS_APPROVED:
            return {"ok": False, "role": row["role"], "status": row["status"],
                    "reason": "not_approved"}
        return {"ok": True, "role": row["role"], "status": row["status"], "reason": ""}

    def verify_any(self, credential: str) -> Dict[str, Any]:
        """只知道凭据、不知道 agent_id 时**按凭据反查**（http 传输头里只有 Bearer）。

        遍历全部代理比对哈希 —— 代理数是**个位数量级**（注册表是治理面，不是用户面），
        一次全表读 + 几次 sha256 比对，比维护一张 `credential_hash → agent_id` 的
        反向索引便宜得多（那张索引一旦与主表漂移，就是「凭据认错人」的故障）。
        `ponytail:` 真到几百个代理再上反查索引（一个 hash field 就够）。
        """
        out: Dict[str, Any] = {"ok": False, "role": ROLE_SUB, "status": "unknown",
                               "reason": "unknown_agent", "agent_id": ""}
        if not credential:
            out["reason"] = "no_credential"
            return out
        got = hash_credential(credential)
        for row in self._be.all_rows():
            if row["credential_hash"] and hmac.compare_digest(got, row["credential_hash"]):
                res = self.verify(row["agent_id"], credential)
                res["agent_id"] = row["agent_id"]
                return res
        return out

    # ---- 活动计数 ----------------------------------------------------
    def touch(self, agent_id: str, kind: str = "read") -> bool:
        """打活动（`read` / `write`）；**限频 60s**，返回本次是否真的写了库。

        限频理由：检索路径每轮都可能调它，而 `last_seen_at` / `reads` 是遥测，
        不是账本 —— 每轮写库会把注册表变成热路径上的写放大点。
        """
        aid = (agent_id or "").strip()
        column = "writes" if str(kind).lower().startswith("w") else "reads"
        with self._touch_lock:
            last = self._last_touch.get(aid)
            now = time.monotonic()
            if last is not None and now - last < TOUCH_MIN_INTERVAL_S:
                return False
            self._last_touch[aid] = now
        self._be.bump(aid, column)
        return True

    # ---- 主脑信任通道（本机文件 bootstrap） --------------------------
    def bootstrap_local(self, agent_id: str, name: str = "", *,
                         as_primary: bool = False) -> Dict[str, Any]:
        """主脑进程初始化时的自登记：**批准（+ 条件性置 primary）**。

        🔴 **只在「provider 与存储同机同进程」时调用**（MCP / 远端客户端一律
        没有这条通道，走 `verify()`）。三条纪律：

        1. `as_primary=True`（主脑 config 自己声明 `is_primary`）**且库里还没有
           primary** ⇒ 才置 primary；已有 primary 时**不再自动上位**
           （防第二个主脑静默上位），只 `logger.error` —— 冲突必须看得见。
           `as_primary=False`（MCP / 子代理进程）⇒ **只批准，不上位**：它能读
           记忆，但它写 `core` 仍按 sub 降级成候选。
        2. 已被吊销的行**不复活**（吊销的语义是「停到人重新批准为止」，否则
           每个进程重启都会把管理员的决定洗白）。
        3. 这一行带 `meta.bootstrap='local'`，是 `verify_local()` 的唯一凭据。
        """
        aid = (agent_id or "").strip()
        if not aid:
            raise ValueError("agent_id 不能为空")
        self.register(aid, name or aid)
        row = self._be.load(aid)
        assert row is not None                      # register 刚建好/确认存在
        if row["status"] == STATUS_REVOKED:
            logger.error(
                "keepsake: 代理 %s 已被吊销，本机 bootstrap 不复活它（请到控制台重新批准）", aid)
            return _public(row)
        others = [r["agent_id"] for r in self._be.all_rows()
                  if r["role"] == ROLE_PRIMARY and r["agent_id"] != aid]
        fields: Dict[str, Any] = {
            "status": STATUS_APPROVED,
            "meta": dict(row["meta"], bootstrap="local"),
        }
        if as_primary and not others:
            fields["role"] = ROLE_PRIMARY
        elif as_primary:
            logger.error(
                "keepsake: 注册表里已有 primary %s —— %s 只批准、不上位（角色请到控制台改）",
                others, aid)
        self._be.update(aid, fields)
        return self.get(aid) or {}

    # ---- 迁移器（retrieval.exclude_tags → 注册表） --------------------
    def migrate_exclude_tags(self, cfg: Any) -> Dict[str, Any]:
        """把 `retrieval.exclude_tags` 里的 `agent:<id>` 条目迁成对应代理的 exclude 标。

        * **只读**用户 config：绝不删 / 改写那个键（写 config 是治理端点的事，
          迁移器擅自改文件 = 越权）；
        * `core_candidate` 之类的非 `agent:` 值**忽略**（候选层的默认排除由
          审核引擎语义承担，见 `core/tiering.py`）；
        * **幂等**：二次运行不重复登记、不重复写（`changed` 为空）。
        """
        report: Dict[str, Any] = {
            "retired_key": RETIRED_EXCLUDE_KEY,
            "seen": [], "migrated": [], "changed": [], "ignored": [],
        }
        raw = ""
        if isinstance(cfg, dict):
            retrieval = cfg.get("retrieval")
            if isinstance(retrieval, dict):
                raw = str(retrieval.get("exclude_tags") or "")
        for tag in (t.strip() for t in raw.split(",")):
            if not tag:
                continue
            if tag in IGNORED_EXCLUDE_VALUES:
                report["ignored"].append(tag)
                continue
            if not tag.startswith(AGENT_TAG_PREFIX):
                report["ignored"].append(tag)
                continue
            aid = tag[len(AGENT_TAG_PREFIX):].strip()
            if not aid:
                report["ignored"].append(tag)
                continue
            report["seen"].append(aid)
            self.register(aid, aid)             # 先登记（值就是 pending），再打 exclude
            if self.set_exclude(aid, True):
                report["changed"].append(aid)
        if report["seen"]:
            report["migrated"] = list(report["seen"])
            logger.warning(
                "keepsake: 配置键 %s 已退役 —— %d 个 agent 条目已迁注册表（%s）。"
                "该键保留在 config 里但不再被读取（迁移器不写用户的配置文件）",
                RETIRED_EXCLUDE_KEY, len(report["migrated"]), ", ".join(report["migrated"]))
        return report
