"""
fragmented-memory — 碎片化记忆系统 for Hermes Agent.

每次对话自动检索相关记忆碎片注入上下文，支持：
  - ✂️ 语义切分 — 按段落/句子边界自动拆分成独立碎片
  - 🔍 向量搜索 — RediSearch KNN 语义检索
  - ⏳ 时间衰减 — 新碎片权重高，旧碎片逐步降权
  - 🔄 自动写入 — memory() 操作和对话轮次自动存档

安装: pip install fragmented-memory
激活: config.yaml 中设置 memory.provider: fragmented
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

from .embedder import create_embedder
from .splitter import split_text
from .storage import RedisStorage

logger = logging.getLogger(__name__)


class FragmentedMemoryProvider(MemoryProvider):
    """
    碎片化记忆提供者。

    和 Hermes builtin 内存共存，不冲突。每轮对话自动检索相关碎片
    注入上下文，并自动将用户消息切分存档。
    """

    _initialized: bool = False
    _storage: Optional[RedisStorage] = None

    def __init__(self, **config):
        """
        参数（通过 config.yaml memory 节传入）:

            memory:
              provider: fragmented
              fragmented:
                redis_host: 127.0.0.1
                redis_port: 6379
                embedder:
                  provider: openai         # openai | dashscope
                  api_key: sk-xxx
                  base_url: https://api.openai.com/v1
                  model: text-embedding-3-small
        """
        super().__init__()
        self._config = config

    # ------------------------------------------------------------------
    # MemoryProvider 接口
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "fragmented"

    def is_available(self) -> bool:
        """检查关键依赖是否就绪。"""
        try:
            import redis as _  # noqa: F401
        except ImportError:
            return False
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """初始化 — 加载配置、连接 Redis、验证 index。"""
        frag_cfg = self._config.get("fragmented", {})
        redis_host = frag_cfg.get("redis_host", "127.0.0.1")
        redis_port = int(frag_cfg.get("redis_port", 6379))

        embed_cfg = frag_cfg.get("embedder", {})
        embedder = create_embedder(
            provider=embed_cfg.get("provider", ""),
            api_key=embed_cfg.get("api_key", ""),
            base_url=embed_cfg.get("base_url", ""),
            model=embed_cfg.get("model", ""),
        )

        self._storage = RedisStorage(
            embedder=embedder,
            host=redis_host,
            port=redis_port,
        )

        if not self._storage.check_index():
            logger.warning(
                "fragmented: Redis / RediSearch not ready at %s:%s. "
                "Make sure Redis with RediSearch module is running and "
                "the index '%s' exists.",
                redis_host, redis_port, RedisStorage.RS_INDEX,
            )
            return

        self._initialized = True
        logger.info("fragmented: connected (session=%s)", session_id)

    def system_prompt_block(self) -> str:
        return (
            "你有碎片化记忆系统（fragmented-memory），连接在 Redis + RediSearch 上。\n"
            "每次对话或 memory(action='add') 操作时，系统会自动检索或存储相关碎片。\n"
            "相关碎片就在下面「相关碎片」段落里，直接使用即可。\n"
            "碎片按语义相似度 + 时间权重综合排序，旧碎片权重逐步衰减。"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """根据用户消息检索相关碎片，注入到上下文。"""
        if not query or len(query.strip()) < 2 or not self._storage:
            return ""

        import time as _time
        start = _time.time()
        fragments = self._storage.search(query.strip())
        elapsed = _time.time() - start

        if not fragments:
            return ""

        lines = ["<fragmented_memory>"]
        lines.append(f"# 相关碎片 (检索耗时 {elapsed:.1f}s)")
        lines.append("")
        for i, frag in enumerate(fragments, 1):
            lines.append(f"[{i}] {frag.get('content', '')}")
            tags = frag.get("tags", "")
            decay = frag.get("_decay_score", 1.0)
            if tags:
                lines.append(f"    标签: {tags}  (时间权重: {decay:.2f})")
            lines.append("")

        lines.append("</fragmented_memory>")
        return "\n".join(lines)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """对话每轮结束后，将用户消息切分存档。"""
        if not self._storage or not user_content or len(user_content.strip()) < 10:
            return

        segments = split_text(user_content.strip())
        sid_short = session_id[:8] if session_id else "unknown"
        for seg in segments:
            self._storage.store(
                text=seg,
                tags=f"session:{sid_short}",
                category="conversation",
                source="sync_turn",
                fragment_type="conversation",
            )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def shutdown(self) -> None:
        if self._storage:
            self._storage.close()
        logger.info("fragmented memory provider shutdown")

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """builtin memory 写入时同步存到碎片库。"""
        if action != "add" or not content or not self._storage:
            return

        for seg in split_text(content):
            self._storage.store(
                text=seg,
                tags=target,
                category="memory_tool",
                source="hermes_agent",
                fragment_type="memory",
            )
