"""Redis + RediSearch 存储层 — 碎片的读写与语义检索。"""

from __future__ import annotations

import logging
import math
import struct
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import redis
from redis.commands.search.query import Query

from .embedder import Embedder

logger = logging.getLogger(__name__)

# RediSearch index 名称
RS_INDEX = "idx:memories"

# KNN 候选数（多拉一些给 rerank 留空间）
KNN_CANDIDATES = 10
# 最终返回条数
FINAL_LIMIT = 5
# 时间衰减半衰期（天）
DECAY_HALF_DAYS = 60


class RedisStorage:
    """碎片存储与检索。

    基于 Redis + RediSearch 实现向量化语义搜索。
    """

    def __init__(
        self,
        embedder: Embedder,
        host: str = "127.0.0.1",
        port: int = 6379,
    ):
        self._embedder = embedder
        self._host = host
        self._port = port
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

    def check_index(self) -> bool:
        """验证 RediSearch index 存在。"""
        client = self._get_client()
        if not client:
            return False
        try:
            client.ft(RS_INDEX).search(Query("*").paging(0, 0))
            return True
        except Exception as e:
            logger.warning("storage: index check failed: %s", e)
            return False

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------------
    # 向量化
    # ------------------------------------------------------------------

    def _text_to_blob(self, text: str) -> Optional[bytes]:
        """文本 → 1536 维 float32 二进制 blob。"""
        vec = self._embedder.get_embedding(text)
        if not vec:
            return None
        return struct.pack(f"{len(vec)}f", *vec)

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
        """将一段文本写入碎片库。

        自动计算 embedding 并存入 Redis Hash。
        """
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

    def search(self, query: str) -> List[Dict[str, Any]]:
        """语义搜索碎片，经时间衰减重排序后返回。

        流程:
          1. 计算查询的 embedding
          2. FT.SEARCH KNN 拉 top KNN_CANDIDATES
          3. 时间衰减重排序
          4. 取 top FINAL_LIMIT
        """
        blob = self._text_to_blob(query)
        if not blob:
            return []

        client = self._get_client()
        if not client:
            return []

        try:
            q = (
                Query("*=>[KNN $K @embed_bin $vec AS score]")
                .sort_by("score")
                .return_fields("content", "tags", "category", "source", "created")
                .dialect(2)
                .paging(0, KNN_CANDIDATES)
            )
            result = client.ft(RS_INDEX).search(
                q, query_params={"vec": blob, "K": KNN_CANDIDATES}
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
                    fragments.append(frag)

            fragments = self._rerank_with_decay(fragments)
            return fragments[:FINAL_LIMIT]

        except Exception as e:
            logger.debug("storage: search error: %s", e)
            return []

    # ------------------------------------------------------------------
    # 时间衰减
    # ------------------------------------------------------------------

    @staticmethod
    def _rerank_with_decay(fragments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按时间衰减重排序。

        综合得分 = 2^(-age_days / half_life_days)
        无时间戳排在最后。
        """
        now = datetime.now(timezone.utc)

        for frag in fragments:
            created_str = frag.get("created", "")
            if not created_str:
                frag["_decay_score"] = 0.01
                continue
            try:
                created = datetime.fromisoformat(created_str)
                age_days = (now - created).total_seconds() / 86400.0
                if age_days < 0:
                    age_days = 0
            except (ValueError, TypeError):
                frag["_decay_score"] = 0.01
                continue

            frag["_decay_score"] = 2.0 ** (-age_days / DECAY_HALF_DAYS)

        fragments.sort(key=lambda x: x.get("_decay_score", 0), reverse=True)
        return fragments
