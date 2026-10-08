"""
PostgreSQL 存储后端 — keepsake 第二个存储实现（批 1：读写全量，检索留批 2）。

🔴 **本批能力边界（务必先读）**
  已实现（读写类全部）：ensure_index / store / get_fragment / get_fragments_batch /
    correct_fragments / supersede_fragment / record_feedback / get_hot_topics /
    match_hot_topics / match_attention / entity_timeline / close / health_check
  **未实现且显式抛错**：search / search_bm25 / search_knn / discover_synonyms /
    generate_jieba_dict —— 全部抛 NotImplementedError，消息里写明 batch 2。
    绝不静默返回空列表（改了 backend=postgres 却静默搜不到记忆 = 最危险的失败形态）。

与 Redis 侧的字段语义对齐（key 命名/字段名/数值语义都刻意保持一致）：
  * 碎片 key：`memory:frag:<sha256(text)[:12]>`；版本化旧版加 `:<epoch>` 后缀
  * 碎片字段：content / tags / category / source / created / sentiment_score /
    sentiment_label / feedback_score / entities / fragment_type / valid_until /
    is_archived / superseded_by / superseded_at / corrected_at / invalid_at / embed_bin
  * 实体时间线 → `ks_entity_timeline`（对齐 `keepsake:entity_timeline:<实体>` ZSET）
  * 实体共现   → `ks_entity_cooc`（对齐 `keepsake:entity_cooc` ZSET）
  * 同义词     → `ks_synonym`（对齐 `keepsake:synonyms` hash，批 2 用）
  * embed_bin  → `bytea` 列（本批不写，见批 2）

两个刻意的设计选择：
  1) **连不上直接抛错，不静默降级**。Redis 侧连不上返回 False/None 是历史契约；
     PG 侧故意不照抄 —— 本批没有检索，静默降级只会被上层读成「搜不到」。
     代价：PG 抖动会往上抛。批 2 补上检索后再评估是否改为可配置。
  2) **不装 psycopg 也能 import 本模块**（连接时才 import），否则不用 PG 的
     人会因为一个 optional 依赖把整个 keepsake 拖挂。

ponytail: 批 2 会把 match_hot_topics / match_attention 的衰减与归一化公式同
RedisStorage 合并成一份共用实现；本批先各写各的（各约 15 行），避免在检索
还没落地时就动 Redis 侧代码造成回归。
"""

from __future__ import annotations

import hashlib
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from .attention import (
    ATTENTION_DAILY,
    ATTENTION_SET,
    ATTENTION_WEEKLY,
    _ATTENTION_TTL,
)
from .emotion import analyze_emotion
from .splitter import extract_entities, extract_keywords
from .storage_base import StorageBase

logger = logging.getLogger(__name__)

# Redis 侧同类常量的副本（对齐语义，不从 storage.py import，避免循环依赖）
HOT_TOPIC_SET = "keepsake:hot_topics"
HOT_TOPIC_DECAY_HALF_DAYS = 30
ENTITY_COOC_TTL = 2592000  # 30 天

_TOPIC_SCOPE_ALL = "all"
_TOPIC_SCOPE_DAILY = "daily"
_TOPIC_SCOPE_WEEKLY = "weekly"
_SCOPE_TO_REDIS_KEY = {
    _TOPIC_SCOPE_ALL: HOT_TOPIC_SET,
    _TOPIC_SCOPE_DAILY: "keepsake:hot_topics:daily",
    _TOPIC_SCOPE_WEEKLY: "keepsake:hot_topics:weekly",
}
_TOPIC_SCOPES = tuple(_SCOPE_TO_REDIS_KEY)
# Redis 侧按 key 设整集 TTL；PG 侧逐行 expire_ts 等价
_TOPIC_TTL = {
    _TOPIC_SCOPE_ALL: 86400 * 7,
    _TOPIC_SCOPE_DAILY: 86400 * 2,
    _TOPIC_SCOPE_WEEKLY: 86400 * 14,
}
_ATTENTION_SCOPE_TO_KEY = {
    _TOPIC_SCOPE_ALL: ATTENTION_SET,
    _TOPIC_SCOPE_DAILY: ATTENTION_DAILY,
    _TOPIC_SCOPE_WEEKLY: ATTENTION_WEEKLY,
}

