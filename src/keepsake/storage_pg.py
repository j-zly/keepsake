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
  查询：`to_tsquery('simple', 'a' | 'b' | ...)`，**召回**走 GIN 索引，
         **打分**在 Python 侧算真 BM25（k1=1.2 / b=0.75，对齐 RediSearch 默认）
         —— 候选集与改造前逐字相同（ts_rank_cd 只当召回窗口的粗排），
         见 `search_bm25` 的 docstring
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
import math
import random
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
TS_RANK_NORM = 32              # ts_rank_cd 归一化位：rank/(1+rank)（只用于召回窗口粗排）

# ---- BM25 参数（与 RediSearch 默认逐字一致）----
# 🔴 为什么不用 ts_rank_cd 当最终相关性分：ts_rank_cd 是**覆盖密度**，
#    输出离散且**大量并列** —— 归一化后 `_sim` 这一维失去分辨率，
#    几百条候选挤在同一分上，名次就被时间衰减/情绪/热词/注意力这些
#    非相关性维度决定了。实测同一查询下 top-50 候选里最大同分簇 50/50（全并列）。
BM25_K1 = 1.2                  # 词频饱和参数（RediSearch 默认 1.2）
BM25_B = 0.75                  # 文档长度归一化（RediSearch 默认 0.75）

# ---- 检索热路径的进程级 TTL 快照（PG 侧，2026-10 ks_pg_perf）----
#
# 🔴 为什么需要（实测取证见 /tmp/ks_pgperf_rootcause.txt）：
#   PG 后端一次 search() 里，**每条候选**的重排都会各发 1 次
#   `match_hot_topics` + 1 次 `match_attention`（storage_shared.rerank_with_decay
#   是共用代码，两个后端同一份函数对象，不在这里动）；加上 BM25 的语料统计聚合
#   （N/avgdl/df 全表 unnest）与每次检索重查同义词表 ⇒ 一次检索几十上百条 SQL。
#   这些查询**每次返回的行完全一样**（只随时间过期），所以缓存「行」而不是结果值。
#
# 与 Redis 侧 `_FRAG_CACHE` 的 TTL 快照同源做法；默认 60s，
# 构造参数 `snapshot_ttl_s` 可调，**设成 0 即彻底关掉快照**（回到每次实查）。
DEFAULT_SNAPSHOT_TTL_S = 60.0

#: 进程级快照 `{目标库指纹: {(group, key): (单调时刻, 值)}}`。
#: 按库指纹分桶 —— 同进程里连多个库不会串味。
#: 单进程单线程检索热路径，**不加锁**（写入都在各自的 GIL 原子步内，
#: 丢一次快照只是下条语句多查一次库，不会读到半个对象）。
_SNAPSHOTS: Dict[str, Dict[tuple, tuple]] = {}

#: KNN 走 HNSW 时内层**多取几倍**再过活记忆过滤。
#: 改写前是「先过滤后排序取 top-N」，过滤掉多少条就少返回多少条；
#: 改写后（HNSW 只能先给有序候选、过滤在索引扫描之后）必须多取一些，
#: 否则「最近的 N 条里有一半是已失效碎片」会把结果集打空。
#: ponytail: 固定倍数、不做自适应回填。失效/超长碎片占比 >25% 时召回会掉，
#: 升级路径：按外层存活数不足再补一轮索引查询（代价是一次额外往返）。
KNN_OVERFETCH = 4

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
# BM25 打分（纯计算，零新扩展）
# =============================================================================

def _parse_tfs(raw: Any) -> Dict[str, float]:
    """把 SQL `array_agg(lexeme || ':' || tf)` 解成 `{lexeme: tf}`。

    lexeme 里理论上不会出现 `:`（`to_tsvector('simple', ...)` 的词元不含冒号），
    所以用 `rpartition` 从**右边**切，词元本身含分隔符也不会切错。
    驱动/适配器把 text[] 退回字符串时（`{a:1,b:2}`）也照样能解。
    """
    out: Dict[str, float] = {}
    if not raw:
        return out
    if isinstance(raw, str):
        items: List[str] = [s for s in raw.strip("{}").split(",") if s]
    else:
        items = [str(s) for s in raw]
    for item in items:
        lexeme, sep, tf = item.rpartition(":")
        if not sep or not lexeme:
            continue
        try:
            out[lexeme] = float(tf)
        except (TypeError, ValueError):
            continue
    return out


