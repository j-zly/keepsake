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

from . import credcrypto

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
    "meta", "dm_enabled", "share_memory", "share_kb", "core_budget_chars", "push_enabled",
    # 凭据密文列（可还原存档）；这里写**字面量**不用 ENC_COLUMN —— AGENT_FIELDS 定义
    # 在常量之前，用名字会在 import 期 NameError（与 dm_enabled 等同一条规矩）。
    "credential_enc",
)

#: `dm_enabled` 的**开**值（列是 text，Redis hash 字段也存字符串）。
#: 关掉的值一律认这几种（大小写都收）；其余（含空 / 缺列）都当**开**——
#: 「新增字段必须兼容旧数据」：老行没有这一列 ⇒ 默认开。
DM_ENABLED_ON = "1"
DM_DISABLED_VALUES: Tuple[str, ...] = ("0", "false", "False", "no", "off")

#: `dm_enabled` 的列名与列声明（建表与补列共用同一份 ⇒ 两边不会漂）。
DM_COLUMN = "dm_enabled"
DM_COLUMN_DECL = "text NOT NULL DEFAULT '1'"

#: `share_memory` 的**开**值（列是 text，Redis hash 字段也存字符串）。
#: 🔴 与 `dm_enabled` **相反**：缺省是**关**（'0'）——「记忆共享」是**放宽**权限的
#: 开关，缺省必须是最严的那一档（老行没有这一列 ⇒ 一律当关，语义不变）。
SHARE_ENABLED_ON = "1"
SHARE_DISABLED_ON = "0"
#: 认作「开」的值（其余一切（含空 / 缺列）都当关）。
SHARE_ENABLED_VALUES: Tuple[str, ...] = ("1", "true", "yes", "on")

#: `share_memory` 的列名与列声明（建表与补列共用同一份 ⇒ 两边不会漂）。
SHARE_COLUMN = "share_memory"
SHARE_COLUMN_DECL = "text NOT NULL DEFAULT '0'"

#: `share_kb` 的列名与列声明（建表与补列共用同一份 ⇒ 两边不会漂）。
#: 🔴 与 `share_memory` **同一档**：缺省是**关**（'0'）——「共享知识库」也是
#: **放宽**权限的开关（别的代理能检索到它名下的知识库文档），缺省必须最严；
#: 老库补列时也落 '0' ⇒ 存量行的语义不变（谁也没被共享出去过）。
#: 「开」的值集复用 `SHARE_ENABLED_VALUES`（两个旗同一把尺子）。
SHARE_KB_COLUMN = "share_kb"
SHARE_KB_COLUMN_DECL = "text NOT NULL DEFAULT '0'"

#: `credential_enc` 的列名与列声明（建表与老库补列共用同一份 ⇒ 两边不会漂）。
#: 🔴 缺省 **NULL**（不是 ''）：这一列存在与否代表「这条凭据能不能还原成明文」，
#: 机制上线前的老行必须落 NULL ⇒ 面板如实显示「不可还原」，而不是假装可还原又取不出。
#: 声明里**不带 NOT NULL / DEFAULT** ⇒ PG 加可空无默认列是元数据操作，不重写整表
#: （与 `core_budget_chars` 同一档做法）。
ENC_COLUMN = "credential_enc"
ENC_COLUMN_DECL = "text NULL"

# ===========================================================================
# 多凭据（`ks_credential`）—— 2026-11-14 用户拍板
# ===========================================================================
#: 一个代理可以同时持有多把凭据：多台机器 / 多个进程各拿一把，能**单独作废**、
#: 能**逐把看用量**（用户原话：「一个代理多个凭证，我只是想对一个代理进来的多个
#: 代理进行监管」）。
#: 🔴 这**不是**身份隔离：凭据只是入口，身份与记忆归属仍是 `agent_id`，多把共用
#:    同一个记忆池（用户已确认「记忆都是算一起的这个不变」）。要独立记忆就注册独立代理。
TABLE_CREDENTIAL = "ks_credential"

#: `cred_id` 长度（十六进制）。它只是**定位与展示用的编号，不是秘密**，
#: 可以进 URL、进审计、进界面；真正的秘密只有 `credential_hash` 对应的那把明文。
CRED_ID_HEX_LEN = 12

#: 标签长度上限（防把整段说明塞进列表行）。
CRED_LABEL_MAX_LEN = 40

#: 批准时自动签发的那一把 / 老三列迁移过来的那一把的默认标签。
CRED_LABEL_DEFAULT = "默认"

CRED_STATUS_ON = "active"
CRED_STATUS_OFF = "revoked"

#: 列序（读与写共用同一份 ⇒ 不会漂），含密文列名（与代理表同一个 `ENC_COLUMN`）。
CRED_FIELDS: Tuple[str, ...] = (
    "cred_id", "agent_id", "label", "credential_hash", "credential_hint",
    ENC_COLUMN, "status", "created_at", "used_at", "reads", "writes",
)

#: 🔴 自本表起，`ks_agent` 上的 `credential_hash` / `credential_hint` /
#: `credential_enc` **三列冻结**：新代码一律不读不写，只作「退回旧版二进制」的
#: 复原底（旧版只认那三列，值还在 ⇒ 回滚不用重新签发、线上代理不断线）。
#: 留着不用 ≠ 第二张鉴权面：鉴权只有 `verify()` / `verify_any()` 一个入口，
#: 它们只查本表（`tests/test_agent_credentials.py` 钉死了这条）。
KS_CREDENTIAL_DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE_CREDENTIAL} (
        cred_id            text PRIMARY KEY,
        -- 归属代理。不设外键级联：删除顺序由代码保证（`Registry.delete`），
        -- 与 ks_message 同一做法（FK 会让"先删哪张"变成部署顺序约束，得不偿失）。
        agent_id           text NOT NULL,
        -- 给人看的名字（"108 执行器" / "win 笔记本"）。空 = 未命名。
        -- 🔴 标签不参与任何判据：改它不影响鉴权，也不需要客户端重配。
        label              text NOT NULL DEFAULT '',
        -- 鉴权唯一依据：sha256(明文)。明文永不入库（与代理表同一纪律）。
        credential_hash    text NOT NULL,
        credential_hint    text NOT NULL DEFAULT '',
        -- Fernet 密文存档，只为「控制台能重复复制」。校验面**永远不读这一列**。
        -- NULL = 不可还原（没主密钥时签的、或从老三列迁来的老凭据）。
        credential_enc     {ENC_COLUMN_DECL},
        -- active / revoked。作废一把**不动代理行**（与"吊销代理"分档：
        -- 吊销 = 停权限留记录；作废一把 = 只掐这一个入口，其余照旧连）。
        status             text NOT NULL DEFAULT '{CRED_STATUS_ON}',
        created_at         text NOT NULL DEFAULT '',
        -- 最近一次用它鉴权通过的时刻（打点限频 60s ⇒ 遥测，不是账本）。
        used_at            text NOT NULL DEFAULT '',
        reads              integer NOT NULL DEFAULT 0,
        writes             integer NOT NULL DEFAULT 0
    )
