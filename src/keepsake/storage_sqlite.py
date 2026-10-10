"""
SQLite 存储后端 — keepsake 第三个存储实现（批 1：读写原语 + schema 自愈）。

🔴 **本批能力边界（务必先读）**
  已实现：health_check / ensure_index / close / store / get_fragment /
    get_fragments_batch / fragment_exists / touch_fragment / scan_fragment_keys /
    write_fragments_batch / update_fragment_fields / delete_fragments_batch /
    record_feedback / supersede_fragment / set_supersedes / correct_fragments
  **未实现且显式抛 NotImplementedError**：search / search_bm25 / search_knn /
    match_attention / match_hot_topics / get_hot_topics / entity_timeline /
    discover_synonyms / generate_jieba_dict
  —— 检索与加权信号属第 2/3 批（FTS5 + sqlite-vec）。**绝不返回空值假装成功**：
  静默空 = 记忆搜不到且无告警，是本项目最危险的失败形态。

## 为什么是 SQLite（定位）
  单机 / 单代理场景的**嵌入式**后端：单文件、零服务、零第三方依赖。
  用户红线仍是「不许双后端并行」——`storage.backend` 选一个，同一时刻只认一个。

## 为什么只用标准库 sqlite3
  不新增任何第三方硬依赖。`sqlite-vec`（向量检索）留到第 3 批做**可选懒加载**。

## 并发模型（WAL，实测口径）
  * `PRAGMA journal_mode=WAL` ⇒ **多进程可并发读，单写者**；写事务进行中别的进程仍能读。
  * 并发写：靠 `PRAGMA busy_timeout=<ms>` 排队 + 写重试（`WRITE_RETRY_ATTEMPTS`）。
  * 写事务一律 `BEGIN IMMEDIATE`：开口就拿写锁，**不等到提交时才升级**
    （延迟升级是 SQLite 最典型的 `database is locked` 死锁来源）。
  * 同一实例内所有语句走一把 `RLock`：`check_same_thread=False` 的连接对象
    本身不是线程安全的，Python 侧必须自己串行化。写事务是**整个事务体**都在锁内
    （BEGIN → 语句 → COMMIT，见 `_write`），不是只锁 BEGIN 那一步。
    ponytail: 进程内读写互斥（粗粒度）。跨进程读仍是并发的（WAL）。
    升级路径：读路径另开一条只读连接（`file:...?mode=ro`），不共用这把锁。

## schema 自愈（批 1 的重点）
  1. **建表即包含全部列** ⇒ 空库首次 `ensure_index()` 一次成功。
     （★ PG 踩过的坑：`CREATE TABLE` 与后续 `ALTER` 撞车 ⇒ 迁移段回滚 ⇒
       `ensure_index` 返回 False ⇒ provider 起不来。这里从根上避开。）
  2. **先查后补**：`PRAGMA table_info` 核对，缺哪列才 `ALTER TABLE ADD COLUMN`
     （幂等；SQLite 的 ADD COLUMN 要求新列有默认值，本表所有列都带 DEFAULT）。
  3. `CREATE TABLE/INDEX IF NOT EXISTS` —— SQLite 侧这俩在并发下拿 schema 锁，
     但**不发 DDL** 的健康路径（第 2 步）零锁，本实现优先走那条。

## 与 Redis / PG 的字段对齐
  列集合以 `storage_pg.FRAGMENT_COLUMNS + MAINTENANCE_COLUMNS` 为**基准**
  （两后端同一份真相，本文件直接引用那两个常量，不复制一份），
  另加 5 列本后端**确实有读取方/写入方**的列：
    updated / touch_count  → `touch_fragment` 真正落库（PG 侧无这两列、返回 False）
    supersedes             → `set_supersedes` 真正落库（PG 侧无该列、返回 False）
    hash                   → `store()` 落内容 hash（与 key 的哈希段同源）
    embed_bin              → `store()` 落 float32 blob（与 Redis 侧同一 struct.pack 格式）
    content_tsv            → 第 2 批 FTS5 预处理文本（本批不写，留位）
    attention_score        → 任务书要求的列集覆盖（本批无读取方，第 2/3 批用）
  二进制只走 BLOB 列（`embed_bin` / `embedding`），文本列一律 str —— 与
  `storage_pg._text_field` / `storage._decode_hash_value` 同一口径：
  **静默兜底解码把二进制毁成乱码，比报错更糟。**

## 向量
  **检索**本批不做（任务书明写）。但**写入已在**：`store()` 有 embedder 就落
  float32 blob（与 Redis/PG 同一 `struct.pack` 小端格式）—— 否则切到 sqlite 后端
  的历史记忆会全部无向量，回头得补一次全量 backfill。`embedding` 列与
  `sqlite-vec` 留到第 3 批（**可选懒加载**，不是硬依赖）。
"""

from __future__ import annotations

import hashlib
import logging
import random
import sqlite3
import struct
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .emotion import analyze_emotion
from .splitter import extract_entities, extract_keywords
from .storage_base import StorageBase
# 列集真相与 TTL 口径复用 PG 版同一份常量（不复制 —— 复制必漂移）。
from .storage_pg import (
    _ATTENTION_SCOPES,
    _ATTENTION_TTL,
    _ENTITY_COOC_TTL,
    _TOPIC_SCOPES,
    _TOPIC_TTL,
    FRAGMENT_COLUMNS,
    MAINTENANCE_COLUMNS,
)

logger = logging.getLogger(__name__)

# 有文档的默认路径：`~/.keepsake/keepsake.db`（单文件，目录自动创建）
DEFAULT_SQLITE_PATH = str(Path.home() / ".keepsake" / "keepsake.db")

