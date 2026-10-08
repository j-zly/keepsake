"""
PostgreSQL 存储后端 — keepsake 第二个存储实现（批 2：读写 + 检索全量）。

🔴 **本批能力边界（务必先读）**
  已实现：ensure_index / store / get_fragment / get_fragments_batch /
    correct_fragments / supersede_fragment / record_feedback / get_hot_topics /
    match_hot_topics / match_attention / entity_timeline / close / health_check /
    **search / search_bm25 / search_knn**
  **未实现且显式抛错**：discover_synonyms / generate_jieba_dict（语料维护类，
    与检索正交；仍显式抛错而不是静默返回空统计）。

## 检索怎么做的（零 PG 中文扩展）

🔴 **硬要求：不装 pg_jieba / zhparser 等任何中文分词扩展。** 中文分词全部在
Python 侧用仓库既有的 jieba 做（`splitter.segment_query` → 同义词扩展 →
`_sanitize_terms`，后者含上游 58bf19c/d00b7eb 修过的「路径/连字符词要拆子词」
处理，不绕过），然后：

  写入：`setweight(to_tsvector('simple', <jieba 分词结果>),'A')
         || setweight(to_tsvector('simple', <entities+tags>),'B')`
         存进 `ks_fragment.content_tsv`，建 GIN 索引
         （A=content 对齐 RediSearch `content TEXT WEIGHT 1`；B=entities+tags
           对齐 RediSearch 侧 `@entities`/`@tags` 的 OR 召回面）
  查询：`to_tsquery('simple', 'a' | 'b' | ...)`，排序 `ts_rank_cd(..., 32)`
         （32 = rank/(1+rank) 归一化，值域 0~1，正好给共用重排的 min-max 用）
  KNN ：`ks_fragment.embedding vector(N)` + HNSW(cosine) 索引，
         `embedding <=> $q` 返回**余弦距离**（0~2，越小越近），
         与 Redis `DISTANCE_METRIC COSINE` 的 doc.score 量纲一致 ⇒ 共用重排函数

  `'simple'` 配置 = 只小写化、不做词干还原 —— 与 jieba 已经切好的词一一对应，
  不会把「迁移」和「转移」合并，也不会二次切词。

## 向量维度/度量对齐（逐字照抄 Redis 索引定义）

`RedisStorage._build_create_index_cmd` 写的是
`embed_bin VECTOR FLAT 6 TYPE FLOAT32 DIM {dim} DISTANCE_METRIC COSINE`。
对应到 PG：`vector(dim)` + `hnsw (embedding vector_cosine_ops)`，dim 取同一个
`self._embed_dim`。索引算法 FLAT↔HNSW 不同（PG 侧要求 HNSW），但**存储类型
（float32）、维度、余弦距离**三项逐字对齐 —— 这三项决定召回语义，索引算法
只决定速度。

与 Redis 侧的字段语义对齐（key 命名/字段名/数值语义都刻意保持一致）：
  * 碎片 key：`memory:frag:<sha256(text)[:12]>`；版本化旧版加 `:<epoch>` 后缀
  * 碎片字段：content / tags / category / source / created / sentiment_score /
    sentiment_label / feedback_score / entities / fragment_type / valid_until /
    is_archived / superseded_by / superseded_at / corrected_at / invalid_at
  * 检索结果字段与 Redis 侧**逐字同形**（见 storage_shared.FRAGMENT_FIELDS +
    `_key` / `_bm25_score` / `_knn_score` / `_sim` / `_combined_score` / `_weights`）
  * 实体时间线 → `ks_entity_timeline`（对齐 `keepsake:entity_timeline:<实体>` ZSET）
  * 实体共现   → `ks_entity_cooc`（对齐 `keepsake:entity_cooc` ZSET）
  * 同义词     → `ks_synonym`（对齐 `keepsake:synonyms` hash）

两个刻意的设计选择：
  1) **连不上直接抛错，不静默降级**。Redis 侧连不上返回 False/None 是历史契约；
     PG 侧故意不照抄 —— 静默降级只会被上层读成「搜不到」。
  2) **不装 psycopg 也能 import 本模块**（连接时才 import），否则不用 PG 的
     人会因为一个 optional 依赖把整个 keepsake 拖挂。

ponytail: 检索后处理（RRF / v2 过滤 / 综合重排 / 批量载入）与注意力、热词的
加权公式**不在本文件**，全在 `storage_shared.py`，Redis 与 PG 绑定的是同一批
函数对象；本文件只写「怎么把候选取出来」的 SQL。
"""

from __future__ import annotations

import functools
import hashlib
import logging
import random
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional

from .emotion import analyze_emotion
from .splitter import extract_entities, extract_keywords, segment_query
from .storage_base import StorageBase
from .storage_shared import (          # 与 Redis 共用的检索后处理（同一批函数对象）
    FRAGMENT_FIELDS,
    SEARCH_FIELDS,
    DECAY_HALF_DAYS,
    FEEDBACK_NEGATIVE_PENALTY,
    FEEDBACK_POSITIVE_BOOST,
    HOT_TOPIC_BOOST,
    HOT_TOPIC_DECAY_HALF_DAYS,
    apply_v2_filters,
    attention_boost_from_topics,
    hot_topic_weighted_hits,
    load_fragments_by_keys,
    rrf_fuse,
    rerank_with_decay,
)
# 查询式构造复用 Redis 侧的同一套（同义词扩展 + 路径/连字符拆子词的 sanitize）。
# 复用而不是重抄：上游 58bf19c/d00b7eb 修过这套的坑，两边各写一份必漂移。
# storage.py 只在函数体内延迟 import storage_pg，模块级不反向依赖 ⇒ 无循环。
from .storage import _expand_terms, _sanitize_terms

logger = logging.getLogger(__name__)

# 检索参数默认值 —— 与 storage.py 的 DEFAULT_* 逐字对齐
DEFAULT_CANDIDATE_COUNT = 10   # KNN 候选数
DEFAULT_BM25_LIMIT = 20        # BM25 候选数
DEFAULT_FINAL_LIMIT = 5        # 最终返回条数
MAX_CONTENT_LEN = 600          # 超长条目视为噪音跳过（对齐 Redis search_bm25）
TS_RANK_NORM = 32              # ts_rank_cd 归一化位：rank/(1+rank)

# 🔴 本文件**不**复制一份 HOT_TOPIC_* Redis key 名（那是 Redis 侧的物理布局）。
# PG 的话题榜在 ks_hot_topic 表里，scope 列区分三榜，语义在 _TOPIC_SCOPES 里。
_ENTITY_COOC_TTL = 2592000  # 30 天，对齐 Redis 侧 ENTITY_COOC_TTL


_TOPIC_SCOPE_ALL = "all"
_TOPIC_SCOPE_DAILY = "daily"
_TOPIC_SCOPE_WEEKLY = "weekly"
# 三榜（对齐 Redis 侧 keepsake:hot_topics{,:daily,:weekly} 三个 ZSET；
# PG 用 ks_hot_topic.scope 列区分，不复制 Redis 的物理 key 名）
_TOPIC_SCOPES = (_TOPIC_SCOPE_ALL, _TOPIC_SCOPE_DAILY, _TOPIC_SCOPE_WEEKLY)
# Redis 侧按 key 设整集 TTL；PG 侧逐行 expire_ts 等价
_TOPIC_TTL = {
    _TOPIC_SCOPE_ALL: 86400 * 7,
    _TOPIC_SCOPE_DAILY: 86400 * 2,
    _TOPIC_SCOPE_WEEKLY: 86400 * 14,
}
# 注意力三榜的 scope 取值与话题榜同名同义（PG 侧不按 Redis key 区分）。
# 三档 TTL 与 `attention._ATTENTION_TTL` 逐字对齐（7/2/14 天）。
_ATTENTION_SCOPES = _TOPIC_SCOPES
_ATTENTION_TTL = {
    _TOPIC_SCOPE_ALL: 86400 * 7,
    _TOPIC_SCOPE_DAILY: 86400 * 2,
    _TOPIC_SCOPE_WEEKLY: 86400 * 14,
}

# 碎片表列（与 Redis hash 字段一一对应；embed_bin 本批只留位不写）
FRAGMENT_COLUMNS = (
    "key", "content", "tags", "category", "source", "created",
    "sentiment_score", "sentiment_label", "feedback_score",
    "entities", "fragment_type", "valid_until", "is_archived",
    "superseded_by", "superseded_at", "corrected_at", "invalid_at",
)

