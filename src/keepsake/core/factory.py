"""存储后端工厂 —— `storage_from_config()` 的唯一实现。

2026-11 Redis 后端退役时从 `core/redis.py` 拆出：工厂、后端判定、排序权重键
口径本就是**后端无关**的，此前寄居在 Redis 实现文件里，导致「删 Redis」必须先
搬家。本模块不 import 任何具体后端类（延迟导入在分支里），新后端接入 = 加一条
分支，不再欠「先删别的后端」的技术债。

对外契约（2026-11 定档，破坏性变更）：
  * 合法后端只剩 `postgres` / `sqlite` 两个；
  * 🔴 **没有默认后端**。缺 `storage.backend` / 空 / 非法 / 写 `redis`
    ⇒ `ValueError` 人话报错。旧版「缺省回 redis」的兜底随 Redis 后端一起删除
    —— 静默兜底到一个不存在的后端比报错严重得多（记忆会写丢）。
兼容层 `keepsake.storage` 模块别名指向本模块（`from keepsake.storage import
storage_from_config` 的既有 cron/脚本逐字可用）。
"""

from __future__ import annotations

import json as _json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .embedder import Embedder, create_embedder, resolve_embedder_api_key
from .base import StorageBase
from .shared import (
    DECAY_HALF_DAYS,
    FEEDBACK_NEGATIVE_PENALTY,
    FEEDBACK_POSITIVE_BOOST,
    HOT_TOPIC_BOOST,
    HOT_TOPIC_DECAY_HALF_DAYS,
    max_content_len_from_config,
    resolve_max_content_len,
)

# 日志名**钉在搬家前的模块名上**：logger 名是对外可观测行为（外部 log 配置、
# caplog 过滤都按名找），搬目录不改名，否则等于悄悄改行为。
logger = logging.getLogger("keepsake.storage")

DEFAULT_KEEPSAKE_CONFIG = str(Path.home() / ".config" / "keepsake" / "config.json")

#: 配置路径的环境变量（既有键，provider / 控制台 / 班车共用一份口径）。
CONFIG_PATH_ENV = "KEEPSAKE_CONFIG"


def config_path() -> Path:
    """本进程实际读取的 ``config.json`` 路径（``KEEPSAKE_CONFIG`` > 默认路径）。

    **单点口径**：provider / 控制台 / 班车（``keepsake.buses``）都走这里 ——
    读写两端各写一份 env 判断，迟早出现「写的是 A 文件、班车读的是 B 文件」。
    """
    return Path(os.environ.get(CONFIG_PATH_ENV) or DEFAULT_KEEPSAKE_CONFIG).expanduser()


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """读**整份** config.json。

    * 文件不存在 ⇒ 空 dict（缺省配置）；
    * 文件坏了 / 读不了 ⇒ 抛 ``ValueError``（**不静默当缺省**：配置坏了还按
      缺省跑 = 班车按另一套阈值干活，比报错危险得多）。
    """
    path = Path(path or config_path())
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ValueError(f"配置文件读不了: {path}（{type(e).__name__}: {e}）") from e
    try:
        cfg = _json.loads(raw) if raw.strip() else {}
    except _json.JSONDecodeError as e:
        raise ValueError(f"配置文件不是合法 JSON: {path}（{e}）") from e
    if not isinstance(cfg, dict):
        raise ValueError(f"配置文件根节点必须是 JSON 对象: {path}")
    return cfg

# ---- 后端选择（2026-11：redis 已移除，见模块 docstring 的契约） ----
BACKEND_POSTGRES = "postgres"
BACKEND_SQLITE = "sqlite"
_VALID_BACKENDS = (BACKEND_POSTGRES, BACKEND_SQLITE)

#: BM25 候选池默认值（工厂两条分支共用；与 pg.py 的同名常量同值，
#: 历史沿革见 core/redis.py@1.1.x 的 ks_recall_v1 注释 —— 20→200 的教训不变）。
DEFAULT_BM25_LIMIT = 200


def _build_embedder(cfg: Dict[str, Any], path: Any) -> Optional[Embedder]:
    """按配置 `embedder` 段造 embedder；没有/构造失败 → None（只影响向量路）。

    抽出来是为了让两条 SQL 分支**同源**：2026-10 ks_pg_b2 之前 PG 分支
    压根不造 embedder，导致 PG 侧永远没有向量、KNN 静默搜不到。
    """
    emb_cfg = dict(cfg.get("embedder") or {})
    if not emb_cfg.get("model"):
        logger.warning("storage: %s 无可用 embedder 段 → 写入的记忆不会有向量", path)
        return None
    try:
        return create_embedder(
            provider=emb_cfg.get("provider", "openai"),
            # 凭据：内联 api_key 优先，其次 api_key_file（控制台「AI 配置」页
            # 写的就是文件型；口径见 core.embedder.resolve_embedder_api_key）
            api_key=resolve_embedder_api_key(emb_cfg),
            base_url=emb_cfg.get("base_url", ""),
            model=emb_cfg.get("model", ""),
        )
    except Exception as e:
        logger.warning("storage: embedder 构造失败（本次写入将无向量）: %s", e)
        return None


