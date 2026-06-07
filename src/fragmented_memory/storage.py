"""Redis + RediSearch 存储层 — 碎片的读写与语义检索。"""

from __future__ import annotations

import hashlib
import logging
import struct
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import redis
from redis.commands.search.query import Query

from .embedder import Embedder

logger = logging.getLogger(__name__)

# RediSearch index 名称
RS_INDEX = "idx:memories"

# 默认 KNN 候选数（多拉一些给 rerank 留空间）
DEFAULT_CANDIDATE_COUNT = 10
# 默认最终返回条数
DEFAULT_FINAL_LIMIT = 5
# 时间衰减半衰期（天）
DECAY_HALF_DAYS = 60

# embedding 缓存 TTL（秒）— 1 小时
EMBED_CACHE_TTL = 3600

# FT.CREATE 命令（首次使用自动执行）
_CREATE_INDEX_CMD = (
    'FT.CREATE idx:memories ON HASH PREFIX 1 "memory:frag:" SCHEMA '
    "content TEXT WEIGHT 1 "
    'tags TAG SEPARATOR "," '
    'category TAG SEPARATOR "," '
    "source TEXT WEIGHT 1 "
    "created TEXT WEIGHT 0 "
    'fragment_type TAG SEPARATOR "," '
    "embed_bin VECTOR FLAT 6 TYPE FLOAT32 DIM 1536 DISTANCE_METRIC COSINE"
)