"""

#: 一个哈希只能属于一把钥匙（撞号 = 复制粘贴事故，直接让库拒绝，比代码里查一遍可靠）。
CRED_INDEX_NAME = "uq_ks_credential_hash"
CRED_INDEX_DDL = (f"CREATE UNIQUE INDEX IF NOT EXISTS {CRED_INDEX_NAME} "
                  f"ON {TABLE_CREDENTIAL} (credential_hash)")


class SingleCredentialBackend(RuntimeError):
    """这个存储后端只能放一把凭据（Redis 侧没有表结构）⇒ 调用方给**人话**，
    不许静默少发一把（静默少发比报错难查十倍）。"""


def new_credential_id() -> str:
    """凭据编号（不是秘密，见 `CRED_ID_HEX_LEN` 的说明）。"""
    return secrets.token_hex(CRED_ID_HEX_LEN // 2)


def _legacy_cred_id(credential_hash: str) -> str:
    """Redis 后端没有 `cred_id` 列 ⇒ 由哈希派生一个**稳定**编号
    （同一把凭据两次拿到的 id 必须一致，否则界面上的按钮会点到别把上）。"""
    return hashlib.sha256(str(credential_hash or "").encode("utf-8")).hexdigest()[:CRED_ID_HEX_LEN]


def _cred_label(raw: Any) -> str:
    """标签归一：去首尾空白、截到上限，空 ⇒ 「默认」。

    截断而不是报错：标签是给人贴的备忘，为几个字拒掉整次签发是本末倒置。
    """
    text = str(raw or "").strip()[:CRED_LABEL_MAX_LEN]
    return text or CRED_LABEL_DEFAULT


def _cred_public(row: Dict[str, Any]) -> Dict[str, Any]:
    """凭据的对外形状（控制台列表 / MCP 治理面共用）。

    🔴 不含 `credential_hash`、不含密文、更不含明文 —— 界面拿 `credential_hint`
    （本来就公开的 8 位）比对，`cred_id` 定位，`recoverable` 决定给不给「复制」钮。
    """
    return {
        "cred_id": row["cred_id"],
        "label": row["label"],
        "credential_hint": row["credential_hint"],
        "status": row["status"],
        "created_at": row["created_at"],
        "used_at": row["used_at"],
        # 作废时刻（在用为空）。
        "revoked_at": str(row.get("revoked_at") or ""),
        "reads": row["reads"],
        "writes": row["writes"],
        "recoverable": bool(str(row.get(ENC_COLUMN) or "")),
    }

#: `push_enabled` 的**开**值（列是 text，Redis hash 字段也存字符串）。
#: 🔴 与 `share_memory` 同一档：**缺省是关**（'0'）——「实时推送」是**往外放行**
#: 的开关（别人能连上来推事件给你），缺省必须是最严的那一档（老行没有这一列
#: ⇒ 一律当关，语义不变）。
PUSH_ENABLED_ON = "1"
PUSH_DISABLED_ON = "0"
#: 认作「开」的值（其余一切（含空 / 缺列）都当关）。
PUSH_ENABLED_VALUES: Tuple[str, ...] = ("1", "true", "yes", "on")

#: `push_enabled` 的列名与列声明（建表与补列共用同一份 ⇒ 两边不会漂）。
PUSH_COLUMN = "push_enabled"
PUSH_COLUMN_DECL = "text NOT NULL DEFAULT '0'"

#: `core_budget_chars` 的列名与列声明（建表与补列共用同一份 ⇒ 两边不会漂）。
#: 🔴 **可空**：NULL = 「沿用全局的注入预算」，有值 = 该代理**自己**的预算。
#: 与 `dm_enabled` / `share_memory` 那两个「NOT NULL + 默认值」不同 ——
#: 这里「没有值」本身就是一种状态（缺省回退全局），不是某个开/关档位。
BUDGET_COLUMN = "core_budget_chars"
BUDGET_COLUMN_DECL = "integer NULL"

#: 计数与布尔列（SQL 里是 integer，Redis hash 里存字符串 —— 规范化时统一成 int/bool）
_INT_FIELDS = ("reads", "writes")
_FLAG_FIELDS = ("exclude_from_primary",)

# 🔴 **`ks_agent` DDL 的唯一定义处**（`core/pg.py` 与 `core/sqlite.py` 各自
# import 它进自己的建表机制）。列全用 `text`（时刻按本库惯用的 ISO 字符串存，
# 与 `ks_fragment.created` / `ks_hit_event.ts` 同口径），所以**两个后端逐字
# 共用这一段**；计数器用 `integer`（SQLite 的 INTEGER / PG 的 integer 同名同义）。
KS_AGENT_DDL = f"""
CREATE TABLE IF NOT EXISTS ks_agent (
    agent_id             text PRIMARY KEY,
    name                 text NOT NULL DEFAULT '',
    role                 text NOT NULL DEFAULT 'sub',
    status               text NOT NULL DEFAULT 'pending',
    credential_hash      text NOT NULL DEFAULT '',
    credential_hint      text NOT NULL DEFAULT '',
    -- 2026-11 ks_credential_recoverable：凭据的 Fernet 密文存档，只为「控制台能重复复制」。
    -- 🔴 校验面**永远不看这一列**（鉴权只认 credential_hash）⇒ 它缺失/解不开都不影响
    --    代理读写，只影响「能不能把明文再取出来」。NULL = 不可还原（机制前的老行、
    --    或服务端没配主密钥）。缺省 NULL 而非 ''：新增字段不许改存量语义。
    credential_enc       {ENC_COLUMN_DECL},
    first_seen_at        text NOT NULL DEFAULT '',
    last_seen_at         text NOT NULL DEFAULT '',
    reads                integer NOT NULL DEFAULT 0,
    writes               integer NOT NULL DEFAULT 0,
    exclude_from_primary integer NOT NULL DEFAULT 0,
    meta                 text NOT NULL DEFAULT '{{}}',
    -- 2026-10 ks_agent_messaging：是否接收别的代理的私信（代理互聊信箱）。
    -- 默认 '1'（开）—— 老库补列时也落 '1'，存量行的语义不变。
    dm_enabled           text NOT NULL DEFAULT '1',
    -- 2026-10 ks_share_memory：是否把这个代理的记忆共享给**别的**已批准代理。
    -- 默认 '0'（关）—— 这是**放宽**权限的开关，缺省必须是最严的那一档；
    -- 老库补列时也落 '0'，存量行的语义不变（谁也没被共享出去）。
    share_memory         text NOT NULL DEFAULT '0',
    -- 2026-11 ks_share_kb：是否把这个代理名下的**知识库文档**共享给别的已批准
    -- 代理。默认 '0'（关）—— 与 share_memory 同一档：放宽权限的开关，缺省最严；
    -- 老库补列时也落 '0' ⇒ 存量行的语义不变（谁也没被共享出去过）。
    -- 🔴 列声明**从 SHARE_KB_COLUMN_DECL 插值**（不是手抄一份）：建表与
    -- 老库补列自愈共用同一份 ⇒ 两边不许漂。
    share_kb             {SHARE_KB_COLUMN_DECL},
    -- 2026-10 ks_agent_budget：这个代理**自己**的常驻层注入字符预算。
    -- NULL = 沿用全局默认（config.json 的 retrieval.core_char_budget，缺省回退，
    -- 不改存量语义）；有值 = 该代理专用（正整数）。
    -- 老库补列落 NULL ⇒ 存量行一个都没被改过。
    core_budget_chars    integer NULL,
    -- 2026-10 ks_dm_sse：是否允许这个代理接 SSE 长连接（私信实时推送快车道）。
    -- 默认 '0'（关）—— 与 share_memory 同一档：往外放行的开关，缺省最严；
    -- 老库补列落 '0' ⇒ 存量行的语义不变（谁也没被连上来过）。
    push_enabled         text NOT NULL DEFAULT '0'
)
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# 连接类异常的判据（**只认类名**，不 import psycopg）
#
# 🔴 为什么只看类名：本模块（及 `govern` 包整体）**只依赖标准库**
#   （`core/pg.py` 反向 import 它的 DDL 建表，见模块 docstring）——
#   在这里 import psycopg 会把「没装 PG 依赖的人」也拖挂。
#   `psycopg.OperationalError` / `psycopg_pool.PoolTimeout` 的类名是稳定的。
# --------------------------------------------------------------------------

#: 广义「服务端暂时读不到」的异常（连接层坏了 **或** 池里借不到连接）。
CONNECTION_ERROR_NAMES = frozenset({
    "OperationalError", "InterfaceError", "PoolTimeout", "PoolClosed",
    "AdminShutdown", "CannotConnectNow", "TooManyConnections", "ConnectionError",
})

#: 连接**实体**坏了（⇒ 池里的这条连接该丢弃/重建）。
#: 🔴 `PoolTimeout` **不在这里**：池被借空是「并发太高」，不是「连接坏了」——
#: 重建池只会凭空多开一批连接，把拥塞变成雪崩。
BROKEN_CONNECTION_ERROR_NAMES = frozenset({
    "OperationalError", "InterfaceError", "AdminShutdown", "CannotConnectNow",
    "TooManyConnections", "ConnectionError",
})


def is_connection_error(exc: BaseException) -> bool:
    """这个异常是不是「服务端暂时读不到」那一类（连接层）。

    业务错误（约束冲突、语法错、权限错）**不在**名单里 ⇒ 不会被误报成
    「PG 抖动」—— 那句文案会让运维去查网络，而真因在 SQL。
    """
    return type(exc).__name__ in CONNECTION_ERROR_NAMES


