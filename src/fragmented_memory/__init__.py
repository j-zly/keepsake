"""
fragmented-memory — 碎片化记忆系统 for Hermes Agent.

每次对话自动检索相关记忆碎片注入上下文，支持：
  - ✂️ 语义切分 — 按段落/句子边界自动拆分成独立碎片
  - 🔍 向量搜索 — RediSearch KNN 语义检索
  - ⏳ 时间衰减 — 新碎片权重高，旧碎片逐步降权
  - 🔄 自动写入 — memory() 操作和对话轮次自动存档
  - 🏷️ 标签过滤 — 可选按标签范围搜索

安装: pip install fragmented-memory
激活: config.yaml 中设置 memory.provider: fragmented

配置优先级: 环境变量 > 配置文件 > 默认值
配置文件: ~/.config/fragmented-memory/config.json (或 FRAGMENTED_MEMORY_CONFIG 自定义路径)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

from .embedder import create_embedder
from .splitter import split_text
from .storage import RedisStorage

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = "~/.config/fragmented-memory/config.json"


def _load_json_config() -> dict:
    """从 JSON 配置文件加载配置。

    路径来源（优先级高到低）:
      1. 环境变量 FRAGMENTED_MEMORY_CONFIG
      2. ~/.config/fragmented-memory/config.json
    文件不存在时返回空 dict。
    """
    path_str = os.environ.get("FRAGMENTED_MEMORY_CONFIG") or _DEFAULT_CONFIG_PATH
    path = Path(path_str).expanduser()
    if not path.exists():
        logger.debug("fragmented: config file not found at %s", path)
        return {}
    try:
        with open(path) as f:
            cfg: dict = json.load(f)
        logger.info("fragmented: loaded config from %s", path)
        return cfg
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("fragmented: failed to load config from %s: %s", path, e)
        return {}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个 dict，override 覆盖 base。"""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


class FragmentedMemoryProvider(MemoryProvider):
    """
    碎片化记忆提供者。

    和 Hermes builtin 内存共存，不冲突。每轮对话自动检索相关碎片
    注入上下文，并自动将用户消息切分存档。

    配置优先级（高→低）:
      1. 环境变量 (FRAGMENTED_REDIS_HOST, FRAGMENTED_EMBEDDER 等)
      2. JSON 配置文件 (~/.config/fragmented-memory/config.json)
      3. config.yaml memory.fragmented 节（由 Hermes 传入）
      4. 硬编码默认值
    """

    _initialized: bool = False
    _storage: Optional[RedisStorage] = None
    _tag_filter: str = ""

    def __init__(self, **config):
        """
        参数（通过 config.yaml memory 节传入）:

            memory:
              provider: fragmented
              fragmented:
                redis_host: 127.0.0.1
                redis_port: 6379
                top_k: 5
                candidate_k: 10
                tag_filter: ""
                embedder:
                  provider: openai
                  api_key: sk-xxx
                  base_url: https://api.openai.com/v1
                  model: text-embedding-3-small
        """
        super().__init__()
        self._config = config

    # ------------------------------------------------------------------
    # 配置合并
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_config(inline_cfg: dict) -> dict:
        """按优先级合并配置源，返回最终配置。

        合并顺序（后覆盖前）: 默认值 ← JSON 文件 ← 环境变量 ← inline
        inline = Hermes 的 config.yaml memory.fragmented 或 __init__ 传参
        """
        # 1. 硬编码默认值
        cfg = {
            "redis_host": "127.0.0.1",
            "redis_port": 6379,
            "top_k": 5,
            "candidate_k": 10,
            "tag_filter": "",
            "embedder": {
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "text-embedding-3-small",
            },
        }

        # 2. JSON 配置文件覆盖
        json_cfg = _load_json_config()
        cfg = _deep_merge(cfg, json_cfg)

        # 3. 环境变量覆盖
        env_overrides = {
            "redis_host": os.environ.get("FRAGMENTED_REDIS_HOST"),
            "redis_port": os.environ.get("FRAGMENTED_REDIS_PORT"),
            "top_k": os.environ.get("FRAGMENTED_TOP_K"),
            "candidate_k": os.environ.get("FRAGMENTED_CANDIDATE_K"),
            "tag_filter": os.environ.get("FRAGMENTED_TAG_FILTER"),
        }
        for key, val in env_overrides.items():
            if val is not None:
                cfg[key] = val

        # 4. inline（Hermes 传入的 config.yaml 配置）覆盖
        cfg = _deep_merge(cfg, inline_cfg)

        return cfg

    # ------------------------------------------------------------------
    # MemoryProvider 接口
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "fragmented"

    def is_available(self) -> bool:
        try:
            import redis as _  # noqa: F401
        except ImportError:
            return False
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """初始化 — 加载配置、连接 Redis、自动创建 index。"""
        cfg = self._resolve_config(self._config)

        redis_host = cfg.get("redis_host", "127.0.0.1")
        redis_port = int(cfg.get("redis_port", 6379))
        top_k = int(cfg.get("top_k", 5))
        candidate_k = int(cfg.get("candidate_k", 10))
        self._tag_filter = cfg.get("tag_filter", "")

        embed_cfg = cfg.get("embedder", {})
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
            candidate_count=candidate_k,
            final_limit=top_k,
        )

        # 自动创建/验证 index
        if not self._storage.ensure_index():
            logger.warning(
                "fragmented: Redis / RediSearch not ready at %s:%s",
                redis_host, redis_port,
            )
            return

        self._initialized = True
        logger.info(
            "fragmented: connected (session=%s, top_k=%d, tag_filter=%s)",
            session_id, top_k, self._tag_filter or "(none)",
        )

    def system_prompt_block(self) -> str:
        parts = [
            "你有碎片化记忆系统（fragmented-memory），连接在 Redis + RediSearch 上。",
            "每次对话或 memory(action='add') 操作时，系统会自动检索或存储相关碎片。",
            "相关碎片就在下面「相关碎片」段落里，直接使用即可。",
            "碎片按语义相似度 + 时间权重综合排序，旧碎片权重逐步衰减。",
        ]
        return "\n".join(parts)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """根据用户消息检索相关碎片，注入到上下文。"""
        if not query or len(query.strip()) < 2 or not self._storage:
            return ""

        import time as _time
        start = _time.time()
        fragments = self._storage.search(
            query.strip(),
            tag_filter=self._tag_filter,
        )
        elapsed = _time.time() - start

        if not fragments:
            return ""

        lines = ["<fragmented_memory>"]
        lines.append(f"# 相关碎片 (检索耗时 {elapsed:.1f}s)")
        lines.append("")
        for i, frag in enumerate(fragments, 1):
            lines.append(f"[{i}] {frag.get('content', '')}")
            tags = frag.get("tags", "")
            combined = frag.get("_combined_score", 0)
            info_parts = []
            if tags:
                info_parts.append(f"标签: {tags}")
            info_parts.append(f"综合: {combined:.2f}")
            lines.append(f"    ({', '.join(info_parts)})")
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
