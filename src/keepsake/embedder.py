"""Embedding 客户端 — 支持 OpenAI / DashScope / 自定义兼容端点。"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Optional
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模型 → 维度映射（2026-09 ks_embed_dim）
#
# 红线：未知模型不得静默兜底为某个默认值 —— 历史事故（nomic 768 被当 1536、
# KNN 静默 0 条、FT.INFO blob size 3072 expected 6144）就是由此而来。
# 未知模型 → resolve_dimension() 返回 None → embedder 标记不可用 → 消费方走
# BM25-only 降级并日志明确报错。宁缺勿错。
# ---------------------------------------------------------------------------

_MODEL_DIMENSIONS: dict[str, int] = {
    # OpenAI text-embedding-3 系列
    # 来源: OpenAI 官方文档 https://platform.openai.com/docs/guides/embeddings
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    # DashScope（阿里云通义）
    # 来源: DashScope 官方 https://help.aliyun.com/zh/model-studio/developer-reference/text-embedding-api-details
    "text-embedding-v2": 1536,
    "text-embedding-v3": 1024,
    # 本地 ollama 常用
    # 来源: ollama library https://ollama.com/library/nomic-embed-text
    "nomic-embed-text": 768,
    # BGE 系列（BAAI，北京智源）— 防再踩未登记事故
    # 来源: https://huggingface.co/BAAI/bge-m3            (dim 1024, max seq 8192)
    #       https://huggingface.co/BAAI/bge-large-zh-v1.5 (dim 1024)
    #       https://huggingface.co/BAAI/bge-large-en-v1.5 (dim 1024)
    #       https://huggingface.co/BAAI/bge-base-zh-v1.5  (dim  768)
    "bge-m3": 1024,
    "bge-large-zh-v1.5": 1024,
    "bge-large-en-v1.5": 1024,
    "bge-base-zh-v1.5": 768,
    # ollama 常用本地 embedding（来自 Mixedbread / Snowflake 官方 model card）
    # 来源: https://huggingface.co/mixedbread-ai/mxbai-embed-large-v1     (dim 1024)
    #       https://huggingface.co/Snowflake/snowflake-arctic-embed-m-v1.5 (dim 1024)
    "mxbai-embed-large": 1024,
    "snowflake-arctic-embed": 1024,
}


def resolve_dimension(model: str) -> Optional[int]:
    """根据模型名返回向量维度；未知模型返回 None（绝不静默兜底）。

    消费方契约（2026-09 ks_embed_dim）：
      - 返回 int → 模型已登记，可用作 embedding dim
      - 返回 None → 模型未登记；embedder 应判为不可用、keepsake 应走
        BM25-only 降级；日志里明确给出修复提示（"add to _MODEL_DIMENSIONS"）
    """
    model_key = model.strip().lower()
    return _MODEL_DIMENSIONS.get(model_key)


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


class Embedder(ABC):
    @abstractmethod
    def get_embedding(self, text: str) -> Optional[list[float]]:
        """输入文本，返回 float 向量。"""
        ...

    @property
    @abstractmethod
    def dimension(self) -> int:
        """返回当前模型输出的向量维度。

        2026-09 ks_embed_dim 契约：
          - 已登记模型 → 返回正整数维度
          - 未登记模型 → 返回 0（哨兵）；调用方必须额外判断 _registered
            或调用 get_embedding 时收到 None 来识别「不可用」状态。
          设计意图：保持 dimension 返回类型稳定（int）以兼容旧调用方
          （如 `dim=%d` 格式化），将「是否可用」语义分离到 _registered。
        """
        ...

    @property
    @abstractmethod
    def _registered(self) -> bool:
        """模型是否在 _MODEL_DIMENSIONS 中登记（= embedder 是否可用）。

        2026-09 ks_embed_dim：消费方应优先用此属性判断 embedder 可用性，
        避免误把 `dimension == 0` 当成有效 dim。
        """
        ...


# ---------------------------------------------------------------------------
# OpenAI 兼容
# ---------------------------------------------------------------------------

_DEFAULT_OPENAI_URL = "https://api.openai.com/v1/embeddings"
_DEFAULT_OPENAI_MODEL = "text-embedding-3-small"


class OpenAIEmbedder(Embedder):
    """兼容 OpenAI Embedding API 的客户端。

    也兼容 DashScope 等提供 /v1/embeddings 端点的服务。

    2026-09 ks_embed_dim 行为变更：
      - 未知模型 → resolve_dimension() 返回 None → self._dim = None →
        dimension 返回 0 哨兵 + _registered = False；get_embedding 返 None。
        调用方应通过 _registered 判断可用性，而非 dimension。
      - 返回维度 ≠ 登记维度：从 INFO 提为 WARN（历史坑：静默自更新会让索引
        与实际维度再次漂移，问题被推迟到下游才发现）。
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = _DEFAULT_OPENAI_URL,
        model: str = _DEFAULT_OPENAI_MODEL,
    ):
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base_url = base_url.rstrip("/")
        self._model = model
        dim = resolve_dimension(model)
        if dim is None:
            logger.warning(
                "embedder: model %r not registered in _MODEL_DIMENSIONS, "
                "embedding disabled (add to src/keepsake/embedder.py:"
                "_MODEL_DIMENSIONS to enable). Keepsake will fall back to "
                "BM25-only.",
                model,
            )
            self._dim = None
        else:
            self._dim = dim

    @property
    def dimension(self) -> int:
        # 未登记模型 → 返回 0 哨兵（保持 int 类型稳定，兼容旧调用方）
        return self._dim if self._dim is not None else 0

    @property
    def _registered(self) -> bool:
        return self._dim is not None

    def get_embedding(self, text: str) -> Optional[list[float]]:
        if self._dim is None:
            # 未知模型 — 不可用路径，所有 get_embedding 直接返 None
            logger.debug(
                "embedder: get_embedding skipped (model %r unregistered)", self._model,
            )
            return None
        if not self._api_key:
            logger.warning("embedder: no API key configured")
            return None

        payload = json.dumps({
            "model": self._model,
            "input": text,
        }).encode("utf-8")

        req = Request(
            f"{self._base_url}/embeddings",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                emb = data["data"][0]["embedding"]
                # 如果服务返回的维度与预期不符，更新 self._dim
                # 2026-09 ks_embed_dim：提为 WARN，明确提示需重建索引
                # 历史坑：INFO 静默自更新会让「登记维度 ≠ 实际维度」的真相
                # 被推迟到下游才发现（KNN 静默 0 条、blob size 不匹配）。
                if len(emb) != self._dim:
                    logger.warning(
                        "embedder: model %s returned %d dims (registered as %d), "
                        "updating self._dim. NOTE: existing RediSearch index "
                        "dim=%d will NOT match — drop and recreate the index, "
                        "or re-register the model in _MODEL_DIMENSIONS.",
                        self._model, len(emb), self._dim, self._dim,
                    )
                    self._dim = len(emb)
                return emb
        except Exception as e:
            logger.debug("embedder: request failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

_EMBEDDER_PROVIDERS: dict[str, type[Embedder]] = {
    "openai": OpenAIEmbedder,
    "dashscope": OpenAIEmbedder,  # DashScope 也走 /v1/embeddings
}


def create_embedder(
    provider: str = "",
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> Embedder:
    """根据配置创建 Embedder 实例。

    参数:
        provider: "openai" | "dashscope" | 自定义
        api_key: API 密钥
        base_url: API 端点
        model: 模型名
    """
    provider = provider or os.environ.get("KEEPSAKE_EMBEDDER", "openai").lower()
    api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
    base_url = base_url or os.environ.get("KEEPSAKE_EMBEDDER_URL", _DEFAULT_OPENAI_URL)
    model = model or os.environ.get("KEEPSAKE_EMBEDDER_MODEL", _DEFAULT_OPENAI_MODEL)

    if provider == "dashscope":
        base_url = base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = model or "text-embedding-v2"

    cls = _EMBEDDER_PROVIDERS.get(provider, OpenAIEmbedder)
    return cls(api_key=api_key, base_url=base_url, model=model)