def is_broken_connection_error(exc: BaseException) -> bool:
    """连接**实体**坏了（该丢弃这条连接 / 重建池）。池超时不算（见上面的说明）。"""
    return type(exc).__name__ in BROKEN_CONNECTION_ERROR_NAMES


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
              "first_seen_at", "last_seen_at", "credential_enc"):
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
    # dm_enabled：缺列 / 空值 ⇒ 默认**开**（与 DDL 的 DEFAULT '1' 同口径，
    # 老行没有这一列时语义不变）；显式的关掉值原样留着。
    dm = str(row.get("dm_enabled") or "").strip()
    row["dm_enabled"] = dm if dm else DM_ENABLED_ON
    # share_memory：缺列 / 空值 ⇒ 默认**关**（与 DDL 的 DEFAULT '0' 同口径，
    # 「新增字段不许改存量语义」：老行一个都没被共享出去）。
    share = str(row.get("share_memory") or "").strip()
    row["share_memory"] = share if share else SHARE_DISABLED_ON
    # share_kb：缺列 / 空值 ⇒ 默认**关**（与 DDL 的 DEFAULT '0' 同口径，
    # 「新增字段不许改存量语义」：老行一个都没被共享出去过）。
    share_kb = str(row.get("share_kb") or "").strip()
    row["share_kb"] = share_kb if share_kb else SHARE_DISABLED_ON
    # push_enabled：缺列 / 空值 ⇒ 默认**关**（与 DDL 的 DEFAULT '0' 同口径，
    # 「新增字段不许改存量语义」：老行一个都没被连上过）。
    push = str(row.get("push_enabled") or "").strip()
    row["push_enabled"] = push if push else PUSH_DISABLED_ON
    # core_budget_chars：缺列 / 空值 ⇒ None（**沿用全局**）；脏值（0 / 负数 /
    # 非整数）也当 None —— 非法预算回落全局，比回落 0 安全得多（0 = 全被截掉）。
    row["core_budget_chars"] = _norm_budget(row.get("core_budget_chars"))
    # 脏数据不许悄悄当权威：角色/状态只认枚举内的值
    if row["role"] not in ROLES:
        row["role"] = ROLE_SUB
    if row["status"] not in STATUSES:
        row["status"] = STATUS_PENDING
    return row