def resolve_backend(config: Optional[Dict[str, Any]] = None) -> str:
    """从配置解析后端名。**2026-11 起不再兜底** —— 解析不出就抛，绝不猜。

    旧口径是「缺行/空/非法一律回 redis」（当年怕「后端选错=记忆静默写丢」）。
    Redis 后端删除后，这条兜底会把人静默引向一个**不存在**的后端，比报错严重，
    所以连同默认值一起拆掉：`storage.backend` 必须是 `postgres` / `sqlite` 之一。
    写 `redis` 给专门的迁移报错（旧库数据的出路见 README「从 Redis 迁移」）。
    """
    storage_cfg = (config or {}).get("storage")
    raw = storage_cfg.get("backend") if isinstance(storage_cfg, dict) else None
    backend = raw.strip().lower() if isinstance(raw, str) else ""
    if backend in _VALID_BACKENDS:
        return backend
    if backend == "redis":
        raise ValueError(
            "storage.backend=redis 已不再支持（2026-11 Redis 后端整体移除）。"
            "请改用 postgres / sqlite；旧库数据迁移见 README『从 Redis 迁移』一节"
            "（最后一个带 Redis 后端的版本是 keepsake-memory 1.1.x）。"
        )
    raise ValueError(
        f"storage.backend 必须显式配置为 {' / '.join(_VALID_BACKENDS)} 之一，"
        f"当前值: {raw!r}（旧版缺省回 redis 的兜底已随 Redis 后端移除，不再猜默认值）"
    )


# ---- 2026-10 ks_knob_unify：排序权重键的取值口径（两支共用**唯一**一份实现）----
# 这 10 个键在 cfg 顶层同名。口径写死为：kwargs（provider 口径） > cfg 顶层 > 模块常量。
# 缺省值逐字等于 PgStorage/SqliteStorage 的形参默认值（= 模块常量），所以「不配 = 现状」。
_KNOB_SPECS: Dict[str, tuple] = {
    "decay_half_days": (DECAY_HALF_DAYS, int),
    "attention_boost_max": (1.5, float),
    "attention_base_increment": (2.0, float),
    "attention_emotion_factor": (1.5, float),
    "emotion_intensity_factor": (0.4, float),
    "feedback_positive_boost": (FEEDBACK_POSITIVE_BOOST, float),
    "feedback_negative_penalty": (FEEDBACK_NEGATIVE_PENALTY, float),
    "hot_topic_boost": (HOT_TOPIC_BOOST, float),
    "hot_topic_decay_half_days": (HOT_TOPIC_DECAY_HALF_DAYS, int),
    "v2_min_score": (0.05, float),
}


