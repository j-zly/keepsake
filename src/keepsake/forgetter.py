"""选择性遗忘 — 主动清理低价值碎片。

价值判断维度:
  1. 年龄: 创建 > max_age_days 的碎片
  2. 反馈: feedback_score 为负或为零
  3. 情绪烈度: intensity 低（用户不激动的内容）
  4. 注意力: 从未被命中过高注意力话题
  5. 召回率: 从未被检索召回过（如果有关联字段追踪）

只有多个维度同时低，才会被遗忘。防止误删有用信息。

配置参数:
  - max_age_days: 最大保留天数（默认 30）
  - min_feedback_score: 最低反馈分（低于此值可遗忘，默认 0）
  - batch_size: 每轮扫描数（默认 200）
  - dry_run: 仅统计不删除（默认 True，安全模式）
  - min_intensity: 最低情绪烈度（低于此值且其他维度也低才删，默认 0.3）
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 默认参数
DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_MIN_FEEDBACK_SCORE = 0
DEFAULT_BATCH_SIZE = 200
DEFAULT_DRY_RUN = True
DEFAULT_MIN_INTENSITY = 0.3


class Forgetter:
    """选择性遗忘引擎。"""

    def __init__(
        self,
        storage: Any,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
        min_feedback_score: int = DEFAULT_MIN_FEEDBACK_SCORE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        dry_run: bool = DEFAULT_DRY_RUN,
        min_intensity: float = DEFAULT_MIN_INTENSITY,
        full_max_age_days: int = 60,
    ):
        self._storage = storage
        self._max_age_days = max_age_days
        self._min_feedback_score = min_feedback_score
        self._batch_size = batch_size
        self._dry_run = dry_run
        self._min_intensity = min_intensity
        self._full_max_age_days = full_max_age_days

    def forget(self, force: bool = False) -> Dict[str, Any]:
        """执行一轮遗忘操作。

        参数:
            force: True 时忽略 dry_run 设置，实际删除

        返回:
            操作统计

        2026-10 ks_pmn：改为**只通过 StorageBase 的维护原语**访问存储
        （`scan_fragment_keys` / `get_fragments_batch` / `delete_fragments_batch`），
        Redis 与 PG 两个后端语义等价，不再有 unsupported。
        """
        stats = {
            "scanned": 0,
            "candidates": 0,
            "deleted": 0,
            "skipped_protected": 0,
            "dry_run": self._dry_run and not force,
        }

        forgettable = self._find_forgettable(stats)

        # 扫描完整记忆（memory:full:*），只按年龄判断
        forgettable_full = self._find_forgettable_full(stats)
        forgettable.extend(forgettable_full)

        stats["candidates"] = len(forgettable)

        if not forgettable:
            return stats

        if self._dry_run and not force:
            # 只统计不删除
            stats["deleted"] = 0
            logger.info(
                "forgetter: [DRY RUN] would delete %d fragments (skipped %d protected)",
                len(forgettable), stats["skipped_protected"],
            )
            return stats

        # 实际删除
        stats["deleted"] = self._storage.delete_fragments_batch(forgettable)
        logger.info(
            "forgetter: deleted %d/%d forgettable fragments",
            stats["deleted"], len(forgettable),
        )
        return stats

    def _find_forgettable(
        self,
        stats: Dict[str, Any],
    ) -> List[str]:
        """扫描并筛选可遗忘的碎片。"""
        now = datetime.now(timezone.utc)
        cutoff_ts = now.timestamp() - self._max_age_days * 86400
        forgettable_keys: List[str] = []

        cursor = ""
        protected = 0

        while True:
            cursor, keys = self._storage.scan_fragment_keys(
                cursor=cursor,
                limit=self._batch_size,
                prefix="memory:frag:",
            )

            if keys:
                # 批量读一页（Redis=pipeline HGETALL，PG=`key = ANY` 一条 SQL）
                docs = self._storage.get_fragments_batch(keys)

                for key in keys:
                    stats["scanned"] += 1

                    doc = docs.get(key)
                    if not doc:
                        continue  # key 不存在或空

                    created_str = doc.get("created", "")
                    fb_str = doc.get("feedback_score", "")
                    sent_str = doc.get("sentiment_score", "")
                    frag_type = doc.get("fragment_type", "")
                    source = doc.get("source", "")
                    content = doc.get("content", "")

                    # ---- 保护规则 ----
                    # 1. 不删 consolidated 碎片
                    if frag_type == "consolidated":
                        protected += 1
                        continue

                    # 2. 不删用户手动存的 memory
                    if source == "hermes_agent":
                        fb = self._parse_float(fb_str, 0)
                        if fb >= 0:
                            protected += 1
                            continue

                    # 3. 不删正反馈碎片
                    fb = self._parse_float(fb_str, 0)
                    if fb > self._min_feedback_score:
                        protected += 1
                        continue

                    # ---- 年龄检查 ----
                    if created_str:
                        try:
                            created_ts = datetime.fromisoformat(created_str).timestamp()
                            if created_ts > cutoff_ts:
                                continue  # 还不够老
                        except (ValueError, TypeError):
                            pass

                    # ---- 情绪烈度检查 ----
                    intensity = self._parse_float(sent_str, 0)
                    if intensity >= self._min_intensity:
                        continue

                    # ---- 注意力检查 ----
                    if content:
                        try:
                            attn_w = self._storage.match_attention(content)
                            if attn_w and attn_w > 1.1:
                                continue  # 高关注度话题，保留
                        except Exception:
                            pass

                    # 所有条件都满足 → 可遗忘
                    forgettable_keys.append(key)

            if not cursor:
                break

        stats["skipped_protected"] = protected
        return forgettable_keys

    @staticmethod
    def _parse_float(val, default: float = 0.0) -> float:
        """安全转 float。"""
        if val is None or val == "":
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    def _find_forgettable_full(
        self,
        stats: Dict[str, Any],
    ) -> List[str]:
        """扫描完整记忆（memory:full:*），只按年龄判断是否可遗忘。

        🔴 2026-10 ks_pmn 取证结论：`memory:full:*` 是 2026-06-27（commit fab4c82
        "store full entries only"）就已删除写方的**遗留旁路** —— 现在
        `grep -rn memory:full src/` 只剩本函数。也就是说 Redis 侧这个 keyspace
        **恒为空**，本循环是个空转。

        PG 侧 `ks_fragment.content` 存的就是**未截断正文**（store() 直接写传入全文），
        与该旁路语义等价但无独立数据模型 ⇒ `scan_fragment_keys(prefix="memory:full:")`
        天然返回空，与 Redis 空 keyspace **行为等价**，不需要建表/加列。
        """
        now = datetime.now(timezone.utc)
        cutoff_ts = now.timestamp() - self._full_max_age_days * 86400
        forgettable_keys: List[str] = []

        cursor = ""
        while True:
            cursor, keys = self._storage.scan_fragment_keys(
                cursor=cursor,
                limit=self._batch_size,
                prefix="memory:full:",
            )
            if keys:
                docs = self._storage.get_fragments_batch(keys)
                for key in keys:
                    stats["scanned"] += 1
                    doc = docs.get(key) or {}
                    raw = doc.get("last_accessed") or doc.get("created") or ""
                    if not raw:
                        continue
                    try:
                        created_ts = float(raw)
                    except (ValueError, TypeError):
                        logger.debug("forgetter: skip full memory key %s: bad ts %r",
                                     key, raw)
                        continue
                    if created_ts > cutoff_ts:
                        continue  # 最近被访问过或创建不久，保留
                    forgettable_keys.append(key)
            if not cursor:
                break

        return forgettable_keys