def _norm_budget(value: Any) -> Optional[int]:
    """`core_budget_chars` 这一列的**唯一判读口**（None = 沿用全局）。

    🔴 脏数据（0 / 负数 / 非整数 / 空串）一律当**没有值**（None）：预算非法时
    回落全局，比回落 0 安全得多（0 = 常驻层全被截掉，等于把这一层关掉）。
    bool 也拒（`int(True)` = 1 = 1 字符预算，那不是人填的数）。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        n = int(text)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _dm_is_on(row: Optional[Dict[str, Any]]) -> bool:
    """`dm_enabled` 这一列的**唯一判读口**（默认**开**）。

    🔴 老行没有这一列 / 值为空 ⇒ 一律当**开**（「新增字段必须兼容旧数据」：
    存量行的语义不许因为加了一列而变）。查无此代理（空行）也当开 ——
    「代理存不存在」是**另一道闸**（由发送侧先判），不是这一列的事。
    """
    return str((row or {}).get("dm_enabled") or DM_ENABLED_ON).strip() not in DM_DISABLED_VALUES


def _share_is_on(row: Optional[Dict[str, Any]], column: str = SHARE_COLUMN) -> bool:
    """`share_memory` / `share_kb` 这类**放宽可见性**的列的**唯一判读口**（默认**关**）。

    🔴 与 `_dm_is_on` 相反：那边缺省**开**（老行语义不变），这边缺省**关** ——
    「记忆共享」/「共享知识库」是**放宽**可见性的开关，缺省必须是最严的那一档。
    查无此代理（空行）也当关 —— 「代理存不存在」是**另一道闸**。
    「开」的值集两个旗共用 `SHARE_ENABLED_VALUES`（同一把尺子）。
    """
    value = str((row or {}).get(column) or "").strip().lower()
    return value in SHARE_ENABLED_VALUES


def _push_is_on(row: Optional[Dict[str, Any]]) -> bool:
    """`push_enabled` 这一列的**唯一判读口**（默认**关**）。

    🔴 与 `_share_is_on` 同一档：缺省最严（老行没有这一列 ⇒ 一律当关）。
    查无此代理（空行）也当关 —— 「代理存不存在」是**另一道闸**（由建流侧先判）。
    """
    value = str((row or {}).get("push_enabled") or PUSH_DISABLED_ON).strip().lower()
    return value in PUSH_ENABLED_VALUES


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    """对外形状（控制台 GET 与判定方共用的字段集）。

    🔴 **不含 `credential_hash`**：注册表的对外读面永远不回显凭据摘要，
    更不含明文（明文只在 approve 的一次性响应里）。

    多凭据（11-14）之后，凭据相关的四个字段来自 `row["_cred"]`（调用方用
    `_cred_summary()` 挂上），**不再读代理行的老三列** —— 那是迁移前的快照，
    拿它显示会出现「界面上还有一把、实际已经作废」的假账。缺 `_cred` 一律按 0 把算。
    """
    cred = row.get("_cred") or {}
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
        # 凭据摘要（监管面的入口，逐把明细在 `list_credentials`）：
        #   count/active → 「这代理有几个入口」；last_used_at → 「最近谁在用」。
        #   hint/recoverable 指**最后一把在用的**那把（界面上先给个能比对的头 8 位；
        #   要取哪把必须进清单点，绝不在列表页猜 —— 猜错就是把另一把的钥匙递出去）。
        # 🔴 只报这些：密文与哈希都不出读面。
        "credential_hint": str(cred.get("credential_hint") or ""),
        "recoverable": bool(cred.get("recoverable")),
        "credential_count": int(cred.get("credential_count") or 0),
        "credential_active": int(cred.get("credential_active") or 0),
        "last_used_at": str(cred.get("last_used_at") or ""),
        "meta": row["meta"],
        # 私信开关（投递偏好）：控制台要能看见当前状态 —— 判读口径与
        # `Registry.dm_enabled` 同一个（`_dm_is_on`），两处不许各写一份。
        "dm_enabled": _dm_is_on(row),
        # 记忆共享开关：true = 这个代理的记忆进**别的**已批准代理的检索可见范围。
        # 判读口径与 `Registry.share_memory` 同一个（`_share_is_on`）。
        "share_memory": _share_is_on(row),
        # 知识库共享开关：true = 这个代理名下的**知识库文档**进**别的**已批准代理
        # 的检索可见范围（与记忆面**解耦**的另一个旗）。判读口径同一个
        # （`_share_is_on`，只是读另一列）。
        "share_kb": _share_is_on(row, SHARE_KB_COLUMN),
        # 私信实时推送开关（SSE 快车道）：true = 允许这个代理接 SSE 长连接。
        # 判读口径与 `Registry.push_enabled` 同一个（`_push_is_on`）。
        "push_enabled": _push_is_on(row),
        # 这个代理**自己**的注入字符预算：None = 沿用全局（缺省回退）。
        # 判读口径与 `Registry.core_budget_chars` 同一个（`_norm_budget`）。
        "core_budget_chars": row.get("core_budget_chars"),
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


class _SqlBase:
    """PG / SQLite 共用的**存取原语**（占位符 / 读 / 写 / 建表入口）。

    🔴 `ks_agent`（本文件）与 `ks_message`（`govern/messaging.py`）**共用这一份**：
    SQL 只差占位符与事务入口，其余逐字相同 —— 复制一份必然漂，而漂的后果是
    「一个模块在某个后端上读不到另一个模块写的行」。
    """

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

    # -- 老库补列（先查后补）------------------------------------------
    def has_column(self, column: str) -> bool:
        """这张表有没有某列（**先查后补**的第一步）。

        🔴 为什么不直接发 `ADD COLUMN IF NOT EXISTS`：PG 侧**列已存在也要拿
        ACCESS EXCLUSIVE**（见 `core/pg.py::ensure_index` 的说明）⇒ 与并发 DML
        互等成环、还挡住整张表的读写。先查后补 ⇒ 列在时**一条 DDL 都不发**。
        """
        if self.kind == "postgres":
            rows = self.read(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = %s AND column_name = %s LIMIT 1",
                (TABLE, column),
            )
            return bool(rows)
        rows = self.read(f"PRAGMA table_info({TABLE})")
        return any(str(r[1]) == column for r in rows)

    def add_column(self, column: str, decl: str) -> None:
        """补一列（已存在 ⇒ 吞掉「重复列」错误，不当失败）。"""
        if self.kind == "postgres":
            sql = f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {column} {decl}"
        else:
            sql = f"ALTER TABLE {TABLE} ADD COLUMN {column} {decl}"
        try:
            self.write(lambda cur: cur.execute(sql))
        except Exception as e:      # noqa: BLE001 —— 并发补列撞车（duplicate column）
            msg = str(e).lower()
            if "duplicate" in msg or "already exists" in msg:
                return
            raise


class _SqlBackend(_SqlBase):
    """PG / SQLite 共用：SQL 只差**占位符**与事务入口，其余逐字相同。"""

    # -- ks_agent 专用 --------------------------------------------------
    def ensure_schema(self) -> bool:
        """建表走各后端自己的 ensure 机制，**外加**老库补 `dm_enabled` /
        `share_memory` / `share_kb` / `core_budget_chars` / `push_enabled` 列。

        🔴 补列走「先查后补」（`_SqlBase.has_column`）：列已在 ⇒ **一条 DDL 都不发**
        （PG 的 `ADD COLUMN IF NOT EXISTS` 列已存在也要拿 ACCESS EXCLUSIVE，
        会与并发 DML 互等成环 —— 见 `core/pg.py::ensure_index` 的说明）。
        🔴 列声明**从常量取**（建表 DDL 里也是同一份插值）⇒ 两边不许漂。
        """
        if not super().ensure_schema():
            return False
        for column, decl in ((DM_COLUMN, DM_COLUMN_DECL),
                             (SHARE_COLUMN, SHARE_COLUMN_DECL),
                             (SHARE_KB_COLUMN, SHARE_KB_COLUMN_DECL),
                             (BUDGET_COLUMN, BUDGET_COLUMN_DECL),
                             (PUSH_COLUMN, PUSH_COLUMN_DECL),
                             (ENC_COLUMN, ENC_COLUMN_DECL)):
            try:
                if not self.has_column(column):
                    self.add_column(column, decl)
            except Exception as e:      # noqa: BLE001 —— 返回 False，不抛（调用方决定降级）
                logger.warning("keepsake: 注册表补列 %s 失败（%s: %s）",
                               column, type(e).__name__, e)
                return False
        # ---- 多凭据（11-14）：老三列 → ks_credential，幂等 ----
        # 表本身由 `core/pg.py` / `core/sqlite.py` 的 DDL 清单建（与 ks_agent 同一处，
        # 这里不自发 DDL）。**迁不动就返回 False**：新代码的鉴权只认凭据表，
        # 悄悄迁失败 = 所有代理集体 `bad_credential`，那必须响，不能闷着上线。
        try:
            moved = _CredSqlBackend(self._st, self.kind).migrate_from_agents(self.all_rows())
            if moved:
                logger.info("keepsake: %d 条老凭据已灌进 %s（老三列冻结留作回滚底）",
                            moved, TABLE_CREDENTIAL)
        except Exception as e:      # noqa: BLE001
            logger.error("keepsake: 凭据表迁移失败（%s: %s）—— 拒绝以半套账启动",
                         type(e).__name__, e)
            return False
        return True

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

    def delete(self, agent_id: str) -> bool:
        """**整行删除**（`DELETE FROM ks_agent WHERE agent_id = ?`）。命中即 True。

        🔴 删的是**整行**（含凭据哈希）—— 真删，备份由调用方在删前落。
        行不存在 ⇒ False（调用方 404，不谎报「删掉了」）。
        """
        def _fn(cur: Any) -> bool:
            cur.execute(f"DELETE FROM {TABLE} WHERE agent_id = {self.ph}", (agent_id,))
            return cur.rowcount > 0

        return bool(self.write(_fn))


class _CredSqlBackend(_SqlBase):
    """`ks_credential` 的存取（PG / SQLite 共用 `_SqlBase` 的连线、占位符与事务入口）。

    🔴 与代理表共用一份 `_SqlBase` 的理由同旧：SQL 只差占位符，复制一份必然漂。
    """

    def list(self, agent_id: str = "") -> List[Dict[str, Any]]:
        sql = f"SELECT {', '.join(CRED_FIELDS)} FROM {TABLE_CREDENTIAL}"
        params: Tuple[Any, ...] = ()
        if agent_id:
            sql += f" WHERE agent_id = {self.ph}"
            params = (agent_id,)
        # 稳定序：先按签发时间，再按编号 ⇒ 同一批数据两次渲染的行序一致。
        sql += " ORDER BY created_at, cred_id"
        return [dict(zip(CRED_FIELDS, row)) for row in self.read(sql, params)]

    def get(self, cred_id: str) -> Optional[Dict[str, Any]]:
        rows = self.read(
            f"SELECT {', '.join(CRED_FIELDS)} FROM {TABLE_CREDENTIAL} WHERE cred_id = {self.ph}",
            (cred_id,))
        return dict(zip(CRED_FIELDS, rows[0])) if rows else None

    def by_hash(self, credential_hash: str) -> Optional[Dict[str, Any]]:
        """按**哈希**查（哈希不是秘密，明文才是）⇒ 反查身份走索引，不逐行比。"""
        if not credential_hash:
            return None
        rows = self.read(
            f"SELECT {', '.join(CRED_FIELDS)} FROM {TABLE_CREDENTIAL} "
            f"WHERE credential_hash = {self.ph}", (credential_hash,))
        return dict(zip(CRED_FIELDS, rows[0])) if rows else None

    def insert(self, row: Dict[str, Any]) -> None:
        cols = ", ".join(CRED_FIELDS)
        marks = ", ".join(self.ph for _ in CRED_FIELDS)
        values = tuple(None if (f == ENC_COLUMN and not row.get(f)) else row.get(f)
                       for f in CRED_FIELDS)
        self.write(lambda cur: cur.execute(
            f"INSERT INTO {TABLE_CREDENTIAL} ({cols}) VALUES ({marks})", values))

    def set_fields(self, cred_id: str, fields: Dict[str, Any]) -> bool:
        """改某把的几个字段（label / status / 密文）；命中才算成功。

        🔴 空 `fields` 直接 False —— 拼出 `SET  WHERE` 是语法错误，白抛一次栈。
        """
        if not fields:
            return False
        sets = ", ".join(f"{f} = {self.ph}" for f in fields)
        enc = ENC_COLUMN

        def _fn(cur: Any) -> bool:
            values = tuple(None if (f == enc and not v) else v
                           for f, v in fields.items())
            cur.execute(f"UPDATE {TABLE_CREDENTIAL} SET {sets} WHERE cred_id = {self.ph}",
                        (*values, cred_id))
            return cur.rowcount > 0

        return bool(self.write(_fn))

    def bump(self, cred_id: str, column: str) -> None:
        """逐把计数（`reads` / `writes`）+ 刷新 `used_at`。

        🔴 `column` 只接受本模块常量名：它是拼进 SQL 的，绝不能来自请求。
        """
        if column not in ("reads", "writes"):
            raise ValueError(f"bump: 非法计数列 {column!r}")
        self.write(lambda cur: cur.execute(
            f"UPDATE {TABLE_CREDENTIAL} SET {column} = COALESCE({column}, 0) + 1, "
            f"used_at = {self.ph} WHERE cred_id = {self.ph}",
            (_now_iso(), cred_id)))

    def revoke_all(self, agent_id: str) -> int:
        """把这个代理**在用的**凭据全部作废（吊销代理 / 停凭据 / 换发都走它）。
        已作废的行留着 —— 它们是"谁在什么时候被掐掉"的历史。"""
        def _fn(cur: Any) -> int:
            cur.execute(
                f"UPDATE {TABLE_CREDENTIAL} SET status = {self.ph} "
                f"WHERE agent_id = {self.ph} AND status = {self.ph}",
                (CRED_STATUS_OFF, agent_id, CRED_STATUS_ON))
            return cur.rowcount

        return int(self.write(_fn) or 0)

    def delete_agent(self, agent_id: str) -> int:
        def _fn(cur: Any) -> int:
            cur.execute(f"DELETE FROM {TABLE_CREDENTIAL} WHERE agent_id = {self.ph}",
                        (agent_id,))
            return cur.rowcount

        return int(self.write(_fn) or 0)

    def rename_agent(self, old_id: str, new_id: str) -> int:
        """换主键时把凭据一起搬走 —— 否则「改名」会变成「所有入口集体掉线」。"""
        def _fn(cur: Any) -> int:
            cur.execute(f"UPDATE {TABLE_CREDENTIAL} SET agent_id = {self.ph} "
                        f"WHERE agent_id = {self.ph}", (new_id, old_id))
            return cur.rowcount

        return int(self.write(_fn) or 0)

    def summaries(self, rows: Optional[List[Dict[str, Any]]] = None
                  ) -> Dict[str, Dict[str, Any]]:
        """代理列表要的汇总：几把 / 几把在用 / 最近使用 / 最后一把在用的 hint 与可还原性。

        🔴 一次读全表在内存里折叠，**不逐代理查**（列表页 N 个代理就 N 次查询是白给的
        性能坑）。行数与代理数同量级（治理面，不是用户面）。
        """
        out: Dict[str, Dict[str, Any]] = {}
        for row in (rows if rows is not None else self.list()):
            aid = str(row.get("agent_id") or "")
            if not aid:
                continue
            item = out.setdefault(aid, {
                "credential_count": 0, "credential_active": 0, "last_used_at": "",
                "credential_hint": "", "recoverable": False,
            })
            item["credential_count"] += 1
            if str(row.get("used_at") or "") > item["last_used_at"]:
                item["last_used_at"] = str(row.get("used_at") or "")
            if row.get("status") == CRED_STATUS_ON:
                # 行序已按签发时间排好 ⇒ 后写覆盖前写 = "最后一把在用的"。
                item["credential_active"] += 1
                item["credential_hint"] = str(row.get("credential_hint") or "")
                item["recoverable"] = bool(str(row.get(ENC_COLUMN) or ""))
        return out

    def migrate_from_agents(self, agents: List[Dict[str, Any]]) -> int:
        """老三列（每代理一把）灌进本表，标签「默认」。幂等：按哈希判重。

        🔴 只灌**不改**老三列（冻结但不清空）⇒ 回滚到旧版二进制时，旧版仍能用原串
        鉴权，不会出现「升级升一半、两个在线代理集体掉线」这种收不了场的局面。
        """
        moved = 0
        for row in agents:
            stored = str(row.get("credential_hash") or "")
            if not stored or self.by_hash(stored):
                continue
            self.insert({
                "cred_id": _legacy_cred_id(stored),
                "agent_id": row["agent_id"],
                "label": CRED_LABEL_DEFAULT,
                "credential_hash": stored,
                "credential_hint": str(row.get("credential_hint") or ""),
                ENC_COLUMN: str(row.get(ENC_COLUMN) or ""),
                "status": CRED_STATUS_ON,
                "created_at": str(row.get("first_seen_at") or _now_iso()),
                "used_at": str(row.get("last_seen_at") or ""),
                "reads": int(row.get("reads") or 0),
                "writes": int(row.get("writes") or 0),
            })
            moved += 1
        return moved


class _CredRedisBackend:
    """Redis 后端的**单把**凭据账 —— 老三列就是它的存储，不造假表。

    🔴 为什么不做多把：Redis 侧只有 `keepsake:agent:<id>` 一个 hash，塞一列 JSON
    装多把就等于在同一份语义上写两套实现（PG/SQLite 走 SQL、Redis 走 JSON 解析），
    漂了就是「换了个后端凭据行为不一样」的故障。生产早已在 PG（Redis 是备胎），
    所以这里只做**兼容映射**：读、校验、作废、回填照常；**加第二把**直接抛
    `SingleCredentialBackend` ⇒ 调用方拿到人话，而不是静默少发一把。
    """

    kind = "redis"

    def __init__(self, backend: Any) -> None:
        self._be = backend

    def _row(self, agent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        stored = str(agent.get("credential_hash") or "")
        if not stored:
            return None
        return {
            "cred_id": _legacy_cred_id(stored),
            "agent_id": agent["agent_id"],
            "label": CRED_LABEL_DEFAULT,
            "credential_hash": stored,
            "credential_hint": str(agent.get("credential_hint") or ""),
            ENC_COLUMN: str(agent.get(ENC_COLUMN) or ""),
            "status": CRED_STATUS_ON,
            "created_at": str(agent.get("first_seen_at") or ""),
            "used_at": str(agent.get("last_seen_at") or ""),
            # 计数沿用代理行的（单把 = 同一批活动）；不去重复加，也不假装分得开。
            "reads": int(agent.get("reads") or 0),
            "writes": int(agent.get("writes") or 0),
        }

    def list(self, agent_id: str = "") -> List[Dict[str, Any]]:
        if agent_id:
            row = self._be.load(agent_id)
            got = self._row(row) if row else None
            return [got] if got else []
        out = []
        for agent in self._be.all_rows():
            got = self._row(agent)
            if got:
                out.append(got)
        return out

    def get(self, cred_id: str) -> Optional[Dict[str, Any]]:
        return next((r for r in self.list() if r["cred_id"] == cred_id), None)

    def by_hash(self, credential_hash: str) -> Optional[Dict[str, Any]]:
        if not credential_hash:
            return None
        return next((r for r in self.list()
                     if hmac.compare_digest(credential_hash, r["credential_hash"])), None)

    def insert(self, row: Dict[str, Any]) -> None:
        """写入 = 落到老三列（只有一个位置）。已有凭据 ⇒ 明确拒绝第二把。"""
        aid = row["agent_id"]
        current = self._row(self._be.load(aid) or {})
        if current and current["credential_hash"] != row["credential_hash"]:
            raise SingleCredentialBackend(aid)
        self._be.update(aid, {
            "credential_hash": row["credential_hash"],
            "credential_hint": row["credential_hint"],
            ENC_COLUMN: row.get(ENC_COLUMN) or "",
        })

    def set_fields(self, cred_id: str, fields: Dict[str, Any]) -> bool:
        row = self.get(cred_id)
        if not row:
            return False
        aid = row["agent_id"]
        merged = {**row, **fields}
        self._be.update(aid, {
            "credential_hash": merged["credential_hash"],
            "credential_hint": merged["credential_hint"],
            ENC_COLUMN: merged.get(ENC_COLUMN) or "",
        })
        return True

    def bump(self, cred_id: str, column: str) -> None:
        """逐把计数在这里是 no-op：单把后端的活动已经记在代理行上，
        两处都加会把同一件事数两遍。"""

    def revoke_all(self, agent_id: str) -> int:
        had = self._row(self._be.load(agent_id) or {})
        self._be.update(agent_id, {"credential_hash": "", "credential_hint": "",
                                   ENC_COLUMN: ""})
        return 1 if had else 0

    def delete_agent(self, agent_id: str) -> int:
        return self.revoke_all(agent_id)

    def rename_agent(self, old_id: str, new_id: str) -> int:
        return 1 if self._row(self._be.load(old_id) or {}) else 0

    def summaries(self, rows: Optional[List[Dict[str, Any]]] = None
                  ) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for row in (rows if rows is not None else self.list()):
            out[row["agent_id"]] = {
                "credential_count": 1, "credential_active": 1,
                "last_used_at": row["used_at"], "credential_hint": row["credential_hint"],
                "recoverable": bool(str(row.get(ENC_COLUMN) or "")),
            }
        return out

    def migrate_from_agents(self, agents: List[Dict[str, Any]]) -> int:
        return 0      # 老三列就是它的存储，没有可迁的东西


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

    def delete(self, agent_id: str) -> bool:
        """**整行删除**（`DEL keepsake:agent:<id>`：整个 hash 连凭据哈希一起没）。

        行不存在 ⇒ False（调用方 404）。🔴 Redis 的 `DEL` 不看 role ——
        「primary 不许删」那道闸在 `Registry.delete` 里（语义层），不在这层。
        """
        return bool(self._client().delete(self.key(agent_id)))


def _text(value: Any) -> str:
    """bytes/str → str（Redis 客户端 `decode_responses=False`，读出来是 bytes）。"""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return "" if value is None else str(value)


def _hash_field(value: Any) -> Any:
    """写进 Redis hash 的值形态（int / bool / dict 统一成字符串，读时 `_norm` 再转回来）。"""
    if value is None:
        return ""            # NULL 列在 Redis 上落空串（读回时 `_norm_budget` 判成 None）
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
# approve 的单点判定：已批准的行再 approve ⇒ 拒（要换发必须显式 reissue）
# --------------------------------------------------------------------------

#: 已批准行再 approve（且**没显式 reissue**）时的统一回显（API 与 MCP 共用这一句）。
ALREADY_APPROVED_NOTE = (
    "该代理已批准——要换发凭据请走「重新生成凭据」（需显式 reissue）"
)


class AlreadyApprovedError(Exception):
    """已 `approved` 的行再 approve、且**没显式 reissue** ⇒ 拒（一个字节都不改）。

    🔴 这是 approve 原语的**默认安全档**：重复批准（审核页双击 / MCP 治理工具对
    已批准代理再批准 / UI 状态未刷新重复点）不再静默签发新凭据把旧的顶掉 ——
    凭据一旦顶掉，线上代理瞬间全断，而操作者毫无感知。

    要换发（补发凭据、泄露止血）必须显式 `reissue=True` —— 与界面「重新生成凭据」
    钮同一条路径（那条路还额外过一道手输确认闸）。
    """

    def __init__(self, agent_id: str) -> None:
        super().__init__(ALREADY_APPROVED_NOTE)
        self.agent_id = agent_id


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
        #: 底层存储句柄（**只读用途**）：`govern/messaging.py` 的信箱要经它连同一个库，
        #: 不另开连接、不另拼 DSN。
        self.storage = storage
        self._be: Any = _SqlBackend(storage, kind) if kind != "redis" else _RedisBackend(storage)
        # 🔴 凭据账与代理账**同后端不同实现**：SQL 走 `ks_credential` 表，
        # Redis 走老三列的单把兼容映射（`add_credential` 在那边会明确拒绝）。
        self._cred: Any = (_CredSqlBackend(storage, kind) if kind != "redis"
                           else _CredRedisBackend(self._be))
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
        summary = self._cred.summaries()      # 一次读全表，不在循环里逐代理查
        return [_public(self._cred_summary(r, summary))
                for r in sorted(rows, key=lambda r: r["agent_id"])]

    def get(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """单个代理（对外形状）；不存在返回 None。"""
        row = self._be.load((agent_id or "").strip())
        return _public(self._cred_summary(row)) if row else None

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
            "dm_enabled": DM_ENABLED_ON,
            "share_memory": SHARE_DISABLED_ON,
            # 🔴 登记**不碰**共享知识库：缺省就是关（放宽权限的开关，缺省最严）。
            "share_kb": SHARE_DISABLED_ON,
            # 🔴 登记**不碰**预算：新建的行一律沿用全局（NULL）——
            # 「谁都没登记过预算」是缺省态，不是某个开/关档位。
            "core_budget_chars": None,
            "push_enabled": PUSH_DISABLED_ON,
            # 密文列登记时为空：新行还没凭据。approve 才会写它（且只在主密钥在场时）。
            ENC_COLUMN: "",
        })
        return self.get(aid) or {}

    # ---- 批准 / 吊销 / 角色 / 上下文开关 -------------------------------
    def _cred_summary(self, row: Dict[str, Any],
                      summary: Optional[Dict[str, Dict[str, Any]]] = None
                      ) -> Dict[str, Any]:
        """把凭据汇总挂到代理行上（`_public` 靠 `row["_cred"]` 出凭据字段）。"""
        data = summary if summary is not None else self._cred.summaries()
        out = dict(row)
        out["_cred"] = data.get(str(row.get("agent_id") or ""), {})
        return out

    # ---- 多凭据（11-14 用户拍板：一个代理可发多把，逐把监管）------------
    def list_credentials(self, agent_id: str) -> List[Dict[str, Any]]:
        """某个代理名下的全部凭据（含已作废的，按签发时间排；不含任何秘密材料）。"""
        return [_cred_public(r) for r in self._cred.list((agent_id or "").strip())]

    def add_credential(self, agent_id: str, label: str = "") -> Optional[str]:
        """给一个**已批准**的代理再加一把钥匙，返回明文（只在这里出现一次）。

        与 `approve(reissue=True)` 的分界：这里**不作废任何旧钥匙** —— 多把并存，
        正是这个功能存在的理由（换机器时新旧同时在线，切完再掐旧的）。
        代理不在册 / 没批准 ⇒ None（调用方 404 / 409）。
        🔴 Redis 后端已有凭据 ⇒ 抛 `SingleCredentialBackend`（宁可报错也不静默少发）。
        """
        aid = (agent_id or "").strip()
        row = self._be.load(aid)
        if not row or row["status"] != STATUS_APPROVED:
            return None
        return self._issue(aid, label)

    def _issue(self, aid: str, label: str = "") -> str:
        """签一把新凭据并落账（`approve` 与 `add_credential` 共用的唯一落点）。"""
        credential = new_credential()
        # 密文只在主密钥在场时存；不在场这把照旧可用，只是不可再取出（面板如实显示）。
        self._cred.insert({
            "cred_id": new_credential_id(),
            "agent_id": aid,
            "label": _cred_label(label),
            "credential_hash": hash_credential(credential),
            "credential_hint": credential_hint(credential),
            ENC_COLUMN: credcrypto.encrypt(credential) or "",
            "status": CRED_STATUS_ON,
            "created_at": _now_iso(),
            "used_at": "",
            "reads": 0,
            "writes": 0,
        })
        return credential

    def revoke_credential(self, agent_id: str, cred_id: str) -> bool:
        """作废**一把**钥匙：代理照旧在册、其余钥匙照旧能用。

        🔴 必须 `agent_id` + `cred_id` 两个都对得上才动手 —— 只按 `cred_id` 找的话，
        一个手滑的 URL 就能废掉别人家的钥匙（跨代理越权，本仓同类事故先例见过）。
        """
        aid = (agent_id or "").strip()
        cid = (cred_id or "").strip()
        row = self._cred.get(cid)
        if not row or row["agent_id"] != aid:
            return False
        if row["status"] == CRED_STATUS_OFF:
            return False        # 已经废过：不谎报"刚废掉"（界面据此提示"这把这前就废了"）
        return self._cred.set_fields(cid, {
            "status": CRED_STATUS_OFF,
            ENC_COLUMN: "",     # 🔴 密文一起清：留着就随时能再取出来用，作废是假的
        })

    def rename_credential(self, agent_id: str, cred_id: str, label: str) -> bool:
        """改标签（纯展示，不参与任何判据 ⇒ 不需要确认闸、也不影响客户端）。"""
        aid = (agent_id or "").strip()
        cid = (cred_id or "").strip()
        row = self._cred.get(cid)
        if not row or row["agent_id"] != aid:
            return False
        return self._cred.set_fields(cid, {"label": _cred_label(label)})

    def approve(self, agent_id: str, reissue: bool = False,
                label: str = "") -> Optional[str]:
        """批准并**生成一条一次性凭据**，返回明文；代理不存在返回 None。

        明文**只在这里出现一次**（返回值 = 控制台的响应体），库里只落 sha256。

        🔴 **默认安全档**（`reissue=False`）：行已是 `approved` ⇒ 抛
        `AlreadyApprovedError`，**凭据哈希 / 状态一个都不动**。重复批准是最容易
        误伤线上代理的操作（审核页双击、治理工具对已批准代理再批准、UI 状态未
        刷新重复点）—— 静默换发会让旧凭据当场作废，而操作者毫无感知。
        要换发（补发凭据 / 泄露止血）必须显式 `reissue=True`：与界面「重新生成
        凭据」钮同一条路径（那边还额外过一道手输确认闸），旧凭据随即失效。
        `pending` / `revoked` 的行照旧直接批准签发（重新批准 = 换一条新凭据）。
        """
        aid = (agent_id or "").strip()
        row = self._be.load(aid)
        if not row:
            return None
        if not reissue and row.get("status") == STATUS_APPROVED:
            raise AlreadyApprovedError(aid)
        # 换发（reissue）= 先把在用的全部掐掉再签新的 —— 与旧版"一把被顶掉"同语义，
        # 只是现在名正言顺：那把钥匙在凭据账上变 revoked，历史留痕、用量留痕。
        # 🔴 老三列此时**一并清掉**：它们是回滚底，但"作废"必须是两边都作废 ——
        #   留着的话退回旧版二进制的瞬间，那把该死的钥匙又活了（安全方向优先于回滚便利）。
        if reissue:
            self._cred.revoke_all(aid)
            self._be.update(aid, {"credential_hash": "", "credential_hint": "",
                                  ENC_COLUMN: ""})
        self._be.update(aid, {"status": STATUS_APPROVED})
        return self._issue(aid, label)

    def revoke(self, agent_id: str) -> bool:
        """吊销（随时可做）：`status=revoked` 且**清掉凭据哈希**（旧凭据立即作废）。"""
        aid = (agent_id or "").strip()
        if not self._be.load(aid):
            return False
        # 🔴 两本账一起掐：代理行清老三列（回滚后也照样作废）+ 凭据表全部置 revoked。
        #    只掐一边的后果：新代码看凭据表 ⇒ 旧代码看老三列 ⇒ 两边各留一把活钥匙。
        self._cred.revoke_all(aid)
        self._be.update(aid, {"status": STATUS_REVOKED,
                              "credential_hash": "", "credential_hint": "",
                              ENC_COLUMN: ""})
        return True

    def clear_credential(self, agent_id: str) -> bool:
        """**只停凭据**：清掉 `credential_hash` / `credential_hint` / `credential_enc`
        三列，代理行留在册。

        🔴 `status` / `role` / `name` / `meta` / 计数**一个都不碰** ——
        这是「只废钥匙」与「吊销」的分界：吊销把 `status` 置 revoked
        （读写全拒、留一条吊销记录），停凭据只让那把钥匙当场作废，
        代理本身照旧留在册、状态照旧。行不存在 ⇒ False（调用方 404）。
        """
        aid = (agent_id or "").strip()
        if not self._be.load(aid):
            return False
        # 🔴 两本账一起掐（同 revoke）；密文留在哪一边，作废就在哪一边是假的。
        # 多凭据之后这一动作的语义 = **停这个代理的全部钥匙**（要停单把去凭据清单）。
        self._cred.revoke_all(aid)
        self._be.update(aid, {"credential_hash": "", "credential_hint": "",
                              ENC_COLUMN: ""})
        return True

    # ---- 可还原凭据（控制台「查看/复制凭据」与「回填已有凭据」的原语）----------
    def reveal_credential(self, agent_id: str, cred_id: str = "") -> Dict[str, Any]:
        """把某把凭据的**明文**取回来（重复复制用）。

        `cred_id` 省略时的规则（**宁拒不猜**）：名下正好一把在用的 ⇒ 就是它；
        一把都没有 ⇒ `no_credential`；**多把在用 ⇒ `ambiguous`（附候选编号）**，
        绝不替调用方挑一把 —— 挑错就是把另一把的钥匙递出去。
        旧调用方（只认单把年代的 API/测试）因此行为不变，只有真多把时才需要指名。

        返回 `{"ok": bool, "credential": str|None, "why": str}` —— `why` 是给调用方
        分诊用的，几种失败后果不同、处置动作也不同，不许糊成一句「取不出来」：

        * `no_row`          —— 代理不在册（调用方 404）；
        * `no_credential`   —— 这行现在**没有凭据**（没批准过 / 已停过 / 已吊销），
                               取不出东西是正常的，不是故障；
        * `not_recoverable` —— 有凭据但**密文没存**（机制上线前签发的老行、
                               或签发时服务端还没有主密钥）⇒ 正解是「回填已有凭据」
                               或换发，不是重试；
        * `key_missing`     —— 密文在，但服务端读不到主密钥 ⇒ 运维问题（配
                               `KEEPSAKE_CREDENTIAL_KEY[_FILE]`），与数据无关；
        * `key_mismatch`    —— 主密钥在场且密文形状正常，但解不开 ⇒ 换过密钥或密文
                               被改坏，**重试无用**，只能回填 / 换发；
        * `hash_mismatch`   —— 解出来了但与在册 sha256 对不上账（两列任一处被手工
                               改过）⇒ 绝不把对不上账的串当凭据发出去。

        🔴 只读注册表自己的行，**不碰校验路径**：鉴权仍然只认 `credential_hash`。
        🔴 明文只进返回值：这里不写日志、不写审计（"取过一次"由调用方记，不含内容）。
        """
        aid = (agent_id or "").strip()
        if not self._be.load(aid):
            return {"ok": False, "credential": None, "why": "no_row"}
        rows = [r for r in self._cred.list(aid) if r.get("status") == CRED_STATUS_ON]
        cid = (cred_id or "").strip()
        if cid:
            row = next((r for r in rows if r["cred_id"] == cid), None)
            if not row:
                # 不区分"不存在"与"已作废"：两者处置动作一样（换一把/重新签发），
                # 而区分它们等于向外探测别人的编号存不存在。
                return {"ok": False, "credential": None, "why": "no_credential",
                        "cred_id": cid}
        elif len(rows) == 1:
            row = rows[0]
        elif not rows:
            return {"ok": False, "credential": None, "why": "no_credential"}
        else:
            return {"ok": False, "credential": None, "why": "ambiguous",
                    "cred_ids": [r["cred_id"] for r in rows]}
        enc = str(row.get(ENC_COLUMN) or "")
        if not enc:
            return {"ok": False, "credential": None, "why": "not_recoverable",
                    "cred_id": row["cred_id"]}
        plain, why = credcrypto.decrypt_with_reason(enc)
        if not plain:
            return {"ok": False, "credential": None, "why": why,
                    "cred_id": row["cred_id"]}
        # 🔴 取出的明文必须与**这把自己**的在册 hash 对得上才回 —— 密文与 hash 是两列，
        #    任何一处被手工改过都不该把「对不上账」的串当凭据发出去。
        if not hmac.compare_digest(hash_credential(plain), str(row["credential_hash"])):
            return {"ok": False, "credential": None, "why": "hash_mismatch",
                    "cred_id": row["cred_id"]}
        return {"ok": True, "credential": plain, "why": "ok",
                "cred_id": row["cred_id"], "label": row["label"]}

    def backfill_credential(self, agent_id: str, credential: str,
                            cred_id: str = "") -> Dict[str, Any]:
        """把**已有**凭据的明文登记成可还原（校验通过才写，绝不换发）。

        场景：可还原机制上线之前签发的行（库里只有 sha256），操作者手上还留着那条串。
        做法 = 拿粘来的明文算 sha256，与在册 `credential_hash` **常数时间**比对，
        对上才写密文 —— 既不用换发（线上代理不断），也不会把别人的串挂到别的行上。

        返回 `{"ok": bool, "why": str, "hint": str}`：

        * `ok`          —— 校验通过且密文已落库（此后这条永久可复制）；
        * `no_row`      —— 不在册；
        * `no_hash`     —— 这行现在没有凭据（没批准过 / 已停过）⇒ 没有账可比，
                           回填无意义，正解是批准 / 换发；
        * `empty`       —— 粘进来的东西是空的（UI 先挡，这里兜底）；
        * `mismatch`    —— 🔴 明文与在册 hash 对不上 ⇒ **一个字节都不改**
                           （串打错了，或那是别的代理的凭据）；
        * `key_missing` —— 对得上但服务端没有主密钥 ⇒ 写不了密文（**不许**退化成
                           把明文塞进库），先配密钥再回填。

        🔴 任何分支都不把明文写进日志/审计；`hint` 只回本来就公开的那 8 位。
        """
        aid = (agent_id or "").strip()
        row = self._be.load(aid)
        if not row:
            return {"ok": False, "why": "no_row", "hint": ""}
        plain = (credential or "").strip()
        if not plain:
            return {"ok": False, "why": "empty",
                    "hint": str(row.get("credential_hint") or "")}
        got = hash_credential(plain)
        cid = (cred_id or "").strip()
        # 正主：凭据账上哈希对得上的那把（`cred_id` 给了就必须同时对上，防认错把）。
        target = next(
            (r for r in self._cred.list(aid)
             if hmac.compare_digest(got, str(r.get("credential_hash") or ""))
             and (not cid or r["cred_id"] == cid)),
            None)
        if target is None and not cid:
            # 迁移漏网的（老三列有账、凭据表没有）⇒ 顺手补一把进表，再照常回填密文。
            legacy = str(row.get("credential_hash") or "")
            if legacy and hmac.compare_digest(got, legacy):
                self._cred.migrate_from_agents([row])
                target = self._cred.by_hash(legacy)
        if target is None:
            # 名下**根本没有凭据账**（没批准过 / 全部作废清了账）⇒ 这不是"粘错了"，
            # 是"没有可比的东西"。两种后果不同（前者重粘一次、后者要先批准），必须分开报。
            if not self._cred.list(aid) and not str(row.get("credential_hash") or ""):
                return {"ok": False, "why": "no_hash", "hint": ""}
            # 🔴 对不上账 ⇒ 一个字节都不改。粘错的串、或那是**别的代理**的凭据。
            return {"ok": False, "why": "mismatch", "hint": credential_hint(plain)}
        if str(target.get(ENC_COLUMN) or ""):
            return {"ok": True, "why": "ok", "hint": target["credential_hint"],
                    "cred_id": target["cred_id"]}      # 已经可还原，幂等返回
        enc = credcrypto.encrypt(plain)
        if not enc:
            return {"ok": False, "why": "key_missing", "hint": credential_hint(plain)}
        self._cred.set_fields(target["cred_id"], {ENC_COLUMN: enc})
        return {"ok": True, "why": "ok", "hint": credential_hint(plain),
                "cred_id": target["cred_id"]}
    def delete(self, agent_id: str) -> bool:
        """**整行删除**：这一行从注册表里消失（凭据哈希一起没）。命中即 True。

        🔴 **primary 角色拒删**（返回 False）：删掉主代理 = 全库没有主脑可写了
        —— 要换主先把另一个设成 primary，再回来删这个（路由层给专属文案）。
        行不存在 ⇒ False（调用方 404，不谎报「删掉了」）。
        """
        aid = (agent_id or "").strip()
        row = self._be.load(aid)
        if not row:
            return False
        if row["role"] == ROLE_PRIMARY:
            return False
        gone = self._be.delete(aid)
        if gone:
            # 代理行没了，凭据一行都不留：留在表里就是**无人认领的活钥匙**
            # （反查能查到 agent_id，但那个人已经不在册 ⇒ 只靠 verify 的代理闸兜着）。
            self._cred.delete_agent(aid)
        return gone

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

    # ---- 私信开关（代理互聊信箱）-------------------------------------
    def dm_enabled(self, agent_id: str) -> bool:
        """这个代理**是否接收私信**（`dm_enabled` 开关）。默认**开**。

        🔴 查不到代理 / 老行没有这一列 ⇒ 一律 True：
        「新增字段必须兼容旧数据」—— 存量行的语义不许因为加了一列而变。
        （代理存不存在是**另一道闸**，由发送侧先判。）
        """
        row = self._be.load((agent_id or "").strip())
        if not row:
            return True
        return _dm_is_on(row)

    def set_dm_enabled(self, agent_id: str, flag: bool) -> bool:
        """开关私信接收。返回**值是否真的变了**（与 `set_exclude` 同款语义）。

        🔴 行不存在 ⇒ False（调用方 404）；`status` / `role` / 凭据**一个都不碰**
        —— 收不收私信是「投递偏好」，不是治理动作。
        """
        aid = (agent_id or "").strip()
        row = self._be.load(aid)
        if not row:
            return False
        want = DM_ENABLED_ON if flag else "0"
        if str(row.get("dm_enabled") or DM_ENABLED_ON).strip() == want:
            return False
        self._be.update(aid, {"dm_enabled": want})
        return True

    # ---- 私信实时推送开关（SSE 快车道）--------------------------------
    def push_enabled(self, agent_id: str) -> bool:
        """这个代理**是否允许接 SSE 长连接**（`push_enabled` 开关）。默认**关**。

        🔴 查不到代理 / 老行没有这一列 ⇒ 一律 False：
        「新增字段必须兼容旧数据」—— 存量行的语义不许因为加了一列而变
        （谁也没被连上过）。这是**往外放行**的开关，缺省必须最严
        （与 `share_memory` 同一档，与 `dm_enabled` 相反）。
        """
        row = self._be.load((agent_id or "").strip())
        if not row:
            return False
        return _push_is_on(row)

    def set_push_enabled(self, agent_id: str, flag: bool) -> bool:
        """开关「这个代理能不能接 SSE 长连接」。返回**值是否真的变了**。

        🔴 行不存在 ⇒ False（调用方 404）；`status` / `role` / 凭据 / 记忆归属
        **一个都不碰** —— 能不能被连上来推事件是**投递偏好**，不是治理动作
        （与 `set_dm_enabled` / `set_share_memory` 同纪律）。
        """
        aid = (agent_id or "").strip()
        row = self._be.load(aid)
        if not row:
            return False
        want = PUSH_ENABLED_ON if flag else PUSH_DISABLED_ON
        if str(row.get("push_enabled") or PUSH_DISABLED_ON).strip() == want:
            return False
        self._be.update(aid, {"push_enabled": want})
        return True

    # ---- 记忆共享开关（读可见性）-------------------------------------
    def share_memory(self, agent_id: str) -> bool:
        """这个代理的记忆**是否共享**给别的已批准代理。默认**关**。

        🔴 查不到代理 / 老行没有这一列 ⇒ 一律 False：
        「新增字段必须兼容旧数据」—— 存量行的语义不许因为加了一列而变
        （谁也没被共享出去过）。这是**放宽**权限的开关，缺省必须最严。
        """
        row = self._be.load((agent_id or "").strip())
        if not row:
            return False
        return _share_is_on(row)

    def set_share_memory(self, agent_id: str, flag: bool) -> bool:
        """开关「这个代理的记忆是否共享给别的已批准代理」。返回**值是否真的变了**。

        🔴 行不存在 ⇒ False（调用方 404）；`status` / `role` / 凭据**一个都不碰**
        —— 共不共享记忆是**读可见性偏好**，不是治理动作（与 `set_dm_enabled` 同纪律）。
        """
        aid = (agent_id or "").strip()
        row = self._be.load(aid)
        if not row:
            return False
        want = SHARE_ENABLED_ON if flag else SHARE_DISABLED_ON
        if str(row.get("share_memory") or SHARE_DISABLED_ON).strip() == want:
            return False
        self._be.update(aid, {"share_memory": want})
        return True

    # ---- 知识库共享开关（读可见性，与记忆面**解耦**）------------------
    def share_kb(self, agent_id: str) -> bool:
        """这个代理名下的**知识库文档**是否共享给别的已批准代理。默认**关**。

        🔴 与 `share_memory` **解耦**（两个旗各自独立），但**口径对称**：
        查不到代理 / 老行没有这一列 ⇒ 一律 False（放宽权限的开关，缺省最严）。
        """
        row = self._be.load((agent_id or "").strip())
        if not row:
            return False
        return _share_is_on(row, SHARE_KB_COLUMN)

    def set_share_kb(self, agent_id: str, flag: bool) -> bool:
        """开关「这个代理名下的知识库文档是否共享给别的已批准代理」。
        返回**值是否真的变了**。

        🔴 行不存在 ⇒ False（调用方 404）；`status` / `role` / 凭据**一个都不碰**
        —— 共不共享知识库是**读可见性偏好**，不是治理动作（与 `set_share_memory`
        同纪律、同闸）。
        """
        aid = (agent_id or "").strip()
        row = self._be.load(aid)
        if not row:
            return False
        want = SHARE_ENABLED_ON if flag else SHARE_DISABLED_ON
        if str(row.get("share_kb") or SHARE_DISABLED_ON).strip() == want:
            return False
        self._be.update(aid, {"share_kb": want})
        return True

    # ---- 注入预算（per-agent）----------------------------------------
    def core_budget_chars(self, agent_id: str) -> Optional[int]:
        """这个代理**自己**的常驻层注入字符预算；**没有 ⇒ None**（沿用全局）。

        🔴 查无此代理 / 老库没这一列 / 值为空 ⇒ 一律 None（= 沿用全局）：
        「新增字段必须兼容旧数据」—— 存量行的语义不许因为加了一列而变。
        🔴 **不缓存**（照 `visibility.shared_agent_ids` 的纪律）：改完预算必须
        **立刻生效**，缓存一加就有「改了没生效」的窗口 —— 那种故障最难查。
        """
        row = self._be.load((agent_id or "").strip())
        if not row:
            return None
        return row.get("core_budget_chars")

    def set_core_budget_chars(self, agent_id: str, value: Optional[int]) -> bool:
        """设 / 清这个代理的注入预算（`None` ⇒ 清空 ⇒ 沿用全局）。

        返回**值是否真的变了**（与 `set_share_memory` 同款语义）。
        🔴 行不存在 ⇒ False（调用方 404）；`status` / `role` / 凭据**一个都不碰**
        —— 预算只是注入阈值的偏好，不是治理动作。
        """
        aid = (agent_id or "").strip()
        row = self._be.load(aid)
        if not row:
            return False
        want = None if value is None else int(value)
        if row.get("core_budget_chars") == want:
            return False
        self._be.update(aid, {"core_budget_chars": want})
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
        moved = self._be.rename_id(old, new)
        if moved:
            # 🔴 凭据账跟着搬。漏这一步的后果不是"显示不对"，是**所有入口当场鉴权失败**
            #    （凭据行还挂在旧 agent_id 上，`verify(new)` 名下查不到任何一把）。
            self._cred.rename_agent(old, new)
        return moved

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
        # 🔴 多凭据起，代理行的老三列**不再是鉴权依据**（它只是迁移前的快照）。
        #    比对仍然常数时间，且要 `status=active`：作废的那把即使在代理批准态下也不能用。
        got = hash_credential(credential or "")
        hit = None
        for cred in self._cred.list(aid):
            if (cred.get("status") == CRED_STATUS_ON
                    and str(cred.get("credential_hash") or "")
                    and hmac.compare_digest(got, str(cred["credential_hash"]))):
                hit = cred
                break
        if not hit:
            return {"ok": False, "role": row["role"], "status": row["status"],
                    "reason": "bad_credential"}
        # `cred_id` / `credential_label` 只给**内部**（打点与以后的按凭证授权），
        # 不进任何对外文案 —— 对外一直只到 hint（8 位）这一档。
        return {"ok": True, "role": row["role"], "status": row["status"], "reason": "",
                "cred_id": hit["cred_id"], "credential_label": hit["label"]}

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
        # 一次索引查（`credential_hash` 上有唯一索引）⇒ 不再全表逐行比。
        # 🔴 拿到行之后**仍走 verify()**：代理状态、这把的 status 都由它统一判，
        #    这里绝不自己下一个"看着对"的结论（两处判据迟早漂成两个安全等级）。
        hit = self._cred.by_hash(got)
        if not hit:
            return out
        res = self.verify(hit["agent_id"], credential)
        res["agent_id"] = hit["agent_id"]
        return res

    # ---- 活动计数 ----------------------------------------------------
    def touch(self, agent_id: str, kind: str = "read", cred_id: str = "") -> bool:
        """打活动（`read` / `write`）；**限频 60s**，返回本次是否真的写了库。

        给了 `cred_id` 就**顺带打那一把**（独立限频键 `代理#凭据`）—— 用户要的多凭据
        就是为这个：同一代理从三台机器进来，得看得出哪台在跑、哪台早就不动了。

        限频理由：检索路径每轮都可能调它，而 `last_seen_at` / `reads` 是遥测，
        不是账本 —— 每轮写库会把注册表变成热路径上的写放大点。
        """
        aid = (agent_id or "").strip()
        column = "writes" if str(kind).lower().startswith("w") else "reads"
        cid = (cred_id or "").strip()
        # 限频键分开：代理维度与凭据维度各有各的 60 秒窗口，
        # 用同一个键会因为先打到的那一边把另一边饿掉（漏计某台机器）。
        throttle_key = f"{aid}#{cid}" if cid else aid
        with self._touch_lock:
            last = self._last_touch.get(throttle_key)
            now = time.monotonic()
            if last is not None and now - last < TOUCH_MIN_INTERVAL_S:
                return False
            self._last_touch[throttle_key] = now
        if cid:
            self._cred.bump(cid, column)      # Redis 后端是 no-op（单把 = 活动已在代理行）
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
            return _public(self._cred_summary(row))
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