# 🔴 **DDL 一律拆成 (对象名, 语句) 两元组**：`ensure_index()` 拿对象名去
# information_schema / pg_indexes 核对，**只补真缺的那些**。
# 为什么不能把整段 DDL 无脑重发（这正是线上死锁的根因，见 ensure_index 的 docstring）：
# `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` **列已存在也要拿 ACCESS EXCLUSIVE**，
# 与并发 DML 互等成环；`CREATE INDEX IF NOT EXISTS` 也要拿 SHARE 锁挡写。
# 详见 `SCHEMA_ADVISORY_LOCK_KEY` 一节。
_DDL_TABLES = (
    ("ks_fragment", """
    CREATE TABLE IF NOT EXISTS ks_fragment (
        key             text PRIMARY KEY,
        content         text NOT NULL DEFAULT '',
        tags            text NOT NULL DEFAULT '',
        category        text NOT NULL DEFAULT '',
        source          text NOT NULL DEFAULT '',
        created         text NOT NULL DEFAULT '',
        sentiment_score text NOT NULL DEFAULT '',
        sentiment_label text NOT NULL DEFAULT '',
        feedback_score  text NOT NULL DEFAULT '0',
        entities        text NOT NULL DEFAULT '',
        fragment_type   text NOT NULL DEFAULT '',
        valid_until     text NOT NULL DEFAULT '',
        is_archived     text NOT NULL DEFAULT '',
        superseded_by   text NOT NULL DEFAULT '',
        superseded_at   text NOT NULL DEFAULT '',
        corrected_at    text NOT NULL DEFAULT '',
        invalid_at      text NOT NULL DEFAULT '',
        embed_bin       bytea
    )
    """),
    # 对齐 keepsake:entity_timeline:<实体> ZSET（member=碎片 key，score=时间戳）
    ("ks_entity_timeline", """
    CREATE TABLE IF NOT EXISTS ks_entity_timeline (
        entity    text NOT NULL,
        frag_key  text NOT NULL,
        ts        double precision NOT NULL,
        PRIMARY KEY (entity, frag_key)
    )
    """),
    # 对齐 keepsake:entity_cooc ZSET（member="a||b"，score=共现次数）
    ("ks_entity_cooc", """
    CREATE TABLE IF NOT EXISTS ks_entity_cooc (
        pair      text PRIMARY KEY,
        score     double precision NOT NULL DEFAULT 0,
        expire_ts double precision NOT NULL
    )
    """),
    # 对齐 keepsake:hot_topics{,:daily,:weekly} 三个 ZSET（scope 列区分）
    ("ks_hot_topic", """
    CREATE TABLE IF NOT EXISTS ks_hot_topic (
        scope     text NOT NULL,
        topic     text NOT NULL,
        score     double precision NOT NULL DEFAULT 0,
        expire_ts double precision NOT NULL,
        PRIMARY KEY (scope, topic)
    )
    """),
    # 对齐 keepsake:hot_topics:last_seen hash
    ("ks_hot_topic_seen", """
    CREATE TABLE IF NOT EXISTS ks_hot_topic_seen (
        topic     text PRIMARY KEY,
        last_seen double precision NOT NULL
    )
    """),
    # 对齐 keepsake:attention{,:daily,:weekly} 三个 ZSET
    ("ks_attention", """
    CREATE TABLE IF NOT EXISTS ks_attention (
        scope     text NOT NULL,
        topic     text NOT NULL,
        score     double precision NOT NULL DEFAULT 0,
        expire_ts double precision NOT NULL,
        PRIMARY KEY (scope, topic)
    )
    """),
    # 对齐 keepsake:synonyms hash（term → JSON 数组）
    ("ks_synonym", """
    CREATE TABLE IF NOT EXISTS ks_synonym (
        term     text PRIMARY KEY,
        synonyms jsonb NOT NULL DEFAULT '[]'::jsonb
    )
    """),
)

_DDL_INDEXES = (
    ("idx_ks_entity_timeline_ts",
     "CREATE INDEX IF NOT EXISTS idx_ks_entity_timeline_ts "
     "ON ks_entity_timeline (entity, ts DESC)"),
)

# 🔴 content_tsv / embedding 两列**不能**写进上面这段 DDL：
#   embedding 的维度是运行期才知道的（`vector(dim)`），写死就是「换个 embedder
#   模型维度就建错表」。所以走 ensure_index() 里的「检查→ALTER→建索引」。
SEARCH_COLUMNS = ("content_tsv", "embedding")


def _search_column_ddl(dim: int) -> tuple:
    """批 2 检索列的 ALTER —— (列名, 语句)。

    对齐 `RedisStorage._build_create_index_cmd` 的
    `VECTOR FLAT 6 TYPE FLOAT32 DIM {dim} DISTANCE_METRIC COSINE`：
      * float32 存储  → `vector(dim)`（pgvector 的元素类型就是 float4）
      * 余弦距离      → `vector_cosine_ops`（`<=>` 运算符）
      * 索引算法      → HNSW（Redis 侧 FLAT 是精确检索；PG 侧 pgvector 0.8.1
                       要求 HNSW 才能建向量索引，且召回语义由距离度量决定，
                       与索引算法无关）

    两列同批**只补缺的那列** —— 列已在就不发 ALTER（见 `ensure_index` docstring：
    无条件发 `ADD COLUMN IF NOT EXISTS` 会拿 ACCESS EXCLUSIVE，与并发 DML 互等成环）。
    """
    if dim < 1:
        raise ValueError(f"embed_dim must be a positive integer, got {dim}")
    return (
        ("content_tsv",
         "ALTER TABLE ks_fragment ADD COLUMN content_tsv tsvector"),
        ("embedding",
         f"ALTER TABLE ks_fragment ADD COLUMN embedding vector({int(dim)})"),
    )


def _search_index_ddl() -> tuple:
    """批 2 两个检索索引 —— (索引名, 语句)。"""
    return (
        ("idx_ks_fragment_content_tsv",
         "CREATE INDEX IF NOT EXISTS idx_ks_fragment_content_tsv "
         "ON ks_fragment USING GIN (content_tsv)"),
        ("idx_ks_fragment_embedding_hnsw",
         "CREATE INDEX IF NOT EXISTS idx_ks_fragment_embedding_hnsw "
         "ON ks_fragment USING hnsw (embedding vector_cosine_ops)"),
    )


# =============================================================================
# 迁移段（ensure_index）的并发安全参数
# =============================================================================

# 🔴 固定 advisory lock key —— 全库迁移段共用一把，**永不要改**。
#   改了等于并发迁移不再互斥，直接退回「两边同时发 DDL」的死锁现场。
#   字面量取 'KSPGSCHM' 的 8 字节 big-endian 编码（0x4B5350475343484D）。
SCHEMA_ADVISORY_LOCK_KEY = 0x4B5350475343484D

# DDL 前 `SET LOCAL lock_timeout`：拿不到锁最多等这么久就报错，
# **绝不无限等**（否则 ensure_index 静默挂住 = 上游初始化卡死）。
# 3~5s 足够覆盖「几百毫秒的 DDL」；再长只是让故障更难发现。
SCHEMA_LOCK_TIMEOUT_MS = 5000

# 有限次重试（带指数退避）。超限**明确返回 False + ERROR 日志**，不静默成功。
SCHEMA_RETRY_ATTEMPTS = 3
SCHEMA_RETRY_BACKOFF_S = 0.2

#: 进程级记忆 `{(目标库指纹, embed_dim): True}`。
#: 同进程第二个 PgStorage 实例再调 ensure_index() 直接命中 ⇒ **一条 SQL 都不发**。
#: 这是「provider + 提炼 cron 同机各建一个存储」这条最省事的路径。
_SCHEMA_READY: Dict[tuple, bool] = {}

# 核对用目录查询（只读，AccessShareLock，不挡写）
_LIST_TABLES_SQL = (
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = current_schema()"
)
_LIST_INDEXES_SQL = (
    "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
)
_LIST_FRAGMENT_COLUMNS_SQL = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = 'ks_fragment'"
)


# =============================================================================
# 第二道防线：有界死锁重试（**单点实现**，全部写路径挂同一个装饰器）
# =============================================================================

# 🔴 为什么写路径需要重试，而第一道防线（排序）还不够：
#   排序把「本模块自己发起的多行写」的死锁根除（见 `_insert_many` 的 docstring），
#   但 PG 的死锁还可能来自**别的事务**（别的进程、别的库、乃至人工在 psql 里手写
#   同样的 upsert）。所以守不住时必须有第二道防线，而不是「应该不会发生」。
#
#   为什么必须**重跑整个事务**、不能续跑半截：
#   psycopg 抛出 DeadlockDetected 时，事务**已经被服务端 abort**（整个回滚），
#   而 `_tx()` 的 `conn.transaction()` 退出时还会去 COMMIT/ROLLBACK 一个已死的事务。
#   半截续跑既没有状态可续，也只会再撞一次同样的锁环 —— 唯一正确的动作是
#   **从头把整个写操作重做一遍**（它对幂等 upsert 天然安全：回滚后库状态与开跑前一致）。
#
#   上限与退避取值：3 次（首次 + 2 次重跑）、退避 0.2 → 0.4s（带抖动）。
#   死锁是概率现象，3 次全撞上的概率已经极低；再往上加只会把「库真的有问题」
#   拖成「调用方一直转圈」。抖动是为了两个事务不在同一毫秒重新冲进同一个锁环。
#   🔴 绝不无限重试；超限**原样抛出**（不吞、不静默成功）—— 吞掉一次死锁等于
#   让调用方以为这批记忆写进去了。
DEADLOCK_RETRY_ATTEMPTS = 3
DEADLOCK_RETRY_BACKOFF_S = 0.2