# 并发写排队上限：busy_timeout 内等不到写锁就重试，重试用尽才抛错。
BUSY_TIMEOUT_MS = 5000
# 预算 = WRITE_RETRY_ATTEMPTS × busy_timeout + 退避总睡眠 ≈ 6×5s + 1.6~4.7s ≈ 34s。
# ★ 为什么从 3 次提到 6 次：实测（tests 见 test_two_processes_concurrent_write_lose_nothing）
#   两个进程抢**全新库**的建表锁时，慢的那方会在 3 次 ×(5s busy + 0.2/0.4/0.6s) ≈ 6.6s
#   内抢不到锁 ⇒ `upsert_fragment` fail-open（只 warning、返回 False、不抛）
#   ⇒ **进程退出码 0 且少行** —— 本项目最危险的「静默丢数据」形态，且跨环境偶发。
#   6 次把预算抬到 ~34s，覆盖「对方正在建全部表 + 索引」的启动期窗口。
WRITE_RETRY_ATTEMPTS = 6
# 退避底数：0.05/0.1/0.2/0.4/0.8/1.6（指数）。线性退避下两个竞争者睡眠等长、
# 永远同相位碰撞（惊群），指数 + 抖动才打散。
WRITE_RETRY_BACKOFF_S = 0.05
# `IN (...)` 的参数个数上限（SQLite 默认 SQLITE_MAX_VARIABLE_NUMBER=999），
# 留一半余量。批量读/删按此分块。
SQL_PARAM_CHUNK = 400

# 留桩方法的统一文案 —— **可辨识**是硬要求（不许静默返回空）
_NOT_IMPLEMENTED = "sqlite backend: {name} 属第 2/3 批，未实现"

# BLOB 列：只有它们允许 bytes；其余列收到 bytes 一律显式报错（同 PG _text_field）
_BLOB_COLUMNS = ("embed_bin", "embedding")

# 本后端独有的附加列（见模块 docstring「与 Redis / PG 的字段对齐」）
_EXTRA_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("updated", "TEXT NOT NULL DEFAULT ''"),
    ("touch_count", "TEXT NOT NULL DEFAULT '0'"),
    ("supersedes", "TEXT NOT NULL DEFAULT ''"),
    ("hash", "TEXT NOT NULL DEFAULT ''"),
    ("attention_score", "TEXT NOT NULL DEFAULT ''"),
    ("content_tsv", "TEXT NOT NULL DEFAULT ''"),
    ("embed_bin", "BLOB"),
    ("embedding", "BLOB"),
)

#: ks_fragment 全列（声明顺序 = 建表顺序 = 批量读的返回顺序）
ALL_COLUMNS: Tuple[str, ...] = FRAGMENT_COLUMNS + MAINTENANCE_COLUMNS + tuple(
    c for c, _ in _EXTRA_COLUMNS
)
#: 列名 → 列声明。**建表语句与补列语句同源生成**（只有这一份真相）——
#: 复制两遍必然漂移，而漂移的后果是老库补不上列 ⇒ `ensure_index` 直接 False。
_COLUMN_DDL: Dict[str, str] = {c: "TEXT NOT NULL DEFAULT ''"
                               for c in FRAGMENT_COLUMNS + MAINTENANCE_COLUMNS}
_COLUMN_DDL.update(dict(_EXTRA_COLUMNS))
#: 批量读回哪几列（对齐 PG `get_fragments_batch`：全列，含维护列）
READ_COLUMNS: Tuple[str, ...] = ALL_COLUMNS

# ks_fragment 的建表语句**由 _COLUMN_DDL 生成**（与补列路径同一份声明），
# 保证「空库建全列」与「老库补缺列」永远一致。`key TEXT PRIMARY KEY` 等价 PG 的
# `key text PRIMARY KEY`（SQLite 的 TEXT 主键允许 NULL 以外的一切，唯一性由索引保证）。
_CREATE_FRAGMENT = (
    "CREATE TABLE IF NOT EXISTS ks_fragment (\n"
    + ",\n".join(
        f"    {c} {'TEXT PRIMARY KEY' if c == 'key' else _COLUMN_DDL[c]}"
        for c in ALL_COLUMNS
    )
    + "\n)"
)

# 表定义：(表名, CREATE TABLE 语句)
_DDL_TABLES: Tuple[Tuple[str, str], ...] = (
    ("ks_fragment", _CREATE_FRAGMENT),
    # 对齐 keepsake:entity_timeline:<实体> ZSET（member=碎片 key，score=时间戳）
    ("ks_entity_timeline", """
    CREATE TABLE IF NOT EXISTS ks_entity_timeline (
        entity   TEXT NOT NULL,
        frag_key TEXT NOT NULL,
        ts       REAL NOT NULL,
        PRIMARY KEY (entity, frag_key)
    )
    """),
    # 对齐 keepsake:entity_cooc ZSET（member="a||b"，score=共现次数）
    ("ks_entity_cooc", """
    CREATE TABLE IF NOT EXISTS ks_entity_cooc (
        pair      TEXT PRIMARY KEY,
        score     REAL NOT NULL DEFAULT 0,
        expire_ts REAL NOT NULL
    )
    """),
    # 对齐 keepsake:hot_topics{,:daily,:weekly} 三个 ZSET（scope 列区分）
    ("ks_hot_topic", """
    CREATE TABLE IF NOT EXISTS ks_hot_topic (
        scope     TEXT NOT NULL,
        topic     TEXT NOT NULL,
        score     REAL NOT NULL DEFAULT 0,
        expire_ts REAL NOT NULL,
        PRIMARY KEY (scope, topic)
    )
    """),
    # 对齐 keepsake:hot_topics:last_seen hash
    ("ks_hot_topic_seen", """
    CREATE TABLE IF NOT EXISTS ks_hot_topic_seen (
        topic     TEXT PRIMARY KEY,
        last_seen REAL NOT NULL
    )
    """),
    # 对齐 keepsake:attention{,:daily,:weekly} 三个 ZSET
    ("ks_attention", """
    CREATE TABLE IF NOT EXISTS ks_attention (
        scope     TEXT NOT NULL,
        topic     TEXT NOT NULL,
        score     REAL NOT NULL DEFAULT 0,
        expire_ts REAL NOT NULL,
        PRIMARY KEY (scope, topic)
    )
    """),
    # 对齐 keepsake:synonyms hash（term → JSON 数组）
    ("ks_synonym", """
    CREATE TABLE IF NOT EXISTS ks_synonym (
        term     TEXT PRIMARY KEY,
        synonyms TEXT NOT NULL DEFAULT '[]'
    )
    """),
)

_DDL_INDEXES: Tuple[Tuple[str, str], ...] = (
    ("idx_ks_entity_timeline_ts",
     "CREATE INDEX IF NOT EXISTS idx_ks_entity_timeline_ts "
     "ON ks_entity_timeline (entity, ts DESC)"),
    # 第 2 批的 FTS5 / KNN 索引不在本批建（那时才有读取方）；
    # 这里只建维护路径真正要用的两条。
    ("idx_ks_fragment_consumed_by",
     "CREATE INDEX IF NOT EXISTS idx_ks_fragment_consumed_by "
     "ON ks_fragment (consumed_by)"),
)