def bm25_score(
    tfs: Dict[str, float],
    doclen: float,
    n_docs: float,
    avgdl: float,
    dfs: Dict[str, float],
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> float:
    """单篇文档的 BM25 分 —— RediSearch 的公式，零新扩展。

        idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
        score  = Σ_t idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl/avgdl))

    参数:
        tfs:    {查询词: 该文档内的词频}（来自 content_tsv 的 positions 数组长度）
        doclen: 该文档的词元总数（**同一口径**：content_tsv 里 positions 的总长度，
                就是入库时 jieba 分词后的词数，不另算、不重切）
        n_docs: 语料文档数 N；avgdl: 平均长度
        dfs:    {查询词: 语料里的文档频率}

    `idf` 用 RediSearch 的 `ln(1 + ...)` 变体（不是 Lucene 的 `ln((N-df+0.5)/(df+0.5))`）：
    短查询里常见的高 df 词在 Lucene 变体下 idf 会变成负数、把**长文档**顶上来。
    """
    if not tfs or n_docs <= 0 or avgdl <= 0:
        return 0.0
    denom_base = k1 * (1.0 - b + b * (float(doclen) / avgdl))
    total = 0.0
    for term, tf in tfs.items():
        tf = float(tf)
        if tf <= 0:
            continue
        df = float(dfs.get(term, 0.0))
        if df <= 0:
            continue
        idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
        total += idf * (tf * (k1 + 1.0)) / (tf + denom_base)
    return total


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

#: 🔴 单条语句**允许携带的绑定参数数上限**（`_insert_many` 的分块依据）。
#:
#: PG 扩展查询协议里参数个数是 Int16，协议上限 65535；psycopg3 在**发出之前**
#: 校验，超了就抛 `number of parameters must be between 0 and 65535` ——
#: 客户端就炸，一个字都没进库。
#:
#: 取 64000 而不是贴着 65535：留一点余量，免得某个驱动/封装层在参数之外再加占位。
#: 这个数只影响「一条语句带多少行」，不影响正确性 —— 分块后行序仍然全局有序，
#: 幂等语义也由 ON CONFLICT 保证（见 `_insert_many` 的 docstring）。
SAFE_PARAM_CAP = 64000


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


class _BytesFieldError(TypeError):
    """文本字段还是 bytes 就被当正文用 —— 早失败，且说清怎么改。

    🔴 为什么值得单独一个类型：这类错误的真实触发路径是「调用方用
    `decode_responses=False` 连 Redis，把 hash 值原样喂进来」。症状原本是
    `json.dumps` 抛 `Object of type bytes is not JSON serializable` —— 那条栈
    指向 embedder，离病因（没解码）十万八千里，排查成本极高。这里在**入口**
    就拦住，并直接告诉调用方字段名与解码办法。
    继承 TypeError：与它取代的那个 json 报错同类，调用方原有的
    `except TypeError` 仍能兜住。
    """


def _text_field(v: Any, field: str) -> str:
    """文本字段取值 —— 仍是 bytes 就抛**可操作**的错，而不是留到 json.dumps 炸。

    解码责任在**调用方**（迁移脚本的字段分类表，见
    `scripts/migrate_redis_to_pg.py::TEXT_FIELDS`）：本函数不做兜底解码，
    因为「静默按 UTF-8 replace 解码」会把真·二进制字段（向量 blob）毁成乱码，
    那比报错更糟。
    """
    if isinstance(v, (bytes, bytearray)):
        raise _BytesFieldError(
            f"storage_pg: 字段 {field!r} 是 {type(v).__name__}（未解码），不能当正文用。"
            f"请在进 upsert_fragment() 之前按 UTF-8 解码，例如 "
            f"_text_field/v.decode('utf-8')；只有向量 blob 字段（embed_bin）"
            f"应保持 bytes 原样传递。"
        )
    return _as_text(v)


def _blob_to_vector(blob: bytes, field: str = "embed_bin") -> List[float]:
    """Redis 的 float32 向量 blob（`struct.pack(f'{n}f', *vec)`）→ float 列表。

    🔴 为什么是「搬运」而不是重算：对照评测要求 Redis 侧与 PG 侧**同源向量**。
    重算会引入与后端无关的差异（模型版本漂移、批量归一化差异），让评测结论
    失真。所以 Redis 里已有的向量一律原样搬，只有确实没有向量时才调 embedder。

    小端 float32：与 `storage.RedisStorage._text_to_blob` 写入时的
    `struct.pack(f'{n}f', ...)` 在 x86/ARM 上同为小端，且与 RediSearch
    `TYPE FLOAT32` 的线格式一致。
    """
    if not isinstance(blob, (bytes, bytearray)):
        raise TypeError(
            f"storage_pg: {field!r} 应为 float32 二进制 blob，实际是 "
            f"{type(blob).__name__}。无法搬运 —— 请检查上游写库是否用了 struct.pack。"
        )
    n = len(blob) // 4
    if len(blob) % 4:
        raise _SchemaDimMismatch(
            f"storage_pg: {field!r} 长度 {len(blob)} 不是 4 的倍数，不是合法的 "
            f"float32 向量 blob。拒绝搬运（重算会掩盖数据损坏）。"
        )
    return list(struct.unpack(f"<{n}f", bytes(blob)))


# 仍未实现的方法（语料维护类，与检索正交）→ 统一文案
_NOT_IMPLEMENTED = (
    "PgStorage.{name}() is not implemented yet — corpus maintenance is out of "
    "scope for batch 2 (which delivered read/write/search). "
    "Raising on purpose: returning a zero-statistics dict here would silently "
    "make every memory unsearchable when backend=postgres is selected."
)


class _PrecomputedWeights:
    """给共用的 `rerank_with_decay` 用的权重代理。

    薄候选的 `content` 是 md5 摘要，不是正文，所以热词/注意力命中数没法在
    Python 侧重算（那是 `storage_shared` 里的纯计算）—— 改成在候选 SQL 里
    一次算完，这里只按摘要查表回给共用公式。
    其余属性（衰减半衰期 / 情绪 / 反馈 / 热门权重配置）全部透传给真实实例，
    共用公式一行没改。查不到摘要时回落到真实查询（防御性，正常路径不会走到）。
    """

    __slots__ = ("_base", "_pre")

    def __init__(self, base: Any, pre: Dict[str, tuple]):
        self._base = base
        self._pre = pre

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def match_hot_topics(self, text: str, limit: int = 10) -> float:
        hit = self._pre.get(text)
        return float(hit[0]) if hit else self._base.match_hot_topics(text, limit=limit)

    def match_attention(self, content: str, top_n: int = 10) -> float:
        hit = self._pre.get(content)
        return float(hit[1]) if hit else self._base.match_attention(content, top_n=top_n)


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

    def _rerank_with_pre(self, fragments, score_key="_bm25_score", is_knn=False,
                         pre_weights=None):
        """共用的 rerank_with_decay + 薄候选入口。

        `pre_weights` 是候选 SQL 侧算好的 `{md5(content): (hot_w, attn_w)}`
        —— 正文没取回来，热词/注意力加权就只能在 SQL 里先算完（公式与共用的
        `hot_topic_weighted_hits` / `attention_boost_from_topics` 逐条对齐，
        包括「没命中就算命中集为空」「total<=0 时不加权」这些边角）。
        公式本身仍在 storage_shared 那一份里跑，PG 侧只换「命中数从哪来」。
        没有 `pre_weights`（完整候选）时行为与改造前逐字相同。

        🔴 收尾多一步 `_tiebreak`：共用的 `rerank_with_decay` 末尾只按
        `_combined_score` 降序排，Python 的 `list.sort` 稳定 ⇒ **同分行之间
        保留输入次序**，而输入次序来自候选 SQL 的 `ORDER BY … , key`/HNSW 返回序，
        跨次运行不保证一致 → 同分行的相对名次会飘。这里按 key 兜死（见 `_tiebreak`）。
        """
        if not fragments or not pre_weights:
            out = rerank_with_decay(self, fragments, score_key=score_key, is_knn=is_knn)
        else:
            out = rerank_with_decay(_PrecomputedWeights(self, pre_weights), fragments,
                                    score_key=score_key, is_knn=is_knn)
        return self._tiebreak(out)

    @staticmethod
    def _tiebreak(fragments: List[Dict[str, Any]],
                  score_key: str = "_combined_score") -> List[Dict[str, Any]]:
        """**并列 tiebreaker**：同分时按 `key` 字典序（升序）定序。

        🔴 为什么需要：共用实现里的排序都只用一个分字段，而 PG 的分是
        `float` 连乘（sim × decay × emotion × feedback × hot × attention），
        **同分极其常见**（尤其 BM25 归一化后大量候选挤在同一个值上）。
        `list.sort` 稳定 ⇒ 同分行沿用输入次序，而输入次序由候选 SQL 的
        `ORDER BY score DESC, key` 与 HNSW 的返回序决定 —— HNSW 是近似检索，
        同一查询两次运行返回的候选集/次序可以不同，于是同分行的名次逐次不同，
        「同一题连查多次结果不一致」的抖动就是这么来的。

        规则：**只改同分项之间的相对次序**，不改任何一行的分数、不改名次分档、
        不改条数（`final_limit` 仍按原样截断）。Redis 侧零改动 ——
        本函数只在 PgStorage 内调用。

        无 `_key` 的行排在最后（`or ""` 兜底），不与有 key 的行抢位置。
        """
        if len(fragments) < 2:
            return fragments
        return sorted(fragments,
                      key=lambda f: (-float(f.get(score_key, 0.0) or 0.0),
                                     f.get("_key") or ""))

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
        snapshot_ttl_s: float = DEFAULT_SNAPSHOT_TTL_S,
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
        # 检索热路径快照 TTL（0 = 关掉，每次实查；负数同 0）
        self._snapshot_ttl_s = max(0.0, float(snapshot_ttl_s))
        self._agent_id = agent_id
        self._is_primary = bool(is_primary)
        self._attention_boost_max = float(attention_boost_max)
        self._attention_base_increment = float(attention_base_increment)
        self._attention_emotion_factor = float(attention_emotion_factor)
        self._hot_topic_decay_half_days = int(hot_topic_decay_half_days)
        self._conn: Optional[Any] = None
        # ---- 并行检索：每线程一条只读连接（psycopg Connection 非线程安全）----
        self._tls = threading.local()
        self._conn_lock = threading.Lock()
        self._worker_conns: List[Any] = []
        self._pool: Optional[ThreadPoolExecutor] = None

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

    def _open_conn(self) -> Any:
        """建一条**新**连接（不碰 `self._conn`）—— 并行检索的工作线程各调一次。"""
        import psycopg  # noqa: PLC0415 — optional 依赖，延迟到真要用 PG 时

        conninfo = self._conninfo()
        try:
            conn = psycopg.connect(conninfo, connect_timeout=self._connect_timeout)
        except Exception as e:
            # 只出 host，不出 conninfo（可能含口令）
            logger.error("storage_pg: connect to %s:%s failed: %s", self._host, self._port, e)
            raise
        logger.info("storage_pg: connected to %s:%s/%s", self._host, self._port, self._dbname)
        return conn

    def _conninfo(self) -> str:
        if self._dsn:
            return self._dsn
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
        return " ".join(parts)

    def _connect(self) -> Any:
        """惰性建连。psycopg 在这里才 import —— 没装也不影响 import 本模块。"""
        if self._conn is not None:
            return self._conn
        self._conn = self._open_conn()
        return self._conn

    def _drop_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # ---- 并行检索用的「每线程一条连接」 ----
    #
    # 🔴 为什么不能共用 `self._conn`：psycopg 的 Connection **不是线程安全的** ——
    #    两个线程各开一个 cursor 在同一条连接上交替 execute，结果集会互相踩
    #    （psycopg 用一把锁把 execute 串起来，但 fetchall 读到的是**对方**的
    #    结果集）。所以 BM25 与 KNN 并行时必须各持一条连接。
    #
    # ponytail: 线程池固定 2 个 worker ⇒ 连接数上界 = 3（主线程 + 2 worker），
    # 不做通用连接池。升级路径：真要放开并发度再换 psycopg_pool。
    def _worker_conn(self) -> Any:
        """取本线程的只读连接（首次调用时建，连到 `self._drop_conn` 之后重建）。"""
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            return conn
        conn = self._open_conn()
        self._tls.conn = conn
        with self._conn_lock:
            self._worker_conns.append(conn)
        return conn

    def _drop_worker_conn(self) -> None:
        """本线程连接出错时丢弃并清空（与 `_drop_conn` 对称）。"""
        conn, self._tls.conn = getattr(self._tls, "conn", None), None
        if conn is None:
            return
        with self._conn_lock:
            if conn in self._worker_conns:
                self._worker_conns.remove(conn)
        try:
            conn.close()
        except Exception:
            pass

    @contextmanager
    def _ro(self) -> Iterator[Any]:
        """只读游标。读完显式 commit 关掉 psycopg 的隐式事务。

        为什么要关：psycopg 默认非 autocommit，任何 SELECT 都会开一个隐式事务并
        一直挂着（既留脏状态，也让后续 `_tx()` 只拿到 SAVEPOINT 而不提交）。
        """
        # 并行检索的工作线程走**自己那条**连接，绝不与主线程共用 cursor。
        if getattr(self._tls, "in_worker", False):
            conn = self._worker_conn()
            try:
                with conn.cursor() as cur:
                    yield cur
                conn.commit()
            except Exception:
                self._drop_worker_conn()
                raise
            return
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            self._drop_conn()
            raise

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
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True)
        with self._conn_lock:
            conns, self._worker_conns = self._worker_conns, []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
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

    # ---- 进程级 TTL 快照（检索热路径，与 Redis 侧热词榜快照同源）----

    def _snap_get(self, group: str, key: tuple) -> Any:
        """取快照；未命中 / 过期 / TTL<=0 一律返回 None（后者 = 彻底关掉快照）。"""
        if self._snapshot_ttl_s <= 0:
            return None
        entry = _SNAPSHOTS.get(self._target_key(), {}).get((group, key))
        if entry is None:
            return None
        ts, value = entry
        return value if (time.monotonic() - ts) <= self._snapshot_ttl_s else None

    def _snap_put(self, group: str, key: tuple, value: Any) -> None:
        if self._snapshot_ttl_s <= 0:
            return
        _SNAPSHOTS.setdefault(self._target_key(), {})[(group, key)] = (time.monotonic(), value)

    def _snap_drop(self, *groups: str) -> None:
        """让快照立即失效 —— **写路径必须调**，否则新写入要等一个 TTL 才可见。"""
        snap = _SNAPSHOTS.get(self._target_key())
        if not snap:
            return
        for g in groups:
            for k in [k for k in snap if k[0] == g]:
                snap.pop(k, None)

    #: 写 ks_fragment / 加权信号表的**唯一出口**清单 —— 每个写入口都调一次
    #: `_snap_drop(*_SNAPSHOT_GROUPS)`，漏一个入口 = 新写入最长陈旧一个 TTL。
    _SNAPSHOT_GROUPS = ("n_avgdl", "dfs", "hot", "attn", "syn")

    def _load_synonym_map(self) -> Dict[str, set]:
        """同义词表（对齐 Redis 的 keepsake:synonyms hash）。

        批 2 的 BM25 查询式构造复用 Redis 侧的 `_expand_terms`，词表必须同源，
        否则「同义词扩展」这条召回面两边不一致，对照评测直接失真。

        🔴 走快照：这张表几乎不变，但每次检索都要查 = 每次检索多一次往返。
        """
        out: Dict[str, set] = {}
        cached = self._snap_get("syn", ())
        if cached is not None:
            return cached
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
        self._snap_put("syn", (), out)
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

    # 🔴 **候选阶段只回「极轻列 + md5(content)」**（2026-10 ks_pgr 收尾）。
    #    原来候选 SQL 直接 SELECT 全部 SEARCH_FIELDS（content/tags/entities…），
    #    于是**候选集大小 × 正文体积**的字节全压在客户端：生产实测一次检索
    #    取回 1.2MB，而 SQL 只跑了 0.46s —— 端到端 1.2~3.4s 的差距全在这。
    #    现在融合/过滤/去重/重排都在 key 级别做（正文对最终 top-N **只取一次**），
    #    所以候选行只需要：
    #      * key        —— 身份（最终取正文的锚点）
    #      * md5(content) 前 16 位 —— RRF 按 content 去重的**代理身份**（16 字节）。
    #        截 16 位够用：这个摘要**只在同一次检索的候选集内**（≤200 行）两两比较，
    #        64 位碰撞概率 ~1e-15。
    #        必须按 content 去重而不是按 key：key 是 sha256(content)[:12] 加版本
    #        尾巴，同一份正文在不同版本下是两个 key，按 key 去重会漏合并。
    #      * created / sentiment_score / feedback_score —— rerank_with_decay 直接读
    #      * 两个布尔 —— rerank 只判 `tags` 里有没有 corrected、_apply_v2_filters
    #        只判 fragment_type == 'consumed'，那就只把「判据」取回来，别取原列
    #    正文/entities/category/source/invalid_at 全部留到 _hydrate_thin() 取。
    #: 「这段正文命中了哪些话题词、各值多少权重」在 SQL 侧算完，只回两个 float。
    #: topics/last_seen 与共用的 match_hot_topics / match_attention 同源同快照
    #: （都是 ≤10 个词），所以公式、顺序、判据逐条一致，只是把 strpos 挪到 PG 里做。
    #:
    #: 写法上必须是 **一次 LEFT JOIN + 聚合**，不能是每行一个相关子查询 ——
    #: 相关子查询要为每条候选各起一次子计划（改前实测 148 行候选就把这一段
    #: 拖到 ~0.7s，比整条检索其余部分加起来还多）。一次 JOIN 走的是
    #: 「候选行 × ≤10 个词」的嵌套循环，代价与候选数线性。
    _TOPIC_HITS_JOIN = (
        "LEFT JOIN unnest(%s::text[], %s::float[], %s::int[]) AS w(tw, wt, kind) "
        "  ON length(w.tw) >= 2 AND strpos({c}, w.tw) > 0"
    )
    _THIN_GROUP_BY_BM25 = (
        "GROUP BY cand.key, cand.chash, cand.created, cand.sentiment_score, "
        "cand.feedback_score, cand.corrected, cand.consumed, "
        "c.doclen, c.tfs, cand.score"
    )
    _THIN_GROUP_BY_KNN = (
        "GROUP BY key, chash, created, sentiment_score, feedback_score, "
        "corrected, consumed, d"
    )
    #: 热词(kind=0) 与注意力(kind=1) 的命中权重和，各自成一个 float。
    _TOPIC_HITS_SELECT = (
        "COALESCE(sum(w.wt) FILTER (WHERE w.kind = 0), 0)::float, "
        "COALESCE(sum(w.wt) FILTER (WHERE w.kind = 1), 0)::float"
    )

    def _rows_to_thin(
        self,
        rows: List[tuple],
        score_key: str,
        default_score: float,
        pre_weights: Dict[str, tuple],
        attn_total: float,
    ) -> List[Dict[str, Any]]:
        """候选行 → 轻量 dict（**不含正文**）。

        `content` 字段放的是 md5 摘要，不是正文：它只被两处用到 ——
        RRF 的去重键、以及热词/注意力加权的查表键（`_PrecomputedWeights`）。
        真正的正文在 `_hydrate_thin()` 里按 key 一次性取回后替换。
        """
        out: List[Dict[str, Any]] = []
        for row in rows:
            key, chash, created, sent, fb, corrected, consumed, hot_w, attn_w = row[:9]
            frag: Dict[str, Any] = {"content": chash}
            if created:
                frag["created"] = created
            if sent:
                frag["sentiment_score"] = sent if isinstance(sent, str) else str(sent)
            if fb:
                frag["feedback_score"] = fb if isinstance(fb, str) else str(fb)
            if corrected:
                frag["tags"] = "corrected"
            if consumed:
                frag["fragment_type"] = "consumed"
            frag["_key"] = key
            frag[score_key] = float(row[-1]) if row[-1] is not None else default_score
            frag["_chash"] = chash
            # attn_w 按共用的 attention_boost_from_topics 口径换算：
            # ratio = min(命中分数和 / 全部分数和, 1.0)，全部为 0 时不加权。
            ratio = min(float(attn_w) / attn_total, 1.0) if attn_total > 0 else 0.0
            pre_weights[chash] = (float(hot_w),
                                  1.0 + (self._attention_boost_max - 1.0) * ratio)
            out.append(frag)
        return out

    def search_bm25(
        self,
        query: str,
        tag_filter: str = "",
        agent_id: str = "",
        is_primary: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """BM25 全文搜索（对外入口：内部薄候选 + 一次取回正文，形状逐字不变）。"""
        return self._hydrate_thin(self._bm25_thin(query, tag_filter, agent_id, is_primary))

    def _bm25_thin(
        self,
        query: str,
        tag_filter: str = "",
        agent_id: str = "",
        is_primary: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """BM25 全文搜索（jieba 分词 → tsquery 召回 → **Python 侧真 BM25 打分**）。

        流程与 Redis 侧 search_bm25 同构：
          1. 分词（segment_query）→ 同义词扩展 → `_sanitize_terms`（含路径/连字符拆子词）
          2. tsquery OR 召回（GIN）；**打分**在 Python 侧算 BM25
          3. 共用的 rerank_with_decay 重排 → 取 final_limit
        空查询 → 空列表并记 WARNING（明确、不静默）。

        🔴 **只换打分，不换召回**：召回条件与候选窗口（`content_tsv @@ tsquery`
        + `ORDER BY ts_rank_cd LIMIT bm25_limit`）与改造前逐字相同 ——
        ts_rank_cd 退化成「召回窗口的粗排」，不再当相关性分输出。
        原来拿它当最终分的问题是**离散 + 大量并列**（覆盖密度不区分词频与长度），
        归一化后 `_sim` 失去分辨率 ⇒ 名次被非相关性维度决定。

        🔴 **不装任何新扩展**：BM25 要的语料统计全部用现有 tsvector 自带的
        `unnest` + `cardinality(positions)` 在一条聚合 SQL 里取齐
        （tf / doclen / df / N / avgdl），零新依赖、零新 DDL。
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
        # `to_tsvector('simple', ...)` 会把词元**小写化**，所以 tf / df 两处
        # 都拿小写词元比对（不清就查不到 tf，BM25 会整批算成 0）。
        lexemes = [t.lower() for t in terms]
        where_sql, params = self._search_filter_sql(tag_filter, agent_id, is_primary)

        # 2. 召回 + 取打分素材。一条 SQL 同时拿：
        #    doclen = 该文档全部词元数（= 入库 jieba 分词后的词数，与分词口径一致）
        #    tfs    = 仅查询词的 tf（FILTER，词元少时不必把整篇倒回来）
        #    score  = ts_rank_cd，只用来给候选窗口粗排（取 bm25_limit 条）
        #
        # 🔴 **LATERAL 必须挂在 LIMIT 之上**（2026-10 ks_pgq 实测修掉的 147ms 主热点）：
        #   旧写法把 LATERAL 直接挂在 `ks_fragment f` 上，于是它在 **Sort 之下**，
        #   对**每一条 GIN 召回命中的行**都要 unnest 一遍 content_tsv
        #   （实测 rows=1013、loops=1013、每篇 15 个词元），
        #   而真正只要 top-bm25_limit 条。EXPLAIN 实锤：
        #       Limit  (actual time=147.464..147.469 rows=20)
        #         Sort  (Sort Key: ts_rank_cd(...))          <- 0ms
        #           Nested Loop Left Join (rows=1013.00)   <- 146ms  ← 全在这里
        #             Bitmap Heap Scan (rows=1013.00)       <- 3.3ms
        #   改法：先用 CTE（`MATERIALIZED`，显式挡住 pullup）选出 top-N 候选，
        #   再对**这 N 条**算 doclen/tfs。`doclen`/`tfs` 只依赖该行自己的
        #   content_tsv，无跨行依赖 ⇒ **候选集、排序、返回列序逐字不变**，
        #   排序公式/权重/过滤语义/候选集大小一律没动。
        outer_fields = ("cand.key, cand.chash, cand.created, cand.sentiment_score, "
                        "cand.feedback_score, cand.corrected, cand.consumed, ")
        topic_t, topic_w, topic_k, attn_total = self._topic_weight_args()
        # 候选 CTE 里顺手把 lower(content) 算好：话题命中在 PG 里做，正文不外传
        sql = (
            "WITH cand AS MATERIALIZED ("
            "  SELECT f.key, left(md5(f.content), 16) AS chash, f.created, f.sentiment_score, "
            "         f.feedback_score, CASE WHEN strpos(f.tags, 'corrected') > 0 THEN 1 ELSE 0 END AS corrected, "
            "         CASE WHEN f.fragment_type = 'consumed' THEN 1 ELSE 0 END AS consumed, "
            "         lower(f.content) AS clower, f.content_tsv AS tsv, "
            f"  ts_rank_cd(f.content_tsv, to_tsquery('simple', %s), {TS_RANK_NORM}) AS score "
            "  FROM ks_fragment f "
            "  WHERE f.content_tsv @@ to_tsquery('simple', %s) "
            f"    AND {where_sql} "
            "  ORDER BY score DESC, f.key LIMIT %s"
            ") "
            f"SELECT {outer_fields}"
            + self._TOPIC_HITS_SELECT + ", "
            + "c.doclen, COALESCE(c.tfs, '{}') AS tfs, cand.score "
            "FROM cand "
            "LEFT JOIN LATERAL ("
            "  SELECT COALESCE(sum(cardinality(t.positions)), 0)::float AS doclen, "
            "         array_agg(t.lexeme || ':' || cardinality(t.positions)::text) "
            "           FILTER (WHERE t.lexeme = ANY(%s)) AS tfs "
            "  FROM unnest(cand.tsv) t"
            ") c ON TRUE "
            + self._TOPIC_HITS_JOIN.format(c="cand.clower") + " "
            + self._THIN_GROUP_BY_BM25 + " "
            "ORDER BY cand.score DESC, cand.key"
        )
        # 语料统计：**一条** SQL 拿齐 N / 总长 / avgdl / 每个查询词的 df。
        # 🔴 绝不为每篇文档单独查一次 df（那是 N+1 往返）；df 用一条
        # `GROUP BY lexeme` 的聚合一次算完，词数再多也只有一趟。
        # 🔴 这条 SQL 里 `corpus` 那半边是**全表 unnest 扫描**（O(语料)），
        #    而 N / avgdl / df 只随「语料变化 + 过滤条件」变，与查询文本无关
        #    （df 另加词元集合）⇒ 走进程级 TTL 快照，命中就不发这条 SQL。
        #    写路径（store/upsert/supersede/correct/import）一律 `_snap_drop`，
        #    新写入立刻可见，不存在「陈旧到影响断言」的窗口。
        # 语料统计：**一条** SQL 拿齐 N / 总长 / avgdl / 每个查询词的 df。
        # 🔴 绝不为每篇文档单独查一次 df（那是 N+1 往返）；df 用一条
        # `GROUP BY lexeme` 的聚合一次算完，词数再多也只有一趟。
        # 🔴 `corpus` 那半边是**全表 unnest 扫描**（O(语料)），而 N / avgdl 只随
        #    「语料变化 + 过滤条件」变、与查询文本无关 ⇒ 走进程级 TTL 快照。
        #
        # 🔴 快照是**两个独立分组**（n_avgdl / dfs），因为生产流量里
        #    「不带 tag/agent 过滤的普通查询」每题词集都不同 ⇒ `dfs` 每次必然 miss，
        #    而 `n_avgdl`（键里没有查询词）几乎次次命中。于是这里按「缺哪半发哪半」
        #    拼 SQL：命中 n_avgdl 时**不再计算 corpus CTE**，否则等于每题白扫一遍
        #    全表（实测 corpus CTE 40ms / 合计 51ms，而只要 df 时只要 11ms）。
        _corpus_cte = (
            "WITH corpus AS ("
            "  SELECT (SELECT COALESCE(sum(cardinality(t.positions)), 0) "
            "          FROM unnest(f.content_tsv) t) AS doclen "
            "  FROM ks_fragment f "
            "  WHERE f.content_tsv IS NOT NULL AND " + where_sql + ")"
        )
        _dfs_cte = (
            "dfs AS ("
            "  SELECT t.lexeme, count(*)::float AS df "
            "  FROM ks_fragment f, unnest(f.content_tsv) t "
            "  WHERE t.lexeme = ANY(%s) "
            "    AND f.content_tsv @@ to_tsquery('simple', %s) "
            "    AND " + where_sql + " "
            "  GROUP BY t.lexeme"
            ")"
        )
        _df_json = "COALESCE((SELECT json_object_agg(lexeme, df) FROM dfs), '{}')"
        _n_json = ("(SELECT count(*) FROM corpus)::float, "
                   "(SELECT COALESCE(sum(doclen), 0)::float FROM corpus)")
        stat_sqls = {
            # 两半都要：一条 SQL 出齐（首次查询 / 刚被写路径失效）
            "both": (_corpus_cte + ", " + _dfs_cte + " SELECT " + _n_json + ", " + _df_json,
                     [*params, lexemes, tsquery, *params]),
            # 只缺 df（生产常态）
            "df": ("WITH " + _dfs_cte + " SELECT " + _df_json, [lexemes, tsquery, *params]),
            # 只缺 N/avgdl（不常发生：n_key 不含查询词）
            "n": (_corpus_cte + " SELECT " + _n_json, [*params]),
        }

        # 快照键：**逐项**包含影响结果的参数。
        #   n_avgdl ← 过滤 SQL + 过滤参数（不含查询词：N/avgdl 与查询词无关）
        #   dfs     ← 过滤 SQL + 过滤参数 + 查询词集合（df 只认 lexeme 集合，
        #             故按排序后的集合做键，同词集换顺序仍命中同一条快照）
        n_key = (where_sql, tuple(params))
        d_key = n_key + (tuple(sorted(lexemes)),)
        n_avgdl = self._snap_get("n_avgdl", n_key)
        df_cached = self._snap_get("dfs", d_key)

        need_n = n_avgdl is None
        need_df = df_cached is None

        with self._ro() as cur:
            cur.execute(sql, [tsquery, tsquery, *params, self._bm25_limit, lexemes,
                              topic_t, topic_w, topic_k])
            rows = cur.fetchall()
            if need_n or need_df:
                which = "both" if (need_n and need_df) else ("df" if need_df else "n")
                stat_sql, stat_params = stat_sqls[which]
                cur.execute(stat_sql, stat_params)
                if need_n and need_df:
                    n_docs, total_len, df_raw = cur.fetchone()
                    n_avgdl = (float(n_docs or 0.0), float(total_len or 0.0))
                elif need_df:
                    (df_raw,) = cur.fetchone()
                else:
                    n_docs, total_len = cur.fetchone()
                    n_avgdl = (float(n_docs or 0.0), float(total_len or 0.0))
                if need_df:
                    df_cached = {str(k).lower(): float(v) for k, v in dict(df_raw or {}).items()}
                    self._snap_put("dfs", d_key, df_cached)
                if need_n:
                    self._snap_put("n_avgdl", n_key, n_avgdl)

        # 3. Python 侧 BM25（候选集不变，只把 ts_rank_cd 换成 BM25 分）
        dfs = df_cached
        n_docs, total_len = n_avgdl
        avgdl = (float(total_len or 0.0) / n_docs) if n_docs > 0 else 0.0
        scored = []
        for row in rows:
            # 列序：… SELECT_FIELDS, doclen, tfs, ts_rank_cd（row[-1] 只是粗排分）
            score = bm25_score(_parse_tfs(row[-2]), float(row[-3] or 0.0),
                               n_docs, avgdl, dfs)
            scored.append(tuple(row[:-1]) + (score,))

        pre_weights: Dict[str, tuple] = {}
        fragments = self._rows_to_thin(scored, "_bm25_score", 0.0,
                                       pre_weights, attn_total)
        fragments = self._rerank_with_pre(fragments, score_key="_bm25_score",
                                          pre_weights=pre_weights)
        # 🔴 tiebreak 在截断之前（理由同 `_knn_thin`）。
        return self._tiebreak(fragments)[: self._final_limit]

    def _text_to_vector(self, text: str) -> Optional[List[float]]:
        """文本 → float 向量（无 embedder 或取不到返回 None）。"""
        # 🔴 bytes 早失败：`get_embedding()` 会把它塞进 `json.dumps` 的 payload，
        # 报出来的错指向 embedder 而非真正的病因（上游漏了解码），排查成本极高。
        if isinstance(text, (bytes, bytearray)):
            raise _BytesFieldError(
                "storage_pg: _text_to_vector() 收到 bytes 类型的文本 "
                f"（{len(text)} 字节）。请先按 UTF-8 解码成 str 再传入，"
                "例如 value.decode('utf-8')。"
            )
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
        """KNN 向量搜索（对外入口：内部薄候选 + 一次取回正文）。"""
        return self._hydrate_thin(self._knn_thin(query, tag_filter, agent_id, is_primary))

    def _knn_thin(
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
        ORDER BY 写成 `f.embedding <=> 常量` —— pgvector 正是这个形状才走 HNSW；
        但**过滤条件必须留在索引扫描之外**（见下面 fetch_n 处的注释），否则规划器
        会因过滤后的行数估偏而放弃索引、改走全表顺序扫。
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
        # 🔴 **内层子查询不带任何过滤**，「活记忆」过滤放在外层 WHERE：
        #   带过滤时规划器把过滤后的行数估成 rows=1218/3655，HNSW 索引路径的
        #   总代价被抬到 ≥ Seq Scan，于是**整条 KNN 退化成全表顺序扫**
        #   （EXPLAIN 证据：见 /tmp/ks_pgperf_rootcause.txt，
        #     Seq Scan 56ms/22171 buffers vs Index Scan 5ms/1229 buffers）。
        #   把过滤挪到外层后，内层只剩 `ORDER BY embedding <=> $q LIMIT n`，
        #   规划器必然选 HNSW；子查询带 LIMIT ⇒ PG 不做 subquery pullup，
        #   过滤不会被下推回索引扫描，语义与原写法逐条对齐。
        #   代价：HNSW 是「先给有序候选、过滤在扫描之后」，所以内层按
        #   KNN_OVERFETCH 多取几倍再由外层 LIMIT 收口，避免失效碎片把结果打空。
        fetch_n = max(self._candidate_count, self._candidate_count * KNN_OVERFETCH)
        topic_t, topic_w, topic_k, attn_total = self._topic_weight_args()
        # 同 BM25 侧：先把过完滤的候选（含 lower(content)）物化成 CTE，
        # 话题命中才在它上面算 —— 否则每个候选要 lower 正文 10 次。
        sql = (
            "WITH cand AS MATERIALIZED ("
            "  SELECT f.key, v.d, left(md5(f.content), 16) AS chash, lower(f.content) AS clower, "
            "         f.created, f.sentiment_score, f.feedback_score, "
            "         CASE WHEN strpos(f.tags, 'corrected') > 0 THEN 1 ELSE 0 END AS corrected, "
            "         CASE WHEN f.fragment_type = 'consumed' THEN 1 ELSE 0 END AS consumed "
            "  FROM ("
            "    SELECT f2.key AS key, (f2.embedding <=> %s::vector) AS d "
            "    FROM ks_fragment f2 "
            "    WHERE f2.embedding IS NOT NULL "
            "    ORDER BY f2.embedding <=> %s::vector "
            "    LIMIT %s"
            "  ) v "
            "  JOIN ks_fragment f ON f.key = v.key "
            f"  WHERE {where_sql} "
            "  ORDER BY v.d, f.key "
            "  LIMIT %s"
            ") "
            "SELECT key, chash, created, sentiment_score, feedback_score, "
            "corrected, consumed, "
            + self._TOPIC_HITS_SELECT + ", d AS score "
            "FROM cand "
            + self._TOPIC_HITS_JOIN.format(c="clower") + " "
            + self._THIN_GROUP_BY_KNN + " ORDER BY d, key"
        )
        with self._ro() as cur:
            cur.execute(sql, [lit, lit, fetch_n, *params, self._candidate_count,
                              topic_t, topic_w, topic_k])
            rows = cur.fetchall()

        pre_weights: Dict[str, tuple] = {}
        fragments = self._rows_to_thin(rows, "_knn_score", 1.0,
                                       pre_weights, attn_total)
        fragments = self._rerank_with_pre(fragments, score_key="_knn_score", is_knn=True,
                                          pre_weights=pre_weights)
        # 🔴 tiebreak 必须发生在**截断之前**：截断点正好落在同分档上时，
        #    「哪几条被切掉」本身就要确定，否则同题多查的返回条数都会不一样。
        return self._tiebreak(fragments)[: self._final_limit]

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

        🔴 **两路并行**（2026-10 ks_ppar）：改前是 `bm25_thin()` 跑完再
        `knn_thin()` 的**串行**，端到端 = 两路耗时相加（180 实测 1.355s ≈
        0.919 + 0.412）。而 KNN 那 0.9s 里裸 SQL 只有 9ms，绝大部分是
        **查询向量那次 embedder HTTP 调用** —— 它与 BM25 完全无依赖，
        串行等于让 BM25 的 SQL 白等一次网络往返。现在两路各投一个 worker：
        KNN 先投（它要先做 HTTP），BM25 同时跑，墙钟 ≈ max(两路)。

        安全性：两路都是**只读**且各自 `self._ro()`；worker 线程走
        `_worker_conn()` 的**独立连接**（psycopg Connection 非线程安全，
        共用一条会让两个 cursor 的结果集互相踩，见 `_worker_conn` 注释）。
        合并顺序、融合/过滤/重排全部在主线程按**原顺序**执行，排序语义未动。

        查询向量**每次 search 只算一次**：`_knn_thin` 是唯一调
        `_text_to_vector(query)` 的地方，且这里只投一次 KNN（见 /tmp/ks_ppar_embed.txt）。
        """
        effective_agent_id = agent_id if agent_id else self._agent_id
        effective_is_primary = is_primary if is_primary is not None else self._is_primary

        if not self._has_embedder():
            bm25_results = self._bm25_thin(query, tag_filter, effective_agent_id,
                                            effective_is_primary)
            return self._hydrate_thin(self._apply_v2_filters(bm25_results))

        knn_results, bm25_results = self._search_parallel(
            query, tag_filter, effective_agent_id, effective_is_primary)
        if knn_results:
            fused = self._rrf_fuse(bm25_results, knn_results)
            return self._hydrate_thin(self._apply_v2_filters(self._tiebreak(fused)))
        return self._hydrate_thin(self._apply_v2_filters(bm25_results))

    def _search_parallel(
        self,
        query: str,
        tag_filter: str,
        agent_id: str,
        is_primary: Optional[bool],
    ) -> tuple:
        """BM25 与 KNN 两路并行跑，返回 `(knn_results, bm25_results)`。

        🔴 异常语义**与串行版逐条一致**：任一路抛错就把那路的异常原样上抛，
        不吞、不降级（`future.result()` 会重抛 worker 里的原始异常）。
        两路都只读，所以「BM25 成功 / KNN 失败」不会留下写副作用。

        线程池**按实例复用**（`_pool` 惰性建、2 个 worker、`close()` 里
        shutdown）—— 每次 search 新建线程池会让 worker 连接反复重建，
        而建连到远端库要 2~4s，比省下的那点时间贵得多。
        """
        pool = self._pool
        if pool is None:
            with self._conn_lock:
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(
                        max_workers=2, thread_name_prefix="ks-pg-search")
                pool = self._pool

        def run(fn, *args):
            # 标记本线程走独立连接（`_ro()` 据此分流到 `_worker_conn`）
            self._tls.in_worker = True
            try:
                return fn(*args)
            finally:
                self._tls.in_worker = False

        # KNN 先投：它要先做查询向量的 embedder HTTP 调用（最慢的一段），
        # 先投进去让 BM25 的 SQL 与它重叠，而不是排在它后面等。
        f_knn = pool.submit(run, self._knn_thin, query, tag_filter, agent_id, is_primary)
        f_bm25 = pool.submit(run, self._bm25_thin, query, tag_filter, agent_id, is_primary)
        # 取 BM25 的结果先（它通常更短），但两路都已 in-flight，取哪个不影响并行度
        bm25_results = f_bm25.result()
        knn_results = f_knn.result()
        # 融合前的输入顺序仍是「BM25 原序 + KNN 原序」，与串行版一致
        return knn_results, bm25_results

    def _hydrate_thin(self, fragments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """薄候选 → 完整结果：**对最终要返回的 N 条，一次 `key = ANY` 取回正文**。

        融合/过滤/去重/重排全在 key 级别做完，正文只需要取一次（≤1 条 SQL）。
        取回后按 SEARCH_FIELDS 的顺序重建成与改造前**逐字同形**的 dict
        （空值不进 dict + 非 str 转 str，与 Redis hash 稀疏语义一致），
        排序分（`_sim`/`_combined_score`/`_weights`/`_key`）原样保留。
        """
        need = [f for f in fragments if f.get("_chash")]
        if not need:
            return fragments
        with self._ro() as cur:
            cur.execute(
                f"SELECT {', '.join(FRAGMENT_COLUMNS)} FROM ks_fragment WHERE key = ANY(%s)",
                ([f["_key"] for f in need],),
            )
            rows = {row[0]: self._row_to_fragment(row) for row in cur.fetchall()}
        out: List[Dict[str, Any]] = []
        gone = 0
        for frag in fragments:      # 按原顺序走一遍，别把两段来源重排了
            if not frag.get("_chash"):
                out.append(frag)
                continue
            row = rows.get(frag.get("_key"))
            if row is None:
                # 候选与取正文之间被删/改写：正文已不存在，宁可不返回也不返回
                # 半条（md5 当正文会污染上层）
                gone += 1
                continue
            rest = {k: v for k, v in frag.items()
                    if k not in SEARCH_FIELDS and k != "_chash"}
            frag.clear()
            for name in SEARCH_FIELDS:
                value = row.get(name)
                if value is None or value == "":
                    continue
                frag[name] = value
            frag.update(rest)
            out.append(frag)
        if gone:
            logger.debug("storage_pg: %d 条候选在取正文前已消失（并发写），已剔除", gone)
        return out

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

        tsvector 照常现场算（迁移脚本不该也不需要自己分词）。
        未提供的列保持原值不被清空（`DO UPDATE` 只覆盖传入的列）。

        🔴 **文本字段必须是 str**：`fields` 若是 `decode_responses=False` 的
        Redis HGETALL 原样产物，值全是 bytes —— 那是上游漏了解码，不是本方法
        该兜底的（兜底会把向量 blob 之类真二进制字段静默毁成乱码）。故在此
        **入口显式抛 `_BytesFieldError`**，并点名是哪个字段。

        🔴 **向量优先搬运**：`fields["embed_bin"]`（float32 blob）若存在就
        原样搬到 `embedding` 列，**不重算** —— 对照评测要两边同源向量。
        只有确实没有向量时才调 embedder 现算。
        """
        key = fields.get("key")
        if not key:
            logger.warning("storage_pg: upsert_fragment 缺 key，跳过")
            return False

        # 写路径让检索快照立即失效：新写的 content_tsv 会改 N/avgdl/df。
        # （放在入口而不是提交后：upsert 失败也只是白丢一次快照，正确性不依赖它。）
        self._snap_drop("n_avgdl", "dfs")

        # INSERT 的列清单必须以 key 打头（参数也是 key 在最前），
        # 否则列数与参数数对不上，psycopg 直接报「placeholder 数不符」。
        cols: List[str] = ["key"]
        params: List[Any] = [key]
        for hash_field, column in self._HASH_TO_COLUMN.items():
            if hash_field == "key" or hash_field not in fields:
                continue
            cols.append(column)
            params.append(_text_field(fields[hash_field], hash_field))
        content = _text_field(fields.get("content"), "content")
        if "content" not in cols:
            cols.append("content")
            params.append(content)

        entities = _text_field(fields.get("entities"), "entities")
        tags = _text_field(fields.get("tags"), "tags")

        # 向量：Redis 已有就搬，没有才算。搬运后必须校验维度 —— 维度不符是
        # 配置/数据错误，重试与重算都没意义，显式抛错（同 _SchemaDimMismatch 语义）。
        blob = fields.get("embed_bin")
        if blob:
            vec = _blob_to_vector(blob)
            if len(vec) != self._embed_dim:
                raise _SchemaDimMismatch(
                    f"storage_pg: embed_bin 维度 {len(vec)} != embed_dim={self._embed_dim} "
                    f"(key={key})。Refusing to mix vector dimensions — 向量必须原样搬运，"
                    f"请核对 embedder 模型与 embed_dim 配置；不要靠重算掩盖。"
                )
        else:
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
        # 写路径让检索快照立即失效：本次会改 ks_fragment 的 content_tsv（→ N/avgdl/df）、
        # 并累加 ks_hot_topic / ks_attention（→ 重排加权信号）。
        self._snap_drop(*self._SNAPSHOT_GROUPS)

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

        ## 🔴 为什么还要**按绑定参数数切块**（真数据上炸出来的）

        psycopg3 走的是 PG 的扩展查询协议：一条语句的参数个数是 Int16，上限 65535。
        实体时间线在真数据上是「4964 个 zset、成员合计 49909 行」，单条 INSERT
        带 3 列就是 149727 个参数 ⇒ psycopg 在**发出之前**就抛：

            psycopg.OperationalError: sending query and params failed:
            number of parameters must be between 0 and 65535

        崩在客户端、库一个字都没写 ⇒ 迁移静默停在 0 行。上一单的夹具只有 2~3 行，
        量级根本够不到这条线（这正是本单要补的那道门）。
        所以切块放在**这里**（多行写的唯一出口），而不是在每个调用点各切一遍 ——
        调用点五六个 + 迁移路径一个，漏一处就退回原样，且漏的那个不报错、只是
        「这批表永远是 0 行」。

        **先排序、后切块**（不是反过来）：排序是按整行做的，切块只是把已排好序的
        列表切成连续段 ⇒ 跨块仍然单调。反过来（先切块再各块排序）会让「全局行序」
        随块边界错位，块与块之间又可能相反 ⇒ 死锁防线被悄悄破坏，而这类退化
        只在并发下偶发，测不出来。
        """
        if not rows:
            return
        ncol = len(columns)
        # 参数个数与列数必须一致，否则下面算出的块大小和实际发出的参数数会错位
        # （多退少补都算不出来，只能等 psycopg 抛一个和病因无关的错）
        for r in rows:
            if len(r) != ncol:
                raise ValueError(
                    f"storage_pg._insert_many: {table} 的行有 {len(r)} 个值，"
                    f"但 columns 有 {ncol} 列 —— 列与行必须一一对应"
                )
        rows = sorted(rows)          # ← 第一道防线的**单点**，见上文「为什么行序=加锁顺序」
        tuple_sql = "(" + ",".join(["%s"] * ncol) + ")"
        head = f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
        chunk = max(1, SAFE_PARAM_CAP // ncol)
        n_blocks = -(-len(rows) // chunk)          # 向上取整；至少 1（rows 非空）
        for i, start in enumerate(range(0, len(rows), chunk), 1):
            block = rows[start:start + chunk]
            params: List[Any] = []
            for row in block:
                params.extend(row)
            try:
                cur.execute(
                    head + ",".join([tuple_sql] * len(block)) + " " + on_conflict,
                    params,
                )
            except Exception as e:              # noqa: BLE001 — 只为补上下文，原样重抛
                # 块内失败要能定位：只报「50000 行里第几块、哪张表、这块多少行」，
                # 否则重跑只能从头再猜一遍。异常本身不吞、不改写。
                logger.error(
                    "storage_pg: %s 写入失败于第 %d/%d 块（该块 %d 行，共 %d 行）: %s: %s",
                    table, i, n_blocks, len(block), len(rows), type(e).__name__, e,
                )
                raise

    @with_deadlock_retry
    def import_aux_rows(
        self,
        table: str,
        columns: tuple,
        rows: List[tuple],
        on_conflict: str,
    ) -> int:
        """🔴 **迁移专用**：按主键**覆盖**导入辅助结构的多行（热词/注意力/时间线/共现/同义词）。

        ## 为什么不能走 `_record_topics()` / `_record_attention()` / `_record_entity_cooc()`

        那三个是**在线累加**语义（`score = ks_x.score + EXCLUDED.score`）：
        每次 store() 都往同一个 (scope, topic) 上加一点。迁移要的是**镜像** ——
        「Redis 里是多少，PG 里就是多少」。用累加路径搬一遍，跑第二次分数就翻倍，
        对照评测的两侧加权数据从此不可比。所以这里换 `SET = EXCLUDED`：覆盖而非累加，
        **重复跑同一份 Redis 快照 ⇒ 行数与分数都不变**（幂等）。

        仍然复用 `_insert_many()` —— 它的「按整行排序再发」是死锁防线（见该方法
        docstring），迁移与在线写入走同一个出口 ⇒ 不会因为「这是迁移路径」就绕过它。

        `table`/`columns`/`on_conflict` 由调用方给（同 `_insert_many` 的约定）：
        表名只来自本模块的辅助表清单，**不接受外部输入**。
        """
        if not rows:
            return 0
        # 迁移会**整表覆盖**辅助结构 ⇒ 对应分组的快照全部作废。
        # 表名不在映射里就**全清**：迁移是低频路径，多清几个分组的代价是一次重查，
        # 漏清一个分组的代价是「迁移后仍读旧快照」。
        group_by_table = {
            "ks_hot_topic": "hot",
            "ks_hot_topic_seen": "hot",
            "ks_attention": "attn",
            "ks_synonym": "syn",
        }
        group = group_by_table.get(table)
        self._snap_drop(*(self._SNAPSHOT_GROUPS if group is None else (group,)))
        with self._tx() as cur:
            self._insert_many(cur, table, columns, rows, on_conflict)
        return len(rows)

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
        self._snap_drop("n_avgdl", "dfs")   # 封边不改 content_tsv，但会改「活记忆」口径的 N
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
        self._snap_drop("n_avgdl", "dfs")   # corrected 会把 valid_until 置空 → 口径变
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

        # ------------------------------------------------------------------
    # 细粒度能力探针（StorageBase，2026-10 ks_pcli）
    # Redis 侧这三个方法走 EXISTS / pipeline HINCRBY / HSET；PG 侧逐个对照：
    #   fragment_exists  → ks_fragment.key 是主键，**完全等价**，真查库
    #   touch_fragment   → 无 touch_count/updated_at 列（R6 的核心语义「不覆盖
    #                      content」由调用方 store() 短路保证，与后端无关）
    #   set_supersedes   → 无 supersedes 列（该字段至今无任何读取方）
    # 后两个返回 False 并留可辨识日志，**禁止静默跳过**。
    # ------------------------------------------------------------------

    def fragment_exists(self, key: str) -> Optional[bool]:
        """EXISTS 的等价实现：主键点查。真查库，不会返回 None（不可达时向上抛）。"""
        if not key:
            return False
        with self._ro() as cur:
            cur.execute("SELECT 1 FROM ks_fragment WHERE key = %s", (key,))
            return cur.fetchone() is not None

    def touch_fragment(self, key: str) -> bool:
        """unsupported：ks_fragment 无 touch_count / updated_at 列。

        为什么不为它建列：这两个字段**全仓无任何读取方**（唯一读点在
        `__init__.py:600` 的临时 dict 上，不碰碎片库）—— 建列即造无人读的数据。
        R6 真正要保的语义「命中后绝不覆盖原 content」由调用方 store() 短路保证，
        PG 后端照样成立。
        ponytail: 若将来要做「最近命中时间」排序/衰减，把这两列加进
        `_DDL_TABLES.ks_fragment` + FRAGMENT_COLUMNS，本方法改成一条 UPDATE 即可。
        """
        if not key:
            return False
        logger.info(
            "storage_pg: touch_fragment(%s) unsupported — ks_fragment 无 "
            "touch_count/updated_at 列（R6 的『不覆盖 content』语义不受影响，"
            "仅这两个遥测字段不持久化）",
            key,
        )
        return False

    def set_supersedes(self, new_key: str, old_key: str) -> bool:
        """unsupported：ks_fragment 无 supersedes 列。

        `supersedes` 是**单向**留痕字段（全仓 grep 无任何读取方）；反向封边走
        `supersede_fragment(old_key, new_key)` —— 那个两后端都已实现，不受影响。
        """
        if not new_key or not old_key:
            return False
        logger.info(
            "storage_pg: set_supersedes(%s <- %s) unsupported — ks_fragment 无 "
            "supersedes 列（该字段无读取方；反向封边请用 supersede_fragment）",
            new_key, old_key,
        )
        return False

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

    def _attn_snapshot(self, top_n: int) -> list:
        """注意力榜（进程级 TTL 快照）—— `match_attention` 与薄候选 SQL 共用同一批行。"""
        key = (top_n,)
        raw = self._snap_get("attn", key)
        if raw is None:
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
            self._snap_put("attn", key, raw)
        return raw

    def _hot_snapshot(self, limit: int) -> tuple:
        """热词榜 + last_seen（进程级 TTL 快照）—— 同上，两处共用。"""
        key = (limit,)
        snap = self._snap_get("hot", key)
        if snap is None:
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
                last_seen: Dict[str, float] = {}
                if topics:
                    cur.execute(
                        "SELECT topic, last_seen FROM ks_hot_topic_seen WHERE topic = ANY(%s)",
                        (topics,),
                    )
                    last_seen = {t: float(ls) for t, ls in cur.fetchall()}
            snap = (topics, last_seen)
            self._snap_put("hot", key, snap)
        return snap

    def _topic_weight_args(self, limit: int = 10) -> tuple:
        """薄候选 SQL 用的话题参数：`(词表, 权重表, 类别表, 注意力总分)`。

        热词（kind=0）与注意力话题（kind=1）合成**一根平行数组**，
        候选 SQL 里一次 `LEFT JOIN unnest(...)` 就把两边的命中权重都算出来。

        每个词的权重与共用的 `hot_topic_weighted_hits` / `attention_boost_from_topics`
        逐条对齐（长度 <2 的词不进命中集；无 last_seen 按 0.5 折半；有则 2^(-天数/半衰期)；
        注意力分母含长度 <2 的词），唯一差别是「某段正文命中了哪些词」这一步从
        Python 挪进 PG（见 `_TOPIC_HITS_JOIN`）。命中之后 hot_w / attn_w 的换算仍在
        共用公式里跑，这里只交出词与权重。
        """
        topics, last_seen = self._hot_snapshot(limit)
        now_ts = datetime.now(timezone.utc).timestamp()
        half = self._hot_topic_decay_half_days
        terms: List[str] = []
        weights: List[float] = []
        kinds: List[int] = []
        for topic in topics:
            if len(topic) < 2:
                continue
            seen = last_seen.get(topic)
            terms.append(topic)
            kinds.append(0)
            weights.append(0.5 if not (seen and seen > 0)
                           else 2.0 ** (-max(0.0, (now_ts - seen) / 86400.0) / half))
        total = 0.0
        for topic, score in self._attn_snapshot(limit):
            terms.append(topic)
            kinds.append(1)
            weights.append(float(score))
            total += float(score)      # 长度 <2 的词也计入分母（与共用公式一致）
        return terms, weights, kinds, total

    def match_attention(self, content: str, top_n: int = 10) -> float:
        """内容命中高注意力话题的加权值（公式与 attention.match_attention_boost 同）。

        🔴 走进程级 TTL 快照：`rerank_with_decay` 对**每条候选**都调一次本方法，
        而它取回的那张榜（scope + top_n + 未过期）在整轮重排里是**同一批行**。
        不缓存就是「每条候选一次往返」——实测占了 search() 总耗时的大头
        （见 /tmp/ks_pgperf_rootcause.txt）。写路径一律 `_snap_drop("attn")`。
        """
        if not content:
            return 1.0
        # 加权公式是后端无关的纯计算 → 与 Redis 侧共用 storage_shared 同一份
        return attention_boost_from_topics(self._attn_snapshot(top_n), content,
                                            self._attention_boost_max)

    def match_hot_topics(self, text: str, limit: int = 10) -> float:
        """内容命中热词的衰减加权命中数（公式与 Redis match_hot_topics 同）。

        🔴 同 `match_attention`：热词榜 + last_seen 走进程级 TTL 快照，
        写路径 `_snap_drop("hot")` 后立刻失效。**只缓存取回的行**，
        `hot_topic_weighted_hits` 仍按每条候选的文本实算 ⇒ 加权公式与排序零改动。
        """
        if not text:
            return 0.0
        topics, last_seen = self._hot_snapshot(limit)
        if not topics:
            return 0.0

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