#: SQLSTATE：40P01 = deadlock_detected，40001 = serialization_failure
#: （后者是同一族「事务被并发冲突打断」，同样只能整体重跑）
_CONFLICT_SQLSTATES = frozenset({"40P01", "40001"})


def _is_retryable_conflict(exc: BaseException) -> bool:
    """异常是否属于「重跑整个事务能救」的并发冲突。

    判据按可靠性排序：SQLSTATE（psycopg 的标准属性，不依赖 import）→ 类型名。
    刻意**不**在模块顶层 import psycopg：没装 PG 依赖的人不该被这个模块拖挂
    （见模块注释第 2 条设计选择），所以类型判据退化成类名字符串。
    """
    if getattr(exc, "sqlstate", None) in _CONFLICT_SQLSTATES:
        return True
    return type(exc).__name__ in ("DeadlockDetected", "SerializationFailure")


def with_deadlock_retry(fn: Callable) -> Callable:
    """给「整个写事务」挂上界重试（第二道防线，全模块单点）。

    🔴 **只能挂在以 `_tx()` 为边界的方法上**：重跑的是整个方法体，因此重跑的是
    整个事务。若挂在只包半截的方法上（比如一个只发一条 SQL 的 helper），
    「重跑整个事务」就名不副实了 —— 那才是我们要防的续跑半截。
    """

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        backoff = DEADLOCK_RETRY_BACKOFF_S
        for attempt in range(1, DEADLOCK_RETRY_ATTEMPTS + 1):
            try:
                return fn(self, *args, **kwargs)
            except Exception as e:              # noqa: BLE001 — 只对下面那两类重试
                if not _is_retryable_conflict(e):
                    raise
                if attempt >= DEADLOCK_RETRY_ATTEMPTS:
                    # 超限：明确失败（记录后再抛），绝不静默成功、绝不无限重试
                    logger.error(
                        "storage_pg: %s.%s 重跑整个事务 %d 次仍遇并发冲突，"
                        "明确失败（未写入）: %s: %s",
                        type(self).__name__, fn.__name__,
                        DEADLOCK_RETRY_ATTEMPTS, type(e).__name__, e,
                    )
                    raise
                sleep_s = backoff * (0.5 + random.random())   # 抖动，防同步重撞
                logger.warning(
                    "storage_pg: %s.%s 第 %d/%d 次遇 %s（事务已整体回滚），"
                    "%.2fs 后**重跑整个事务**: %s",
                    type(self).__name__, fn.__name__,
                    attempt, DEADLOCK_RETRY_ATTEMPTS, type(e).__name__, sleep_s, e,
                )
                time.sleep(sleep_s)
                backoff *= 2

    return wrapper


class _SchemaDimMismatch(ValueError):
    """线上 embedding 列维度与 embed_dim 不符 —— 配置错误，重试没有意义。

    继承 ValueError：保持既有「显式抛错」的对外语义，同时让 ensure_index 的
    重试循环**不**去重试它。
    """


def _fetch_col(cur: Any, sql: str) -> set:
    """单列目录查询 → 集合（迁移段专用，只读）。"""
    cur.execute(sql)
    return {row[0] for row in cur.fetchall() if row and row[0]}


def _as_text(v: Any) -> str:
    """bytes/None/任意 → str（迁移时 Redis hash 的值是 bytes）。"""
    if v is None:
        return ""
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


# 仍未实现的方法（语料维护类，与检索正交）→ 统一文案
_NOT_IMPLEMENTED = (
    "PgStorage.{name}() is not implemented yet — corpus maintenance is out of "
    "scope for batch 2 (which delivered read/write/search). "
    "Raising on purpose: returning a zero-statistics dict here would silently "
    "make every memory unsearchable when backend=postgres is selected."
)


