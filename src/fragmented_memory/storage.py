"""
Redis + RediSearch 存储层 — 碎片的读写与检索。

支持两种检索模式（可共存）：
  - BM25 全文搜索（默认，零成本）— 同义词扩展 + 标签过滤
  - KNN 向量搜索（可选）— 需要 embedder 配置
"""

from __future__ import annotations

import hashlib
import logging
import os
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

# BM25 检索参数
DEFAULT_BM25_LIMIT = 10        # BM25 搜多少条候选
DEFAULT_FINAL_LIMIT = 5        # 最终返回条数

# KNN 参数（embedding 模式用）
DEFAULT_CANDIDATE_COUNT = 10   # KNN 候选数

# 时间衰减半衰期（天）
DECAY_HALF_DAYS = 60

# embedding 缓存 TTL（秒）
EMBED_CACHE_TTL = 3600

# FT.CREATE 命令（首次使用自动执行）
# 注意：用于 client.execute_command(*parts)，不要加引号（split 后引号变字面字符）
_CREATE_INDEX_CMD = (
    "FT.CREATE idx:memories ON HASH PREFIX 1 memory:frag: SCHEMA "
    "content TEXT WEIGHT 1 "
    "tags TAG SEPARATOR , "
    "category TAG SEPARATOR , "
    "source TEXT WEIGHT 1 "
    "created TEXT WEIGHT 0 "
    "fragment_type TAG SEPARATOR , "
    "embed_bin VECTOR FLAT 6 TYPE FLOAT32 DIM 1536 DISTANCE_METRIC COSINE"
)

SYNONYM_HASH_KEY = "fragmented:synonyms"


def _expand_terms(terms: List[str], synonym_map: Dict[str, set]) -> List[str]:
    """用同义词表展开搜索词列表。"""
    if not synonym_map:
        return terms
    expanded = list(terms)
    for t in terms:
        tl = t.lower()
        if tl in synonym_map:
            for syn in synonym_map[tl]:
                if syn not in expanded:
                    expanded.append(syn)
    return expanded