# 碎片表列（与 Redis hash 字段一一对应；embed_bin 本批只留位不写）
FRAGMENT_COLUMNS = (
    "key", "content", "tags", "category", "source", "created",
    "sentiment_score", "sentiment_label", "feedback_score",
    "entities", "fragment_type", "valid_until", "is_archived",
    "superseded_by", "superseded_at", "corrected_at", "invalid_at",
)

_DDL = (
    """
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
    """,
    # 对齐 keepsake:entity_timeline:<实体> ZSET（member=碎片 key，score=时间戳）
    """
    CREATE TABLE IF NOT EXISTS ks_entity_timeline (
        entity    text NOT NULL,
        frag_key  text NOT NULL,
        ts        double precision NOT NULL,
        PRIMARY KEY (entity, frag_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ks_entity_timeline_ts ON ks_entity_timeline (entity, ts DESC)",
    # 对齐 keepsake:entity_cooc ZSET（member="a||b"，score=共现次数）
    """
    CREATE TABLE IF NOT EXISTS ks_entity_cooc (
        pair      text PRIMARY KEY,
        score     double precision NOT NULL DEFAULT 0,
        expire_ts double precision NOT NULL
    )
    """,
    # 对齐 keepsake:hot_topics{,:daily,:weekly} 三个 ZSET（scope 列区分）
    """
    CREATE TABLE IF NOT EXISTS ks_hot_topic (
        scope     text NOT NULL,
        topic     text NOT NULL,
        score     double precision NOT NULL DEFAULT 0,
        expire_ts double precision NOT NULL,
        PRIMARY KEY (scope, topic)
    )
    """,
    # 对齐 keepsake:hot_topics:last_seen hash
    """
    CREATE TABLE IF NOT EXISTS ks_hot_topic_seen (
        topic     text PRIMARY KEY,
        last_seen double precision NOT NULL
    )
    """,
    # 对齐 keepsake:attention{,:daily,:weekly} 三个 ZSET
    """
    CREATE TABLE IF NOT EXISTS ks_attention (
        scope     text NOT NULL,
        topic     text NOT NULL,
        score     double precision NOT NULL DEFAULT 0,
        expire_ts double precision NOT NULL,
        PRIMARY KEY (scope, topic)
    )
    """,
    # 对齐 keepsake:synonyms hash（term → JSON 数组），批 2 的 BM25 用
    """
    CREATE TABLE IF NOT EXISTS ks_synonym (
        term     text PRIMARY KEY,
        synonyms jsonb NOT NULL DEFAULT '[]'::jsonb
    )
    """,
)

# 批 2 未实现的方法 → 统一文案
_BATCH2 = (
    "PgStorage.{name}() is not implemented yet — scheduled for batch 2 "
    "(BM25 via jieba→tsvector, KNN via pgvector + HNSW). "
    "Raising on purpose: returning an empty result here would silently make "
    "every memory unsearchable when backend=postgres is selected."
)


class PgStorage(StorageBase):
    """PostgreSQL 存储后端（批 1：读写全量，检索显式抛错）。

    与 RedisStorage 共用 `keepsake.storage.StorageBase` 接口；
    碎片 key 与字段语义按 Redis 侧对齐（见模块注释）。
    """

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
        attention_boost_max: float = 1.5,
        attention_base_increment: float = 2.0,
        attention_emotion_factor: float = 1.5,
        hot_topic_decay_half_days: int = HOT_TOPIC_DECAY_HALF_DAYS,
    ):
        self._dsn = dsn
        self._host = host
        self._port = int(port)
        self._dbname = dbname
        self._user = user
        self._password = password
        self._sslmode = sslmode
        self._connect_timeout = int(connect_timeout)
        # 批 1 不写向量；保留参数只为接口对齐与批 2 平滑接入（不引入新依赖）
        self._embedder = embedder
        self._agent_id = agent_id
        self._attention_boost_max = float(attention_boost_max)
        self._attention_base_increment = float(attention_base_increment)
        self._attention_emotion_factor = float(attention_emotion_factor)
        self._hot_topic_decay_half_days = int(hot_topic_decay_half_days)
        self._conn: Optional[Any] = None

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
        """事务 + 游标。异常时丢弃连接并原样上抛（不吞、不降级）。"""
        conn = self._connect()
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    yield cur
        except Exception:
            self._drop_conn()
            raise

    @contextmanager
    def _ro(self) -> Iterator[Any]:
        """只读游标（不显式开事务：psycopg 下 SELECT 自动进入隐式事务）。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                yield cur
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
        """幂等建表。任一条 DDL 失败即抛错（不静默）。"""
        with self._tx() as cur:
            for stmt in _DDL:
                cur.execute(stmt)
        logger.info("storage_pg: schema ready on %s:%s/%s", self._host, self._port, self._dbname)
        return True

    def close(self) -> None:
        """关连接，可重复调用。"""
        self._drop_conn()

    # ------------------------------------------------------------------
    # 批 2 未实现（显式抛错，绝不静默空返回）
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        tag_filter: str = "",
        agent_id: str = "",
        is_primary: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError(_BATCH2.format(name="search"))

    def search_bm25(
        self,
        query: str,
        tag_filter: str = "",
        agent_id: str = "",
        is_primary: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError(_BATCH2.format(name="search_bm25"))

    def search_knn(
        self,
        query: str,
        tag_filter: str = "",
        agent_id: str = "",
        is_primary: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError(_BATCH2.format(name="search_knn"))

    def discover_synonyms(self, rebuild: bool = False) -> Dict[str, Any]:
        raise NotImplementedError(_BATCH2.format(name="discover_synonyms"))

    def generate_jieba_dict(self, output_path: str = None) -> Dict[str, Any]:
        raise NotImplementedError(_BATCH2.format(name="generate_jieba_dict"))

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

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

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

            cur.execute(
                """
                INSERT INTO ks_fragment (
                    key, content, tags, category, source, created,
                    sentiment_score, sentiment_label, feedback_score,
                    entities, fragment_type
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    key, text, final_tags, category, source, now_iso,
                    str(intensity), label, existing_feedback,
                    ",".join(entities) if entities else "", fragment_type,
                ),
            )
            # embed_bin：批 1 不写（无 pgvector、也不做向量化）—— 批 2 补
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
        """单条多行 INSERT（ON CONFLICT 由调用方给）。

        为什么批量：远端库单次往返 ~90ms，逐行 execute 会让一次 store() 打三四十个
        往返（几秒起步）；合并成一条后固定 3~4 个往返。
        """
        if not rows:
            return
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
            for scope, redis_key in _ATTENTION_SCOPE_TO_KEY.items():
                rows.append(
                    (scope, kw_lower, increment, now_ts + _ATTENTION_TTL.get(redis_key, 86400))
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
        expire_ts = now_ts + ENTITY_COOC_TTL
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

    def correct_fragments(self, keys: List[str]) -> int:
        """打 corrected 标签 + feedback_score=-1，返回实际处理条数。"""
        if not keys:
            return 0
        now_iso = datetime.now(timezone.utc).isoformat()
        count = 0
        with self._tx() as cur:
            cur.execute(
                "SELECT key, tags FROM ks_fragment WHERE key = ANY(%s)",
                (list(keys),),
            )
            for key, tags in cur.fetchall():
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
        if not raw:
            return 1.0
        content_lower = content.lower()
        total_score = 0.0
        max_score = 0.0
        for topic, sc in raw:
            sc = float(sc)
            if len(topic) >= 2 and topic in content_lower:
                total_score += sc
            max_score += sc
        if max_score <= 0:
            return 1.0
        ratio = min(total_score / max_score, 1.0)
        return 1.0 + (self._attention_boost_max - 1.0) * ratio

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

        now = datetime.now(timezone.utc).timestamp()
        decay_half = float(self._hot_topic_decay_half_days)
        text_lower = text.lower()
        weighted_hits = 0.0
        for topic in topics:
            if len(topic) < 2 or topic not in text_lower:
                continue
            seen_ts = last_seen.get(topic)
            if seen_ts and seen_ts > 0:
                days_ago = max(0, (now - seen_ts) / 86400.0)
                weighted_hits += 2.0 ** (-days_ago / decay_half)
            else:
                weighted_hits += 0.5  # 无时间戳的折半
        return weighted_hits

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