class PgStorage(StorageBase):
    """PostgreSQL 存储后端（批 2：读写 + 检索全量）。

    与 RedisStorage 共用 `keepsake.storage.StorageBase` 接口；
    碎片 key、字段名、检索结果形状都按 Redis 侧对齐（见模块注释）。
    检索后处理绑定的是 `storage_shared` 的同一批函数对象（不是副本）。
    """

    # ---- 共用实现绑定（与 RedisStorage 绑的是**同一个函数对象**）----
    _rrf_fuse = rrf_fuse
    _apply_v2_filters = apply_v2_filters
    _rerank_with_decay = rerank_with_decay
    _load_fragments_by_keys = load_fragments_by_keys

    def __init__(
        self,
        dsn: str = "",
        host: str = "127.0.0.1",
        port: int = 5432,
        dbname: str = "keepsake",
        user: str = "",
        password: str = "",
        sslmode: str = "",
        connect_timeout: int = 10,
        embedder: Optional[Any] = None,
        agent_id: str = "",
        is_primary: bool = False,
        embed_dim: int = 1536,
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
    ):
        self._dsn = dsn
        self._host = host
        self._port = int(port)
        self._dbname = dbname
        self._user = user
        self._password = password
        self._sslmode = sslmode
        self._connect_timeout = int(connect_timeout)
        # ---- embedding 写开关（与 Redis 侧同一套判据，见 _has_embedder）----
        self._embed_enabled = True
        self._embedder = embedder
        if embedder is not None:
            # 用 _registered 判定（不要用 `dimension == 0` —— 0 是哨兵但语义上是
            # 「未登记」，靠 _registered 显式判断最稳）
            if not getattr(embedder, "_registered", True):
                logger.error(
                    "storage_pg: embedder %r is unregistered (dimension=0 sentinel). "
                    "EMBEDDING DISABLED — vector writes will be skipped. Add the "
                    "model to src/keepsake/embedder.py:_MODEL_DIMENSIONS.",
                    getattr(embedder, "_model", "<unknown>"),
                )
                self._embed_enabled = False
            else:
                embed_dim = embedder.dimension
        if not self._embedder and (embed_dim is None or embed_dim < 1):
            embed_dim = 1536
        self._embed_dim = int(embed_dim)
        # ---- 检索参数（与 RedisStorage 同名同义，默认值逐字对齐）----
        self._candidate_count = int(candidate_count)
        self._final_limit = int(final_limit)
        self._bm25_limit = int(bm25_limit)
        self._decay_half_days = int(decay_half_days)
        self._emotion_intensity_factor = float(emotion_intensity_factor)
        self._feedback_positive_boost = float(feedback_positive_boost)
        self._feedback_negative_penalty = float(feedback_negative_penalty)
        self._hot_topic_boost = float(hot_topic_boost)
        self._v2_min_score = float(v2_min_score)
        self._agent_id = agent_id
        self._is_primary = bool(is_primary)
        self._attention_boost_max = float(attention_boost_max)
        self._attention_base_increment = float(attention_base_increment)
        self._attention_emotion_factor = float(attention_emotion_factor)
        self._hot_topic_decay_half_days = int(hot_topic_decay_half_days)
        self._conn: Optional[Any] = None

    def _has_embedder(self) -> bool:
        """能否写/查向量（判据与 RedisStorage._has_embedder 一致）。"""
        return (
            self._embed_enabled
            and self._embedder is not None
            and hasattr(self._embedder, "get_embedding")
        )

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def _connect(self) -> Any:
        """惰性建连。psycopg 在这里才 import —— 没装也不影响 import 本模块。"""
        if self._conn is not None:
            return self._conn
        import psycopg  # noqa: PLC0415 — optional 依赖，延迟到真要用 PG 时

        if self._dsn:
            conninfo = self._dsn
        else:
            parts = [
                f"host={self._host}",
                f"port={self._port}",
                f"dbname={self._dbname}",
            ]
            if self._user:
                parts.append(f"user={self._user}")
            if self._password:
                parts.append(f"password={self._password}")
            if self._sslmode:
                parts.append(f"sslmode={self._sslmode}")
            conninfo = " ".join(parts)
        try:
            self._conn = psycopg.connect(conninfo, connect_timeout=self._connect_timeout)
        except Exception as e:
            # 只出 host，不出 conninfo（可能含口令）
            logger.error("storage_pg: connect to %s:%s failed: %s", self._host, self._port, e)
            raise
        logger.info("storage_pg: connected to %s:%s/%s", self._host, self._port, self._dbname)
        return self._conn

    def _drop_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        """事务 + 游标。异常时丢弃连接并原样上抛（不吞、不降级）。

        🔴 退出时**必须显式 commit**（2026-10 ks_pg_b2 实测修掉的静默丢写）：
        `_ro()` 会在连接上开一个隐式事务。此后 psycopg 的 `conn.transaction()`
        只会建 SAVEPOINT，退出时释放保存点、外层隐式事务**仍未提交**，
        而 `Connection.close()` 会把它回滚 ⇒ 「先查后写」时写入全部丢失。
        本函数是本模块唯一的写入口，所以在这里兜死。
        """
        conn = self._connect()
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    yield cur
            conn.commit()
        except Exception:
            self._drop_conn()
            raise

    @contextmanager
    def _ro(self) -> Iterator[Any]:
        """只读游标。读完显式 commit 关掉 psycopg 的隐式事务。

        为什么要关：psycopg 默认非 autocommit，任何 SELECT 都会开一个隐式事务并
        一直挂着（既留脏状态，也让后续 `_tx()` 只拿到 SAVEPOINT 而不提交）。
        """
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            self._drop_conn()
            raise

    def health_check(self) -> bool:
        """后端中立存活探针：SELECT 1。"""
        try:
            with self._ro() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except Exception as e:
            logger.warning("storage_pg: health_check failed: %s", e)
            return False

    def ensure_index(self) -> bool:
        """幂等建表 + 建检索列/索引。**成功 True / 失败 False**（接口约定）。

        ## 🔴 为什么**不能**每次都发 DDL（线上死锁的根因，PG 服务端日志实锤）

        旧写法是「把整段 DDL 无脑重发一遍，靠 `IF NOT EXISTS` 兜底」。错在：

        * `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` —— **列已存在也要拿
          ACCESS EXCLUSIVE**（要先查目录、要落元数据），于是它与并发 DML 互等：
          ```
          Process A waits for RowExclusiveLock on ks_entity_timeline; blocked by B
          Process B waits for AccessExclusiveLock on ks_fragment;   blocked by A
          A: INSERT INTO ks_entity_timeline (...) ON CONFLICT ... DO UPDATE
          B: ALTER TABLE ks_fragment ADD COLUMN IF NOT EXISTS content_tsv tsvector
          ```
          ⇒ 死锁。更糟的是 ACCESS EXCLUSIVE **挡住整张 ks_fragment 的读写**。
        * `CREATE INDEX IF NOT EXISTS` 索引已在时也要 SHARE 锁，同样挡写。
        * provider / 提炼 cron / 迁移脚本各自构造存储 → 各自调 ensure_index()
          ⇒ 这不是测试问题，是线上天天踩的雷。

        所以本实现做了三层：

        1. **先查后补**：`information_schema` / `pg_indexes` 核对，
           **缺什么才发什么**；齐备时**一条 DDL 都不发**（只发只读目录查询）。
        2. **进程内一次性**：`_SCHEMA_READY[(目标库, dim)]` 命中就直接返回，
           **一条 SQL 都不发**。
        3. **并发串行化 + 超时 + 有限重试**（真要发 DDL 时才走这条）：
           * **谁拿锁**：`pg_advisory_lock(SCHEMA_ADVISORY_LOCK_KEY)`，固定 key，
             全库迁移段共用一把，会话级锁。
           * **加锁顺序**：**先** `SET LOCAL lock_timeout`（5s），
             **再**取 advisory lock，**后**核对目录 → 发 DDL → 释放。
             lock_timeout 先设 ⇒ 连 advisory lock 本身等太久也会超时，
             不会无声挂死。
           * **超时**：`SET LOCAL lock_timeout = 5000`（事务结束自动还原，
             不污染同连接后面的普通读写）。
           * **重试上限**：`SCHEMA_RETRY_ATTEMPTS = 3`，退避 0.2/0.4s。
             超限 → `logger.error` + **返回 False**（明确失败，不静默成功）。

        ## 为什么 CREATE INDEX 不用 CONCURRENTLY

        `CONCURRENTLY` **不能在事务块里执行**，而本迁移段刻意跑在显式事务里
        （失败整体回滚，不会留下「建了一半」的表/列）。要迁就它就得把迁移段拆成
        事务外的多条语句 + 自己实现失败补偿，反而更容易半成品。加上它要扫两遍表、
        失败会残留 INVALID 索引，而这里的建索引只在**首次引导**（之后被第 1 层
        「只补缺的」彻底挡掉）才发生 —— 挡写窗口可接受。故保留普通 CREATE INDEX，
        但**用 lock_timeout 兜住**，让它最多阻塞 5s 而不是无限挡写。

        ## 维度漂移保护

        线上 `embedding` 列的维度必须等于 `self._embed_dim`，不等就**显式抛错**
        （`_SchemaDimMismatch`）而不是继续 —— 否则会像 2026-09 ks_embed_dim 记录的
        那样，把错维度向量灌进库里、KNN 静默搜不到（Redis 侧那次是 WARN+禁写）。
        """
        memo_key = (self._target_key(), self._embed_dim)
        if _SCHEMA_READY.get(memo_key):
            # 进程内第二次：一律不发 SQL（这是「同机多个入口」最常见的那条路）
            logger.debug(
                "storage_pg: schema already ensured in this process (dim=%d), skipping",
                self._embed_dim,
            )
            return True

        backoff = SCHEMA_RETRY_BACKOFF_S
        for attempt in range(1, SCHEMA_RETRY_ATTEMPTS + 1):
            try:
                self._migrate_schema()
            except _SchemaDimMismatch:
                raise                      # 配置错误，重试无意义
            except Exception as e:
                logger.warning(
                    "storage_pg: ensure_index 第 %d/%d 次失败（%s: %s），%.2fs 后重试",
                    attempt, SCHEMA_RETRY_ATTEMPTS, type(e).__name__, e, backoff,
                )
                if attempt >= SCHEMA_RETRY_ATTEMPTS:
                    logger.error(
                        "storage_pg: ensure_index 重试 %d 次仍失败，明确返回 False"
                        "（不静默成功）：%s: %s",
                        SCHEMA_RETRY_ATTEMPTS, type(e).__name__, e,
                    )
                    return False
                time.sleep(backoff)
                backoff *= 2
                continue
            _SCHEMA_READY[memo_key] = True
            logger.info(
                "storage_pg: schema ready on %s:%s/%s (tsv+GIN, embedding vector(%d) + HNSW cosine)",
                self._host, self._port, self._dbname, self._embed_dim,
            )
            return True
        return False      # 理论不可达（循环内已 return），留作兜底

    def _target_key(self) -> str:
        """进程级记忆的库指纹。

        DSN 可能含口令 ⇒ 只留 sha256 前 16 位，**不进任何日志/异常消息**。
        """
        if self._dsn:
            return "dsn:" + hashlib.sha256(self._dsn.encode("utf-8")).hexdigest()[:16]
        return f"{self._host}:{self._port}/{self._dbname}"

    def _migrate_schema(self) -> None:
        """跑一遍迁移段（advisory lock 包裹）。失败向上抛，由 ensure_index 重试。

        这里**不做**重试/降级 —— 保证「一次尝试 = 一个明确的成败」，
        重试策略集中在 `ensure_index` 一处，便于后来者读懂上限。
        """
        with self._tx() as cur:
            # 顺序要紧：lock_timeout 必须**先于**任何可能等锁的语句设置，
            # 否则 advisory lock 可能无声挂住。SET LOCAL 随事务结束自动还原，
            # 不会污染这条连接后续的普通读写。
            # （SET 是 utility 语句、不接受占位参数 ⇒ 值直接内联；它是模块内 int 常量。）
            cur.execute(f"SET LOCAL lock_timeout = {int(SCHEMA_LOCK_TIMEOUT_MS)}")
            # 会话级锁：与本事务无关，别的进程/线程的迁移段在此排队。
            cur.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_ADVISORY_LOCK_KEY,))
            try:
                for stmt in self._plan_schema_ddl(cur):
                    cur.execute(stmt)
                self._check_embedding_dim(cur)
            finally:
                # 失败时事务已 abort，这句解锁大概率也失败 —— 连接随即被
                # `_tx` 丢弃，断连会自动释放会话级锁，不会泄锁。
                try:
                    cur.execute("SELECT pg_advisory_unlock(%s)",
                                (SCHEMA_ADVISORY_LOCK_KEY,))
                except Exception as e:      # noqa: BLE001 — 解锁失败不得掩盖原异常
                    logger.warning("storage_pg: pg_advisory_unlock failed: %s: %s",
                                   type(e).__name__, e)

    def _plan_schema_ddl(self, cur: Any) -> List[str]:
        """**先查后补**：返回确实缺的 DDL 语句，齐备时返回空列表。

        查询与 DDL 在同一个事务 + 同一把 advisory lock 下 ⇒ check-then-act
        不会与另一个迁移段交错。缺什么补什么 ⇒ 列/索引已存在时**不发任何 DDL**。
        """
        have_tables = _fetch_col(cur, _LIST_TABLES_SQL)
        have_indexes = _fetch_col(cur, _LIST_INDEXES_SQL)
        have_columns = _fetch_col(cur, _LIST_FRAGMENT_COLUMNS_SQL)

        plan: List[str] = []
        for name, sql in _DDL_TABLES:
            if name not in have_tables:
                plan.append(sql)
        for name, sql in _DDL_INDEXES:
            if name not in have_indexes:
                plan.append(sql)
        for name, sql in _search_column_ddl(self._embed_dim):
            if name not in have_columns:
                plan.append(sql)
        for name, sql in _search_index_ddl():
            if name not in have_indexes:
                plan.append(sql)
        if plan:
            logger.info("storage_pg: schema incomplete, applying %d DDL statement(s): %s",
                        len(plan), [s.split("\n")[0][:60] for s in plan])
        return plan

    def _check_embedding_dim(self, cur: Any) -> None:
        """维度漂移保护：线上 embedding 列维度必须等于 embed_dim，不等就抛错。"""
        cur.execute(
            """
            SELECT format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            WHERE a.attrelid = 'ks_fragment'::regclass
              AND a.attname = 'embedding' AND NOT a.attisdropped
            """
        )
        row = cur.fetchone()
        if row and row[0] and row[0] != f"vector({self._embed_dim})":
            raise _SchemaDimMismatch(
                f"storage_pg: ks_fragment.embedding is {row[0]} but embed_dim="
                f"{self._embed_dim}. Refusing to mix vector dimensions — drop "
                f"the column and re-run ensure_index after fixing the embedder."
            )

    def close(self) -> None:
        """关连接，可重复调用。"""
        self._drop_conn()

    # ------------------------------------------------------------------
    # 语料维护类仍未实现（显式抛错，绝不静默返回零统计）
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 检索：BM25（tsvector）/ KNN（pgvector HNSW）/ 混合（RRF，共用重排）
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_tag(tag: str) -> str:
        r"""tag 值清理：去结构字符（| , { } " ' 与空白）—— 与 Redis `_tag_safe` 同意图。

        PG 侧用 `strpos('|'||tags||'|', '|tag|')` 做 TAG 语义匹配，
        两侧用同一套「首尾补竖线 + 子串」判据，所以 tag 里不能出现 `|`。
        """
        for ch in ("\\", "{", "}", "|", ",", '"', "'", " "):
            tag = tag.replace(ch, "")
        return tag.strip()

    def _load_synonym_map(self) -> Dict[str, set]:
        """同义词表（对齐 Redis 的 keepsake:synonyms hash）。

        批 2 的 BM25 查询式构造复用 Redis 侧的 `_expand_terms`，词表必须同源，
        否则「同义词扩展」这条召回面两边不一致，对照评测直接失真。
        """
        out: Dict[str, set] = {}
        try:
            with self._ro() as cur:
                cur.execute("SELECT term, synonyms FROM ks_synonym")
                rows = cur.fetchall()
        except Exception as e:
            logger.warning("storage_pg: load synonyms error: %s", e)
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
                    out.setdefault(sl, set()).add(key)
        return out

    def _search_filter_sql(
        self,
        tag_filter: str,
        agent_id: str,
        is_primary: Optional[bool],
    ) -> tuple:
        """检索 WHERE 片段（标签 + agent 隔离），语义逐条对齐 Redis 侧。

        Redis：`@tags:{...}` + `@tags:{agent:X} || @tags:{shared}`（TAG 精确匹配）
        PG   ：`strpos('|'||tags||'|', '|tag|') > 0`（同样只在完整标签边界上匹配，
               不会让 `agent:a` 命中 `agent:ab`）。用 strpos 而不是 LIKE，
               是为了不让 tag/agent_id 里的 `%` `_` 被当通配符。
        """
        clauses: List[str] = []
        params: List[Any] = []

        effective_agent_id = agent_id if agent_id else self._agent_id
        effective_is_primary = is_primary if is_primary is not None else self._is_primary

        # 非主脑：只能搜 agent 自己的 或 shared 的碎片
        if not effective_is_primary:
            if effective_agent_id:
                agent_tag = self._clean_tag(f"agent:{effective_agent_id}")
                clauses.append(
                    "(strpos('|' || tags || '|', %s) > 0"
                    " OR strpos('|' || tags || '|', '|shared|') > 0)"
                )
                params.append(f"|{agent_tag}|")
            else:
                clauses.append("strpos('|' || tags || '|', '|shared|') > 0")

        if tag_filter:
            tags = [
                f"|{self._clean_tag(t)}|"
                for t in (x.strip() for x in tag_filter.split(","))
                if self._clean_tag(t)
            ]
            if tags:
                clauses.append("strpos('|' || tags || '|', " + " || ".join(["%s"] * len(tags))
                               + ") > 0")
                params.extend(tags)

        # 两版检索共同的「不是活记忆」过滤（对齐 Redis search_* 里的 continue）
        clauses.append("invalid_at = ''")
        clauses.append("valid_until = ''")
        clauses.append(f"content <> '' AND length(content) <= {MAX_CONTENT_LEN}")
        return " AND ".join(clauses), params

    # 🔴 SELECT 列表必须**按 SEARCH_FIELDS 的顺序**排，不能按 FRAGMENT_COLUMNS 排：
    #    两者的列顺序不同（FRAGMENT_COLUMNS 里 entities/fragment_type 排在
    #    invalid_at 前面），按错的顺序取会把 entities 的值塞进 invalid_at 键 ——
    #    结果字典看着正常、语义全错，且上层按 invalid_at 判「不是活记忆」会误判。
    _SELECT_FIELDS = "f.key, " + ", ".join(f"f.{c}" for c in SEARCH_FIELDS)

    def _rows_to_fragments(self, rows: List[tuple], score_key: str, default_score: float) -> List[Dict[str, Any]]:
        """DB 行 → 检索结果 dict（形状与 Redis 侧**逐字同形**）。

        键集合 = SEARCH_FIELDS（空值不进 dict，同 Redis hash 稀疏语义）
                 + `_key` + `_bm25_score`/`_knn_score`；
        随后的 `_sim` / `_combined_score` / `_weights` 由共用的 rerank_with_decay 补。
        """
        out: List[Dict[str, Any]] = []
        for row in rows:
            key = row[0]
            frag: Dict[str, Any] = {}
            for name, value in zip(SEARCH_FIELDS, row[1:1 + len(SEARCH_FIELDS)]):
                if value is None or value == "":
                    continue
                frag[name] = value if isinstance(value, str) else str(value)
            if not frag.get("content"):
                continue
            frag["_key"] = key
            frag[score_key] = float(row[-1]) if row[-1] is not None else default_score
            out.append(frag)
        return out

    def search_bm25(
        self,
        query: str,
        tag_filter: str = "",
        agent_id: str = "",
        is_primary: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """BM25 全文搜索（jieba 分词 → tsquery → ts_rank_cd）。

        流程与 Redis 侧 search_bm25 同构：
          1. 分词（segment_query）→ 同义词扩展 → `_sanitize_terms`（含路径/连字符拆子词）
          2. tsquery OR 检索，ts_rank_cd 排序
          3. 共用的 rerank_with_decay 重排 → 取 final_limit
        空查询 → 空列表并记 WARNING（明确、不静默）。
        """
        if not (query or "").strip():
            logger.warning("storage_pg: search_bm25 called with an empty query — "
                           "returning no results (explicit, not a backend outage)")
            return []

        # 1. 查询式构造 —— 复用 Redis 侧的同义词扩展 + sanitize（含拆子词那套）
        terms = _expand_terms(segment_query(query), self._load_synonym_map())
        terms = _sanitize_terms(terms)
        if not terms:
            logger.warning("storage_pg: query %r sanitized down to zero terms — "
                           "returning no results (explicit)", query[:50])
            return []

        # tsquery：每个词加单引号做 lexeme 引用（词内含 : ' 也不会破语法），
        # 单引号在 tsquery 里靠写两遍转义。
        tsquery = " | ".join("'" + t.replace("'", "''") + "'" for t in terms)
        where_sql, params = self._search_filter_sql(tag_filter, agent_id, is_primary)

        sql = (
            f"SELECT {self._SELECT_FIELDS}, "
            f"ts_rank_cd(f.content_tsv, to_tsquery('simple', %s), {TS_RANK_NORM}) AS score "
            "FROM ks_fragment f "
            "WHERE f.content_tsv @@ to_tsquery('simple', %s) "
            f"AND {where_sql} "
            "ORDER BY score DESC LIMIT %s"
        )
        with self._ro() as cur:
            cur.execute(sql, [tsquery, tsquery, *params, self._bm25_limit])
            rows = cur.fetchall()

        fragments = self._rows_to_fragments(rows, "_bm25_score", 0.0)
        fragments = self._rerank_with_decay(fragments, score_key="_bm25_score")
        return fragments[: self._final_limit]

    def _text_to_vector(self, text: str) -> Optional[List[float]]:
        """文本 → float 向量（无 embedder 或取不到返回 None）。"""
        if not self._has_embedder():
            return None
        vec = self._embedder.get_embedding(text)
        return list(vec) if vec else None

    @staticmethod
    def _vector_literal(vec: List[float]) -> str:
        """向量 → pgvector 文本字面量（`[1.0,2.0]`）。

        🔴 为什么不直接传 list：pgvector 的 Python 适配器（`pgvector` 包）本机
        没装，也不该为「写个字符串」引入新依赖。psycopg 原生能把 text 参数
        按 `::vector` 转型，语义完全等价且零新依赖。
        """
        return "[" + ",".join(repr(float(x)) for x in vec) + "]"

    def search_knn(
        self,
        query: str,
        tag_filter: str = "",
        agent_id: str = "",
        is_primary: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """KNN 向量搜索（HNSW + 余弦距离），经共用的时间衰减重排后返回。

        `embedding <=> $q` 返回**余弦距离**（0~2，越小越近），
        与 Redis `DISTANCE_METRIC COSINE` 的 doc.score 量纲一致，
        所以 `_rerank_with_decay(is_knn=True)` 可以两边共用同一个实现。
        ORDER BY 写成 `f.embedding <=> 常量` —— pgvector 正是这个形状才走 HNSW。
        """
        if not (query or "").strip():
            logger.warning("storage_pg: search_knn called with an empty query — "
                           "returning no results (explicit, not a backend outage)")
            return []
        if not self._has_embedder():
            logger.warning("storage_pg: search_knn called but no usable embedder — "
                           "returning no results (vector path unavailable, BM25 unaffected)")
            return []

        vec = self._text_to_vector(query)
        if not vec:
            logger.warning("storage_pg: search_knn: embedder returned no vector — "
                           "returning no results (explicit, not a backend outage)")
            return []

        where_sql, params = self._search_filter_sql(tag_filter, agent_id, is_primary)
        lit = self._vector_literal(vec)
        sql = (
            f"SELECT {self._SELECT_FIELDS}, (f.embedding <=> %s::vector) AS score "
            "FROM ks_fragment f "
            "WHERE f.embedding IS NOT NULL "
            f"AND {where_sql} "
            "ORDER BY f.embedding <=> %s::vector "
            "LIMIT %s"
        )
        with self._ro() as cur:
            cur.execute(sql, [lit, *params, lit, self._candidate_count])
            rows = cur.fetchall()

        fragments = self._rows_to_fragments(rows, "_knn_score", 1.0)
        fragments = self._rerank_with_decay(fragments, score_key="_knn_score", is_knn=True)
        return fragments[: self._final_limit]

    def search(
        self,
        query: str,
        tag_filter: str = "",
        agent_id: str = "",
        is_primary: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """统一检索入口（与 RedisStorage.search 同构）。

          1. BM25 全文搜索（零成本，有同义词扩展）
          2. 有 embedder 时并行取 KNN，两路都非空 → RRF 融合
          3. v2 后置过滤（剔除 consumed / superseded + 相似度地板）

        两步的重排/融合/过滤都走 `storage_shared` 的共用实现，不是副本。
        """
        effective_agent_id = agent_id if agent_id else self._agent_id
        effective_is_primary = is_primary if is_primary is not None else self._is_primary

        bm25_results = self.search_bm25(query, tag_filter, effective_agent_id,
                                        effective_is_primary)
        if self._has_embedder():
            knn_results = self.search_knn(query, tag_filter, effective_agent_id,
                                          effective_is_primary)
            if knn_results:
                fused = self._rrf_fuse(bm25_results, knn_results)
                return self._apply_v2_filters(fused)
        return self._apply_v2_filters(bm25_results)

    # ------------------------------------------------------------------
    # 后端专属取数钩子（storage_shared 的两个后端接口）
    # ------------------------------------------------------------------

    def _fetch_superseded_by(self, keys: List[str]) -> Dict[str, str]:
        """批量读 superseded_by（一条 SQL，不 N+1）。"""
        if not keys:
            return {}
        with self._ro() as cur:
            cur.execute(
                "SELECT key, superseded_by FROM ks_fragment WHERE key = ANY(%s)",
                (list(keys),),
            )
            return {k: v for k, v in cur.fetchall() if v}

    def _fetch_fragments(self, keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量读碎片（一条 SQL，不 N+1），字段裁到检索形状。"""
        out: Dict[str, Dict[str, Any]] = {}
        if not keys:
            return out
        with self._ro() as cur:
            cur.execute(
                f"SELECT {', '.join(FRAGMENT_COLUMNS)} FROM ks_fragment WHERE key = ANY(%s)",
                (list(keys),),
            )
            for row in cur.fetchall():
                out[row[0]] = self._row_to_fragment(row)
        return out

    # ------------------------------------------------------------------
    # 语料维护类仍未实现（显式抛错，绝不静默返回零统计）
    # ------------------------------------------------------------------

    def discover_synonyms(self, rebuild: bool = False) -> Dict[str, Any]:
        raise NotImplementedError(_NOT_IMPLEMENTED.format(name="discover_synonyms"))

    def generate_jieba_dict(self, output_path: str = None) -> Dict[str, Any]:
        raise NotImplementedError(_NOT_IMPLEMENTED.format(name="generate_jieba_dict"))

    # ------------------------------------------------------------------
    # 行 <-> 碎片 dict
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_fragment(row: tuple) -> Dict[str, Any]:
        """DB 行 → 碎片 dict（键值全是 str，空值不进 dict）。

        与 Redis hash 语义对齐：Redis 只存写过的字段，这里靠「空串不进 dict」
        达到同样的稀疏效果（上层 get_fragment 的用法一致）。
        """
        out: Dict[str, Any] = {}
        for name, value in zip(FRAGMENT_COLUMNS, row):
            if name == "key":
                continue
            if value is None or value == "":
                continue
            out[name] = value if isinstance(value, str) else str(value)
        return out

    @staticmethod
    def _fragment_key_for(text: str) -> str:
        """碎片 key —— 与 Redis store() 同一套算法（sha256 前 12 位）。"""
        return f"memory:frag:{hashlib.sha256(text.encode()).hexdigest()[:12]}"

    @staticmethod
    def _tsv_tokens(text: str) -> List[str]:
        """文本 → tsvector 的词元串（jieba 切，纯 Python，不依赖任何 PG 扩展）。

        🔴 这是「不装 pg_jieba/zhparser」的落点：中文分词全在 Python 侧做，
        写进库时就算成 tsvector 存列，检索时只做 tsquery 匹配，PG 全程不碰分词。
        'simple' 配置只小写化、不做词干还原 ⇒ 一词一 token，不会二次合并。
        """
        import jieba  # noqa: PLC0415 — 与 splitter 同款延迟 import（jieba 首调建词典）

        out: List[str] = []
        for w in jieba.lcut(text or ""):
            w = w.strip()
            if not w or not any(ch.isalnum() for ch in w):
                continue   # 纯标点/空白 → to_tsvector 只会造出噪音 token
            out.append(w)
        return out

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    # Redis hash 字段 → PG 列的映射（迁移脚本按 Redis 侧字段名喂进来）
    _HASH_TO_COLUMN = {c: c for c in FRAGMENT_COLUMNS}

    @with_deadlock_retry
    def upsert_fragment(self, fields: Dict[str, Any]) -> bool:
        """按给定字段**原样**写一条碎片（迁移专用，幂等）。

        与 `store()` 的区别（这正是迁移要用它的原因）：
          * `store()` 按内容 hash 造 key、遇同内容把旧版另起 `:<epoch>` 新键
            —— 那是「在线写入」的去重语义
          * 本方法**用调用方给的 key**，`ON CONFLICT (key) DO UPDATE` 覆盖
            ⇒ 重复跑不产生重复行，且 Redis 里是什么样 PG 里就什么样

        tsvector / embedding 照常现场算（迁移脚本不该也不需要自己分词）。
        未提供的列保持原值不被清空（`DO UPDATE` 只覆盖传入的列）。
        """
        key = fields.get("key")
        content = fields.get("content") or ""
        if not key:
            logger.warning("storage_pg: upsert_fragment 缺 key，跳过")
            return False

        # INSERT 的列清单必须以 key 打头（参数也是 key 在最前），
        # 否则列数与参数数对不上，psycopg 直接报「placeholder 数不符」。
        cols: List[str] = ["key"]
        params: List[Any] = [key]
        for hash_field, column in self._HASH_TO_COLUMN.items():
            if hash_field == "key" or hash_field not in fields:
                continue
            value = fields[hash_field]
            cols.append(column)
            params.append(_as_text(value))
        if "content" not in cols:
            cols.append("content")
            params.append(content)

        entities = _as_text(fields.get("entities"))
        tags = _as_text(fields.get("tags"))
        vec = self._text_to_vector(content)
        tsv_sql = ("setweight(to_tsvector('simple', %s), 'A') "
                   "|| setweight(to_tsvector('simple', %s), 'B')")
        params.extend([" ".join(self._tsv_tokens(content)),
                       " ".join(self._tsv_tokens(f"{entities} {tags}"))])
        vec_sql = "%s::vector" if vec else "NULL::vector"
        if vec:
            params.append(self._vector_literal(vec))

        set_sql = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
        with self._tx() as cur:
            cur.execute(
                f"""
                INSERT INTO ks_fragment ({', '.join(cols)}, content_tsv, embedding)
                VALUES ({', '.join(['%s'] * len(cols))}, {tsv_sql}, {vec_sql})
                ON CONFLICT (key) DO UPDATE SET {set_sql},
                    content_tsv = EXCLUDED.content_tsv,
                    embedding = COALESCE(EXCLUDED.embedding, ks_fragment.embedding)
                """,
                params,
            )
        return True

    @with_deadlock_retry
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
        """写入碎片。字段/去重/版本化/热词语义与 RedisStorage.store 一致。"""
        # 情绪分析（除非明确传入）
        if sentiment_score is None or sentiment_label is None:
            intensity, label = analyze_emotion(text)
        else:
            intensity, label = sentiment_score, sentiment_label

        keywords = extract_keywords(text, max_keywords=5)

        # agent tag 注入（与 Redis 侧同一套规则）
        final_tags = tags
        if self._agent_id:
            if not final_tags:
                final_tags = f"agent:{self._agent_id}"
            else:
                tag_list = [t.strip() for t in final_tags.split(",") if t.strip()]
                filtered_tags = [t for t in tag_list if not t.startswith("agent:")]
                filtered_tags.append(f"agent:{self._agent_id}")
                final_tags = ",".join(filtered_tags)

        entities = extract_entities(text)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        key = self._fragment_key_for(text)

        with self._tx() as cur:
            # 去重：同内容已存在 → 保留 feedback_score + 旧版标失效 + 新版独立 key
            cur.execute(
                "SELECT feedback_score, valid_until FROM ks_fragment WHERE key = %s",
                (key,),
            )
            row = cur.fetchone()
            existing_feedback = (row[0] if row and row[0] else "0") or "0"

            if row is not None:
                cur.execute(
                    "UPDATE ks_fragment SET valid_until = %s, is_archived = '1' WHERE key = %s",
                    (now_iso, key),
                )
                key = f"{key}:{int(time.time())}"

            entities_str = ",".join(entities) if entities else ""
            # ---- 批 2：同步算好检索用的两列，别让调用方回填 ----
            # 1) content_tsv = A(content 的 jieba 分词) || B(entities + tags)
            #    与 Redis 侧的 @content / @entities / @tags 三路 OR 召回面同源。
            tsv_sql = (
                "setweight(to_tsvector('simple', %s), 'A') "
                "|| setweight(to_tsvector('simple', %s), 'B')"
            )
            tsv_params = [" ".join(self._tsv_tokens(text)),
                          " ".join(self._tsv_tokens(f"{entities_str} {final_tags}"))]
            # 2) embedding：与 Redis 侧同一 embedder 的 float32 向量
            # 无 embedder → 向量列留 NULL（KNN 查不到这条，但 BM25 照常命中；
            # 与 Redis 侧「没 embedder 就没 embed_bin」的处理完全一致）
            vec = self._text_to_vector(text)
            vec_sql = "%s::vector" if vec else "NULL::vector"

            cur.execute(
                f"""
                INSERT INTO ks_fragment (
                    key, content, tags, category, source, created,
                    sentiment_score, sentiment_label, feedback_score,
                    entities, fragment_type, content_tsv, embedding
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          {tsv_sql}, {vec_sql})
                """,
                (
                    key, text, final_tags, category, source, now_iso,
                    str(intensity), label, existing_feedback,
                    entities_str, fragment_type,
                    *tsv_params,
                    *([self._vector_literal(vec)] if vec else []),
                ),
            )
            self._record_attention(cur, keywords, intensity, now_ts=now.timestamp())
            if entities:
                self._record_entity_cooc(cur, entities, now_ts=now.timestamp())
                ts = time.time()
                self._insert_many(
                    cur, "ks_entity_timeline", ("entity", "frag_key", "ts"),
                    [(ent, key, ts) for ent in entities],
                    "ON CONFLICT (entity, frag_key) DO UPDATE SET ts = EXCLUDED.ts",
                )
            self._record_topics(cur, keywords, label, now_ts=now.timestamp())
        return True

    @staticmethod
    def _insert_many(
        cur: Any,
        table: str,
        columns: tuple,
        rows: List[tuple],
        on_conflict: str,
    ) -> None:
        """单条多行 INSERT（ON CONFLICT 由调用方给）—— **发 SQL 前统一排序**。

        ## 🔴 为什么行序 = 加锁顺序（这就是线上死锁的根因）

        `INSERT ... ON CONFLICT DO UPDATE` 碰到已存在的唯一键时，会先对该行
        拿一把**行级排他锁**再更新。也就是说：**PG 是按 VALUES 里的行顺序逐行加锁的**。
        两个事务若插入**同一批唯一键**却按**不同顺序**发出，就会互相等对方事务结束：

            A: 锁住 ('all','部署')，等 ('all','上线')  ← 这把锁在 B 手里
            B: 锁住 ('all','上线')，等 ('all','部署')  ← 这把锁在 A 手里
            ⇒ 环形等待 ⇒ deadlock detected

        主脑抓到的现场原文：
            DeadlockDetected: deadlock detected
            DETAIL: Process A waits for ShareLock on transaction 7581; blocked by B
                    Process B waits for ShareLock on transaction 7580; blocked by A
            CONTEXT: while inserting index tuple (0,86) in relation "ks_hot_topic"

        ## 🔴 为什么来自 set/dict 的输入**必须先排序**（不能靠调用方自觉）

        行序来自 `keywords` / `entities` 这类集合：Python 的 `set`/`dict` 迭代顺序
        由字符串哈希决定，而 **CPython 默认开启哈希随机化** ⇒ **不同进程里同一个
        set 的迭代顺序可以不同**。provider 与提炼 cron 各跑各的进程，同一批热词
        在两边的行序就可能相反 ⇒ 不是「偶尔撞上」，是**必然成环**。
        所以排序必须落在**所有多行写的唯一出口**（本函数），而不是每个调用点各写一遍
        —— 调用点有五六个，漏一个就退回原样，且漏的那个没人看得出来。

        ## 排序键怎么选

        本模块五张多行写的表，唯一键**一律是 columns 的前几列**
        （ks_hot_topic/ks_attention=(scope,topic)、ks_hot_topic_seen=(topic)、
        ks_entity_cooc=(pair)、ks_entity_timeline=(entity,frag_key)），
        所以**按整行 `sorted()`** 与「按唯一键排序」完全等价：唯一键互不相同时，
        决定顺序的只有前几列，后面的 score/ts 永远轮不到比较。
        不给每张表单开一份键列表 = 少一处会漂移的配置。
        """
        if not rows:
            return
        rows = sorted(rows)          # ← 第一道防线的**单点**，见上文「为什么行序=加锁顺序」
        values_sql = ",".join(["(" + ",".join(["%s"] * len(columns)) + ")"] * len(rows))
        params: List[Any] = []
        for row in rows:
            params.extend(row)
        cur.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES {values_sql} {on_conflict}",
            params,
        )

    def _record_attention(
        self,
        cur: Any,
        keywords: List[str],
        intensity: float,
        now_ts: float,
    ) -> None:
        """注意力累加（对齐 attention.record_attention 的公式与三档 TTL）。"""
        if not keywords:
            return
        increment = self._attention_base_increment + intensity * self._attention_emotion_factor
        rows = []
        for kw in keywords:
            kw_lower = kw.lower().strip()
            if len(kw_lower) < 2:
                continue
            for scope in _ATTENTION_SCOPES:
                rows.append(
                    (scope, kw_lower, increment, now_ts + _ATTENTION_TTL.get(scope, 86400))
                )
        self._insert_many(
            cur, "ks_attention", ("scope", "topic", "score", "expire_ts"), rows,
            "ON CONFLICT (scope, topic) DO UPDATE"
            " SET score = ks_attention.score + EXCLUDED.score, expire_ts = EXCLUDED.expire_ts",
        )

    def _record_entity_cooc(self, cur: Any, entities: List[str], now_ts: float) -> None:
        """实体共现对累加（对齐 Redis _record_entity_cooccurrence）。"""
        if len(entities) < 2:
            return
        sorted_ents = sorted(e.lower().strip() for e in entities if e.strip())
        expire_ts = now_ts + _ENTITY_COOC_TTL
        rows = [
            (f"{sorted_ents[i]}||{sorted_ents[j]}", 1.0, expire_ts)
            for i in range(len(sorted_ents))
            for j in range(i + 1, len(sorted_ents))
        ]
        self._insert_many(
            cur, "ks_entity_cooc", ("pair", "score", "expire_ts"), rows,
            "ON CONFLICT (pair) DO UPDATE"
            " SET score = ks_entity_cooc.score + EXCLUDED.score, expire_ts = EXCLUDED.expire_ts",
        )

    def _record_topics(
        self,
        cur: Any,
        keywords: List[str],
        sentiment_label: str,
        now_ts: float,
    ) -> None:
        """热词累加（对齐 Redis _record_topics 的情感权重与三榜 TTL）。"""
        if not keywords:
            return
        sentiment_weight = 1.0
        if sentiment_label == "positive":
            sentiment_weight = 1.5
        elif sentiment_label == "negative":
            sentiment_weight = 1.3
        rows = [
            (scope, kw, sentiment_weight, now_ts + _TOPIC_TTL[scope])
            for kw in keywords
            for scope in _TOPIC_SCOPES
        ]
        self._insert_many(
            cur, "ks_hot_topic", ("scope", "topic", "score", "expire_ts"), rows,
            "ON CONFLICT (scope, topic) DO UPDATE"
            " SET score = ks_hot_topic.score + EXCLUDED.score, expire_ts = EXCLUDED.expire_ts",
        )
        self._insert_many(
            cur, "ks_hot_topic_seen", ("topic", "last_seen"), [(kw, now_ts) for kw in keywords],
            "ON CONFLICT (topic) DO UPDATE SET last_seen = EXCLUDED.last_seen",
        )

    @with_deadlock_retry
    def supersede_fragment(self, old_key: str, new_key: str) -> bool:
        """封边：标 superseded_by + superseded_at（不物理删）。"""
        if not old_key:
            return False
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._tx() as cur:
            cur.execute(
                "UPDATE ks_fragment SET superseded_by = %s, superseded_at = %s WHERE key = %s",
                (new_key or "__void__", now_iso, old_key),
            )
        return True

    @with_deadlock_retry
    def correct_fragments(self, keys: List[str]) -> int:
        """打 corrected 标签 + feedback_score=-1，返回实际处理条数。

        🔴 `SELECT ... WHERE key = ANY(%s)` 的返回顺序**没有任何保证**（PG 按
        物理位置返回），而下面逐行 UPDATE 是逐行拿行锁 ⇒ 两个进程并发纠正同一批
        碎片时会按不同顺序加锁 ⇒ 与 `_insert_many` 同款死锁。这里按 key 排序，
        把加锁顺序固定成全局一致的顺序（第一道防线，见 `_insert_many` docstring）。
        """
        if not keys:
            return 0
        now_iso = datetime.now(timezone.utc).isoformat()
        count = 0
        with self._tx() as cur:
            cur.execute(
                "SELECT key, tags FROM ks_fragment WHERE key = ANY(%s)",
                (list(keys),),
            )
            for key, tags in sorted(cur.fetchall()):     # ← 排序即固定加锁顺序
                tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
                if "corrected" not in tag_list:
                    tag_list.append("corrected")
                    cur.execute(
                        """
                        UPDATE ks_fragment
                        SET tags = %s, feedback_score = '-1', corrected_at = %s
                        WHERE key = %s
                        """,
                        (",".join(tag_list), now_iso, key),
                    )
                else:
                    cur.execute(
                        "UPDATE ks_fragment SET feedback_score = '-1', corrected_at = %s"
                        " WHERE key = %s",
                        (now_iso, key),
                    )
                count += 1
        if count:
            logger.info("storage_pg: corrected %d fragments", count)
        return count

    @with_deadlock_retry
    def record_feedback(self, fragment_key: str, is_positive: bool) -> bool:
        """反馈累加：有用 +1 / 没用 -2（与 Redis hincrby 同公式）。

        语义对齐说明：Redis 的 HINCRBY 对不存在的 key 也会建 hash 并返回 True；
        PG 的 UPDATE 命中 0 行，同样按「调用成功即 True」返回（不改成报错，
        否则与 Redis 后端行为分叉）。上层只会对真实存在的碎片反馈。
        """
        delta = 1 if is_positive else -2
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE ks_fragment
                SET feedback_score = (COALESCE(NULLIF(feedback_score, ''), '0')::numeric
                                      + %s)::text
                WHERE key = %s
                """,
                (delta, fragment_key),
            )
        return True

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get_fragment(self, key: str) -> Optional[Dict[str, Any]]:
        """读单个碎片全字段；不存在返回 None。"""
        if not key:
            return None
        with self._ro() as cur:
            cur.execute(
                f"SELECT {', '.join(FRAGMENT_COLUMNS)} FROM ks_fragment WHERE key = %s",
                (key,),
            )
            row = cur.fetchone()
        return self._row_to_fragment(row) if row else None

    def get_fragments_batch(self, keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量读；缺失的 key 不出现在结果里。"""
        out: Dict[str, Dict[str, Any]] = {}
        if not keys:
            return out
        with self._ro() as cur:
            cur.execute(
                f"SELECT {', '.join(FRAGMENT_COLUMNS)} FROM ks_fragment WHERE key = ANY(%s)",
                (list(keys),),
            )
            for row in cur.fetchall():
                out[row[0]] = self._row_to_fragment(row)
        return out

    def entity_timeline(self, entity: str, limit: int = 20) -> List[Dict[str, Any]]:
        """按时间倒序返回某实体的记忆时间线。"""
        if not entity or not entity.strip():
            return []
        with self._ro() as cur:
            cur.execute(
                """
                SELECT f.content, f.created, f.valid_until
                FROM ks_entity_timeline t
                JOIN ks_fragment f ON f.key = t.frag_key
                WHERE t.entity = %s
                ORDER BY t.ts DESC
                LIMIT %s
                """,
                (entity.strip(), limit),
            )
            return [
                {
                    "content": content,
                    "created": created or None,
                    "valid_until": valid_until or None,
                }
                for content, created, valid_until in cur.fetchall()
            ]

    # ------------------------------------------------------------------
    # 加权信号
    # ------------------------------------------------------------------

    def match_attention(self, content: str, top_n: int = 10) -> float:
        """内容命中高注意力话题的加权值（公式与 attention.match_attention_boost 同）。"""
        if not content:
            return 1.0
        with self._ro() as cur:
            cur.execute(
                """
                SELECT topic, score FROM ks_attention
                WHERE scope = %s AND expire_ts > extract(epoch from now())
                ORDER BY score DESC LIMIT %s
                """,
                (_TOPIC_SCOPE_ALL, top_n),
            )
            raw = cur.fetchall()
        # 加权公式是后端无关的纯计算 → 与 Redis 侧共用 storage_shared 同一份
        return attention_boost_from_topics(raw, content, self._attention_boost_max)

    def match_hot_topics(self, text: str, limit: int = 10) -> float:
        """内容命中热词的衰减加权命中数（公式与 Redis match_hot_topics 同）。"""
        if not text:
            return 0.0
        with self._ro() as cur:
            cur.execute(
                """
                SELECT topic FROM ks_hot_topic
                WHERE scope = %s AND expire_ts > extract(epoch from now())
                ORDER BY score DESC LIMIT %s
                """,
                (_TOPIC_SCOPE_ALL, limit),
            )
            topics = [t for (t,) in cur.fetchall()]
            if not topics:
                return 0.0
            cur.execute(
                "SELECT topic, last_seen FROM ks_hot_topic_seen WHERE topic = ANY(%s)",
                (topics,),
            )
            last_seen = {t: float(ls) for t, ls in cur.fetchall()}

        # 衰减加权同样是纯计算 → 与 Redis 侧共用 storage_shared 同一份
        return hot_topic_weighted_hits(
            topics, last_seen, text,
            datetime.now(timezone.utc).timestamp(),
            decay_half_days=self._hot_topic_decay_half_days,
        )

    def get_hot_topics(
        self,
        limit: int = 10,
        period: str = "all",
    ) -> List[Dict[str, Any]]:
        """热门话题榜；period ∈ {all, daily, weekly}（未知值同 Redis 回落 all）。"""
        scope = period if period in _TOPIC_SCOPES else _TOPIC_SCOPE_ALL
        with self._ro() as cur:
            cur.execute(
                """
                SELECT topic, score FROM ks_hot_topic
                WHERE scope = %s AND expire_ts > extract(epoch from now())
                ORDER BY score DESC LIMIT %s
                """,
                (scope, limit),
            )
            return [{"topic": t, "count": round(float(s), 1)} for t, s in cur.fetchall()]