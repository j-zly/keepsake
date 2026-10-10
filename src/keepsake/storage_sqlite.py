"""
SQLite 存储后端 — keepsake 第三个存储实现（批 1：读写原语 + schema 自愈；批 2：检索）。

🔴 **本批能力边界（务必先读）**
  已实现：health_check / ensure_index / close / store / get_fragment /
    get_fragments_batch / fragment_exists / touch_fragment / scan_fragment_keys /
    write_fragments_batch / update_fragment_fields / delete_fragments_batch /
    record_feedback / supersede_fragment / set_supersedes / correct_fragments
    —— 批 2 —— search / search_bm25（jieba + FTS5 + bm25 + 共用重排 + 加权信号）
    match_attention / match_hot_topics / get_hot_topics / entity_timeline
  **未实现且显式抛 NotImplementedError**：search_knn（需要 `sqlite-vec` 扩展，
    第 3 批可选懒加载）/ discover_synonyms / generate_jieba_dict
  —— **绝不返回空值假装成功**：静默空 = 记忆搜不到且无告警，是本项目最危险的
  失败形态。向量路径不可用时 `search_knn` **抛明确 NotImplementedError** 并说明
  需要该扩展，`search` 则记 WARNING 后降级为 BM25 单路（降级可辨识、不静默）。

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
    content_tsv            → 批 2：FTS5 的分词文本（jieba 切词后空格分隔）
    attention_score        → 任务书要求的列集覆盖（本批无读取方，第 2/3 批用）
  二进制只走 BLOB 列（`embed_bin` / `embedding`），文本列一律 str —— 与
  `storage_pg._text_field` / `storage._decode_hash_value` 同一口径：
  **静默兜底解码把二进制毁成乱码，比报错更糟。**

## 批 2：检索（FTS5 + jieba + BM25 + 共用重排 + 辅助结构）
  * **分词在 Python 侧**（jieba），与 PG 同路：写库时切一次存进 `content_tsv`，
    查库时 FTS5 只在**空格分隔的 token 串**上匹配。
    🔴 为什么不用 trigram 分词器：它有 3 字下限，「证书续签」里的「证书」这种
    2 字词查恒 0 —— 中文检索里 2 字词占大头，必须靠 jieba 预切 + 默认分词器。
  * **打分复用 PG 的同一个 `storage_pg.bm25_score`**（p2.1 起）：FTS5 只负责**召回**
    与候选窗口粗排（对应 PG 侧候选 CTE 里的 `ts_rank_cd`），最终相关性分在 Python 侧
    按 PG 的 RediSearch 公式算 —— 公式复制一份必然漂移，而漂移的后果就是两后端同一
    份语料的名次对不上（实测：`记忆检索` 这类同分密集的查询只有 2/5 重合）。
    FTS5 内建 `bm25()` 只在「取负翻正后」用来给候选窗口排序，不再当最终分。
  * **排序/融合/过滤一律复用 `storage_shared`**（`rerank_with_decay` /
    `apply_v2_filters` / `rrf_fuse` / `attention_boost_from_topics` /
    `hot_topic_weighted_hits`）—— 三后端一份实现是用户明确要求，本文件不重写。
  * **降级路径可辨识**：`sqlite-vec` 未装 ⇒ `search_knn` 抛
    `NotImplementedError`（消息点名需要该扩展），**绝不静默返回空**；
    `search` 在向量路不可用时记 WARNING 后降级 BM25 单路并照常返回结果。

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
import re
import sqlite3
import struct
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .emotion import analyze_emotion
from .splitter import extract_entities, extract_keywords, segment_query
from .storage_base import StorageBase
# 检索后处理与排序权重公式：与 Redis / PG **同一个函数对象**（不重写、不复制）。
from .storage_shared import (
    DECAY_HALF_DAYS,
    FEEDBACK_NEGATIVE_PENALTY,
    FEEDBACK_POSITIVE_BOOST,
    HOT_TOPIC_BOOST,
    HOT_TOPIC_DECAY_HALF_DAYS,
    SEARCH_FIELDS,
    apply_v2_filters,
    attention_boost_from_topics,
    hot_topic_weighted_hits,
    load_fragments_by_keys,
    rrf_fuse,
    rerank_with_decay,
)
# 查询式构造复用 Redis 侧的同一套（同义词扩展 + 拆子词的 sanitize），同 PG 侧口径。
from .storage import _expand_terms, _sanitize_terms
# 列集真相与 TTL 口径复用 PG 版同一份常量（不复制 —— 复制必漂移）。
from .storage_pg import (
    DEFAULT_BM25_LIMIT,
    DEFAULT_CANDIDATE_COUNT,
    DEFAULT_FINAL_LIMIT,
    MAX_CONTENT_LEN,
    _ATTENTION_SCOPES,
    _ATTENTION_TTL,
    _ENTITY_COOC_TTL,
    _TOPIC_SCOPE_ALL,
    _TOPIC_SCOPES,
    _TOPIC_TTL,
    FRAGMENT_COLUMNS,
    MAINTENANCE_COLUMNS,
    # BM25 公式本身也**复用 PG 的同一个函数对象**（p2.1 B3）：打分公式复制一份
    # 必然漂移，而漂移的后果就是同序前缀对不上（实测：FTS5 内建 bm25 与它不一致）。
    bm25_score,
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

#: FTS5 虚表名。**与 ks_fragment 解耦**（不是 external-content 表）——
#: SQLite 的 external-content 表要求删除时手工喂旧 token 串（`'delete'` 命令），
#: 一旦某条路径漏喂就留下不可见的僵尸行；独立虚表 `DELETE FROM ... WHERE key=?`
#: 语义直白、不可能漏。代价是 token 串存两份（content_tsv 是「真相」，虚表是索引）。
FTS_TABLE = "ks_fragment_fts"
_CREATE_FTS = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
    # key 只作回表锚点，不进索引（UNINDEXED），与 PG 侧 tsvector 不含 key 同理。
    f"frag_key UNINDEXED, content_tok, tokenize='unicode61')"
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
    # 批 2：FTS5 虚表（与上表同源，ensure_index 的「先查后补」循环一并建）
    (FTS_TABLE, _CREATE_FTS),
)

_DDL_INDEXES: Tuple[Tuple[str, str], ...] = (
    ("idx_ks_entity_timeline_ts",
     "CREATE INDEX IF NOT EXISTS idx_ks_entity_timeline_ts "
     "ON ks_entity_timeline (entity, ts DESC)"),
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


#: unicode61 的词元口径 = `\w+`（含下划线，ASCII 大小写由 unicode61 折叠）。
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tsv_lexemes(text: str) -> List[str]:
    """文本 → **词元**列表（按 unicode61 的口径切开；纯标点/空白自然消失）。

    🔴 为什么要多切一刀：jieba 会吐出带连字符/点号的整块（如 `needs-attention`）。
    FTS5 的 `unicode61` 会把它索引成**两个** token（needs / attention），PG 的
    `to_tsvector('simple', …)` 同样 ⇒ 但 `content_tsv` 里仍是一个空格分隔的「词」，
    于是「按空格数长度」与「按词元数长度」算出来的 doclen / avgdl 会漂。
    这里统一成 unicode61 词元（等价于 PG 的 lexeme），FTS5 索引内容**逐字不变**
    （unicode61 本来就是这么切的），但 Python 侧与 SQL 侧的计数口径对齐了。
    """
    return _WORD_RE.findall(text or "")


def _tok_string(text: str) -> str:
    """文本 → FTS5 可索引的**空格分隔 token 串**（jieba 切词，与 PG 同路）。

    🔴 为什么必须切：FTS5 的 `unicode61` 分词器按「非字母数字」切，
    一整串中文会被当成**一个** token ⇒ 查「流程」永远命中不了「部署流程」。
    预切 + 空格分隔后，unicode61 把空格当天然边界，索引/查询两侧口径一致。

    🔴 为什么不用 trigram 分词器：它有 3 字下限，中文里 2 字词（网关/备份）
    占大头 ⇒ 查 2 字恒 0 命中。任务书明写「不要 trigram」。
    """
    import jieba  # noqa: PLC0415 — 与 splitter / storage_pg 同款延迟 import（首调建词典）

    return " ".join(_tsv_lexemes(" ".join(w.strip() for w in jieba.lcut(text or ""))))


def _fts_match_expr(terms: List[str]) -> str:
    """查询词 → FTS5 MATCH 表达式（每个词用双引号包住，OR 连接）。

    为什么逐词加引号：FTS5 查询式里裸词含 `-` `.` `:` 等会被当语法（NEAR/`-` 排除），
    一个特殊符号就**整条查询报错** → 静默 0 召回（本项目最典型的静默失败形态）。
    引号内按字面量匹配，双引号本身按 FTS5 规则写两遍转义。
    """
    parts = ['"' + t.replace('"', '""') + '"' for t in terms if t]
    return " OR ".join(parts)


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

    # ---- 检索后处理：与 Redis / PG **同一个函数对象**（三后端一份实现）----
    _rrf_fuse = rrf_fuse
    _apply_v2_filters = apply_v2_filters
    _rerank_with_decay = rerank_with_decay
    _load_fragments_by_keys = load_fragments_by_keys

    def __init__(
        self,
        path: str = "",
        agent_id: str = "",
        is_primary: bool = False,
        embedder: Optional[Any] = None,
        embed_dim: int = 1536,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
        # ---- 检索参数（键名/默认值与 PG 版逐字同源，值全部来自配置）----
        candidate_count: int = DEFAULT_CANDIDATE_COUNT,
        final_limit: int = DEFAULT_FINAL_LIMIT,
        bm25_limit: int = DEFAULT_BM25_LIMIT,
        decay_half_days: int = DECAY_HALF_DAYS,
        attention_boost_max: float = 1.5,
        attention_base_increment: float = 2.0,
        attention_emotion_factor: float = 1.5,
        hot_topic_decay_half_days: int = HOT_TOPIC_DECAY_HALF_DAYS,
        hot_topic_boost: float = HOT_TOPIC_BOOST,
        emotion_intensity_factor: float = 0.4,
        feedback_positive_boost: float = FEEDBACK_POSITIVE_BOOST,
        feedback_negative_penalty: float = FEEDBACK_NEGATIVE_PENALTY,
        v2_min_score: float = 0.05,
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
        # ---- 检索参数（与 PG 同名同义：共用重排按这些键取值，一个都不能少）----
        self._candidate_count = int(candidate_count)
        self._final_limit = int(final_limit)
        self._bm25_limit = int(bm25_limit)
        self._decay_half_days = int(decay_half_days)
        self._emotion_intensity_factor = float(emotion_intensity_factor)
        self._feedback_positive_boost = float(feedback_positive_boost)
        self._feedback_negative_penalty = float(feedback_negative_penalty)
        self._hot_topic_boost = float(hot_topic_boost)
        self._v2_min_score = float(v2_min_score)
        self._attention_boost_max = float(attention_boost_max)
        self._attention_base_increment = float(attention_base_increment)
        self._attention_emotion_factor = float(attention_emotion_factor)
        self._hot_topic_decay_half_days = int(hot_topic_decay_half_days)
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
                # 🔴 FTS 自愈（批 2）：批 1 建的库**有数据但索引是空的**
                #    （虚表当时才建/才补）。行数不一致就全量重建一次。
                #    判据用「计数」而不是「是否存在」—— 空索引与满索引长得一样，
                #    只有计数能区分；重建是幂等的（DELETE + 逐行重插）。
                n_frag = cur.execute("SELECT COUNT(*) FROM ks_fragment").fetchone()[0]
                n_fts = cur.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()[0]
                rebuilt = 0
                if n_frag != n_fts:
                    rebuilt = self._fts_rebuild(cur)
            if added:
                logger.info("storage_sqlite: ks_fragment 补列 %d 个: %s", len(added), added)
            if rebuilt:
                logger.info("storage_sqlite: FTS 索引重建 %d 行（批 1 老库补索引）", rebuilt)
            logger.info("storage_sqlite: schema ready on %s", self._path)
            self._last_ensure_ok, self._last_ensure_error = True, None
            return True
        except Exception as e:      # noqa: BLE001 — 接口约定：失败返回 False
            logger.error("storage_sqlite: ensure_index 失败: %s: %s", type(e).__name__, e)
            self._last_ensure_ok = False
            self._last_ensure_error = f"{type(e).__name__}: {e}"
            return False

    def _schema_ready(self) -> Tuple[bool, Optional[str]]:
        """`ks_fragment` 是否已具备**全部列** + FTS 虚表是否在（读写路径的前提）。

        只发 `PRAGMA table_info` / 查 `sqlite_master`（只读、不拿 schema 锁、不发 DDL），
        健康路径成本可忽略。表不存在 ⇒ 返回空集合 ⇒ 判为未就绪。
        **探测本身出错也判未就绪**（不是就绪）。返回 (是否就绪, 探测失败时的原因)。
        """
        try:
            with self._lock:
                cur = self._db().cursor()
                have = self._table_columns(cur, "ks_fragment")
                has_fts = cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (FTS_TABLE,),
                ).fetchone() is not None
        except Exception as e:      # noqa: BLE001 — 探测失败即「未就绪」，绝不当作就绪
            return False, f"{type(e).__name__}: {e}"
        if not set(ALL_COLUMNS) <= have:
            return False, "ks_fragment 缺列: " + ",".join(sorted(set(ALL_COLUMNS) - have))
        if not has_fts:
            return False, f"{FTS_TABLE} 虚表不存在（FTS5 索引未就绪）"
        return True, None

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
        # 🔴 迁移/批量写（`write_fragments_batch` → 本方法）拿来的行往往**没有
        # entities 字段**（Redis hash / 导出 json 都可能缺），而 `store()` 是用
        # `extract_entities(content)` 落实体的 —— 两条写路径口径不一致 ⇒ 同一份
        # 语料经 PG 侧 store() 入库有 entities、经这里入库没有 ⇒ 检索返回的字段
        # 集合少一项（p2.1 B2）。这里补齐：**调用方给了就以调用方为准，没给才推导**。
        if not row.get("entities") and row.get("content"):
            ents = extract_entities(row["content"])
            if ents:
                row["entities"] = ",".join(ents)
        params: List[Any] = [str(key)]
        for col in ALL_COLUMNS:
            if col == "key":
                continue
            params.append(row.get(col, None if col in _BLOB_COLUMNS else ""))
        try:
            with self._write() as cur:
                cur.execute(_UPSERT_SQL, params)
                # FTS 索引：按**主表落库后的实际值**建（upsert 可能只覆盖部分列，
                # 用 fields 里的值会漏掉未传的 content/entities/tags）。
                row = cur.execute(
                    "SELECT content, COALESCE(entities,''), COALESCE(tags,'') "
                    "FROM ks_fragment WHERE key = ?", (str(key),),
                ).fetchone()
                if row is not None:
                    cur.execute(
                        "UPDATE ks_fragment SET content_tsv = ? WHERE key = ?",
                        (self._fts_tokens(*row), str(key)),
                    )
                    self._fts_write(cur, str(key), self._fts_tokens(*row))
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
                # 批 2：分词文本与 FTS 索引**同一个写事务**、且在主表之后
                # —— 任一步抛错都整体 ROLLBACK，绝不出现「主表有行、索引没有」。
                tokens = self._fts_tokens(text, entities_str, final_tags)
                cur.execute(
                    """
                    INSERT INTO ks_fragment (
                        key, content, tags, category, source, created, updated, hash,
                        sentiment_score, sentiment_label, feedback_score,
                        entities, fragment_type, embed_bin, content_tsv
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET
                        content=excluded.content, tags=excluded.tags,
                        category=excluded.category, source=excluded.source,
                        created=excluded.created, updated=excluded.updated,
                        hash=excluded.hash,
                        sentiment_score=excluded.sentiment_score,
                        sentiment_label=excluded.sentiment_label,
                        feedback_score=excluded.feedback_score,
                        entities=excluded.entities, fragment_type=excluded.fragment_type,
                        embed_bin=COALESCE(excluded.embed_bin, ks_fragment.embed_bin),
                        content_tsv=excluded.content_tsv
                    """,
                    (key, text, final_tags, category, source, now_iso, now_iso,
                     content_hash, str(intensity), label, existing_feedback,
                     entities_str, fragment_type, self._text_to_blob(text), tokens),
                )
                self._fts_write(cur, key, tokens)
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

    # ------------------------------------------------------------------
    # FTS5 索引维护（批 2）
    # ------------------------------------------------------------------

    @staticmethod
    def _fts_tokens(content: str, entities: str, tags: str) -> str:
        """分词文本 = content 的 jieba 词 + entities/tags 的 jieba 词。

        与 PG 侧 `content_tsv = A(content) || B(entities + tags)` **同一召回面**：
        正文、实体、标签三路都进索引（对应 Redis 的 `@content|@entities|@tags`）。
        """
        return " ".join(x for x in (_tok_string(content or ""),
                                    _tok_string(f"{entities or ''} {tags or ''}")) if x)

    def _fts_write(self, cur: sqlite3.Cursor, key: str, tokens: str) -> None:
        """FTS 行 upsert（先删后插 —— FTS5 没有 ON CONFLICT）。"""
        cur.execute(f"DELETE FROM {FTS_TABLE} WHERE frag_key = ?", (key,))
        if tokens:
            cur.execute(
                f"INSERT INTO {FTS_TABLE} (frag_key, content_tok) VALUES (?,?)",
                (key, tokens),
            )

    def _fts_delete(self, cur: sqlite3.Cursor, key: str) -> None:
        cur.execute(f"DELETE FROM {FTS_TABLE} WHERE frag_key = ?", (key,))

    def _fts_rebuild(self, cur: sqlite3.Cursor) -> int:
        """**全量重建** FTS（老库首次补索引 / 自愈用）。

        🔴 为什么需要：批 1 建的库已有数据但没有 FTS 表，补建虚表后索引是空的
        ⇒ 检索恒 0 命中，而「表在、查询正常执行、结果永远为空」正是最危险的
        静默失败形态。`ensure_index()` 用「行数不一致就重建」这条幂等判据兜住。
        """
        cur.execute(f"DELETE FROM {FTS_TABLE}")
        rows = cur.execute(
            "SELECT key, content, COALESCE(entities,''), COALESCE(tags,'') FROM ks_fragment"
        ).fetchall()
        for key, content, entities, tags in rows:
            self._fts_write(cur, key, self._fts_tokens(content, entities, tags))
        return len(rows)

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

        🔴 批 2 追加：若本次更新碰了**进索引的三列**（content/entities/tags），
        就地重算 `content_tsv` 并同步 FTS —— 否则正文改了、索引还是旧的，
        检索结果**静默错**（不是召回不到，是召回错误内容）。与 PG 侧
        `update_fragment_fields` 不碰 content_tsv 的差异就此消掉。
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
        reindex = bool({"content", "entities", "tags"} & set(cols))
        try:
            with self._write() as cur:
                cur.execute(f"UPDATE ks_fragment SET {set_sql} WHERE key = ?", params)
                ok = cur.rowcount > 0
                if ok and reindex:
                    row = cur.execute(
                        "SELECT content, COALESCE(entities,''), COALESCE(tags,'') "
                        "FROM ks_fragment WHERE key = ?", (key,),
                    ).fetchone()
                    if row is not None:
                        tokens = self._fts_tokens(*row)
                        cur.execute("UPDATE ks_fragment SET content_tsv = ? WHERE key = ?",
                                    (tokens, key))
                        self._fts_write(cur, key, tokens)
                return ok
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
                    # 🔴 FTS 必须同步删：不删就是**僵尸行** —— 正文已不存在，
                    # 检索仍能召回它，取回正文时又查不到 ⇒ 静默错结果。
                    cur.execute(
                        f"DELETE FROM {FTS_TABLE} WHERE frag_key IN ({placeholders})", chunk,
                    )
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
    # 检索（批 2：jieba + FTS5 + bm25 + 共用重排）
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_tag(tag: str) -> str:
        """tag 值清理 —— 与 PG `PgStorage._clean_tag` / Redis `_tag_safe` 同意图。"""
        for ch in ("\\", "{", "}", "|", ",", '"', "'", " "):
            tag = tag.replace(ch, "")
        return tag.strip()

    def _search_filter_sql(
        self,
        tag_filter: str,
        agent_id: str,
        is_primary: Optional[bool],
    ) -> Tuple[str, List[Any]]:
        """检索 WHERE 片段（标签 + agent 隔离 + 活记忆），语义逐条对齐 PG/Redis。

        🔴 tag 匹配用 `instr('|'||tags||'|', '|tag|') > 0` 而不是 LIKE：
        与 PG 侧 `strpos` 同理 —— 只在**完整标签边界**上匹配（`agent:a` 不会命中
        `agent:ab`），且 tag 里的 `%` `_` 不会被当通配符。
        """
        clauses: List[str] = []
        params: List[Any] = []
        effective_agent_id = agent_id if agent_id else self._agent_id
        effective_is_primary = is_primary if is_primary is not None else self._is_primary

        if not effective_is_primary:
            if effective_agent_id:
                clauses.append(
                    "(instr('|'||tags||'|', ?) > 0 OR instr('|'||tags||'|', '|shared|') > 0)"
                )
                params.append(f"|{self._clean_tag(f'agent:{effective_agent_id}')}|")
            else:
                clauses.append("instr('|'||tags||'|', '|shared|') > 0")

        if tag_filter:
            needles = [f"|{self._clean_tag(t)}|"
                       for t in (x.strip() for x in tag_filter.split(","))
                       if self._clean_tag(t)]
            if needles:
                # 🔴 拼法用「重复 N 次再 OR 连起来」：**单标签时不能写成
                #    `(instr(...) > 0 OR )`** —— 那是语法错，整条检索抛
                #    `near ")": syntax error`（p2.1 实测：单 tag_filter 必崩）。
                clauses.append("(" + " OR ".join(["instr('|'||tags||'|', ?) > 0"]
                                                 * len(needles)) + ")")
                params.extend(needles)

        clauses.append("invalid_at = ''")
        clauses.append("valid_until = ''")
        clauses.append(f"content <> '' AND length(content) <= {int(MAX_CONTENT_LEN)}")
        return " AND ".join(clauses), params

    def _load_synonym_map(self) -> Dict[str, set]:
        """同义词表（对齐 Redis `keepsake:synonyms` / PG `ks_synonym`）。

        🔴 必须同源：PG/Redis 的 BM25 都拿它做查询式扩展，本后端自己造一张表
        就等于「同义词这条召回面单边失效」，三后端对照直接失真。
        """
        out: Dict[str, set] = {}
        try:
            with self._lock:
                rows = self._db().execute("SELECT term, synonyms FROM ks_synonym").fetchall()
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: load synonyms error: %s", e)
            return out
        for term, syns in rows:
            key = (term or "").lower().strip()
            if not key:
                continue
            bucket = out.setdefault(key, set())
            for s in (syns or []):
                sl = str(s).lower().strip()
                if sl and sl != key:
                    bucket.add(sl)
        return out

    def _fetch_superseded_by(self, keys: List[str]) -> Dict[str, str]:
        """storage_shared 的后端钩子：批量读封边标记（一条 SQL，不 N+1）。"""
        if not keys:
            return {}
        out: Dict[str, str] = {}
        try:
            with self._lock:
                cur = self._db().cursor()
                for chunk in _chunks(list(keys)):
                    placeholders = ", ".join("?" for _ in chunk)
                    for k, sb in cur.execute(
                        f"SELECT key, superseded_by FROM ks_fragment "
                        f"WHERE key IN ({placeholders})", chunk,
                    ):
                        if sb:
                            out[k] = sb
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: _fetch_superseded_by failed: %s", e)
        return out

    def _fetch_fragments(self, keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """storage_shared 的后端钩子：批量读碎片（字段裁到检索形状）。"""
        if not keys:
            return {}
        cols = ("key",) + tuple(c for c in SEARCH_FIELDS if c != "key")
        out: Dict[str, Dict[str, Any]] = {}
        try:
            with self._lock:
                cur = self._db().cursor()
                for chunk in _chunks(list(keys)):
                    placeholders = ", ".join("?" for _ in chunk)
                    for row in cur.execute(
                        f"SELECT {', '.join(cols)} FROM ks_fragment "
                        f"WHERE key IN ({placeholders})", chunk,
                    ):
                        out[row[0]] = self._row_to_fragment(row, cols)
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: _fetch_fragments failed: %s", e)
        return out

    @staticmethod
    def _tiebreak(fragments: List[Dict[str, Any]],
                  score_key: str = "_combined_score") -> List[Dict[str, Any]]:
        """**并列 tiebreaker**：同分时按 `key` 字典序（升序）定序。

        与 `PgStorage._tiebreak` 同语义：共用重排只用一个分字段，
        BM25 归一化后大量候选挤在同一值上 ⇒ 排序不稳定会让「同一题连查多次
        结果不一致」。规则只改同分项之间的相对次序，不改分数、不改条数。
        """
        if len(fragments) < 2:
            return fragments
        return sorted(fragments,
                      key=lambda f: (-float(f.get(score_key, 0.0) or 0.0),
                                     f.get("_key") or ""))

    def search(self, query: str, tag_filter: str = "", agent_id: str = "",
               is_primary: Optional[bool] = None) -> List[Dict[str, Any]]:
        """统一检索入口（与 `RedisStorage.search` / `PgStorage.search` 同构）。

          1. BM25 全文（jieba + FTS5 + 内建 bm25）
          2. KNN 向量路（`sqlite-vec`，第 3 批可选懒加载）
          3. 两路都非空 → RRF 融合 → v2 后置过滤

        🔴 **降级路径可辨识**：向量扩展不可用时记一条 WARNING 后走 BM25 单路
        （与 PG「无 embedder 就只走 BM25」同构），**不是静默返回空**；
        需要向量结果的调用方请直接调 `search_knn`，那里抛的是明确的
        `NotImplementedError`。
        """
        effective_agent_id = agent_id if agent_id else self._agent_id
        effective_is_primary = is_primary if is_primary is not None else self._is_primary

        bm25_results = self.search_bm25(query, tag_filter, effective_agent_id,
                                        effective_is_primary)
        try:
            knn_results = self.search_knn(query, tag_filter, effective_agent_id,
                                          effective_is_primary)
        except NotImplementedError as e:
            logger.warning(
                "storage_sqlite: search() 向量路不可用，降级为 BM25 单路（不是静默空结果）：%s", e)
            knn_results = []
        if knn_results:
            fused = self._tiebreak(self._rrf_fuse(bm25_results, knn_results))
            return self._apply_v2_filters(fused)
        return self._apply_v2_filters(bm25_results)

    def _bm25_stats(self, where_sql: str, params: List[Any],
                    terms: List[str]) -> Tuple[float, float, Dict[str, float]]:
        """语料统计 (N, avgdl, dfs) —— 与 PG `_bm25_thin` 的统计**同一口径**。

        * N / avgdl：过滤后的活记忆行数与平均长度（`content_tsv` 空格分隔 ⇒
          「空格数 + 1」就是词元数，与 PG 的 `sum(cardinality(positions))` 等价）。
        * df(t)：**含词元 t 的文档数**。PG 侧的口径是「lexeme = t 且命中 tsquery」
          ⇒ 等价于「该词元的文档频率」。本实现用 FTS5 `MATCH` 数（走索引，不扫表）。
          只有一个 unicode61 词元形态的查询词才可能有 df；含 `-`/空格等的多词查询词
          在 tsvector 里压根不存在这个 lexeme ⇒ df=0（与 PG 同，不特殊照顾）。

        ponytail: 每次查询两趟 SQL（N/avgdl 一趟、df 一趟 UNION ALL），无缓存。
        嵌入式单文件库上代价可忽略；语料量级上到十万行再给 df/avgdl 加 TTL 快照
        （PG 侧 `_snap_get` 那套，同款）。
        """
        # 只有「单词元」的查询词才可能有 df —— 与 PG 的 lexeme 口径逐字对齐
        singles = sorted({t.lower() for t in terms if _WORD_RE.fullmatch(t)})
        df_sql = " UNION ALL ".join(
            # 🔴 FTS5 的 MATCH 必须写**表名**（不能给虚表起别名），否则 no such column
            f"SELECT ? AS term, COUNT(*) AS df FROM ks_fragment f "
            f"JOIN {FTS_TABLE} ON {FTS_TABLE}.frag_key = f.key "
            f"WHERE {FTS_TABLE} MATCH ? AND {where_sql}" for _ in singles)
        df_params: List[Any] = []
        for t in singles:
            df_params.extend([t, _fts_match_expr([t]), *params])
        with self._lock:
            cur = self._db().cursor()
            cur.execute(
                f"SELECT COUNT(*), COALESCE(SUM("
                f"CASE WHEN content_tsv = '' THEN 0"
                f" ELSE length(content_tsv) - length(replace(content_tsv, ' ', '')) + 1 END"
                f"), 0) FROM ks_fragment WHERE {where_sql}", params)
            n_docs, total_len = cur.fetchone()
            dfs: Dict[str, float] = {}
            if singles:
                cur.execute(df_sql, df_params)
                for term, df in cur.fetchall():
                    dfs[str(term)] = float(df)
        n_docs_f = float(n_docs or 0.0)
        avgdl = (float(total_len or 0.0) / n_docs_f) if n_docs_f > 0 else 0.0
        return n_docs_f, avgdl, dfs

    def search_bm25(self, query: str, tag_filter: str = "", agent_id: str = "",
                    is_primary: Optional[bool] = None) -> List[Dict[str, Any]]:
        """BM25 全文搜索（jieba 切词 → FTS5 MATCH 召回 → **PG 的同一个 BM25 公式**
        在 Python 侧打分 → 共用 `rerank_with_decay` 重排 → 取 `final_limit` 条）。

        🔴 **p2.1 B3：打分换回 PG 公式**。原来用 FTS5 内建 `bm25()`：它的 idf 变体
        （BM25 默认 vs RediSearch 的 `ln(1+…)`）与长度归一化口径都和 PG 不同，
        同一份语料两后端的名次对不上（同序前缀差一大截）。现在**召回**仍走 FTS5
        （索引在 SQLite 侧），但**打分**用 `storage_pg.bm25_score`（同一个函数
        对象）+ 同一份语料统计 ⇒ 分数与名次与 PG 逐条一致。
        FTS5 的 `bm25()` 只用来给**候选窗口**粗排（`ORDER BY … LIMIT bm25_limit`），
        对应 PG 侧候选 CTE 里的 `ts_rank_cd` —— 两边都是「窗口粗排、非最终分」。

        返回字段与 PG `search_bm25` 逐字段对齐（`SEARCH_FIELDS` + `_key` /
        `_bm25_score` / `_sim` / `_combined_score` / `_weights`）。
        """
        if not (query or "").strip():
            logger.warning("storage_sqlite: search_bm25 called with an empty query — "
                           "returning no results (explicit, not a backend outage)")
            return []
        self._require_ready("search_bm25")

        # 1. 查询式构造：与 PG 侧逐字同一套（同义词扩展 + sanitize 拆子词）
        terms = _sanitize_terms(
            _expand_terms(segment_query(query), self._load_synonym_map()))
        if not terms:
            logger.warning("storage_sqlite: query %r sanitized down to zero terms — "
                           "returning no results (explicit)", query[:50])
            return []

        where_sql, params = self._search_filter_sql(tag_filter, agent_id, is_primary)
        expr = _fts_match_expr(terms)
        try:
            with self._lock:
                cur = self._db().cursor()
                # 候选窗口：`content_tsv` 一并取回（Python 侧算 tf/doclen 用）
                cur.execute(
                    f"SELECT f.key, f.content, f.tags, f.category, f.source, f.created, "
                    f"f.sentiment_score, f.sentiment_label, f.feedback_score, "
                    f"f.entities, f.fragment_type, f.invalid_at, f.content_tsv, "
                    # 🔴 FTS5 的 bm25() 只用来给**窗口粗排**（对应 PG 侧的 ts_rank_cd），
                    #    最终分在 Python 侧按 PG 公式重算 —— 见方法 docstring。
                    f"-bm25({FTS_TABLE}) AS bm25_pos "
                    f"FROM {FTS_TABLE} JOIN ks_fragment f ON f.key = {FTS_TABLE}.frag_key "
                    f"WHERE {FTS_TABLE} MATCH ? AND {where_sql} "
                    f"ORDER BY bm25_pos DESC, f.key LIMIT ?",
                    [expr, *params, self._bm25_limit],
                )
                rows = cur.fetchall()
            if not rows:
                return []      # 0 候选就不必算语料统计（N/avgdl 是全表聚合）
            n_docs, avgdl, dfs = self._bm25_stats(where_sql, params, terms)
        except sqlite3.OperationalError as e:
            # FTS5 查询式语法错/表缺失 → **显式告警**（PG/Redis 都犯过「语法错被
            # debug 吞掉 ⇒ 整类查询静默 0 召回」的错，这里提到 warning 并向上抛）。
            logger.error("storage_sqlite: FTS MATCH 失败 (query=%r): %s", query[:50], e)
            raise
        except Exception as e:      # noqa: BLE001
            logger.warning("storage_sqlite: search_bm25 error: %s: %s", type(e).__name__, e)
            return []

        fragments: List[Dict[str, Any]] = []
        lower_terms = {t.lower() for t in terms}
        for row in rows:
            key, content, tags, category, source, created, sent, label, fb, \
                entities, ftype, invalid_at, tsv, _window_score = row
            # tf / doclen：词元口径与 PG 的 tsvector positions 逐字对齐
            # （content_tsv 落库时已按 unicode61 词元切分，见 `_tok_string`）
            words = [w.lower() for w in (tsv or "").split()]
            counts = Counter(words)
            tfs = {t: float(c) for t, c in counts.items() if c and t in lower_terms}
            score = bm25_score(tfs, float(len(words)), n_docs, avgdl, dfs)
            frag: Dict[str, Any] = {"_key": key, "_bm25_score": score}
            for name, value in (
                ("content", content), ("tags", tags), ("category", category),
                ("source", source), ("created", created), ("sentiment_score", sent),
                ("sentiment_label", label), ("feedback_score", fb),
                ("entities", entities), ("fragment_type", ftype), ("invalid_at", invalid_at),
            ):
                if value is None or value == "":
                    continue          # 稀疏语义：空值不进 dict（同 PG/Redis）
                frag[name] = value if isinstance(value, str) else str(value)
            if frag.get("content"):
                fragments.append(frag)
        if not fragments:
            return []

        # 2. 共用重排（时间衰减 × 情绪 × 反馈 × 热门 × 注意力）+ tiebreak + 截断。
        #    与 PG `search_bm25` 逐步同序：重排 → tiebreak → [:final_limit]。
        ranked = self._rerank_with_decay(fragments, score_key="_bm25_score")
        return self._tiebreak(ranked)[: self._final_limit]

    # ------------------------------------------------------------------
    # 加权信号（批 2）
    # ------------------------------------------------------------------

    def _hot_snapshot(self, limit: int) -> Tuple[List[str], Dict[str, float]]:
        """热词榜（scope=all、未过期）+ last_seen —— 与 PG 侧同源同口径。"""
        now_ts = time.time()
        with self._lock:
            cur = self._db().cursor()
            topics = [t for (t,) in cur.execute(
                f"SELECT topic FROM ks_hot_topic WHERE scope = ? AND expire_ts > ? "
                f"ORDER BY score DESC LIMIT ?", (_TOPIC_SCOPE_ALL, now_ts, int(limit)),
            )]
            last_seen: Dict[str, float] = {}
            if topics:
                placeholders = ", ".join("?" for _ in topics)
                last_seen = {t: float(ls) for t, ls in cur.execute(
                    f"SELECT topic, last_seen FROM ks_hot_topic_seen "
                    f"WHERE topic IN ({placeholders})", topics,
                )}
        return topics, last_seen

    def _attn_snapshot(self, top_n: int) -> List[tuple]:
        """注意力榜（scope=all、未过期）—— 与 PG 侧同源同口径。"""
        with self._lock:
            return self._db().execute(
                "SELECT topic, score FROM ks_attention "
                "WHERE scope = ? AND expire_ts > ? ORDER BY score DESC LIMIT ?",
                (_TOPIC_SCOPE_ALL, time.time(), int(top_n)),
            ).fetchall()

    def match_attention(self, content: str, top_n: int = 10) -> float:
        """内容命中高注意力话题的加权值（公式 = 共用 `attention_boost_from_topics`）。"""
        if not content:
            return 1.0
        return attention_boost_from_topics(self._attn_snapshot(top_n), content,
                                           self._attention_boost_max)

    def match_hot_topics(self, text: str, limit: int = 10) -> float:
        """内容命中热词的衰减加权命中数（公式 = 共用 `hot_topic_weighted_hits`）。"""
        if not text:
            return 0.0
        topics, last_seen = self._hot_snapshot(limit)
        if not topics:
            return 0.0
        return hot_topic_weighted_hits(
            topics, last_seen, text, time.time(),
            decay_half_days=self._hot_topic_decay_half_days,
        )

    def get_hot_topics(self, limit: int = 10, period: str = "all") -> List[Dict[str, Any]]:
        """热门话题榜；period ∈ {all, daily, weekly}（未知值回落 all，同 PG/Redis）。"""
        scope = period if period in _TOPIC_SCOPES else _TOPIC_SCOPE_ALL
        with self._lock:
            rows = self._db().execute(
                "SELECT topic, score FROM ks_hot_topic "
                "WHERE scope = ? AND expire_ts > ? ORDER BY score DESC LIMIT ?",
                (scope, time.time(), int(limit)),
            ).fetchall()
        return [{"topic": t, "count": round(float(s), 1)} for t, s in rows]

    def entity_timeline(self, entity: str, limit: int = 20) -> List[Dict[str, Any]]:
        """按时间倒序返回某实体的记忆时间线（字段与 PG 侧逐字同形）。"""
        if not entity or not entity.strip():
            return []
        with self._lock:
            rows = self._db().execute(
                "SELECT f.content, f.created, f.valid_until "
                "FROM ks_entity_timeline t JOIN ks_fragment f ON f.key = t.frag_key "
                "WHERE t.entity = ? ORDER BY t.ts DESC LIMIT ?",
                (entity.strip(), int(limit)),
            ).fetchall()
        return [
            {"content": c, "created": cr or None, "valid_until": vu or None}
            for c, cr, vu in rows
        ]

    # ------------------------------------------------------------------
    # 留桩（第 3 批 / 仍未实现）—— 显式抛错，绝不静默返回空值
    # ------------------------------------------------------------------

    def search_knn(self, query: str, tag_filter: str = "", agent_id: str = "",
                   is_primary: Optional[bool] = None) -> List[Dict[str, Any]]:
        """向量检索 —— 需要 `sqlite-vec` 扩展（第 3 批可选懒加载），本批未实现。

        🔴 **绝不静默返回空列表**：向量路不可用时唯一正确的形态是抛错，
        让调用方明确知道「这条路没通」。`search()` 会捕获它并降级为 BM25 单路
        （且打 WARNING）。
        """
        raise NotImplementedError(
            "sqlite backend: search_knn 需要 sqlite-vec 扩展（向量检索属第 3 批，"
            "可选懒加载依赖）。本后端当前只有 BM25 全文检索可用；"
            "请调 search_bm25/search，或安装 sqlite-vec 后再启用向量路。"
        )

    def discover_synonyms(self, rebuild: bool = False) -> Dict[str, Any]:
        raise NotImplementedError(_NOT_IMPLEMENTED.format(name="discover_synonyms"))

    def generate_jieba_dict(self, output_path: str = None) -> Dict[str, Any]:
        raise NotImplementedError(_NOT_IMPLEMENTED.format(name="generate_jieba_dict"))