class _BytesFieldError(TypeError):
    """文本列收到 bytes —— 显式报错，绝不静默 str() 毁掉二进制。"""


class StorageNotReadyError(RuntimeError):
    """schema 未就绪 ⇒ 读/写路径**显式拒绝**（消息带 path 与最近一次 ensure 结果）。

    为什么不用「返回 False / 返回空」：表不存在时每条 upsert 都 `no such table`，
    逐条 warning 之后**进程照样退出码 0、库里 0 行** —— 静默丢数据，是本项目
    最危险的失败形态（实测见 docs 与 /tmp/ks_sqfl_mechanism_hold20.txt）。
    抛错后调用方至少能拿到非零退出码 + 明确的库路径，而不是「看起来成功」。
    """


def _text_value(value: Any, field: str) -> str:
    """列值 → str。bytes/bytearray **显式报错**（同 PG `_text_field`）。

    为什么不让 bytes 混进 TEXT 列：`str(b'\\xcd\\xbc')` 出来的是 `"b'\\xcd\\xbc'"`，
    读回再写就是第二次损坏 —— 与 Redis 侧 `embed_bin` 被解码成乱码是同一类事故。
    二进制只能走 BLOB 列（`embed_bin` / `embedding`）。
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        raise _BytesFieldError(
            f"storage_sqlite: 列 {field!r} 收到二进制（{type(value).__name__}）。"
            f"二进制字段只能写 BLOB 列 {_BLOB_COLUMNS}；文本列收到 bytes 说明上游漏了"
            f"解码，静默 str() 会把它毁成 \"b'...'\" 字面量。"
        )
    return value if isinstance(value, str) else str(value)


def _blob_value(value: Any) -> Optional[bytes]:
    """BLOB 列值 → bytes | None（str 走 UTF-8 编码，bytes 原样）。"""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    return bytes(value)


def _as_text(value: Any) -> str:
    """任意标量 → str（局部更新用；**不做** bytes 报错 —— 上层语义见调用点）。"""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return value if isinstance(value, str) else str(value)


def _chunks(seq: List[Any], size: int = SQL_PARAM_CHUNK) -> Iterator[List[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _sha12(text: str) -> str:
    """内容 hash 前 12 位 —— 与 Redis/PG 的 key 算法逐字同一套。"""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


# `INSERT ... ON CONFLICT(key) DO UPDATE`（SQLite ≥3.24 内建 upsert，无外部依赖）。
# 列集是模块常量 ⇒ 语句拼一次即可。
_UPSERT_SQL = (
    f"INSERT INTO ks_fragment ({', '.join(ALL_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in ALL_COLUMNS)}) "
    "ON CONFLICT(key) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in ALL_COLUMNS if c != "key")
)


class SqliteStorage(StorageBase):
    """SQLite 存储后端（嵌入式单文件）。

    与另两个后端的**失败姿态**刻意一致：写/删路径出错记日志并返回 falsy
    （沿用 Redis 历史契约），但**留桩方法显式抛 NotImplementedError**，
    检索类绝不静默返回空列表。

    🔴 **唯一的例外是 schema 未就绪**（p1.2）：此时抛 `StorageNotReadyError`
    而不是 fail-open。理由：表不存在时 fail-open 的形态是「每条 upsert 打一行
    `no such table` warning，进程退出码仍是 0、库里 0 行」—— fail-open 在这里
    等于**静默丢数据**，比抛错坏得多。逐条级错误（锁竞争、单条脏值）仍然
    fail-open + 汇总告警，与 PG/Redis 同口径。
    """

    def __init__(
        self,
        path: str = "",
        agent_id: str = "",
        is_primary: bool = False,
        embedder: Optional[Any] = None,
        embed_dim: int = 1536,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
        attention_base_increment: float = 2.0,
        attention_emotion_factor: float = 1.5,
        **_: Any,
    ):
        self._path = str(path or DEFAULT_SQLITE_PATH)
        self._agent_id = agent_id
        self._is_primary = bool(is_primary)
        # ---- embedding 写开关（判据与 Redis/PG 两侧逐字同一套）----
        self._embed_enabled = True
        self._embedder = embedder
        if embedder is not None:
            # 用 _registered 判定（不要用 `dimension == 0` —— 0 是哨兵但语义是
            # 「未登记」，靠 _registered 显式判断最稳）
            if not getattr(embedder, "_registered", True):
                logger.error(
                    "storage_sqlite: embedder %r is unregistered (dimension=0 sentinel). "
                    "EMBEDDING DISABLED — vector writes will be skipped. Add the "
                    "model to src/keepsake/embedder.py:_MODEL_DIMENSIONS.",
                    getattr(embedder, "_model", "<unknown>"),
                )
                self._embed_enabled = False
            else:
                # embedder 在场时以它的真实维度为准（调用方可能漏传 embed_dim）
                embed_dim = int(getattr(embedder, "dimension", embed_dim) or embed_dim)
        self._embed_dim = int(embed_dim)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._attention_base_increment = float(attention_base_increment)
        self._attention_emotion_factor = float(attention_emotion_factor)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        # 最近一次 ensure_index 的结果（进异常消息 ⇒ 「未就绪」可定位，不用猜）
        self._last_ensure_ok: Optional[bool] = None
        self._last_ensure_error: Optional[str] = None
        # 最近一次 upsert_fragment 的失败原因（批量写的汇总告警要「原因分类」，
        # 而 upsert 本身是 fail-open 不抛 ⇒ 只能就地留痕）
        self._last_upsert_error: Optional[str] = None

    def _has_embedder(self) -> bool:
        """能否写向量（判据与 RedisStorage._has_embedder 一致）。"""
        return (
            self._embed_enabled
            and self._embedder is not None
            and hasattr(self._embedder, "get_embedding")
        )

    def _text_to_blob(self, text: str) -> Optional[bytes]:
        """文本 → float32 二进制 blob（小端，与 Redis 侧 `struct.pack` 同格式）。

        🔴 与 Redis 侧唯一差别：没有 `embed_cache` 往返（那依赖 Redis 缓存键），
        本地后端直接调 embedder —— 少一次网络往返，无功能差异。
        本批还没有向量**读取**方（第 3 批接 sqlite-vec），但写入必须先在：
        否则切到 sqlite 后端的历史记忆全部无向量，要回头补全量 backfill。
        """
        if not self._has_embedder():
            return None
        vec = self._embedder.get_embedding(text)
        if not vec:
            return None
        return struct.pack(f"{len(vec)}f", *vec)

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """建（并缓存）连接。`:memory:` 只对**本进程本连接**成立，故不缓存复用之外的东西。

        WAL + busy_timeout 是并发正确性的两个前提，每次建连都设一遍
        （WAL 是**文件级**属性但只在建连时生效一次，busy_timeout 是连接级）。
        """
        if self._path != ":memory:":
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self._path, timeout=self._busy_timeout_ms / 1000.0,
            check_same_thread=False, isolation_level=None,   # 自己管事务 ⇒ 显式 BEGIN IMMEDIATE
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _db(self) -> sqlite3.Connection:
        """取连接（可重复调用）。断了就重连一次。"""
        with self._lock:
            if self._conn is None:
                self._conn = self._connect()
                logger.info("storage_sqlite: connected to %s", self._path)
            return self._conn

    def health_check(self) -> bool:
        """后端中立存活探针：`SELECT 1`。"""
        try:
            with self._lock:
                self._db().execute("SELECT 1").fetchone()
            return True
        except Exception as e:      # noqa: BLE001 — 探针不许抛
            logger.warning("storage_sqlite: health_check failed: %s: %s", type(e).__name__, e)
            return False

    def close(self) -> None:
        """关连接，可重复调用。"""
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("storage_sqlite: close failed: %s", e)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Cursor]:
        """写事务：`BEGIN IMMEDIATE` + busy_timeout 排队 + 有限重试。

        为什么是 IMMEDIATE：默认的 deferred 事务读到「写」时才申请写锁，
        两个事务都先读后写就会**锁升级互等** ⇒ `database is locked` 且 busy_timeout
        救不了。IMMEDIATE 开口就拿写锁，失败时立刻重试即可。

        重试只包住**拿写锁**这一步（`BEGIN`），不包事务体 —— 事务体重跑等于把
        上半截副作用再执行一遍。重试用尽 → 向上抛（调用点各自决定 fail-open 还是报错）。

        ★ 守卫放这里（而不是每个写方法各写一遍）：所有写路径都经过 `_write()`，
        一处守卫覆盖 store/upsert/删/改/反馈全部入口 —— 漏一个就是一个静默丢数据的洞。

        🔴 **p1.3：整个事务体（BEGIN → 语句 → COMMIT/ROLLBACK）都在 `self._lock` 内**
        —— 原来锁只在 `_begin_immediate()` 里持有/释放、事务体在锁外，于是同一实例
        的两个线程会共用一条 `check_same_thread=False` 的连接各开各的事务：
        后开的那个直接 `cannot start a transaction within a transaction`，
        先开的那个会被另一个线程的 COMMIT 提前提交（**静默丢数据**）。
        跨进程语义不变：WAL 照旧、`busy_timeout` 照旧 —— 进程内串行、跨进程仍排队。
        代价：同进程读写**粗粒度互斥**（写事务期间读要排队），单进程吞吐换正确性。
        ponytail: 单连接一把锁。要放开读并发就另开 `file:...?mode=ro` 只读连接。
        """
        self._require_ready("写路径")
        with self._lock:
            conn = self._begin_immediate()
            cur = conn.cursor()
            try:
                yield cur
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            try:
                conn.execute("COMMIT")
            except sqlite3.OperationalError as e:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise sqlite3.OperationalError(
                    f"storage_sqlite: COMMIT 失败（并发写被挤掉）: {e}"
                ) from e

    def _begin_immediate(self) -> sqlite3.Connection:
        """拿写锁（带重试 + 指数退避抖动），返回已开启事务的连接。"""
        last: Optional[Exception] = None
        for attempt in range(1, WRITE_RETRY_ATTEMPTS + 1):
            with self._lock:
                conn = self._db()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    return conn
                except sqlite3.OperationalError as e:
                    msg = str(e)
                    if "locked" not in msg and "busy" not in msg:
                        raise
                    last = e
                    logger.warning(
                        "storage_sqlite: 写锁被占用，第 %d/%d 次重试（%s）",
                        attempt, WRITE_RETRY_ATTEMPTS, e,
                    )
            # 抖动：多个竞争者等长睡眠会同相位碰撞（惊群），乘 0.5~1.5 打散
            time.sleep(WRITE_RETRY_BACKOFF_S * (2 ** (attempt - 1)) * (0.5 + random.random()))
        raise sqlite3.OperationalError(
            f"storage_sqlite: 取写锁重试 {WRITE_RETRY_ATTEMPTS} 次仍失败: {last}"
        ) from last

    # ------------------------------------------------------------------
    # schema 自愈
    # ------------------------------------------------------------------

    @staticmethod
    def _table_columns(cur: sqlite3.Cursor, table: str) -> set:
        """`PRAGMA table_info` → 列名集合（先查后补的依据）。"""
        return {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}

    def ensure_index(self) -> bool:
        """幂等建表 + 补列。**成功 True / 失败 False**（接口约定）。

        两条路径都必须过：
          * 空库 —— 建表语句**已含全部列**，`IF NOT EXISTS` 一次成功
          * 老库 —— `PRAGMA table_info` 核对后只发**确实缺**的 `ALTER TABLE ADD COLUMN`

        🔴 与 PG 版的差别：这里**不**做「进程内一次性记忆」。SQLite 的健康路径
        只是几条 `PRAGMA table_info`（只读、不拿 schema 锁），自愈窗口内成本可忽略；
        记忆反而会让「别的进程刚补的列」在本进程里看不见。
        """
        try:
            with self._lock:
                conn = self._db()
                cur = conn.cursor()
                for name, ddl in _DDL_TABLES:
                    if name in self._table_columns(cur, name):
                        continue
                    cur.execute(ddl)          # 建表即含全部列 ⇒ 不与下面的 ALTER 撞车
                # 先查后补：只补真缺的列（SQLite 的 ADD COLUMN 要求新列有默认值）
                added: List[str] = []
                have = self._table_columns(cur, "ks_fragment")
                for col in ALL_COLUMNS:
                    if col == "key" or col in have:
                        continue
                    cur.execute(f"ALTER TABLE ks_fragment ADD COLUMN {col} {_COLUMN_DDL[col]}")
                    added.append(col)
                # 🔴 索引必须在补列**之后**建：老库上 consumed_by 列可能还没有，
                #    先建索引就是 `no such column: consumed_by` ⇒ ensure_index False。
                for _, ddl in _DDL_INDEXES:
                    cur.execute(ddl)
            if added:
                logger.info("storage_sqlite: ks_fragment 补列 %d 个: %s", len(added), added)
            logger.info("storage_sqlite: schema ready on %s", self._path)
            self._last_ensure_ok, self._last_ensure_error = True, None
            return True
        except Exception as e:      # noqa: BLE001 — 接口约定：失败返回 False
            logger.error("storage_sqlite: ensure_index 失败: %s: %s", type(e).__name__, e)
            self._last_ensure_ok = False
            self._last_ensure_error = f"{type(e).__name__}: {e}"
            return False

    def _schema_ready(self) -> Tuple[bool, Optional[str]]:
        """`ks_fragment` 是否已具备**全部列**（写路径的前提）。

        只发 `PRAGMA table_info`（只读、不拿 schema 锁、不发 DDL），健康路径成本可忽略。
        表不存在 ⇒ 返回空集合 ⇒ 判为未就绪。**探测本身出错也判未就绪**（不是就绪）。
        返回 (是否就绪, 探测失败时的原因)。
        """
        try:
            with self._lock:
                have = self._table_columns(self._db().cursor(), "ks_fragment")
        except Exception as e:      # noqa: BLE001 — 探测失败即「未就绪」，绝不当作就绪
            return False, f"{type(e).__name__}: {e}"
        if set(ALL_COLUMNS) <= have:
            return True, None
        return False, "ks_fragment 缺列: " + ",".join(sorted(set(ALL_COLUMNS) - have))

    def _require_ready(self, op: str) -> None:
        """读写路径入口守卫：schema 未就绪 ⇒ **补跑一次** `ensure_index()`，仍不就绪就抛。

        为什么要守卫：表不存在时每条 upsert 都会 `no such table`，逐条 warning 之后
        进程**照样退出码 0、库里 0 行** —— 这就是「静默丢数据」，必须消除。

        为什么允许补跑（而不是一次 ensure 失败就抛）：WAL 下「另一进程正在建表」
        是**可重试的竞争**（`ensure_index` 自带 busy_timeout 排队），此时显式拒绝会
        把「对方 3s 后就建好了」误判成永久故障。补跑一次仍不就绪 ⇒ 才判定为真故障。
        """
        ready, why = self._schema_ready()
        if not ready and self.ensure_index():
            ready, why = self._schema_ready()
        if not ready:
            raise StorageNotReadyError(
                f"storage_sqlite: {op} 被拒绝 —— schema 未就绪（path={self._path}；"
                f"补跑后仍不就绪：{why or '未知'}；最近一次 ensure_index()="
                f"{self._last_ensure_ok!r} err={self._last_ensure_error}）。"
                f"不做逐条写入 —— 那只会得到「进程退出码 0 + 库里 0 行」的静默丢数据。"
            )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    @staticmethod
    def _fragment_key_for(text: str) -> str:
        """碎片 key —— 与 Redis/PG store() 同一套算法（sha256 前 12 位）。"""
        return f"memory:frag:{_sha12(text)}"

    def _agent_tags(self, tags: str) -> str:
        """注入 `agent:<id>` 标签（与 Redis/PG 同一套规则：旧的 agent: 全替换）。"""
        if not self._agent_id:
            return tags
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        filtered = [t for t in tag_list if not t.startswith("agent:")]
        filtered.append(f"agent:{self._agent_id}")
        return ",".join(filtered)

    def upsert_fragment(self, fields: Dict[str, Any]) -> bool:
        """按给定字段**原样**写一条碎片（迁移专用，幂等）。

        与 `store()` 的区别：这里**用调用方给的 key**（`store()` 按内容 hash 造 key、
        遇同内容另起 `:<epoch>` 新键）；`ON CONFLICT DO UPDATE` 只覆盖传入的列，
        未提供的列保持原值不被清空。
        """
        key = fields.get("key")
        if not key:
            logger.warning("storage_sqlite: upsert_fragment 缺 key，跳过")
            self._last_upsert_error = "缺 key"
            return False
        self._last_upsert_error = None
        row: Dict[str, Any] = {}
        for col in ALL_COLUMNS:
            if col == "key":
                continue
            if col not in fields:
                continue
            row[col] = (_blob_value(fields[col]) if col in _BLOB_COLUMNS
                        else _text_value(fields[col], col))
        params: List[Any] = [str(key)]
        for col in ALL_COLUMNS:
            if col == "key":
                continue
            params.append(row.get(col, None if col in _BLOB_COLUMNS else ""))
        try:
            with self._write() as cur:
                cur.execute(_UPSERT_SQL, params)
            return True
        except (_BytesFieldError, StorageNotReadyError):
            raise
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: upsert_fragment(%s) failed: %s", key, e)
            self._last_upsert_error = f"{type(e).__name__}: {e}"
            return False

    def store(
        self,
        text: str,
        tags: str = "",
        category: str = "",
        source: str = "",
        fragment_type: str = "",
        sentiment_score: Optional[float] = None,
        sentiment_label: Optional[str] = None,
    ) -> bool:
        """写入碎片。字段/去重/版本化/热词语义与 RedisStorage.store、PgStorage.store 一致。"""
        # 情绪分析（除非明确传入）
        if sentiment_score is None or sentiment_label is None:
            intensity, label = analyze_emotion(text)
        else:
            intensity, label = sentiment_score, sentiment_label

        keywords = extract_keywords(text, max_keywords=5)
        final_tags = self._agent_tags(tags)
        entities = extract_entities(text)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        content_hash = _sha12(text)
        key = f"memory:frag:{content_hash}"

        try:
            with self._write() as cur:
                # 去重：同内容已存在 → 保留 feedback_score + 旧版标失效 + 新版独立 key
                row = cur.execute(
                    "SELECT feedback_score FROM ks_fragment WHERE key = ?", (key,)
                ).fetchone()
                existing_feedback = (row[0] if row and row[0] else "0") or "0"
                if row is not None:
                    cur.execute(
                        "UPDATE ks_fragment SET valid_until = ?, is_archived = '1' WHERE key = ?",
                        (now_iso, key),
                    )
                    key = f"{key}:{int(time.time())}"
                entities_str = ",".join(entities) if entities else ""
                cur.execute(
                    """
                    INSERT INTO ks_fragment (
                        key, content, tags, category, source, created, updated, hash,
                        sentiment_score, sentiment_label, feedback_score,
                        entities, fragment_type, embed_bin
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET
                        content=excluded.content, tags=excluded.tags,
                        category=excluded.category, source=excluded.source,
                        created=excluded.created, updated=excluded.updated,
                        hash=excluded.hash,
                        sentiment_score=excluded.sentiment_score,
                        sentiment_label=excluded.sentiment_label,
                        feedback_score=excluded.feedback_score,
                        entities=excluded.entities, fragment_type=excluded.fragment_type,
                        embed_bin=COALESCE(excluded.embed_bin, ks_fragment.embed_bin)
                    """,
                    (key, text, final_tags, category, source, now_iso, now_iso,
                     content_hash, str(intensity), label, existing_feedback,
                     entities_str, fragment_type, self._text_to_blob(text)),
                )
                self._record_attention(cur, keywords, intensity, now_ts=now.timestamp())
                if entities:
                    self._record_entity_cooc(cur, entities, now_ts=now.timestamp())
                    ts = now.timestamp()
                    cur.executemany(
                        "INSERT INTO ks_entity_timeline (entity, frag_key, ts) VALUES (?,?,?) "
                        "ON CONFLICT(entity, frag_key) DO UPDATE SET ts=excluded.ts",
                        [(ent, key, ts) for ent in entities],
                    )
                self._record_topics(cur, keywords, label, now_ts=now.timestamp())
            return True
        except StorageNotReadyError:
            raise
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: store error: %s", e)
            return False

    def _upsert_scores(self, cur: sqlite3.Cursor, table: str, key_cols: Tuple[str, ...],
                       rows: List[tuple]) -> None:
        """分数累加的 upsert（score 相加、expire_ts 覆盖）—— 对齐 PG 的 DO UPDATE。"""
        if not rows:
            return
        cols = key_cols + ("score", "expire_ts")
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in key_cols)
        cur.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT({', '.join(key_cols)}) DO UPDATE SET {updates}",
            rows,
        )

    def _record_attention(self, cur: sqlite3.Cursor, keywords: List[str],
                          intensity: float, now_ts: float) -> None:
        """注意力累加（对齐 attention.record_attention 的公式与三档 TTL）。"""
        increment = self._attention_base_increment + intensity * self._attention_emotion_factor
        rows = [
            (scope, kw.lower().strip(), increment, now_ts + _ATTENTION_TTL.get(scope, 86400))
            for kw in keywords
            for scope in _ATTENTION_SCOPES
            if len(kw.lower().strip()) >= 2
        ]
        self._upsert_scores(cur, "ks_attention", ("scope", "topic"), rows)

    def _record_entity_cooc(self, cur: sqlite3.Cursor, entities: List[str],
                            now_ts: float) -> None:
        """实体共现对累加（对齐 Redis `_record_entity_cooccurrence`）。"""
        if len(entities) < 2:
            return
        ents = sorted(e.lower().strip() for e in entities if e.strip())
        expire_ts = now_ts + _ENTITY_COOC_TTL
        rows = [(f"{ents[i]}||{ents[j]}", 1.0, expire_ts)
                for i in range(len(ents)) for j in range(i + 1, len(ents))]
        self._upsert_scores(cur, "ks_entity_cooc", ("pair",), rows)

    def _record_topics(self, cur: sqlite3.Cursor, keywords: List[str],
                       sentiment_label: str, now_ts: float) -> None:
        """热词累加（对齐 Redis `_record_topics` 的情感权重与三榜 TTL）。"""
        if not keywords:
            return
        sentiment_weight = {"positive": 1.5, "negative": 1.3}.get(sentiment_label, 1.0)
        rows = [(scope, kw, sentiment_weight, now_ts + _TOPIC_TTL[scope])
                for kw in keywords for scope in _TOPIC_SCOPES]
        self._upsert_scores(cur, "ks_hot_topic", ("scope", "topic"), rows)
        cur.executemany(
            "INSERT INTO ks_hot_topic_seen (topic, last_seen) VALUES (?,?) "
            "ON CONFLICT(topic) DO UPDATE SET last_seen=excluded.last_seen",
            [(kw, now_ts) for kw in keywords],
        )

    def supersede_fragment(self, old_key: str, new_key: str) -> bool:
        """封边：标 superseded_by + superseded_at（不物理删）。"""
        if not old_key:
            return False
        try:
            with self._write() as cur:
                cur.execute(
                    "UPDATE ks_fragment SET superseded_by = ?, superseded_at = ? WHERE key = ?",
                    (new_key or "__void__", datetime.now(timezone.utc).isoformat(), old_key),
                )
            return True
        except StorageNotReadyError:
            raise
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: supersede_fragment %s→%s failed: %s",
                           old_key, new_key, e)
            return False

    def correct_fragments(self, keys: List[str]) -> int:
        """打 corrected 标签 + feedback_score=-1，返回实际处理条数。"""
        keys = [k for k in keys if k]
        if not keys:
            return 0
        now_iso = datetime.now(timezone.utc).isoformat()
        count = 0
        try:
            with self._write() as cur:
                for chunk in _chunks(keys):
                    placeholders = ", ".join("?" for _ in chunk)
                    rows = cur.execute(
                        f"SELECT key, tags FROM ks_fragment WHERE key IN ({placeholders})",
                        chunk,
                    ).fetchall()
                    for key, tags in sorted(rows):   # 排序即固定加锁顺序（对齐 PG 侧）
                        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
                        if "corrected" not in tag_list:
                            tag_list.append("corrected")
                            cur.execute(
                                "UPDATE ks_fragment SET tags = ?, feedback_score = '-1',"
                                " corrected_at = ? WHERE key = ?",
                                (",".join(tag_list), now_iso, key),
                            )
                        else:
                            cur.execute(
                                "UPDATE ks_fragment SET feedback_score = '-1',"
                                " corrected_at = ? WHERE key = ?", (now_iso, key),
                            )
                        count += 1
        except StorageNotReadyError:
            raise
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: correct_fragments failed: %s", e)
            return 0
        if count:
            logger.info("storage_sqlite: corrected %d fragments", count)
        return count

    def record_feedback(self, fragment_key: str, is_positive: bool) -> bool:
        """反馈累加：有用 +1 / 没用 -2（与 Redis `HINCRBY` 同公式）。

        语义对齐 PG：UPDATE 命中 0 行也返回 True（Redis 的 HINCRBY 会建 hash，
        PG 的 UPDATE 命中 0 行；两边都按「调用成功即 True」处理，不分叉）。
        """
        delta = 1 if is_positive else -2
        try:
            with self._write() as cur:
                cur.execute(
                    "UPDATE ks_fragment SET feedback_score = CAST(ROUND("
                    "COALESCE(NULLIF(feedback_score, ''), '0') + ?"
                    ") AS TEXT) WHERE key = ?",
                    (delta, fragment_key),
                )
            return True
        except StorageNotReadyError:
            raise
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: record_feedback(%s) failed: %s", fragment_key, e)
            return False

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_fragment(row: tuple, columns: Tuple[str, ...] = READ_COLUMNS) -> Dict[str, Any]:
        """DB 行 → 碎片 dict（值全是 str/BLOB，空值不进 dict）。

        与 Redis hash / PG `_row_to_fragment` 同一份稀疏语义：只存写过的字段。
        **BLOB 列保持 bytes 原样**（不解码、不 str）—— 与 `storage._decode_hash_value`
        同一口径：解不了就原样留，静默兜底解码会把向量 blob 静默毁成乱码。
        """
        out: Dict[str, Any] = {}
        for name, value in zip(columns, row):
            if name == "key" or value is None or value == "":
                continue
            out[name] = value if isinstance(value, (bytes, bytearray)) else str(value)
        return out

    def get_fragment(self, key: str) -> Optional[Dict[str, Any]]:
        """读单个碎片全字段；不存在返回 None。**schema 未就绪则显式拒绝**（见 `_require_ready`）。"""
        if not key:
            return None
        self._require_ready("get_fragment")
        try:
            with self._lock:
                row = self._db().execute(
                    f"SELECT {', '.join(READ_COLUMNS)} FROM ks_fragment WHERE key = ?", (key,)
                ).fetchone()
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: get_fragment(%s) error: %s", key, e)
            return None
        return self._row_to_fragment(row) if row else None

    def get_fragments_batch(self, keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量读；缺失的 key 不出现在结果里。

        🔴 对齐 Redis 侧 `get_fragments_batch` 的 **HGETALL** 语义 ⇒ 这里读**全列**。
        ★ Redis 踩过的坑（`embed_bin` 一律 utf-8 解码 ⇒ 抛异常被吞 ⇒ 整批返回空
        ⇒ 合并/遗忘静默扫 0 条）：SQLite 的 TEXT/BLOB 本就分型，**BLOB 原样返回 bytes**，
        结构上不可能复现那条 bug；真出错也只 `logger.warning`（不能是 debug）。
        """
        out: Dict[str, Dict[str, Any]] = {}
        keys = [k for k in keys if k]
        if not keys:
            return out
        self._require_ready("get_fragments_batch")
        cols = ("key",) + tuple(c for c in READ_COLUMNS if c != "key")
        sql = f"SELECT {', '.join(cols)} FROM ks_fragment WHERE key IN ({{}})"
        try:
            with self._lock:
                cur = self._db().cursor()
                for chunk in _chunks(keys):
                    placeholders = ", ".join("?" for _ in chunk)
                    for row in cur.execute(sql.format(placeholders), chunk):
                        out[row[0]] = self._row_to_fragment(row, cols)
        except Exception as e:      # noqa: BLE001
            # 带上 key 数便于判断是单页问题还是全库问题（同 Redis 侧）。
            logger.warning("storage_sqlite: get_fragments_batch failed (%d keys, got %d): %s",
                           len(keys), len(out), e)
        return out

    # ------------------------------------------------------------------
    # 维护原语（合并 / 遗忘）
    #
    # 🔴 扫描用 **keyset 分页**（`key > cursor ORDER BY key LIMIT n`），**禁止 OFFSET**：
    # 全量表上 `OFFSET n` 要先扫掉 n 行才吐数据，页数越多越慢。keyset 走主键索引。
    # ------------------------------------------------------------------

    def scan_fragment_keys(
        self,
        cursor: str = "",
        limit: int = 200,
        prefix: str = "memory:frag:",
    ) -> Tuple[str, List[str]]:
        """keyset 分页扫描 key。对齐 Redis `SCAN MATCH prefix*` 与 PG 侧同款 keyset。

        游标 = 上一页最后一个 key（`""` = 从头）。`next_cursor=""` 表示扫完
        （页不满即到底；页恰好满则下一轮返回空页 + `""`，多一次空查询，不影响正确性）。
        """
        limit = max(1, int(limit))
        self._require_ready("scan_fragment_keys")
        try:
            with self._lock:
                rows = self._db().execute(
                    "SELECT key FROM ks_fragment WHERE key > ? AND key LIKE ? "
                    "ORDER BY key LIMIT ?",
                    (cursor or "", f"{prefix}%", limit),
                ).fetchall()
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: scan_fragment_keys failed (cursor=%s): %s", cursor, e)
            return "", []
        keys = [r[0] for r in rows]
        if not keys:
            return "", []
        if len(keys) < limit:
            return "", keys
        return keys[-1], keys

    def write_fragments_batch(self, rows: List[Dict[str, Any]]) -> int:
        """批量 upsert。复用 `upsert_fragment`（它已负责分型/二进制校验）。

        合并每组只写 1 条 consolidated 碎片 ⇒ 逐条调用即可，
        **不引入第二条写路径**（一条新路径 = 一份要单独测的语义，同 PG 版口径）。

        🔴 **失败可见**（p1.2）：返回值是**实际成功条数**，且有失败时打**一条**汇总
        WARNING（成功 M / 失败 N + 原因分类）。「缺 key」的旧行为是静默 `continue`
        —— 调用方拿到 0 却不知道自己丢了东西；现在它计入失败数并出现在汇总里。
        与 PG/Redis 一致仍是 **fail-open**（个别条失败不抛；schema 整体未就绪由
        `_write()` 的守卫抛 `StorageNotReadyError`，那是另一回事、不在此列）。
        """
        if not rows:
            return 0
        n = 0
        reasons: Dict[str, int] = {}
        for row in rows:
            if not row.get("key"):
                why = "缺 key"
            elif self.upsert_fragment(row):
                n += 1
                continue
            else:
                why = self._last_upsert_error or "未知原因"
            reasons[why] = reasons.get(why, 0) + 1
        if reasons:
            logger.warning(
                "storage_sqlite: write_fragments_batch 部分失败：成功 %d / 共 %d（失败 %d）；"
                "原因分类: %s",
                n, len(rows), len(rows) - n,
                "; ".join(f"{why} ×{c}" for why, c in reasons.items()),
            )
        return n

    def update_fragment_fields(self, key: str, fields: Dict[str, Any]) -> bool:
        """局部 UPDATE，**不碰 content / content_tsv / embedding**。

        只更新白名单列里出现的字段；其它 key 一律忽略（拼 SQL 前必须白名单校验）。
        """
        if not key or not fields:
            return False
        allowed = set(ALL_COLUMNS) - {"key"}
        cols = [c for c in fields if c in allowed]
        if not cols:
            return False
        set_sql = ", ".join(f"{c} = ?" for c in cols)
        params = [(_blob_value(fields[c]) if c in _BLOB_COLUMNS else _as_text(fields[c]))
                  for c in cols] + [key]
        try:
            with self._write() as cur:
                cur.execute(f"UPDATE ks_fragment SET {set_sql} WHERE key = ?", params)
                return cur.rowcount > 0
        except StorageNotReadyError:
            raise
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: update_fragment_fields(%s) failed: %s", key, e)
            return False

    def delete_fragments_batch(self, keys: List[str]) -> int:
        """批量硬删 + 时间线清理（碎片没了，时间线成员就是死引用）。

        hot_topic / attention / hot_topic_seen 是**聚合信号**（话题级、非碎片级），
        Redis 侧 forget 也不动它们 ⇒ 这里同样不动，保持后端语义等价。
        """
        keys = [k for k in keys if k]
        if not keys:
            return 0
        deleted = 0
        try:
            with self._write() as cur:
                for chunk in _chunks(keys):
                    placeholders = ", ".join("?" for _ in chunk)
                    cur.execute(
                        f"DELETE FROM ks_entity_timeline WHERE frag_key IN ({placeholders})",
                        chunk,
                    )
                    deleted += cur.execute(
                        f"DELETE FROM ks_fragment WHERE key IN ({placeholders})", chunk,
                    ).rowcount
        except StorageNotReadyError:
            raise
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: delete_fragments_batch failed: %s", e)
            return 0
        return deleted

    # ------------------------------------------------------------------
    # 细粒度能力探针（StorageBase）
    #
    # 与 PG 侧的差别（**都是功能更全，不是降级**）：
    #   touch_fragment → PG 无 touch_count/updated_at 列、返回 False；
    #                   本后端建表时就有这两列 ⇒ 真落库返回 True
    #   set_supersedes → PG 无 supersedes 列、返回 False；本后端有 ⇒ 真落库
    # ------------------------------------------------------------------

    def fragment_exists(self, key: str) -> Optional[bool]:
        """EXISTS 的等价实现：主键点查。真查库（不可达时异常向上抛，绝不返回 None 假装「查不到」）。"""
        if not key:
            return False
        self._require_ready("fragment_exists")
        with self._lock:
            return self._db().execute(
                "SELECT 1 FROM ks_fragment WHERE key = ?", (key,)
            ).fetchone() is not None

    def touch_fragment(self, key: str) -> bool:
        """刷新「最近命中」：updated=now + touch_count+1，**绝不覆盖 content**。"""
        if not key:
            return False
        try:
            with self._write() as cur:
                cur.execute(
                    "UPDATE ks_fragment SET updated = ?, "
                    "touch_count = CAST(CAST(COALESCE(NULLIF(touch_count,''),'0') AS INTEGER)"
                    " + 1 AS TEXT) WHERE key = ?",
                    (datetime.now(timezone.utc).isoformat(), key),
                )
                return cur.rowcount > 0
        except StorageNotReadyError:
            raise
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: touch_fragment(%s) failed: %s", key, e)
            return False

    def set_supersedes(self, new_key: str, old_key: str) -> bool:
        """给新碎片打单向 `supersedes=<old_key>` 留痕（反向封边走 `supersede_fragment`）。"""
        if not new_key or not old_key:
            return False
        try:
            with self._write() as cur:
                cur.execute(
                    "UPDATE ks_fragment SET supersedes = ? WHERE key = ?", (old_key, new_key)
                )
                return cur.rowcount > 0
        except StorageNotReadyError:
            raise
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: set_supersedes(%s<-%s) failed: %s",
                           new_key, old_key, e)
            return False

    # ------------------------------------------------------------------
    # 留桩（批 2/3）—— 显式抛错，绝不静默返回空值
    # ------------------------------------------------------------------

    def search(self, query: str, tag_filter: str = "", agent_id: str = "",
               is_primary: Optional[bool] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError(_NOT_IMPLEMENTED.format(name="search"))

    def search_bm25(self, query: str, tag_filter: str = "", agent_id: str = "",
                    is_primary: Optional[bool] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError(_NOT_IMPLEMENTED.format(name="search_bm25"))

    def search_knn(self, query: str, tag_filter: str = "", agent_id: str = "",
                   is_primary: Optional[bool] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError(_NOT_IMPLEMENTED.format(name="search_knn"))

    def match_attention(self, content: str, top_n: int = 10) -> float:
        raise NotImplementedError(_NOT_IMPLEMENTED.format(name="match_attention"))

    def match_hot_topics(self, text: str, limit: int = 10) -> float:
        raise NotImplementedError(_NOT_IMPLEMENTED.format(name="match_hot_topics"))

    def get_hot_topics(self, limit: int = 10, period: str = "all") -> List[Dict[str, Any]]:
        raise NotImplementedError(_NOT_IMPLEMENTED.format(name="get_hot_topics"))

    def entity_timeline(self, entity: str, limit: int = 20) -> List[Dict[str, Any]]:
        raise NotImplementedError(_NOT_IMPLEMENTED.format(name="entity_timeline"))

    def discover_synonyms(self, rebuild: bool = False) -> Dict[str, Any]:
        raise NotImplementedError(_NOT_IMPLEMENTED.format(name="discover_synonyms"))

    def generate_jieba_dict(self, output_path: str = None) -> Dict[str, Any]:
        raise NotImplementedError(_NOT_IMPLEMENTED.format(name="generate_jieba_dict"))
