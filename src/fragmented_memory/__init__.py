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

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from .embedder import create_embedder
from .splitter import split_text
from .storage import RedisStorage
from .consolidator import Consolidator
from .forgetter import Forgetter

# ---------------------------------------------------------------------------
# 纠正检测 — 用户否定/纠正时降权前几轮碎片
# ---------------------------------------------------------------------------

_CORRECTION_PATTERNS = [
    '不对', '不是', '错了', '删除了', '回退',
    '不是这样', '说错了', '搞错了', '弄错了',
    '不对啊', '不对呀', '不对吧',
]

def _is_correction(text: str) -> bool:
    """检测用户消息是否为纠正/否定语气。"""
    for pattern in _CORRECTION_PATTERNS:
        if pattern in text:
            return True
    return False

# ---------------------------------------------------------------------------
# 工具扇区（供 Hermes MemoryProvider 注册）
# ---------------------------------------------------------------------------

FEEDBACK_SCHEMA = {
    "name": "frag_memory_feedback",
    "description": (
        "记录用户对一条碎片的反馈 — 标记有用/没用。"
        "正反馈让该碎片在未来搜索中排名更高，"
        "负反馈大幅降权（标记为没用的碎片几乎不会再出现）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fragment_key": {
                "type": "string",
                "description": "碎片的 Redis key（如 memory:frag:abc123），从相关碎片的 key 字段获得。",
            },
            "is_positive": {
                "type": "boolean",
                "description": "True = 这条记忆有用，False = 没用",
            },
        },
        "required": ["fragment_key", "is_positive"],
    },
}

