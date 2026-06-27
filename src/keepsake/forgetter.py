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
        """
        client = self._storage._get_client()
        if not client:
            return {"status": "error", "reason": "Redis not available"}

        stats = {
            "scanned": 0,
            "candidates": 0,
            "deleted": 0,
            "skipped_protected": 0,
            "dry_run": self._dry_run and not force,
        }

        forgettable = self._find_forgettable(client, stats)

        # 扫描完整记忆（memory:full:*），只按年龄判断
        forgettable_full = self._find_forgettable_full(client, stats)
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
        deleted = 0
        for key in forgettable:
            try:
                client.delete(key)
                deleted += 1
            except Exception as e:
                logger.debug("forgetter: delete %s failed: %s", key, e)

        stats["deleted"] = deleted
        logger.info(
            "forgetter: deleted %d/%d forgettable fragments",
            deleted, len(forgettable),
        )
        return stats

    def _find_forgettable(
        self,
        client,
        stats: Dict[str, Any],
    ) -> List[str]:
        """扫描并筛选可遗忘的碎片。"""
        now = datetime.now(timezone.utc)
        cutoff_ts = now.timestamp() - self._max_age_days * 86400
        forgettable_keys: List[str] = []

        cursor = 0
        protected = 0

        while True:
            cursor, keys = client.scan(
                cursor=cursor,
                match="memory:frag:*",
                count=self._batch_size,
            )

            if not keys:
                if cursor == 0:
                    break
                continue

            # 用 pipeline 批量 HMGET，减少网络往返
            pipe = client.pipeline()
            hmget_fields = ["created", "feedback_score", "sentiment_score",
                            "fragment_type", "source", "category", "content"]
            for key_b in keys:
                pipe.hmget(key_b, hmget_fields)
            pipe_results = pipe.execute()

            for key_b, fields in zip(keys, pipe_results):
                key = key_b.decode("utf-8") if isinstance(key_b, bytes) else key_b
                stats["scanned"] += 1

                if not fields or not any(fields):
                    continue  # key 不存在或空

                def _d(v):
                    if v is None:
                        return ""
                    return v.decode("utf-8") if isinstance(v, bytes) else str(v)

                created_str = _d(fields[0])
                fb_str = _d(fields[1])
                sent_str = _d(fields[2])
                frag_type = _d(fields[3])
                source = _d(fields[4])
                category = _d(fields[5])
                content = _d(fields[6])

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

            if cursor == 0:
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
        client,
        stats: Dict[str, Any],
    ) -> List[str]:
        """扫描完整记忆（memory:full:*），只按年龄判断是否可遗忘。"""
        now = datetime.now(timezone.utc)
        cutoff_ts = now.timestamp() - self._full_max_age_days * 86400
        forgettable_keys: List[str] = []

        cursor = 0
        while True:
            cursor, keys = client.scan(
                cursor=cursor,
                match="memory:full:*",
                count=self._batch_size,
            )
            for key_b in keys:
                key = key_b.decode("utf-8") if isinstance(key_b, bytes) else key_b
                stats["scanned"] += 1
                try:
                    created_data = client.hget(key, "last_accessed") or client.hget(key, "created")
                    if not created_data:
                        continue
                    created_ts = float(created_data)
                    if created_ts > cutoff_ts:
                        continue  # 最近被访问过或创建不久，保留
                    forgettable_keys.append(key)
                except Exception as e:
                    logger.debug("forgetter: skip full memory key %s: %s", key, e)
                    continue
            if cursor == 0:
                break

        return forgettable_keys