class RedisStorage:
    """碎片存储与检索。

    基于 Redis + RediSearch 实现向量化语义搜索。
    """

    def __init__(
        self,
        embedder: Embedder,
        host: str = "127.0.0.1",
        port: int = 6379,
        candidate_count: int = DEFAULT_CANDIDATE_COUNT,
        final_limit: int = DEFAULT_FINAL_LIMIT,
    ):
        self._embedder = embedder
        self._host = host
        self._port = port
        self._candidate_count = candidate_count
        self._final_limit = final_limit
        self._client: Optional[redis.Redis] = None

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def _get_client(self) -> Optional[redis.Redis]:
        if self._client is not None:
            try:
                self._client.ping()
                return self._client
            except redis.ConnectionError:
                self._client = None
        try:
            self._client = redis.Redis(
                host=self._host,
                port=self._port,
                socket_connect_timeout=3,
                socket_timeout=5,
                decode_responses=False,
                protocol=2,
            )
            self._client.ping()
            return self._client
        except redis.ConnectionError as e:
            logger.warning("storage: Redis not reachable (%s)", e)
            return None

    def ensure_index(self) -> bool:
        """初始化时自动创建/验证 RediSearch index。"""
        client = self._get_client()
        if not client:
            return False

        # 先尝试检查 index 是否已存在
        try:
            client.ft(RS_INDEX).search(Query("*").paging(0, 0))
            return True
        except Exception:
            pass

        # index 不存在，自动创建
        try:
            parts = _CREATE_INDEX_CMD.split()
            client.execute_command(*parts)
            logger.info("storage: created RediSearch index '%s'", RS_INDEX)
            return True
        except Exception as e:
            logger.warning("storage: failed to create index: %s", e)
            return False

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------------
    # 向量化（带 MD5 缓存）
    # ------------------------------------------------------------------

    def _text_to_blob(self, text: str) -> Optional[bytes]:
        """文本 → 1536 维 float32 二进制 blob。

        用 MD5 做缓存 key，相同文本重复调 embedding API 时秒回。
        """
        md5 = hashlib.md5(text.encode("utf-8")).hexdigest()
        cache_key = f"embed_cache:{md5}"

        client = self._get_client()
        if client:
            try:
                cached = client.get(cache_key)
                if cached is not None and isinstance(cached, bytes):
                    return cached
            except Exception:
                pass

        vec = self._embedder.get_embedding(text)
        if not vec:
            return None
        blob = struct.pack(f"{len(vec)}f", *vec)

        if client:
            try:
                client.setex(cache_key, EMBED_CACHE_TTL, blob)
            except Exception:
                pass

        return blob

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
    ) -> bool:
        """将一段文本写入碎片库。"""
        blob = self._text_to_blob(text)
        if not blob:
            return False

        client = self._get_client()
        if not client:
            return False

        key = f"memory:frag:{uuid.uuid4().hex[:12]}"
        try:
            mapping: Dict[str, Any] = {
                "content": text,
                "tags": tags,
                "category": category,
                "source": source,
                "created": datetime.now(timezone.utc).isoformat(),
                "embed_bin": blob,
            }
            if fragment_type:
                mapping["fragment_type"] = fragment_type
            client.hset(key, mapping=mapping)
            return True
        except Exception as e:
            logger.warning("storage: store error: %s", e)
            return False

    # ------------------------------------------------------------------
    # 检索 + 重排序
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        tag_filter: str = "",
    ) -> List[Dict[str, Any]]:
        """语义搜索碎片，经时间衰减重排序后返回。

        参数:
            query: 搜索文本
            tag_filter: 可选的标签过滤，如 "btc,eth" 只搜这些标签的碎片

        流程:
          1. 计算查询的 embedding
          2. FT.SEARCH KNN 拉 top candidate_count（支持标签过滤）
          3. 综合得分（向量距离 × 时间衰减）重排序
          4. 取 top final_limit
        """
        blob = self._text_to_blob(query)
        if not blob:
            return []

        client = self._get_client()
        if not client:
            return []

        try:
            # 构建查询：可选标签过滤 + KNN
            if tag_filter:
                # 注意：TAG 字段用大括号语法
                tags = ",".join(t.strip() for t in tag_filter.split(",") if t.strip())
                query_expr = f"@tags:{{{tags}}}=>[KNN $K @embed_bin $vec AS score]"
            else:
                query_expr = "*=>[KNN $K @embed_bin $vec AS score]"

            q = (
                Query(query_expr)
                .sort_by("score")
                .return_fields("content", "tags", "category", "source", "created")
                .dialect(2)
                .paging(0, self._candidate_count)
            )
            result = client.ft(RS_INDEX).search(
                q, query_params={"vec": blob, "K": self._candidate_count}
            )

            fragments: List[Dict[str, Any]] = []
            for doc in result.docs:
                frag: Dict[str, Any] = {}
                for field in ("content", "tags", "category", "source", "created"):
                    val = getattr(doc, field, None)
                    if val is not None and val != "":
                        if isinstance(val, bytes):
                            val = val.decode("utf-8")
                        frag[field] = val
                if frag.get("content"):
                    # 记录原始向量距离（score 越小越近）
                    frag["_raw_score"] = getattr(doc, "score", 1.0)
                    fragments.append(frag)

            fragments = self._rerank_with_decay(fragments)
            return fragments[: self._final_limit]

        except Exception as e:
            logger.debug("storage: search error: %s", e)
            return []

    # ------------------------------------------------------------------
    # 综合得分重排序（向量距离 × 时间衰减）
    # ------------------------------------------------------------------

    @staticmethod
    def _rerank_with_decay(fragments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """综合得分重排序。

        综合得分 = 归一化向量相似度 × 时间衰减权重

        向量距离（score）范围 0~2（余弦距离），先转为相似度：
            sim = 1 - score / 2  （距离 0 → 1.0，距离 2 → 0.0）

        时间衰减权重：
            decay = 2^(-age_days / 60)

        综合得分：
            combined = sim × decay

        无时间戳碎片排在最后。
        """
        now = datetime.now(timezone.utc)

        for frag in fragments:
            # 向量相似度：score 是余弦距离（0~2），越小越近
            raw = float(frag.get("_raw_score", 1.0))
            sim = 1.0 - max(0.0, min(1.0, raw / 2.0))

            # 时间衰减
            created_str = frag.get("created", "")
            if not created_str:
                frag["_combined_score"] = sim * 0.01
                continue
            try:
                created = datetime.fromisoformat(created_str)
                age_days = (now - created).total_seconds() / 86400.0
                if age_days < 0:
                    age_days = 0
            except (ValueError, TypeError):
                frag["_combined_score"] = sim * 0.01
                continue

            decay = 2.0 ** (-age_days / DECAY_HALF_DAYS)
            frag["_combined_score"] = sim * decay

        fragments.sort(key=lambda x: x.get("_combined_score", 0), reverse=True)
        return fragments