def _resolve_knobs(cfg: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """合并 10 个排序权重键：kwargs > cfg 顶层 > 模块常量（见 _KNOB_SPECS）。"""
    return {
        k: cast(kwargs[k] if k in kwargs and kwargs[k] is not None
                 else cfg.get(k, default))
        for k, (default, cast) in _KNOB_SPECS.items()
    }


# ⚠️ def 行故意保持单行：cron/脚本入口的首参名（config_path）被验收门禁逐字匹配，
#    折行会被判成「既有签名丢了」。参数含义见下面 docstring。
def storage_from_config(config_path: Optional[str] = None, *, config: Optional[Dict[str, Any]] = None, **kwargs: Any) -> StorageBase:
    """按 config.json 构造存储后端（**含 embedder**）——cron/脚本统一入口。

    2026-09-15 实锤：`cron/memory_distill.py` 各自手搓存储构造**漏传 embedder**
    ⇒ 每小时提炼写入的记忆没有 embed_bin。统一走本函数，避免「东补西补」式接线。

    2026-11：Redis 分支随该后端整体移除；`storage.backend` 必须是
    `postgres` / `sqlite`，缺失/非法/`redis` 一律 `ValueError`（见 resolve_backend）。
    仅关键字参数：
      config:   调用方手上已有 cfg 时直接复用，省一次读盘（provider 走这条）
      **kwargs: 10 个排序权重键按 kwargs > cfg 顶层 > 模块常量 合并
                （见 _KNOB_SPECS / _resolve_knobs），两支共用同一份实现。
    """
    path = Path(config_path or DEFAULT_KEEPSAKE_CONFIG)
    cfg: Dict[str, Any] = {}
    if isinstance(config, dict):
        cfg = config
    else:
        try:
            cfg = _json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("storage: 读配置失败 %s: %s", path, e)

    backend = resolve_backend(cfg)
    # 2026-10 ks_lenfloor：检索面长度地板 —— **唯一配置源**在这里读一次，
    # 两个分支共用同一个值（kwargs 显式传入时 kwargs 胜出，与 _resolve_knobs 同款口径）。
    max_content_len = kwargs.get("max_content_len")
    if max_content_len is None:
        max_content_len = max_content_len_from_config(cfg)
    else:
        max_content_len = resolve_max_content_len(max_content_len)
    if backend == BACKEND_SQLITE:
        # 嵌入式单文件、零第三方依赖（只用标准库 sqlite3；`sqlite-vec` 可选懒加载）。
        from .sqlite import DEFAULT_SQLITE_PATH, SqliteStorage  # 延迟导入，同 PG 分支

        sq_cfg = (cfg.get("storage") or {}).get("sqlite") or {}
        if not isinstance(sq_cfg, dict):
            sq_cfg = {}
        return SqliteStorage(
            path=str(sq_cfg.get("path", "") or DEFAULT_SQLITE_PATH),
            busy_timeout_ms=int(sq_cfg.get("busy_timeout_ms", 5000)),
            agent_id=str(kwargs.get("agent_id", cfg.get("agent_id", "")) or ""),
            embedder=_build_embedder(cfg, path),
            embed_dim=int(cfg.get("embed_dim", 1536)),
            is_primary=bool(cfg.get("is_primary", False)),
            # 2026-10 ks_sqlite_p2_1：**条数上限与排序权重全部读配置**，与 PG 分支
            # 同一份取值来源（PG 分支就是从这里拿的）。修前这三个键一个都不传
            # ⇒ sqlite 恒用模块默认 ⇒ 同一 query 与 PG 差一个数量级。
            final_limit=int(cfg.get("top_k", 15)),
            bm25_limit=int(cfg.get("bm25_limit", DEFAULT_BM25_LIMIT)),
            candidate_count=int(cfg.get("candidate_k", 20)),
            # 2026-10 ks_lenfloor：检索面长度地板（同源接线，见函数开头）
            max_content_len=max_content_len,
            # 2026-10 ks_sqlite_p3_1 / ks_pg_syn：同义词三阈值**同源接线**
            # （同一份 cfg 顶层键、同一组默认值 10/0.5/3；kwargs 显式传入时胜出）。
            synonym_min_word_freq=int(kwargs.get(
                "synonym_min_word_freq", cfg.get("synonym_min_word_freq", 10))),
            synonym_jaccard_threshold=float(kwargs.get(
                "synonym_jaccard_threshold", cfg.get("synonym_jaccard_threshold", 0.5))),
            synonym_min_co_occurrence=int(kwargs.get(
                "synonym_min_co_occurrence", cfg.get("synonym_min_co_occurrence", 3))),
            **_resolve_knobs(cfg, kwargs),
        )

    from .pg import PgStorage  # 延迟导入：optional 依赖，隔离在真要用 PG 之后

    pg_cfg = (cfg.get("storage") or {}).get("postgres") or {}
    if not isinstance(pg_cfg, dict):
        pg_cfg = {}

    # 排序权重键两条分支**同源**：同一份 _resolve_knobs（kwargs > cfg > 常量）。
    return PgStorage(
        host=str(pg_cfg.get("host", "127.0.0.1")),
        port=int(pg_cfg.get("port", 5432)),
        dbname=str(pg_cfg.get("dbname", "keepsake")),
        user=str(pg_cfg.get("user", "")),
        password=str(pg_cfg.get("password", "")),
        sslmode=str(pg_cfg.get("sslmode", "")),
        agent_id=str(kwargs.get("agent_id", cfg.get("agent_id", "")) or ""),
        # 2026-10 ks_pg_b2：检索参数与 embedder 同源构造 ——
        # PG 侧 KNN 靠 embedder，维度必须与其他后端对齐（否则两库向量不可比）
        embedder=_build_embedder(cfg, path),
        embed_dim=int(cfg.get("embed_dim", 1536)),
        is_primary=bool(cfg.get("is_primary", False)),
        final_limit=int(cfg.get("top_k", 15)),
        bm25_limit=int(cfg.get("bm25_limit", DEFAULT_BM25_LIMIT)),
        candidate_count=int(cfg.get("candidate_k", 20)),
        # 2026-10 ks_lenfloor：检索面长度地板（同源接线，见函数开头）
        max_content_len=max_content_len,
        synonym_min_word_freq=int(kwargs.get(
            "synonym_min_word_freq", cfg.get("synonym_min_word_freq", 10))),
        synonym_jaccard_threshold=float(kwargs.get(
            "synonym_jaccard_threshold", cfg.get("synonym_jaccard_threshold", 0.5))),
        synonym_min_co_occurrence=int(kwargs.get(
            "synonym_min_co_occurrence", cfg.get("synonym_min_co_occurrence", 3))),
        **_resolve_knobs(cfg, kwargs),
    )
