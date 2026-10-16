"""
keepsake — Keepsake记忆系统 for Hermes Agent.

每次对话自动检索相关记忆注入上下文，支持：
  - 🔍 向量搜索 — RediSearch KNN 语义检索
  - ⏳ 时间衰减 — 新记忆权重高，旧记忆逐步降权
  - 📝 自动写入 — memory(action='add') 操作自动存档完整内容
  - 🏷️ 标签过滤 — 可选按标签范围搜索

安装: pip install keepsake
激活: config.yaml 中设置 memory.provider: keepsake

配置优先级: 环境变量 > 配置文件 > 默认值
配置文件: ~/.config/keepsake/config.json (或 KEEPSAKE_CONFIG 自定义路径)
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from agent.memory_provider import MemoryProvider
    from tools.registry import tool_error
except ImportError:  # 独立测试环境回退；Hermes 实际运行时从 agent 包导入
    class MemoryProvider:
        def __init__(self, *args, **kwargs):
            pass

    def tool_error(msg):
        return f"[tool_error] {msg}"

from ...core.embedder import create_embedder
from ...core.redis import DEFAULT_BM25_LIMIT, RedisStorage, storage_from_config
from ...core.ops.forgetter import Forgetter
# 治理收口（2026-10 ks_agent_registry）：身份 / 角色 / 上下文的判定源。
# `govern` 只依赖标准库，这里 import 它不会与 `core/*` 成环。
from ...govern.registry import AGENT_TAG_PREFIX, Registry
from ...govern.registry import ROLE_PRIMARY as REGISTRY_ROLE_PRIMARY
# v2（2026-09）：写侧两相管线（Mem0 风格 + 封边取代）
from ...core.pipeline import Pipeline, DEFAULT_PIPELINE_CONFIG as _LLM_PIPELINE_DEFAULTS
# 通用契约的 Capabilities（依赖方向只准 hermes → generic）
from ..generic import Capabilities

# consolidator.py 的运行时接线已于 2026-09-09 退役：
#   * 两相管线 pipeline.py 已全面接管碎片提纯（提取相+更新相，封边取代旧合并）
#   * Consolidator 的「相似度分组 → LLM 缝合多级合并」与 v2 supersede 链互不相认
#   * 类源码保留（git 可溯），仅摘掉运行接线；resolve_llm_channel / _call_llm
#     仍从 keepsake.consolidator 模块按需导入，供 v2 pipeline 复用

# ---------------------------------------------------------------------------
# 工具扇区（供 Hermes MemoryProvider 注册）
# ---------------------------------------------------------------------------

FEEDBACK_SCHEMA = {
    "name": "keepsake_feedback",
    "description": (
        "记录用户对一条记忆的反馈 — 标记有用/没用。"
        "正反馈让该记忆在未来搜索中排名更高，"
        "负反馈大幅降权（标记为没用的记忆几乎不会再出现）。"
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
    "name": "keepsake_topics",
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


# 日志名**钉在搬家前的模块名上**：logger 名是对外可观测行为（外部 log 配置、
# caplog 过滤都按名找），搬目录不改名，否则等于悄悄改行为。
logger = logging.getLogger("keepsake")

_DEFAULT_CONFIG_PATH = "~/.config/keepsake/config.json"

# ---------------------------------------------------------------------------
# 常驻核心层（core tag）—— 2026-10 keepsake_core_tier_v1
#
# core 碎片 = `tags` 里含 `core` 的**普通碎片**（不新增表、不新增列，三后端共用）。
# ---------------------------------------------------------------------------

#: 核心层标记 tag。检索层见到它就跳过（`prefetch`），写入层见它就原文入库。
_CORE_TAG = "core"

#: **非 primary 写 core 的降级目标**（多代理治理）：子代理对常驻层只读，
#: 它写 `core` 只能落成候选（`core_candidate`），经审核后才由主脑转正。
#: 同时是主脑检索出口 `retrieval.exclude_tags` 的缺省排除标签。
CORE_CANDIDATE_TAG = "core_candidate"

#: 核心层缓存 TTL（秒）。每轮都查库没有意义 —— core 层是低频变的常驻记忆。
_CORE_CACHE_TTL = 60.0

#: **代理角色缓存 TTL（秒）**：写闸每轮都可能判定身份，而角色是低频变的。
#: 取 5s 是「approve / set_role 之后最多 5s 收敛」与「不在热路径上每轮查库」的折中。
_ROLE_CACHE_TTL = 5.0

#: 单次核心层读取的时间预算（秒）。超了就本轮不注入（fail-open），宁可少一轮
#: 上下文也不能让对话卡住 —— 记忆缺失是降级，卡死是事故。
_CORE_READ_BUDGET_S = 2.0

#: 核心层注入字符预算的**兜底默认值**。真实生效值走 `_core_budget()`
#: （读 config.json 的 `retrieval.core_char_budget`）—— 这个常量只在配置缺失/
#: 非法时兜底，不再是唯一真值源。MEMORY.md/USER.md 原先全量注入约 8 万字符，
#: 8 万 token 级的内容会挤爆上下文，所以设一个硬上限并**显式标注截断**。
_CORE_MAX_CHARS = 60000

#: 进程级缓存 `{"ts": 单调时刻, "frags": [{key, content, tags, updated_at}, ...]}`
_CORE_CACHE: Dict[str, Any] = {"ts": 0.0, "frags": None}

#: `retrieval.core_char_budget` 的进程内缓存 `{配置路径: (指纹, 预算)}`。
#: 指纹取 (mtime_ns, size) —— 照抄 `consolidator.resolve_llm_channel_cached` 的
#: 缓存手法（改了 config.json 下一轮注入就热生效，不必重启进程）。
_CORE_BUDGET_CACHE: Dict[str, Any] = {}


def config_path() -> Path:
    """本进程实际读取的 `config.json` 路径（`KEEPSAKE_CONFIG` > 默认）。

    控制台的预算设置端点写的就是这个文件 —— 读写两端必须同一条解析规则，
    各写一份 env 判断迟早变成「改了却不生效」。
    """
    return Path(os.environ.get("KEEPSAKE_CONFIG") or _DEFAULT_CONFIG_PATH).expanduser()


def _core_budget() -> int:
    """常驻层注入字符预算 —— **单点真值源**（注入路径与控制台口径共用本函数）。

    取 config.json 的 `retrieval.core_char_budget`；键缺失 / 非整数 / ≤0 ⇒ 回落
    `_CORE_MAX_CHARS` 并 warning（一次，每次配置变更至多一条）。

    🔴 按文件指纹（mtime_ns, size）缓存而不是每次重读：注入路径每轮对话都走，
    而 config.json 大概率不变；无脑重读是每轮一次多余的 stat+parse。指纹变了
    自然 miss ⇒ 改完配置下一轮就生效。文件不存在也缓存住（fp=None），避免
    「配置永久缺失」时每轮都 stat 一次。

    🔴 **同尺寸改值指纹可能不变**（20000→30000 在 mtime 粒度粗的文件系统上
    mtime_ns 一字不改），所以写配置的一侧必须调 `invalidate_core_budget_cache()`；
    光靠指纹在这一种情况下会「提示改成功了、注入预算还是老值」。
    """
    path = config_path()
    key = str(path)
    try:
        st = os.stat(path)
        fp = (getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), st.st_size)
    except OSError:
        fp = None

    hit = _CORE_BUDGET_CACHE.get(key)
    if hit is not None and hit[0] == fp:
        return hit[1]

    budget = _CORE_MAX_CHARS
    if fp is not None:
        try:
            with open(path) as f:
                raw = (json.load(f) or {}).get("retrieval", {}).get("core_char_budget")
        except (json.JSONDecodeError, OSError, AttributeError) as e:
            logger.warning("keepsake: config 读取失败（%s: %s），core 预算回落 %d",
                           path, e, _CORE_MAX_CHARS)
        else:
            # 只认 int 与「整数形态的字符串」。float/bool 一律拒：`int(1.5)` 会
            # 悄悄截成 1（预算变 1 字符 = 常驻层全被截掉），`int(True)` 是 1 同理。
            if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                n = 0
            else:
                try:
                    n = int(raw)
                except ValueError:
                    n = 0
            if n > 0:
                budget = n
            elif raw is not None:
                logger.warning("keepsake: retrieval.core_char_budget=%r 非法，回落 %d",
                               raw, _CORE_MAX_CHARS)
    _CORE_BUDGET_CACHE[key] = (fp, budget)
    return budget


def invalidate_core_budget_cache() -> None:
    """丢弃 `_core_budget()` 的进程内缓存（改完 config.json 的一侧必须调）。

    存在理由见 `_core_budget` 文档段：指纹是 (mtime_ns, size)，同尺寸改值可能
    指纹不变，光靠指纹会漏掉这一次热生效。
    """
    _CORE_BUDGET_CACHE.clear()


def _has_tag(tags: Any, tag: str) -> bool:
    """`tags` 字段（逗号串）里是否含 `tag` —— **逗号边界的完整标签相等**。

    与 `storage_shared.tag_match_sql`（PG/SQLite 的 SQL 判据）、
    `storage._tags_padded`（Redis 的 Python 判据）同一口径：`core` 不会命中
    `core-x` / `xcore`。三处判据必须同步改，否则「写进去了但检索排不掉」。
    """
    if not tags or not tag:
        return False
    norm = str(tags).replace("|", ",")
    for ch in ((" ,", ","), (", ", ",")):
        norm = norm.replace(*ch)
    return f",{tag}," in f",{norm},"


def _load_json_config() -> dict:
    """从 JSON 配置文件加载配置。

    路径来源（优先级高到低）:
      1. 环境变量 KEEPSAKE_CONFIG
      2. ~/.config/keepsake/config.json
    文件不存在时返回空 dict。
    """
    path_str = os.environ.get("KEEPSAKE_CONFIG") or _DEFAULT_CONFIG_PATH
    path = Path(path_str).expanduser()
    if not path.exists():
        logger.debug("keepsake: config file not found at %s", path)
        return {}
    try:
        with open(path) as f:
            cfg: dict = json.load(f)
        logger.info("keepsake: loaded config from %s", path)
        return cfg
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("keepsake: failed to load config from %s: %s", path, e)
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


def _build_query_expansion_llm_fn(cfg: dict):
    """构造一个 LLM callable 给 query_expansion 用。

    返回 (messages, model) -> Optional[str]。失败时返回 None（不抛），
    避免后台线程把 daemon 拖死。

    实现：
        * 复用 consolidator.resolve_llm_channel_cached（按 mtime 热生效）
        * 复用 consolidator._call_llm（统一错误处理 / 重试）
        * 通道 unconfigured 或 key 缺失 → 返回 None
    """
    from ...core.ops.consolidator import resolve_llm_channel_cached, _call_llm

    def _call(messages, model):
        try:
            ch = resolve_llm_channel_cached(cfg)
        except Exception as e:
            logger.debug("keepsake: qexp resolve_llm_channel failed: %s", e)
            return None
        if not ch.get("valid"):
            return None
        try:
            return _call_llm(messages, model, channel=ch, max_retries=0)
        except Exception as e:
            logger.debug("keepsake: qexp _call_llm raised: %s", e)
            return None

    return _call


class KeepsakeProvider(MemoryProvider):
    """
    Keepsake记忆提供者。

    和 Hermes builtin 内存共存，不冲突。每轮对话自动检索相关记忆
    注入上下文。仅 memory(action='add') 操作时存储完整内容。

    配置优先级（高→低）:
      1. 环境变量 (KEEPSAKE_REDIS_HOST, KEEPSAKE_EMBEDDER 等)
      2. JSON 配置文件 (~/.config/keepsake/config.json)
      3. config.yaml memory.keepsake 节（由 Hermes 传入）
      4. 硬编码默认值
    """

    _initialized: bool = False
    _storage: Optional[RedisStorage] = None
    _tag_filter: str = ""
    _is_primary: bool = False
    #: 主脑检索出口要剔除的噪音标签（`retrieval.exclude_tags`，初始化时读入 +
    #: 治理收口后并入注册表里标了 `exclude_from_primary` 的代理标签）。
    #: 类属性兜底 = 未 initialize 的实例（如测试直接挂 storage）不排除任何东西。
    _exclude_tags: Tuple[str, ...] = ()
    _forgetter: Optional[Forgetter] = None
    _last_maintenance: float = 0.0
    _maintenance_interval: float = 7200.0  # 每 2h 跑一次维护
    #: 代理注册表（`keepsake.govern.registry.Registry`）：身份 / 角色 / 上下文的唯一真相源。
    #: **类属性兜底 None** = 没装配注册表（裸实例 / 单测），此时写闸回落到 config 自报。
    _registry: Optional[Any] = None
    #: 本进程自己的 agent_id（注册表登记与角色查询用）
    _agent_id: str = ""
    #: 角色查询的进程内缓存 `{"ts": 单调时刻, "is_primary": bool}`
    _role_cache: Optional[Dict[str, Any]] = None

    def __init__(self, **config):
        """
        参数（通过 config.yaml memory 节传入）:

            memory:
              provider: keepsake
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
        inline = Hermes 的 config.yaml memory.keepsake 或 __init__ 传参
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
            "entity_cooc_top_n": 3,
            "entity_cooc_min_count": 2,
            # 写闸门 v1（2026-09）：enabled 默认开、max_len 默认 2000
            "ingest_gate": {"enabled": True, "max_len": 2000},
            # v2 写侧两相管线（2026-09）：默认开启，缺 LLM 时自动回落 v1
            "llm_pipeline": dict(_LLM_PIPELINE_DEFAULTS),
            # v2 检索侧相似度地板（按 _sim 归一化）
            "v2_min_score": 0.05,
            # LLM 通道配置（2026-09 ks_noqwen）：base_url/model/key_file/api_key；
            # 空节/缺字段 = 无有效 LLM 通道 → pipeline 不启动 + 调用方走 v1 兜底
            # （不再硬编码回落付费模型——见 consolidator.resolve_llm_channel）
            "llm": {},
            # 2026-09 ks_retr：LLM 查询扩展（治词汇鸿沟；热路径零拖慢）
            # enabled 默认开；缓存未命中且结果 < min_results 才起后台扩展
            "retrieval": {
                "query_expansion": {
                    "enabled": True,
                    "min_results": 3,
                    "max_terms": 6,
                    "cache_ttl": 86400,
                },
            },
        }

        # 2. JSON 配置文件覆盖
        json_cfg = _load_json_config()
        cfg = _deep_merge(cfg, json_cfg)

        # 3. 环境变量覆盖
        env_overrides = {
            "redis_host": os.environ.get("KEEPSAKE_REDIS_HOST"),
            "redis_port": os.environ.get("KEEPSAKE_REDIS_PORT"),
            "redis_password": os.environ.get("KEEPSAKE_REDIS_PASSWORD"),
            "top_k": os.environ.get("KEEPSAKE_TOP_K"),
            "candidate_k": os.environ.get("KEEPSAKE_CANDIDATE_K"),
            "tag_filter": os.environ.get("KEEPSAKE_TAG_FILTER"),
            "agent_id": os.environ.get("KEEPSAKE_AGENT_ID"),
            "is_primary": os.environ.get("KEEPSAKE_IS_PRIMARY"),
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

        # 6.1 解析 `retrieval.exclude_tags`（主脑检索出口的噪音排除，逗号分隔标签串）
        # 缺省 = CORE_CANDIDATE_TAG（子代理写 core 降级出来的候选层不进主脑检索）；
        # 显式空串 = 不排除。None（键存在但没值）按缺省处理，容错写法照抄 is_primary。
        raw_exclude = cfg.get("retrieval", {}).get("exclude_tags", CORE_CANDIDATE_TAG)
        if raw_exclude is None:
            raw_exclude = CORE_CANDIDATE_TAG
        cfg["exclude_tags"] = [t.strip() for t in str(raw_exclude).split(",") if t.strip()]

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
                    logger.warning("keepsake: failed to load skip patterns from %s: %s", skip_patterns_file, e)
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
        return "keepsake"

    def is_available(self) -> bool:
        try:
            import redis as _  # noqa: F401
        except ImportError:
            return False
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """初始化 — 加载配置、连接 Redis、自动创建 index。

        2026-09 ks_pipefix：把合并完成的 cfg 深拷贝存到 self._resolved_config。
        修复前 _init_pipeline 用 self._config（Hermes 传入的 inline 原始配置，
        不带 llm 节）解析 channel，导致 v2 pipeline 错判 unconfigured 不启动。
        修复后 _init_pipeline 读 self._resolved_config，与日志/同步链保持单源。
        _channel_refresher / resolve_llm_channel_cached 按文件路径 mtime 重读，
        不依赖此属性，不受影响。
        """
        cfg = self._resolve_config(self._config)
        # 深拷贝：防后续步骤或外部测试修改 cfg 时反向污染 _config 链
        # （测试里 monkeypatch inline 后再 resolve 会得到新 dict，本属性不变）
        self._resolved_config = copy.deepcopy(cfg)

        # 加载/重载 jieba 自定义词典（发 /new 时生效）
        from ...domain.splitter import init_domain_dict
        init_domain_dict()

        redis_host = cfg.get("redis_host", "127.0.0.1")
        redis_port = int(cfg.get("redis_port", 6379))
        top_k = int(cfg.get("top_k", 5))
        candidate_k = int(cfg.get("candidate_k", 10))
        self._tag_filter = cfg.get("tag_filter", "")
        # 多代理治理：非 primary 写 core 的降级闸；主脑检索出口的噪音排除
        self._is_primary = bool(cfg.get("is_primary", False))
        self._exclude_tags = tuple(cfg.get("exclude_tags", (CORE_CANDIDATE_TAG,)))

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
                "keepsake: embedder enabled (%s, dim=%d)",
                embed_provider, embed_dim,
            )
        else:
            embedder = None
            embed_dim = 1536
            logger.info("keepsake: BM25-only mode (no embedder configured)")

        # 2026-10 ks_pg_b1：后端由 `storage.backend` 决定（缺省 redis = 行为不变）。
        # 显式传 redis_cls=RedisStorage —— 这样 `keepsake.RedisStorage` 的
        # monkeypatch 仍能拦到构造（既有测试靠它打桩，不连真实 Redis）。
        self._storage = storage_from_config(
            config=cfg,
            redis_cls=RedisStorage,
            embedder=embedder,
            host=redis_host,
            port=redis_port,
            password=cfg.get("redis_password") or None,
            candidate_count=candidate_k,
            final_limit=top_k,
            embed_dim=embed_dim,
            # 🔴 2026-10 ks_recall_v1：候选池默认值与三个后端的 DEFAULT_BM25_LIMIT
            # 同源（此前这里写死 10，storage.py 侧写死 20，两处默认值本身就不一致）。
            # 旧配置键 `bm25_limit` 不变，配了就以配置为准。
            bm25_limit=int(cfg.get("bm25_limit", DEFAULT_BM25_LIMIT)),
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
            entity_cooc_top_n=int(cfg.get("entity_cooc_top_n", 3)),
            entity_cooc_min_count=int(cfg.get("entity_cooc_min_count", 2)),
            # v2（2026-09）：检索侧相似度地板，按 _sim 归一化值过滤
            v2_min_score=float(cfg.get("v2_min_score", 0.05)),
            # 2026-09 ks_retr：LLM 查询扩展配置（热路径零延迟）
            query_expansion_enabled=bool(
                cfg.get("retrieval", {}).get("query_expansion", {}).get("enabled", True)
            ),
            query_expansion_min_results=int(
                cfg.get("retrieval", {}).get("query_expansion", {}).get("min_results", 3)
            ),
            query_expansion_max_terms=int(
                cfg.get("retrieval", {}).get("query_expansion", {}).get("max_terms", 6)
            ),
            query_expansion_ttl=int(
                cfg.get("retrieval", {}).get("query_expansion", {}).get("cache_ttl", 86400)
            ),
            # 2026-09 ks_retr：把 LLM 通道绑成 query_expansion_llm_fn
            # （通道未配置/无效 → 传 None → 后台扩展调用静默失败，不影响热路径）
            query_expansion_llm_fn=_build_query_expansion_llm_fn(cfg),
        )

        # 自动创建/验证 index
        if not self._storage.ensure_index():
            # 2026-10 ks_pg_b1：探活改走后端中立的 health_check()，
            # 不再在消息里假定后端一定是 Redis（后端由 storage.backend 决定）
            logger.warning(
                "keepsake: 存储后端 %s 未就绪（health_check=%s, redis %s:%s）",
                type(self._storage).__name__, self._storage.health_check(),
                redis_host, redis_port,
            )
            return

        self._initialized = True
        logger.info(
            "keepsake: connected (session=%s, top_k=%d, tag_filter=%s)",
            session_id, top_k, self._tag_filter or "(none)",
        )

        # 治理收口（2026-10 ks_agent_registry）：自登记 + 角色归注册表 + 迁移退役键
        self._init_governance(cfg)

        # 初始化 skip patterns 配置
        self._skip_min_length = cfg.get("skip_min_length", 2)
        self._skip_patterns = cfg.get("skip_patterns", set())

        # 写闸门配置（ingest_gate v1，2026-09）
        self._gate_cfg = cfg.get("ingest_gate", {"enabled": True, "max_len": 2000})

        # 解析 LLM 通道（base_url/model/api_key）—— 2026-09 ks_noqwen 起无 llm 节 = unconfigured
        # Consolidator 退役后保留此单独 import：函数仍从 consolidator 模块取，
        # 但不再构造 Consolidator 实例。
        from ...core.ops.consolidator import resolve_llm_channel
        llm_channel = resolve_llm_channel(cfg)

        # Consolidator 退役（2026-09-09）：提纯职能由 v2 两相管线（pipeline.py）接管，
        # 其 supersede 封边链与 Consolidator 的「相似度分组 → LLM 缝合」互不相认。
        # 仅保留 Forgetter（守护模式）。
        self._forgetter = Forgetter(
            storage=self._storage,
            max_age_days=int(cfg.get("forget_max_age_days", 30)),
            dry_run=bool(cfg.get("forget_dry_run", True)),
        )
        logger.info("keepsake: maintenance engines initialized (Consolidator retired)")

        # v2（2026-09）：写侧两相管线 — 仅在 enabled 且有 LLM 时启动
        self._init_pipeline(cfg.get("llm_pipeline", {}) or {})

        # 自动注册定时任务（仅在首次启动时创建）
        self._ensure_cron_jobs()

    # ------------------------------------------------------------------
    # 定时任务自动注册
    # ------------------------------------------------------------------

    CRON_JOBS = [
        {
            "name": "memory-maintenance",
            "script": "memory-maintenance.py",
            "schedule": {"kind": "interval", "minutes": 120, "display": "every 120m"},
        },
        {
            "name": "synonym-discovery-daily",
            "script": "discover_synonyms.py",
            "schedule": {"kind": "cron", "expr": "0 */8 * * *", "display": "0 */8 * * *"},
        },
        {
            "name": "记忆去重",
            "script": "dedup-memory.sh",
            "schedule": {"kind": "cron", "expr": "0 * * * *", "display": "0 * * * *"},
        },
    ]

    @staticmethod
    def _ensure_cron_jobs() -> None:
        """启动时自动注册三条定时任务（不存在才创建），并确保脚本文件到位。"""
        cron_dir = Path("~/.hermes/cron").expanduser()
        scripts_dir = Path("~/.hermes/scripts").expanduser()
        jobs_file = cron_dir / "jobs.json"

        if not jobs_file.exists():
            logger.info("keepsake: cron jobs.json not found, skipping auto-register")
            return

        # 读取现有任务
        try:
            data = json.loads(jobs_file.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("keepsake: failed to read jobs.json")
            return

        jobs = data.get("jobs", [])
        existing_names = {j.get("name") for j in jobs}

        # 获取插件包的 cron/ 目录（作为脚本源）
        pkg_cron = Path(__file__).resolve().parent.parent.parent / "cron"

        now = datetime.now(timezone.utc).isoformat()

        for jdef in KeepsakeProvider.CRON_JOBS:
            name = jdef["name"]
            script = jdef["script"]
            if name in existing_names:
                continue

            # 确保脚本文件存在
            target = scripts_dir / script
            if not target.exists() and pkg_cron.exists():
                src = pkg_cron / script
                if src.exists():
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(src.read_text())
                        target.chmod(0o755)
                        logger.info("keepsake: installed cron script %s", script)
                    except OSError as e:
                        logger.warning("keepsake: failed to install %s: %s", script, e)

            new_job = {
                "id": uuid.uuid4().hex[:12],
                "name": name,
                "prompt": "",
                "skills": [],
                "skill": None,
                "model": None,
                "provider": None,
                "base_url": None,
                "script": script,
                "no_agent": True,
                "context_from": None,
                "schedule": jdef["schedule"].copy(),
                "schedule_display": jdef["schedule"]["display"],
                "repeat": {"times": None, "completed": 0},
                "enabled": True,
                "state": "scheduled",
                "paused_at": None,
                "paused_reason": None,
                "created_at": now,
                "next_run_at": None,
                "last_run_at": None,
                "last_status": None,
                "last_error": None,
                "last_delivery_error": None,
                "deliver": "local",
                "origin": None,
                "enabled_toolsets": None,
                "workdir": None,
                "profile": None,
                "fire_claim": None,
            }
            jobs.append(new_job)
            logger.info("keepsake: auto-registered cron job '%s'", name)

        if any(jdef["name"] not in existing_names for jdef in KeepsakeProvider.CRON_JOBS):
            data["jobs"] = jobs
            data["updated_at"] = now
            try:
                jobs_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                logger.info("keepsake: cron jobs.json updated")
            except OSError as e:
                logger.warning("keepsake: failed to write jobs.json: %s", e)

    def system_prompt_block(self) -> str:
        parts = [
            "你有Keepsake记忆系统（keepsake），连接在 Redis + RediSearch 上。",
            "当执行 memory(action='add') 操作时，系统会自动存储完整内容并支持后续检索。",
            "相关的记忆条目就在下面「相关记忆」段落里，直接使用即可。",
            "记忆综合排序 = BM25相似度 × 时间衰减 × 情感权重 × 反馈权重 × 热门话题权重。",
            "正反馈用 keepsake_feedback(key, positive=True) 标记有用，",
            "负反馈用 keepsake_feedback(key, positive=False) 标记没用。",
            "热门话题用 keepsake_topics() 查询。",
        ]
        block = self._core_block()
        if block:
            parts.append("")
            parts.append(block)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 常驻核心层（core tag）—— 2026-10 keepsake_core_tier_v1
    #
    # 两套记忆并存的根因：Hermes 本体每轮全量注入 MEMORY.md/USER.md（约 8 万字符），
    # keepsake 只做按查询召回的「相关记忆」段落。本层把常驻记忆收进碎片库，
    # 让 keepsake 成为**单一记忆系统**：core 碎片每轮原文全量注入，
    # 其余碎片仍走 prefetch 检索。
    #
    # 🔴 **本层绝不抛异常、绝不阻塞对话**（fail-open）：
    #   * 读库异常 / 耗时超预算 → 只返回静态文案 + logger.warning
    #   * 截断时必须留一行可见的截断说明，不许静默丢条目
    # 理由：`system_prompt_block` 在 Hermes 每轮组装 system prompt 的关键路径上，
    # 在这里抛出去等于整轮对话失败 —— 记忆是增强项，不该有能力让对话瘫掉。
    # ------------------------------------------------------------------

    def _core_fragments(self) -> List[Dict[str, Any]]:
        """取核心层碎片，带 60s 进程内缓存；任何异常/超时都退化成空列表 + warning。

        缓存是进程级而非实例级：core 层在一次对话里只应查一次库，而 Hermes
        可能为多 session 构造 provider 实例。
        """
        import time as _time

        now = _time.time()
        cached = _CORE_CACHE.get("frags")
        if cached is not None and now - _CORE_CACHE["ts"] < _CORE_CACHE_TTL:
            return cached
        try:
            start = _time.time()
            frags = list(self._storage.list_by_tag(_CORE_TAG))
            elapsed = _time.time() - start
        except Exception as e:
            logger.warning("keepsake: core 层读取失败（fail-open，只注入静态文案）: %s", e)
            return []
        if elapsed > _CORE_READ_BUDGET_S:
            logger.warning("keepsake: core 层读取耗时 %.1fs 超预算 %.1fs（fail-open，本轮不注入）",
                           elapsed, _CORE_READ_BUDGET_S)
            return []
        _CORE_CACHE["ts"] = now
        _CORE_CACHE["frags"] = frags
        return frags

    def _core_block(self) -> str:
        """渲染 `<keepsake-core>…</keepsake-core>` 块；无内容时返回空串（不留空行）。"""
        if not self._storage:
            return ""
        frags = self._core_fragments()
        if not frags:
            return ""
        lines = [f"- {f.get('content', '')}" for f in frags]
        budget = _core_budget()
        if sum(len(x) for x in lines) > budget:
            # 按 list_by_tag 的稳定顺序（updated_at 升序、key 升序）**从头截** ——
            # 老条目是常驻记忆里最该被无条件看到的那部分，末尾的超预算条目才丢。
            kept, total = [], 0
            for line in lines:
                if total + len(line) > budget:
                    break
                kept.append(line)
                total += len(line)
            lines = kept + [f"[core 层已截断：共 {len(frags)} 条，本次注入 {len(kept)} 条]"]
            logger.warning("keepsake: core 层超 %d 字符预算，截断为 %d 条",
                           budget, len(kept))
        return "\n".join(["<keepsake-core>", *lines, "</keepsake-core>"])

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """根据用户消息检索相关碎片，注入到上下文。"""
        if not self._should_search(query):
            return ""

        if not query or not self._storage:
            return ""

        import time as _time

        # 检查工作流锁（后端无关探测：Redis 读 keepsake:workflow_lock key；
        # PG 后端没有这张「全局开关表」⇒ 恒 False = 锁不存在，放行检索）
        try:
            if self._storage.fragment_exists("keepsake:workflow_lock"):
                logger.debug("keepsake: workflow lock active, skipping search")
                return ""
        except Exception as e:
            logger.debug("keepsake: workflow lock probe failed, proceeding: %s", e)

        start = _time.time()
        fragments = self._storage.search(
            query.strip(),
            tag_filter=self._tag_filter,
        )
        elapsed = _time.time() - start

        # core 碎片由 system_prompt_block 无条件全量注入，这里必须排除 ——
        # 否则同一条记忆每轮出现两次（一次全量、一次检索），且两份可能不同版本。
        fragments = [f for f in fragments if not _has_tag(f.get("tags", ""), _CORE_TAG)]
        fragments = self._filter_excluded(fragments)

        if not fragments:
            return ""

        lines = ["<keepsake>"]
        lines.append(f"# 相关记忆 (检索耗时 {elapsed:.1f}s)")
        lines.append("")
        for i, frag in enumerate(fragments, 1):
            lines.append(f"[{i}] {frag.get('content', '')}")
            tags = frag.get("tags", "")
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

        lines.append("</keepsake>")
        return "\n".join(lines)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """v2（2026-09）：格式闸门 → v2 pipeline 入队 → 兜底 v1 decide() 直存。"""
        if not self._storage or not user_content or not user_content.strip():
            return
        text = user_content.strip()
        decision, existing_meta = self._gate_decide(text, "turn_memory")
        if decision.action == "reject":
            return
        pipeline = getattr(self, "_pipeline", None)
        if pipeline is not None and getattr(pipeline, "_llm_fn", None) is not None:
            try:
                pipeline.enqueue(text, assistant_content or "")
                return
            except Exception as e:
                logger.warning("keepsake: pipeline.enqueue failed (%s); falling back to v1", e)
        self._v1_store_after_decide(text, decision, existing_meta, "turn_memory", "hermes_agent", "")

    def _v1_fallback_store(self, user_text: str, category: str = "turn_memory") -> None:
        """v2 pipeline 失败时的窗口级兜底（与 sync_turn 的 v1 路径同源）。"""
        if not self._storage or not user_text or not user_text.strip():
            return
        decision, existing_meta = self._gate_decide(user_text.strip(), category)
        if decision.action == "reject":
            return
        self._v1_store_after_decide(user_text.strip(), decision, existing_meta, category, "pipeline_v2_fallback", "fallback:v1")

    def _gate_decide(self, text: str, category: str):
        """R1-R7 闸门裁决 + 探测同 hash 既有 meta（v1 与 v2 共享）。"""
        from ...core.ingest_gate import decide
        frag_key = f"memory:frag:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        existing_meta = None
        try:
            if self._storage.fragment_exists(frag_key):
                existing_meta = {"key": frag_key}
        except Exception as e:
            # ⚠️ 不再静默：探测异常会让 R6 去重失效（同一内容会被当新碎片再存一次）。
            #   降级本身可接受（fail-open），但必须留痕，否则读日志看不出「去重没跑」。
            logger.warning("keepsake: 既有碎片探测失败，R6 去重本次跳过 (%s)", e)
        return decide(text, category, existing_meta, getattr(self, "_gate_cfg", None)), existing_meta

    def _v1_store_after_decide(self, text, decision, existing_meta, category, source, extra_tags) -> None:
        """decide() 之后直存（含 update_state 闭环；v1 与 v2 兜底共享）。

        R7 接线：store 路径入库前先 scrub_secrets（仅 store 路径；
        update_state 路径只刷时间戳，按任务书约定不 scrub）。
        """
        from ...core.ingest_gate import scrub_secrets, update_state_only
        if decision.action == "update_state":
            update_state_only(self._storage, existing_meta)
            return
        # R7：入库前凭据打码（sync_turn v1 + v2 兜底共用此点）
        try:
            scrubbed_text, _n_masks = scrub_secrets(text)
            if scrubbed_text:
                text = scrubbed_text
        except Exception as e:
            logger.debug("keepsake: scrub_secrets failed in _v1_store_after_decide: %s", e)
        try:
            tags = "conversation" + ("," + extra_tags if extra_tags else "")
            self._storage.store(text=text, tags=tags, category=category, source=source,
                                fragment_type="memory")
        except Exception as e:
            logger.warning("keepsake: _v1_store_after_decide failed: %s", e)

    def _init_pipeline(self, llm_pipe_cfg: dict) -> None:
        """v2（2026-09）：构建 Pipeline 实例并启动 daemon（LLM 不可用则跳过）。

        2026-09 ks_noqwen 变更：
          * 移除硬编码 model 字段兜底；配置无效直接走 v1 fallback
          * 注入 channel_refresher 给 Pipeline，每次 _drain_now 开头按 mtime 重读
            config.json → 改完下一处理窗口生效，无需重启网关

        2026-09 ks_pipefix 变更：
          * channel 解析源由 self._config（Hermes 传入的 inline 原始配置，
            通常无 llm 节）改为 self._resolved_config（initialize() 里合并完成的
            cfg，含 config.json 的 llm 节）。这是关键修复——修前 inline 空 + 文件
            有节时会被错判 unconfigured，v2 pipeline 永不启动。
          * _channel_refresher / resolve_llm_channel_cached 按文件路径 mtime
            重读，不依赖 self._resolved_config，不受影响。
        """
        self._pipeline = None
        if not llm_pipe_cfg.get("enabled", True):
            return
        from ...core.ops.consolidator import _call_llm, resolve_llm_channel
        # ks_pipefix：用合并后的 cfg（含 config.json 的 llm 节）解析 channel，
        # 不要再用 self._config（仅含 inline）
        llm_channel = resolve_llm_channel(self._resolved_config)
        if not llm_channel.get("valid"):
            # 缺 base_url/model/api_key 之一 → 无 LLM 通道 → pipeline 不启动
            logger.warning(
                "keepsake: v2 pipeline disabled (llm channel unconfigured, source=%s); falls back to v1",
                llm_channel.get("source", "?"),
            )
            return

        # functools.partial 绑定 channel —— llm_fn 仍为 callable，测试 mock 不受影响
        import functools
        llm_fn = functools.partial(_call_llm, channel=llm_channel)

        # 热生效：每次 _drain_now 开头按 mtime 重读 config.json，重解析 channel，
        # 用新 channel 跑本窗。channel_refresher 返回 (channel_dict, llm_fn)。
        from ...core.ops.consolidator import resolve_llm_channel_cached

        def _channel_refresher():
            # 显式读磁盘 → 走 mtime 缓存路径
            fresh = resolve_llm_channel_cached()
            new_llm_fn = functools.partial(_call_llm, channel=fresh)
            return (fresh, new_llm_fn)

        # model 默认值：config.json llm.model 优先；管道配置项次之；空 → 不预设
        initial_model = (
            llm_pipe_cfg.get("model")
            or llm_channel.get("model")
            or ""
        )

        self._pipeline = Pipeline(
            storage=self._storage, llm_fn=llm_fn,
            model=initial_model,
            window_pairs=int(llm_pipe_cfg.get("window_pairs", 4)),
            window_seconds=float(llm_pipe_cfg.get("window_seconds", 30.0)),
            max_calls_per_window=int(llm_pipe_cfg.get("max_calls_per_window", 8)),
            update_top_k=int(llm_pipe_cfg.get("update_top_k", 5)),
            recent_context_size=int(llm_pipe_cfg.get("recent_context_size", 8)),
            gate_fallback=self._v1_fallback_store,
            channel_refresher=_channel_refresher,
        )
        self._pipeline.start()
        logger.info("keepsake: v2 pipeline started (channel=%s)", llm_channel.get("source", "?"))

    def _maybe_maintain(self) -> None:
        """检查是否该执行维护，执行 Forget（Consolidator 已退役）。"""
        import time as _time
        now = _time.time()
        if now - self._last_maintenance < self._maintenance_interval:
            return
        self._last_maintenance = now
        self.maintenance()

    def maintenance(self) -> Dict[str, Any]:
        """执行一轮完整维护：Forget（Consolidator 已退役）。

        Consolidator 退役（2026-09-09）：提纯职能由 v2 两相管线（pipeline.py）接管。
        这里仍保留 `consolidator` 字段以便 cron/health 探针可观测（status=retired），
        不再触发任何合并循环。

        返回:
            维护统计
        """
        stats: Dict[str, Any] = {
            # Consolidator 退役（2026-09-09）：保留字段供观测，无运行接线
            "consolidator": {"status": "retired", "reason": "v2 pipeline takeover"},
            "forgetter": {"status": "skipped"},
        }

        # Step 1: Selective Forgetting（Consolidator 已摘除，仅剩 Forget）
        if self._forgetter:
            try:
                result = self._forgetter.forget()
                stats["forgetter"] = result
                logger.info("keepsake: forgetting done — %s", result)
            except Exception as e:
                logger.warning("keepsake: forgetting error: %s", e)
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

        if tool_name == "keepsake_feedback":
            return self._handle_feedback(args, _json)
        elif tool_name == "keepsake_topics":
            return self._handle_hot_topics(args, _json)
        return tool_error(f"Unknown keepsake memory tool: '{tool_name}'")

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
        # v2（2026-09）：先排空管线，再关 storage；保证 shutdown 前队列内消息不丢
        pipeline = getattr(self, "_pipeline", None)
        if pipeline is not None:
            try:
                pipeline.stop(drain=True, timeout=5.0)
            except Exception as e:
                logger.warning("keepsake: pipeline stop failed: %s", e)
        if self._storage:
            self._storage.close()
        logger.info("keepsake memory provider shutdown")

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Hermes 常驻记忆（MEMORY.md / USER.md）写入时，**原文**存为 core 碎片。

        2026-10 keepsake_core_tier_v1：这是把常驻记忆收进 keepsake 的唯一入口 ——
        与本方法原先的「直接 return」（2026-09 ingest_gate R5 关闭双份入库）相反，
        现在两侧合并成同一套：常驻记忆存进碎片库并打上 `core` 标签，
        `system_prompt_block` 每轮原文全量注入，检索层不再重复召回。

        🔴 **原文逐字入库，绝不经 LLM 提炼 / 摘要改写**：常驻记忆是 agent 行为的
        硬约束（「端口是 8091」被改写成「用户偏好某端口」就是记忆损坏）。
        仍走 `scrub_secrets` 打码 —— 那是安全闸（R7），不是内容改写。

        🔴 **任何异常都吞掉 + warning，绝不影响 Hermes 主流程**：本方法由 Hermes
        在 memory 工具的成功路径上同步调用，抛出去会让用户的记忆写入失败。
        """
        try:
            self._core_write(action, target, content, metadata or {})
        except Exception as e:
            logger.warning("keepsake: on_memory_write(%s/%s) 失败（已吞掉）: %s",
                           action, target, e)

    def _core_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Dict[str, Any],
    ) -> None:
        """`on_memory_write` 的实际动作。**调用方保证异常已被吞掉。**

        action 语义（对齐 Hermes memory 工具）：
          * `add`                  → 入库为 core 碎片
          * `replace` / `update`    → 先删旧（metadata 的 key 或 old_text）再写新
          * `remove`                → 删掉 metadata 指的那条
        识别不了的 action 一律 warning + 跳过，绝不猜。
        """
        if not self._storage:
            return
        action = (action or "").strip().lower()
        # remove 不需要新内容：Hermes 对 remove 传的 content 是空串，在这里早退会让
        # memory(action=remove) 变成完全无效的操作（2026-10-12 实测：删除从未生效）。
        if action != "remove" and (not content or not content.strip()):
            return

        # R7：入库前凭据打码（与 _v1_store_after_decide 同一入口，同一口径）。
        # 只打码、不改写其余文字 —— 原文入库是硬要求。
        from ...core.ingest_gate import scrub_secrets
        try:
            if content:
                scrubbed, _n = scrub_secrets(content)
                if scrubbed and scrubbed.strip():
                    content = scrubbed
        except Exception as e:
            logger.debug("keepsake: scrub_secrets failed in _core_write: %s", e)

        tags = f"{_CORE_TAG},{target or 'memory'}"

        if action == "remove":
            self._core_delete(metadata, content or "")
        elif action in ("replace", "update"):
            # 先删旧再写新。找不到旧条目时退化成「新增」—— 用户重写一条记忆
            # 而 Hermes 没给 old_text/key 时，这是唯一不丢内容的做法。
            self._core_delete(metadata, metadata.get("old_text") or "")
            self._core_add(content, tags)
        elif action == "add":
            self._core_add(content, tags)
        else:
            logger.warning("keepsake: on_memory_write action=%r 无法识别，跳过", action)
            return

        # 写入/删除成功 ⇒ 核心层缓存立即失效（下一轮立刻看到新内容）。
        _CORE_CACHE["ts"] = 0.0
        _CORE_CACHE["frags"] = None

    def _core_add(self, content: str, tags: str) -> None:
        """原文入库为 core 碎片；同 content 已存在则不重复入库（幂等）。

        幂等判据用「现存 core 碎片里有逐字相同的一条」而不是靠 `store()` 的
        内容 hash 去重 —— 后者对 PG/SQLite 的版本化语义不保证，而常驻记忆
        被重复注入是要用户能看出来的故障。
        """
        for f in self._core_fragments():
            if f.get("content") == content:
                return
        self._storage.store(
            text=content,
            tags=tags,
            category="core",
            source="builtin_memory",
            fragment_type="core",
        )

    def _core_delete(self, metadata: Dict[str, Any], fallback_text: str) -> None:
        """删掉 metadata 指向的那条 core 碎片。

        解析顺序（Hermes 侧契约见 agent/memory_manager.notify_memory_tool_write）：

          1. `metadata["key"]` —— 明确指定，直接用；
          2. `metadata["previous_content"]` —— 镜像带过来的**旧条目全文**，精确匹配；
          3. `old_text` / fallback —— 先精确全文，再**唯一子串**匹配。

        🔴 第 3 步的子串匹配是必需的：Hermes memory 工具规定 `old_text` 是「一段能唯一定位
        条目的短子串」而不是全文。只做全文相等 ⇒ replace/remove 时旧条目删不掉、新条目另存
        一条，同一事实库里两份（2026-10-12 实测 core 里已有这样一对）。

        命中多条时**不猜**：记 warning 并跳过 —— 宁可不删，也不能删错记忆。
        完全没命中时同样只 warning：replace 会退化成新增（既有语义），remove 则等于没删，
        由 Hermes 那侧决定重试。
        """
        key = metadata.get("key")
        if not key:
            candidates = [t.strip() for t in (metadata.get("previous_content"),
                                              metadata.get("old_text"), fallback_text)
                          if t and t.strip()]
            if not candidates:
                return
            frags = self._core_fragments()
            key = self._resolve_core_key(frags, candidates)
            if not key:
                logger.warning(
                    "keepsake: core 删除未命中（old_text=%r，候选 %d 个，core %d 条）——"
                    "replace 会退化成新增，remove 等于没删",
                    candidates[-1][:80], len(candidates), len(frags))
        if key:
            self._storage.delete_fragments_batch([key])

    @staticmethod
    def _resolve_core_key(frags: List[Dict[str, Any]], candidates: List[str]) -> Optional[str]:
        """候选文本 → 唯一 core key：逐候选先全文精确，再唯一子串；多命中不猜（返回 None）。"""
        def _norm(value: Optional[str]) -> str:
            return (value or "").strip()

        for text in candidates:
            needle = _norm(text)
            for frag in frags:
                if _norm(frag.get("content")) == needle:
                    return frag.get("key")
        for text in candidates:
            needle = _norm(text)
            if not needle:
                continue
            hits = [f for f in frags if needle in (f.get("content") or "")]
            if len(hits) == 1:
                return hits[0].get("key")
            if len(hits) > 1:
                logger.warning("keepsake: core 删除文本命中 %d 条（%r）——歧义，跳过以免删错",
                               len(hits), needle[:60])
                return None
        return None

    # ------------------------------------------------------------------
    # 通用记忆契约（adapters/generic/protocol.py）—— 2026-10 keepsake_p0_layout_v1
    #
    # 下面这组方法**不参与 Hermes 的每轮对话路径**：Hermes 走的是
    # system_prompt_block / prefetch / sync_turn / on_memory_write。
    # 它们是把同一个存储能力按「与宿主无关的契约」再暴露一遍，供通用适配层
    # （别的宿主、非 Hermes 的调用方）使用。新增，不改动任何既有路径。
    #
    # 失败语义（protocol.py 里的三条铁律，此处逐条落地）：
    #   * 注入层 fail-open —— system_prompt_block / prefetch 见上文，本组不涉及
    #   * 检索层显式抛错 —— search / get **不 try/except**，后端异常原样抛出，
    #     绝不塌成空列表（静默空 = 记忆搜不到且无告警）
    #   * 写层 fail-closed —— add / update / remove 捕获后端异常并返回 falsy +
    #     warning，绝不谎报成功
    # ------------------------------------------------------------------

    def _require_storage(self):
        """取 storage；未 initialize 时抛错（读路径按铁律「显式抛错」处理）。"""
        if not self._storage:
            raise RuntimeError(
                "keepsake: storage 未初始化 —— 先调用 initialize(session_id=...)"
            )
        return self._storage

    @staticmethod
    def _fragment_key_for(text: str) -> str:
        """碎片 key = `memory:frag:{sha256(text)[:12]}`。

        三个后端的 `store()` 用的是同一套算法（Redis / SQLite 内联，
        PG 抽成了 `PgStorage._fragment_key_for`），这里为了让 `add()` 能把
        key 交回给调用方而复刻一行。⚠️ 改任一后端的算法必须同步改这里 ——
        `tests/contract/test_provider_protocol.py` 用真 SqliteStorage
        跑 add→get 做交叉校验，改歪了会红。
        """
        return f"memory:frag:{hashlib.sha256(text.encode()).hexdigest()[:12]}"

    # ---- 检索层（显式抛错）----

    def _filter_excluded(self, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """剔除带 `retrieval.exclude_tags` 噪音标签的命中（多代理治理的第三条规则）。

        `search()` / `prefetch()` 两个检索出口统一走这里 —— 命中形态是
        `{"content":…, "tags": "逗号串", …}`，标签判据复用 `_has_tag`
        （逗号边界的完整标签相等，与存储层 SQL/Python 判据同口径）。
        排除发生在截断**之前**：不让噪音挤掉本该进上下文的名额。
        """
        tags = self._exclude_tags
        if not tags:
            return hits
        return [h for h in hits
                if not any(_has_tag(h.get("tags", ""), t) for t in tags)]

    def search(self, query: str, *, limit: int = 10) -> List[Dict[str, Any]]:
        """按查询检索，返回最多 `limit` 条命中。

        🔴 显式抛错：后端不可达 / 查询非法都让异常往外走，**绝不返回 []** ——
        空列表在契约里只有一个含义「确实没有匹配」。
        """
        storage = self._require_storage()
        hits = self._filter_excluded(
            list(storage.search(query.strip(), tag_filter=self._tag_filter)))
        self._touch("read")      # 活动计数：主脑每轮都在检索，这条路径必须记
        return hits[:limit]

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """按 key 取单条碎片；**确实不存在**返回 None，后端故障抛错。"""
        storage = self._require_storage()
        row = storage.get_fragment(key)
        self._touch("read")
        return row

    # ---- 写层（fail-closed）----

    # ------------------------------------------------------------------
    # 治理收口（2026-10 ks_agent_registry）
    # ------------------------------------------------------------------

    def _init_governance(self, cfg: dict) -> None:
        """装配代理注册表：自登记 → 本机 bootstrap → 迁移退役的 exclude 键。

        **绝不因治理失败而拦住对话**（与本文件其余注入路径同款 fail-open）：
        注册表不可用时 `self._registry` 留 None，写闸据此 fail-closed 按 sub
        （见 `_registry_is_primary`）—— 记忆写入降级，人还能用。
        """
        try:
            registry = Registry(self._storage)
            if not registry.ensure_schema():
                logger.warning("keepsake: 注册表 schema 未就绪，写闸将按 sub 处理（fail-closed）")
                return
            self._agent_id = str(cfg.get("agent_id") or self._agent_id or "")
            # 本机文件通道：批准自己；`is_primary` 为真且库里还没有主脑时才上位。
            # 🔴 这一行的 `is_primary` 只是**首次引导**的种子，之后角色一律由注册表
            #    说了算（控制台改一次即生效，写闸 5s 内收敛）。
            registry.bootstrap_local(self._agent_id, self._agent_id,
                                     as_primary=bool(cfg.get("is_primary", False)))
            registry.migrate_exclude_tags(cfg)
            self._registry = registry
            self._role_cache = None
            # 「是否进主脑上下文」现在是每个代理身上的开关：被排除的代理，
            # 它的 `agent:<id>` 标签进检索排除表（配置键退役后的新家）。
            extra = tuple(
                f"{AGENT_TAG_PREFIX}{a['agent_id']}"
                for a in registry.list_agents() if a["exclude_from_primary"]
            )
            if extra:
                self._exclude_tags = tuple(dict.fromkeys(self._exclude_tags + extra))
            logger.info("keepsake: 代理注册表已装配（agent_id=%s）", self._agent_id)
        except Exception as e:      # noqa: BLE001 —— 治理不拦对话（写闸 fail-closed 兜底）
            logger.warning("keepsake: 注册表装配失败（写闸按 sub 处理）: %s: %s",
                           type(e).__name__, e)

    def _registry_is_primary(self) -> bool:
        """**我是不是主脑** —— 判定源是注册表角色（5s 缓存），不是 config 自报。

        🔴 三条 fail-closed 纪律：
          * 没装配注册表（裸实例 / 单测）⇒ 回落 `_is_primary`（保持既有装配行为）；
          * 注册表里查不到 / 不是 primary ⇒ sub；
          * **注册表抛异常 ⇒ sub**（宁可多拦一个 core 写入，也不放进一个假 primary）。
        """
        registry = self._registry
        if registry is None:
            return bool(self._is_primary)
        import time as _time

        now = _time.monotonic()
        cache = self._role_cache
        if cache is not None and now - cache["ts"] < _ROLE_CACHE_TTL:
            return bool(cache["is_primary"])
        try:
            info = registry.verify_local(self._agent_id)
            is_primary = bool(info.get("ok")) and info.get("role") == REGISTRY_ROLE_PRIMARY
        except Exception as e:      # noqa: BLE001 —— fail-closed 按 sub
            logger.warning("keepsake: 注册表角色查询失败（按 sub 处理）: %s", type(e).__name__)
            is_primary = False
        self._role_cache = {"ts": now, "is_primary": is_primary}
        return is_primary

    def _touch(self, kind: str) -> None:
        """打活动计数（`reads` / `writes`）—— **本机 provider 侧的记账点**。

        🔴 为什么主脑路径必须自己打（2026-10 计数排查）：`Registry.touch()`
        原先只有一个调用点 —— MCP 闸门 `server.py:237` 的 `record_activity`。
        那是**子代理**经 MCP 连上来时的路径；主脑是同进程直接调 provider 的
        `search()` / `add()`，**从不经过 MCP 闸门**，于是它的 reads/writes 恒为 0
        （面板上「主代理 0/0」的由来）。挂点补在这里，语义与 MCP 那侧逐字一致。

        三条 fail-open 纪律（与本文件其余注入路径同款）：
          * 没装配注册表（裸实例 / 单测）⇒ 直接返回；
          * `touch()` 抛异常 ⇒ 吞掉记日志，**绝不让计数把记忆读写带崩**；
          * 限频 60s 在 `Registry.touch()` 内部，这里不重复实现。
        """
        registry = self._registry
        if registry is None or not self._agent_id:
            return
        try:
            registry.touch(self._agent_id, kind)
        except Exception as e:      # noqa: BLE001 —— 计数是遥测，不是账本
            logger.debug("keepsake: 活动计数失败（忽略）: %s: %s", type(e).__name__, e)

    def _apply_write_gate(self, tags: str) -> str:
        """写闸：非 primary 写 `core` 一律降级为 `core_candidate`（多代理治理第二条）。

        🔴 2026-10 ks_agent_registry：判定源已从 config 自报的 `_is_primary`
        切到 **registry 的角色**（`self._registry_is_primary()`，5s 缓存；
        注册表不可达时 fail-closed 按 sub —— 见那处的三条纪律）。

        子代理对常驻层**只读** —— 它写 `core` 不能直接进常驻层，只能落候选，
        经审核后由主脑转正。标签判据是**逗号边界的完整标签相等**（复用 `_has_tag`，
        `core_x` / `xcore` 不算命中），其余标签原样保留、顺序不变。
        primary 原样放行；返回 key 语义不受影响。
        """
        if self._registry_is_primary() or not _has_tag(tags, _CORE_TAG):
            return tags
        parts = [t.strip() for t in str(tags).split(",") if t.strip()]
        kept = [CORE_CANDIDATE_TAG if t == _CORE_TAG else t for t in parts]
        logger.warning(
            "keepsake: 非 primary 写 core 降级为 %s（tags: %r → %r）",
            CORE_CANDIDATE_TAG, tags, ",".join(kept))
        return ",".join(kept)

    def add(
        self,
        text: str,
        *,
        tags: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """写入一条记忆，返回它的 key；写入失败返回空串（falsy）。

        `meta` 目前只承载 `source` / `category` / `fragment_type` 三个字段 ——
        它是契约里预留的扩展位，不认识的键直接忽略，不报错。
        """
        meta = meta or {}
        try:
            storage = self._require_storage()
            ok = storage.store(
                text,
                tags=self._apply_write_gate(tags),
                category=str(meta.get("category", "")),
                source=str(meta.get("source", "")),
                fragment_type=str(meta.get("fragment_type", "")),
            )
        except Exception as e:
            logger.warning("keepsake: add 失败（fail-closed，返回空串）: %s", e)
            return ""
        if not ok:
            logger.warning("keepsake: add 未落库（fail-closed，返回空串）")
            return ""
        self._touch("write")     # 落库成功才记：写失败的尝试不是写入活动
        return self._fragment_key_for(text)

    def update(
        self,
        key: str,
        *,
        content: Optional[str] = None,
        tags: Optional[str] = None,
    ) -> bool:
        """局部更新一条碎片。两个参数都为 None 时**什么都不做**并返回 False。"""
        fields: Dict[str, Any] = {}
        if content is not None:
            fields["content"] = content
        if tags is not None:
            fields["tags"] = tags
        if not fields:
            return False
        try:
            ok = bool(self._require_storage().update_fragment_fields(key, fields))
        except Exception as e:
            logger.warning("keepsake: update(%s) 失败（fail-closed，返回 False）: %s", key, e)
            return False
        if ok:
            self._touch("write")
        return ok

    def remove(self, target: str) -> bool:
        """按 key 删除；key 不存在或后端失败返回 False。"""
        try:
            ok = bool(self._require_storage().delete_fragments_batch([target]))
        except Exception as e:
            logger.warning("keepsake: remove(%s) 失败（fail-closed，返回 False）: %s", target, e)
            return False
        if ok:
            self._touch("write")
        return ok

    # ---- 维护层 ----

    def merge(self, *, dry_run: bool = True) -> Dict[str, Any]:
        """合并。Consolidator 的运行接线已于 2026-09-09 退役（v2 两相管线接管提纯），
        所以这里**恒定报告 retired**，既不扫描也不写库 —— 一个字都不动。
        """
        stats = {"status": "retired", "reason": "v2 pipeline takeover",
                 "dry_run": bool(dry_run), "merged": 0, "groups": 0}
        logger.info("keepsake: merge() 被调用 —— consolidator 已退役，不做任何合并")
        return stats

    def forget(self, *, dry_run: bool = True) -> Dict[str, Any]:
        """遗忘。`dry_run=True`（默认）走 forgetter 自带的 dry_run 口径，
        只统计不删；`dry_run=False` 传 force=True 强制实际删除。"""
        if not self._forgetter:
            return {"status": "skipped", "reason": "forgetter not initialized"}
        return self._forgetter.forget(force=not dry_run)

    def capabilities(self) -> Capabilities:
        """能力声明。core 层与 KNN 是 keepsake 的既定能力，开箱即有。"""
        return Capabilities(core_tier=True, knn=True, dry_run_merge=True, version="v1")