class RedisStorage:
    """碎片存储与检索。

    基于 Redis + RediSearch，同时支持 BM25 全文搜索（默认）和 KNN 向量搜索。"""
    def __init__(
        self,
        embedder: Optional[Embedder] = None,
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

    def _has_embedder(self) -> bool:
        """检查 embedder 是否可用。"""
        return self._embedder is not None and hasattr(self._embedder, "get_embedding")

    def ensure_index(self) -> bool:
        """初始化时自动创建/验证 RediSearch index + 注册同义词。"""
        client = self._get_client()
        if not client:
            return False

        # 尝试检查 index 是否已存在
        try:
            client.ft(RS_INDEX).search(Query("*").paging(0, 0))
            idx_exists = True
        except Exception:
            idx_exists = False

        # 创建 index（如果不存在）
        if not idx_exists:
            try:
                parts = _CREATE_INDEX_CMD.split()
                client.execute_command(*parts)
                logger.info("storage: created RediSearch index '%s'", RS_INDEX)
            except redis.ResponseError as e:
                if "already exists" in str(e).lower():
                    logger.info("storage: index '%s' already exists", RS_INDEX)
                else:
                    logger.warning("storage: failed to create index: %s", e)
                    return False
            except Exception as e:
                logger.warning("storage: failed to create index: %s", e)
                return False

        # 注册同义词组（幂等）
        self._ensure_synonyms(client)
        return True

    def _ensure_synonyms(self, client: redis.Redis) -> None:
        """已废弃 — 在 _load_synonym_map 中动态加载。"""
        pass

    def _load_synonym_map(self) -> Dict[str, set]:
        """从 Redis Hash fragmented:synonyms 加载同义词表。"""
        client = self._get_client()
        if not client:
            return {}
        try:
            raw = client.hgetall(SYNONYM_HASH_KEY)
            if not raw:
                return {}
            import json as _json
            synonym_map: Dict[str, set] = {}
            for term_b, val_b in raw.items():
                term = term_b.decode("utf-8").lower().strip()
                if not term:
                    continue
                try:
                    syns = _json.loads(val_b.decode("utf-8"))
                except (_json.JSONDecodeError, UnicodeDecodeError):
                    continue
                terms_set = set()
                for s in syns:
                    sl = s.lower().strip()
                    if sl and sl != term:
                        terms_set.add(sl)
                if terms_set:
                    synonym_map[term] = terms_set
                    for s in terms_set:
                        if s not in synonym_map:
                            synonym_map[s] = set()
                        synonym_map[s].add(term)
            return synonym_map
        except Exception as e:
            logger.debug("storage: load synonyms error: %s", e)
            return {}

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------------
    # 向量化（带 MD5 缓存，仅 embedding 模式使用）
    # ------------------------------------------------------------------

    def _text_to_blob(self, text: str) -> Optional[bytes]:
        """文本 → float32 二进制 blob。"""
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

        if not self._has_embedder():
            return None
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
        """将一段文本写入碎片库。

        embed_bin 是可选的（仅 embedding 模式需要）。
        BM25 全文搜索只需要 content + tags 字段。
        """
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
            }
            if fragment_type:
                mapping["fragment_type"] = fragment_type

            # embed_bin 可选：有 embedder 时计算并存
            if self._has_embedder():
                blob = self._text_to_blob(text)
                if blob:
                    mapping["embed_bin"] = blob

            client.hset(key, mapping=mapping)
            return True
        except Exception as e:
            logger.warning("storage: store error: %s", e)
            return False

    # ------------------------------------------------------------------
    # BM25 全文检索（默认，零成本）
    # ------------------------------------------------------------------

    def search_bm25(
        self,
        query: str,
        tag_filter: str = "",
    ) -> List[Dict[str, Any]]:
        """BM25 全文搜索，经时间衰减重排序后返回。

        流程:
          1. 构建 RediSearch 文本查询（自动扩展同义词）
          2. 可选标签过滤
          3. 时间衰减重排序
          4. 取 top final_limit
        """
        client = self._get_client()
        if not client or not query.strip():
            return []

        try:
            synonym_map = self._load_synonym_map()
            raw_terms = query.strip().split()
            expanded = _expand_terms(raw_terms, synonym_map)
            if not expanded:
                return []

            # 用 | 连接所有词（OR 语义）
            safe_terms = "|".join(expanded)

            # 构建全文查询
            if tag_filter:
                tags = ",".join(t.strip() for t in tag_filter.split(",") if t.strip())
                query_expr = f"@tags:{{{tags}}} @content:{safe_terms}"
            else:
                query_expr = f"@content:{safe_terms}"

            q = (
                Query(query_expr)
                .paging(0, DEFAULT_BM25_LIMIT)
                .dialect(2)
                .return_fields("content", "tags", "category", "source", "created")
            )

            result = client.ft(RS_INDEX).search(q)

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
                    # BM25 score 越大越相关
                    frag["_bm25_score"] = float(getattr(doc, "score", 0.0))
                    fragments.append(frag)

            fragments = self._rerank_with_decay(fragments, score_key="_bm25_score")
            return fragments[: self._final_limit]

        except Exception as e:
            logger.debug("storage: BM25 search error: %s", e)
            return []

    # ------------------------------------------------------------------
    # KNN 向量检索（可选，需 embedder）
    # ------------------------------------------------------------------

    def search_knn(
        self,
        query: str,
        tag_filter: str = "",
    ) -> List[Dict[str, Any]]:
        """KNN 向量搜索，经时间衰减重排序后返回。"""
        if not self._has_embedder():
            return []

        blob = self._text_to_blob(query)
        if not blob:
            return []

        client = self._get_client()
        if not client:
            return []

        try:
            if tag_filter:
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
                    frag["_knn_score"] = float(getattr(doc, "score", 1.0))
                    fragments.append(frag)

            fragments = self._rerank_with_decay(fragments, score_key="_knn_score", is_knn=True)
            return fragments[: self._final_limit]

        except Exception as e:
            logger.debug("storage: KNN search error: %s", e)
            return []

    # ------------------------------------------------------------------
    # 统一检索入口
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        tag_filter: str = "",
    ) -> List[Dict[str, Any]]:
        """统一检索入口。

        策略:
          1. 先走 BM25 全文搜索（零成本，有同义词扩展）
          2. 如果 BM25 无结果 且 embedder 可用，走 KNN 向量搜索
          3. 返回合并后的去重结果
        """
        # BM25 全文搜索（默认）
        results = self.search_bm25(query, tag_filter)
        if results:
            return results

        # 无结果时尝试 KNN 向量搜索（如果 embedder 可用）
        if self._has_embedder():
            results = self.search_knn(query, tag_filter)

        return results

    # ------------------------------------------------------------------
    # 综合得分重排序
    # ------------------------------------------------------------------

    @staticmethod
    def _rerank_with_decay(
        fragments: List[Dict[str, Any]],
        score_key: str = "_bm25_score",
        is_knn: bool = False,
    ) -> List[Dict[str, Any]]:
        """综合得分重排序。

        BM25 模式: combined = BM25得分 × 时间衰减
        KNN 模式:   combined = (1 - 余弦距离/2) × 时间衰减
        """
        now = datetime.now(timezone.utc)

        for frag in fragments:
            # 获取原始分数并归一化到 0~1
            raw = float(frag.get(score_key, 0.0))
            if is_knn:
                # KNN: score 是余弦距离（0~2），越小越近
                sim = 1.0 - max(0.0, min(1.0, raw / 2.0))
            else:
                # BM25: score 越大越相关，归一化到 0~1
                sim = min(1.0, max(0.0, raw / 10.0))

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