HOT_TOPICS_SCHEMA = {
    "name": "frag_hot_topics",
    "description": (
        "查询全局热门话题统计。返回跨会话出现最频繁的话题词。"
        "可选日榜/周榜/全局。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "返回条数（默认 10，最大 30）",
                "default": 10,
            },
            "period": {
                "type": "string",
                "enum": ["all", "daily", "weekly"],
                "description": "统计周期：all=全局, daily=日榜, weekly=周榜",
                "default": "all",
            },
        },
        "required": [],
    },
}


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
    _consolidator: Optional[Consolidator] = None
    _forgetter: Optional[Forgetter] = None
    _last_maintenance: float = 0.0
    _maintenance_interval: float = 7200.0  # 每 2h 跑一次维护
    _recent_fragment_keys: List[str] = []
    _max_recent_keys: int = 30

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
        # 1. 硬编码默认值（不含 embedder — 由配置文件/环境变量按需开启）
        cfg: dict = {
            "redis_host": "127.0.0.1",
            "redis_port": 6379,
            "redis_password": "",
            "top_k": 5,
            "candidate_k": 10,
            "tag_filter": "",
            "synonym_min_word_freq": 10,
            "synonym_jaccard_threshold": 0.5,
            "synonym_min_co_occurrence": 3,
        }

        # 2. JSON 配置文件覆盖
        json_cfg = _load_json_config()
        cfg = _deep_merge(cfg, json_cfg)

        # 3. 环境变量覆盖
        env_overrides = {
            "redis_host": os.environ.get("FRAGMENTED_REDIS_HOST"),
            "redis_port": os.environ.get("FRAGMENTED_REDIS_PORT"),
            "redis_password": os.environ.get("FRAGMENTED_REDIS_PASSWORD"),
            "top_k": os.environ.get("FRAGMENTED_TOP_K"),
            "candidate_k": os.environ.get("FRAGMENTED_CANDIDATE_K"),
            "tag_filter": os.environ.get("FRAGMENTED_TAG_FILTER"),
            "agent_id": os.environ.get("FRAGMENTED_AGENT_ID"),
            "is_primary": os.environ.get("FRAGMENTED_IS_PRIMARY"),
        }
        for key, val in env_overrides.items():
            if val is not None:
                cfg[key] = val

        # 4. inline（Hermes 传入的 config.yaml 配置）覆盖
        cfg = _deep_merge(cfg, inline_cfg)

        # 5. 验证 agent_id 必须配置
        agent_id = cfg.get("agent_id")
        if agent_id is None or agent_id == "":
            raise ValueError("agent_id must be configured in config file, environment variable, or inline config")

        # 6. 解析 is_primary，默认为 false
        is_primary = cfg.get("is_primary", False)
        if isinstance(is_primary, str):
            is_primary = is_primary.lower() in ("true", "1", "yes", "on")
        cfg["is_primary"] = bool(is_primary)

        # 7. 加载 skip patterns 配置
        # skip_min_length: int，默认 2，从 config.json 的 skip_min_length 读取
        skip_min_length = cfg.get("skip_min_length", 2)
        cfg["skip_min_length"] = skip_min_length

        # skip_patterns_file: str，默认空字符串，从 config.json 的 skip_patterns_file 读取
        skip_patterns_file = cfg.get("skip_patterns_file", "")
        if skip_patterns_file:
            skip_patterns_file = Path(skip_patterns_file).expanduser()
            if skip_patterns_file.exists():
                try:
                    with open(skip_patterns_file) as f:
                        patterns = set()
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#"):
                                patterns.add(line.lower())
                    cfg["skip_patterns"] = patterns
                except Exception as e:
                    logger.warning("fragmented: failed to load skip patterns from %s: %s", skip_patterns_file, e)
            else:
                cfg["skip_patterns"] = set()
        else:
            cfg["skip_patterns"] = set()

        return cfg

    def _should_search(self, query: str) -> bool:
        """判断当前用户消息是否需要检索碎片。

        跳过条件：
          1. 长度 < skip_min_length（默认 2）
          2. query 精确匹配外部文件中的 skip pattern（忽略大小写）
        """
        q = query.strip()
        min_len = int(getattr(self, '_skip_min_length', 2))
        if len(q) < min_len:
            return False
        patterns = getattr(self, '_skip_patterns', [])
        if q.lower() in patterns:
            return False
        return True

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

        # 加载/重载 jieba 自定义词典（发 /new 时生效）
        from .splitter import init_domain_dict
        init_domain_dict()

        redis_host = cfg.get("redis_host", "127.0.0.1")
        redis_port = int(cfg.get("redis_port", 6379))
        top_k = int(cfg.get("top_k", 5))
        candidate_k = int(cfg.get("candidate_k", 10))
        self._tag_filter = cfg.get("tag_filter", "")

        embed_cfg = cfg.get("embedder", {})
        embed_provider = embed_cfg.get("provider", "").strip().lower()
        # 只有显式配置了 embedder provider 才创建，否则走 BM25-only 模式
        if embed_provider and embed_provider not in ("", "default", "none"):
            embedder = create_embedder(
                provider=embed_cfg.get("provider", ""),
                api_key=embed_cfg.get("api_key", ""),
                base_url=embed_cfg.get("base_url", ""),
                model=embed_cfg.get("model", ""),
            )
            embed_dim = embedder.dimension
            logger.info(
                "fragmented: embedder enabled (%s, dim=%d)",
                embed_provider, embed_dim,
            )
        else:
            embedder = None
            embed_dim = 1536
            logger.info("fragmented: BM25-only mode (no embedder configured)")

        self._storage = RedisStorage(
            embedder=embedder,
            host=redis_host,
            port=redis_port,
            password=cfg.get("redis_password") or None,
            candidate_count=candidate_k,
            final_limit=top_k,
            embed_dim=embed_dim,
            bm25_limit=int(cfg.get("bm25_limit", 10)),
            decay_half_days=int(cfg.get("decay_half_days", 60)),
            embed_cache_ttl=int(cfg.get("embed_cache_ttl", 3600)),
            sentiment_boost_positive=float(cfg.get("sentiment_boost_positive", 1.5)),
            sentiment_boost_negative=float(cfg.get("sentiment_boost_negative", 1.3)),
            feedback_positive_boost=float(cfg.get("feedback_positive_boost", 1.3)),
            feedback_negative_penalty=float(cfg.get("feedback_negative_penalty", 0.5)),
            hot_topic_boost=float(cfg.get("hot_topic_boost", 1.2)),
            hot_topic_decay_half_days=int(cfg.get("hot_topic_decay_half_days", 30)),
            emotion_intensity_factor=float(cfg.get("emotion_intensity_factor", 0.4)),
            attention_boost_max=float(cfg.get("attention_boost_max", 1.5)),
            attention_base_increment=float(cfg.get("attention_base_increment", 2.0)),
            attention_emotion_factor=float(cfg.get("attention_emotion_factor", 1.5)),
            agent_id=cfg.get("agent_id", ""),
            is_primary=cfg.get("is_primary", False),
            synonym_min_word_freq=int(cfg.get("synonym_min_word_freq", 10)),
            synonym_jaccard_threshold=float(cfg.get("synonym_jaccard_threshold", 0.5)),
            synonym_min_co_occurrence=int(cfg.get("synonym_min_co_occurrence", 3)),
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

        # 初始化 skip patterns 配置
        self._skip_min_length = cfg.get("skip_min_length", 2)
        self._skip_patterns = cfg.get("skip_patterns", set())

        # 初始化 Consolidator 和 Forgetter（守护模式）
        self._consolidator = Consolidator(
            storage=self._storage,
            min_group_size=int(cfg.get("consolidate_min_group", 2)),
            max_age_hours=int(cfg.get("consolidate_max_age_hours", 72)),
        )
        self._forgetter = Forgetter(
            storage=self._storage,
            max_age_days=int(cfg.get("forget_max_age_days", 30)),
            dry_run=bool(cfg.get("forget_dry_run", True)),
        )
        logger.info("fragmented: maintenance engines initialized")

    def system_prompt_block(self) -> str:
        parts = [
            "你有碎片化记忆系统（fragmented-memory），连接在 Redis + RediSearch 上。",
            "每次对话或 memory(action='add') 操作时，系统会自动检索或存储相关碎片。",
            "相关碎片就在下面「相关碎片」段落里，直接使用即可。",
            "碎片综合排序 = BM25相似度 × 时间衰减 × 情感权重 × 反馈权重 × 热门话题权重。",
            "正反馈用 frag_memory_feedback(key, positive=True) 标记有用，",
            "负反馈用 frag_memory_feedback(key, positive=False) 标记没用。",
            "热门话题用 frag_hot_topics() 查询。",
        ]
        return "\n".join(parts)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """根据用户消息检索相关碎片，注入到上下文。"""
        if not self._should_search(query):
            return ""

        if not query or not self._storage:
            return ""

        import time as _time

        # 检查工作流锁（复用 Redis client）
        lock_client = None
        try:
            lock_client = self._storage._get_client()
            if lock_client and lock_client.exists("fragmented:workflow_lock"):
                logger.debug("fragmented: workflow lock active, skipping search")
                return ""
        except Exception:
            pass

        start = _time.time()
        fragments = self._storage.search(
            query.strip(),
            tag_filter=self._tag_filter,
        )
        elapsed = _time.time() - start

        if not fragments:
            return ""

        # 获取完整记忆内容 + 溯源联想（取 top 3 碎片）
        full_memories: Dict[str, str] = {}
        try:
            client = lock_client or self._storage._get_client()
            if client:
                full_ids = set()
                for frag in fragments[:3]:  # 只处理前3个碎片
                    tags = frag.get("tags", "")
                    if isinstance(tags, str):
                        for tag in tags.split(","):
                            tag = tag.strip()
                            if tag.startswith("source:full:"):
                                full_ids.add(tag.split(":", 2)[2])

                if full_ids:
                    for fid in full_ids:
                        full_key = f"memory:full:{fid}"
                        full_data = client.hgetall(full_key)
                        if full_data:
                            raw_content = full_data.get(b"content") or full_data.get("content")
                            full_content = raw_content.decode("utf-8") if isinstance(raw_content, bytes) else (raw_content or "")
                            if full_content:
                                full_memories[f"source:full:{fid}"] = full_content
                                # 记录此次访问，保鲜该完整记忆
                                client.hset(full_key, "last_accessed", str(_time.time()))
                                # 用完整记忆再搜关联碎片（保留原有溯源联想逻辑）
                                related = self._storage.search(
                                    full_content[:200],
                                    tag_filter=self._tag_filter,
                                )
                                if related:
                                    existing = {f.get("content", "") for f in fragments}
                                    for r in related:
                                        if r.get("content", "") not in existing:
                                            fragments.append(r)
                                            existing.add(r.get("content", ""))
        except Exception:
            logger.warning("fragmented: full memory recall failed", exc_info=True)

        lines = ["<fragmented_memory>"]
        lines.append(f"# 相关碎片 (检索耗时 {elapsed:.1f}s)")
        lines.append("")
        for i, frag in enumerate(fragments, 1):
            lines.append(f"[{i}] {frag.get('content', '')}")
            # 检查是否有完整记忆
            tags = frag.get("tags", "")
            if isinstance(tags, str):
                for tag in tags.split(","):
                    tag = tag.strip()
                    if tag in full_memories:
                        text = full_memories[tag]
                        if len(text) > 500:
                            text = text[:500] + "..."
                        lines.append(f"    (完整记忆: {text})")
            combined = frag.get("_combined_score", 0)
            weights = frag.get("_weights", {})
            info_parts = []
            if tags:
                info_parts.append(f"标签: {tags}")
            info_parts.append(f"综合: {combined:.2f}")
            if weights:
                info_parts.append(f"w: sim={weights.get('sim',0):.2f} decay={weights.get('decay',0):.2f} "
                                  f"emotion={weights.get('emotion',1):.1f} fb={weights.get('feedback',1):.1f} "
                                  f"hot={weights.get('hot_topic',1):.1f}")
            # 情感标签可视化
            sent_label = frag.get("sentiment_label", "")
            if sent_label and sent_label != "neutral":
                sent_score = frag.get("sentiment_score", "0")
                icon = "😊" if sent_label == "positive" else "😠"
                info_parts.append(f"{icon} {sent_label}({sent_score})")
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
        """对话每轮结束后，将用户消息切分存档，并检测纠正标记。"""
        if not self._storage or not user_content or len(user_content.strip()) < 10:
            return

        raw_text = user_content.strip()
        full_id = hashlib.sha256(raw_text.encode()).hexdigest()[:8]
        full_key = f"memory:full:{full_id}"
        full_tag = f"source:full:{full_id}"

        # 存储完整原文到 Redis（供碎片溯源联想）
        try:
            client = self._storage._get_client()
            if client:
                import time as _time
                client.hset(full_key, mapping={
                    "content": raw_text,
                    "created": str(_time.time()),
                    "session_id": session_id,
                })
        except Exception:
            logger.warning("fragmented: failed to store full memory", exc_info=True)

        segments = split_text(raw_text)
        sid_short = session_id[:8] if session_id else "unknown"
        current_keys: List[str] = []
        for seg in segments:
            content_hash = hashlib.sha256(seg.encode()).hexdigest()[:12]
            key = f"memory:frag:{content_hash}"
            current_keys.append(key)
            self._storage.store(
                text=seg,
                tags=f"session:{sid_short},{full_tag}",
                category="conversation",
                source="sync_turn",
                fragment_type="conversation",
            )

        # 纠正检测：如果用户否定了前面的内容，降权 ring buffer 中之前的碎片
        if _is_correction(user_content) and self._recent_fragment_keys:
            self._storage.correct_fragments(self._recent_fragment_keys)

        # 更新 ring buffer（当前轮次加入，限制最大条目数）
        self._recent_fragment_keys.extend(current_keys)
        self._recent_fragment_keys = self._recent_fragment_keys[-self._max_recent_keys:]

        # 定期触发维护（Consolidation + Forget）
        self._maybe_maintain()

    def _maybe_maintain(self) -> None:
        """检查是否该执行维护，执行 Consolidation + Forget。"""
        import time as _time
        now = _time.time()
        if now - self._last_maintenance < self._maintenance_interval:
            return
        self._last_maintenance = now
        self.maintenance()

    def maintenance(self) -> Dict[str, Any]:
        """执行一轮完整维护：Consolidation → Forget。

        返回:
            维护统计
        """
        stats: Dict[str, Any] = {
            "consolidator": {"status": "skipped"},
            "forgetter": {"status": "skipped"},
        }

        # Step 1: Consolidation
        if self._consolidator:
            try:
                result = self._consolidator.consolidate()
                stats["consolidator"] = result
                logger.info("fragmented: consolidation done — %s", result)
            except Exception as e:
                logger.warning("fragmented: consolidation error: %s", e)
                stats["consolidator"] = {"status": "error", "reason": str(e)}

        # Step 2: Selective Forgetting
        if self._forgetter:
            try:
                result = self._forgetter.forget()
                stats["forgetter"] = result
                logger.info("fragmented: forgetting done — %s", result)
            except Exception as e:
                logger.warning("fragmented: forgetting error: %s", e)
                stats["forgetter"] = {"status": "error", "reason": str(e)}


        return stats

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [FEEDBACK_SCHEMA, HOT_TOPICS_SCHEMA]

    def handle_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        **kwargs,
    ) -> str:
        """Route tool calls to the appropriate handler."""
        import json as _json

        if tool_name == "frag_memory_feedback":
            return self._handle_feedback(args, _json)
        elif tool_name == "frag_hot_topics":
            return self._handle_hot_topics(args, _json)
        return tool_error(f"Unknown fragmented memory tool: '{tool_name}'")

    # ------------------------------------------------------------------
    # Tool handlers
    # ------------------------------------------------------------------

    def _handle_feedback(self, args: Dict[str, Any], _json) -> str:
        key = args.get("fragment_key", "")
        is_pos = bool(args.get("is_positive", True))
        if not key:
            return tool_error("fragment_key is required")
        if not self._storage:
            return tool_error("Memory storage not initialized")
        ok = self._storage.record_feedback(key, is_pos)
        if ok:
            action = "有用 👍" if is_pos else "没用 👎"
            return _json.dumps({"success": True, "action": action, "key": key})
        return tool_error("Failed to record feedback")

    def _handle_hot_topics(self, args: Dict[str, Any], _json) -> str:
        limit = min(int(args.get("limit", 10)), 30)
        period = args.get("period", "all")
        if not self._storage:
            return tool_error("Memory storage not initialized")
        topics = self._storage.get_hot_topics(limit=limit, period=period)
        return _json.dumps({"topics": topics, "count": len(topics)}, ensure_ascii=False)

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

        raw_text = content.strip()
        full_id = hashlib.sha256(raw_text.encode()).hexdigest()[:8]
        full_key = f"memory:full:{full_id}"
        full_tag = f"source:full:{full_id}"

        # 存储完整原文到 Redis（供碎片溯源联想）
        try:
            client = self._storage._get_client()
            if client:
                import time as _time
                client.hset(full_key, mapping={
                    "content": raw_text,
                    "created": str(_time.time()),
                    "target": target,
                })
        except Exception:
            logger.warning("fragmented: failed to store full memory on write", exc_info=True)

        for seg in split_text(raw_text):
            self._storage.store(
                text=seg,
                tags=f"{target},{full_tag}",
                category="memory_tool",
                source="hermes_agent",
                fragment_type="memory",
            )
