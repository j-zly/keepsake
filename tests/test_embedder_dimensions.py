"""ks_embed_dim（2026-09）— Embedder 维度注册表 + 未知模型显式失败测试。

覆盖任务书验收点：
  1. _MODEL_DIMENSIONS 登记表断言（每个登记模型的实际维度）
  2. resolve_dimension("unknown") 返回 None（绝不静默兜底）
  3. OpenAIEmbedder(model=unknown).dimension == None（embedder 标记不可用）
  4. OpenAIEmbedder(model=unknown).get_embedding() 返回 None（无 API 请求）
  5. 已知模型 embedder 可用 + 维度正确（bge-m3=1024 等）
  6. 返回维度 ≠ 登记维度 → WARN + self._dim 更新 + 提示重建索引
  7. storage embed_dim 冲突（线上索引 DIM ≠ embedder dim）→ _embed_enabled=False
  8. storage embedder.dimension is None → _embed_enabled=False
  9. BM25 检索路径在 embedder 不可用时不受影响（search() 走 BM25-only）

设计要点：
  * 不连 Redis/网络 —— 全 mock（monkeypatch urlopen、FakeClient）
  * 任务书红线（禁碰 11434 ollama）；URL mock 一个真实请求都不许发
  * 既有用例不得改：本文件只新增断言，不动现有测试
"""

from __future__ import annotations

import json
import logging
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from keepsake.embedder import (
    OpenAIEmbedder,
    create_embedder,
    resolve_dimension,
)


# ===========================================================================
# 登记表断言
# ===========================================================================

class TestModelDimensionTable:
    """_MODEL_DIMENSIONS 各登记条目维度正确（防再踩未登记事故）。"""

    @pytest.mark.parametrize("model,expected_dim", [
        # OpenAI
        ("text-embedding-3-small", 1536),
        ("text-embedding-3-large", 3072),
        ("text-embedding-ada-002", 1536),
        # DashScope
        ("text-embedding-v2", 1536),
        ("text-embedding-v3", 1024),
        # 本地 ollama
        ("nomic-embed-text", 768),
        # BGE 系列（本次任务核心）
        ("bge-m3", 1024),
        ("bge-large-zh-v1.5", 1024),
        ("bge-large-en-v1.5", 1024),
        ("bge-base-zh-v1.5", 768),
        # ollama 常用（扩展登记）
        ("mxbai-embed-large", 1024),
        ("snowflake-arctic-embed", 1024),
    ])
    def test_resolve_dimension_returns_registered(self, model: str, expected_dim: int):
        """登记模型 → resolve_dimension 返回正确维度。"""
        assert resolve_dimension(model) == expected_dim, (
            f"model {model!r} 应映射到 dim={expected_dim}；"
            f"实际 {resolve_dimension(model)}"
        )

    def test_resolve_dimension_strips_and_lowercases(self):
        """大小写 / 前后空格不敏感 —— 配置喂「BGE-M3 」也应命中 bge-m3。"""
        assert resolve_dimension("BGE-M3") == 1024
        assert resolve_dimension(" bge-m3 ") == 1024
        assert resolve_dimension("Bge-M3") == 1024


# ===========================================================================
# 未知模型显式失败
# ===========================================================================

class TestUnknownModelExplicitFail:
    """未知模型必须返回 None / 标记不可用 —— 绝不静默兜底。"""

    def test_resolve_dimension_unknown_returns_none(self):
        """resolve_dimension("not-a-real-model") → None，不应返回任何 int 常量。"""
        result = resolve_dimension("not-a-real-model")
        assert result is None, (
            f"未知模型必须返回 None（红线条目）；实际 {result!r}"
        )

    def test_resolve_dimension_unknown_returns_none_not_1536(self):
        """G2 红线：未知模型路径不再返回任何数字常量（绝不允许 1536 顶上）。"""
        # 任务书 G2 红线 —— 即便换任何未知名字都应返回 None
        for name in (
            "totally-fake", "my-custom-embedder", "gpt-embedding-fake",
            "bge-m3-typo", "BGE-m3-v2", "random-model", "",
        ):
            assert resolve_dimension(name) is None, (
                f"未知模型 {name!r} 必须返回 None（红线条目 G2）；"
                f"实际 {resolve_dimension(name)!r}"
            )

    def test_openai_embedder_unknown_model_dim_is_zero_sentinel(self, caplog):
        """OpenAIEmbedder(model="unknown").dimension → 0 哨兵；_registered=False。

        2026-09 ks_embed_dim 设计选择：dimension 保持 int 返回类型以兼容旧
        调用方（如 %d 格式化），将「是否可用」语义分离到 _registered。
        """
        with caplog.at_level(logging.WARNING, logger="keepsake.embedder"):
            emb = OpenAIEmbedder(api_key="sk-fake-test-key-001",
                                 base_url="http://127.0.0.1:9999/v1",
                                 model="totally-fake-model")
        # dimension 返回 0 哨兵（不是 None —— 保持 int 类型稳定）
        assert emb.dimension == 0, (
            f"未知模型 dimension 必须为 0 哨兵；实际 {emb.dimension!r}"
        )
        # _registered 必须为 False
        assert emb._registered is False, (
            f"未知模型 _registered 必须为 False；实际 {emb._registered!r}"
        )
        # 必须打了「未登记 + 建议补登」的 WARN 日志（任务书硬性要求日志明确写）
        msgs = [rec.getMessage() for rec in caplog.records]
        joined = " | ".join(msgs)
        assert "totally-fake-model" in joined, (
            f"WARN 日志必须点名未知模型名 'totally-fake-model'；实际 {msgs}"
        )
        assert "_MODEL_DIMENSIONS" in joined, (
            f"WARN 日志必须给出修复提示（指向 _MODEL_DIMENSIONS）；实际 {msgs}"
        )

    def test_openai_embedder_unknown_model_get_embedding_returns_none(
        self, caplog, monkeypatch,
    ):
        """未知模型的 embedder.get_embedding 必须直接返回 None —— 不得发任何请求。

        用哨兵 urlopen：若 urlopen 被调用一次就直接 raise，让测试失败（任务书红线）。
        """
        def _fail_urlopen(*args, **kwargs):
            raise AssertionError(
                "未知模型 embedder.get_embedding 不应发任何 HTTP 请求（任务书红线）"
            )

        monkeypatch.setattr("keepsake.embedder.urlopen", _fail_urlopen)

        emb = OpenAIEmbedder(api_key="sk-fake-test-key-002",
                             base_url="http://127.0.0.1:9999/v1",
                             model="another-fake-model")
        with caplog.at_level(logging.WARNING, logger="keepsake.embedder"):
            result = emb.get_embedding("任意文本")
        assert result is None, (
            f"未知模型 get_embedding 必须返 None；实际 {result!r}"
        )

    def test_create_embedder_unknown_model_returns_unavailable_instance(self):
        """create_embedder 工厂方法：未知模型仍能构造实例，但 dimension=0 哨兵 + _registered=False。"""
        emb = create_embedder(
            provider="openai",
            api_key="sk-fake-test-key-003",
            base_url="http://127.0.0.1:9999/v1",
            model="bge-m3-typo",  # 不存在的拼写错误
        )
        # 构造不抛异常 —— 但 dimension=0 哨兵 + _registered=False
        assert emb.dimension == 0, (
            f"create_embedder 工厂对未知模型应返 dimension=0 哨兵；"
            f"实际 dimension={emb.dimension!r}"
        )
        assert emb._registered is False, (
            f"未知模型 _registered 必须为 False；实际 {emb._registered!r}"
        )

    def test_create_embedder_dashscope_default_model(self):
        """create_embedder(provider='dashscope') → 默认 model='text-embedding-v2'（已登记）。

        验证既有的 dashscope 工厂默认模型行为未被破坏。
        """
        emb = create_embedder(
            provider="dashscope",
            api_key="sk-fake-test-key-004",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="",
        )
        assert emb.dimension == 1536, (
            f"dashscope 默认 model='text-embedding-v2' 应映射 dim=1536；"
            f"实际 {emb.dimension!r}"
        )


# ===========================================================================
# 已知模型：可正常工作 + 维度正确
# ===========================================================================

class TestKnownModelUsable:
    """已登记模型 embedder 正常可用。"""

    def test_bge_m3_embedder_dimension_is_1024(self):
        """核心目标：bge-m3 必须可识别为 1024 维（任务书核心要求）。"""
        emb = OpenAIEmbedder(api_key="sk-fake-test-key-005",
                             base_url="http://127.0.0.1:9999/v1",
                             model="bge-m3")
        assert emb.dimension == 1024, (
            f"bge-m3 应映射 dim=1024；实际 {emb.dimension!r}"
        )

    def test_bge_large_zh_v15_dimension_is_1024(self):
        emb = OpenAIEmbedder(api_key="sk-fake", base_url="http://x", model="bge-large-zh-v1.5")
        assert emb.dimension == 1024

    def test_bge_large_en_v15_dimension_is_1024(self):
        emb = OpenAIEmbedder(api_key="sk-fake", base_url="http://x", model="bge-large-en-v1.5")
        assert emb.dimension == 1024

    def test_bge_base_zh_v15_dimension_is_768(self):
        emb = OpenAIEmbedder(api_key="sk-fake", base_url="http://x", model="bge-base-zh-v1.5")
        assert emb.dimension == 768


# ===========================================================================
# 返回维度 ≠ 登记维度：WARN + self._dim 更新 + 提示重建
# ===========================================================================

class TestDimensionMismatchWarning:
    """Ollama / 自定义服务返回维度与登记不符：WARN 升级 + 提示重建索引。"""

    def test_mismatch_logs_warning_not_info(self, caplog, monkeypatch):
        """服务返回 2048 维，但登记为 1536 → 应打 WARN（不再是 INFO）。"""
        # mock urlopen 返回 2048 维向量
        def _fake_urlopen(req, timeout=30):
            resp = MagicMock()
            resp.__enter__ = lambda self: resp
            resp.__exit__ = lambda self, *args: None
            resp.read = lambda: json.dumps({
                "data": [{"embedding": [0.0] * 2048}],
            }).encode("utf-8")
            return resp

        monkeypatch.setattr("keepsake.embedder.urlopen", _fake_urlopen)

        emb = OpenAIEmbedder(api_key="sk-fake-mismatch-key",
                             base_url="http://127.0.0.1:9999/v1",
                             model="text-embedding-3-small")  # 登记 1536

        with caplog.at_level(logging.WARNING, logger="keepsake.embedder"):
            vec = emb.get_embedding("测试文本")

        # 关键断言 1：返回的向量是 2048 维（实际服务返回）
        assert vec is not None and len(vec) == 2048
        # 关键断言 2：self._dim 已更新为 2048
        assert emb.dimension == 2048, (
            f"维度不符后 self._dim 应更新到 2048；实际 {emb.dimension!r}"
        )
        # 关键断言 3：打了 WARN（不再是 INFO）—— 历史坑的修复点
        msgs = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING]
        assert any("returned 2048 dims" in m for m in msgs), (
            f"维度不符必须打 WARN（含 'returned 2048 dims'）；实际 {msgs}"
        )
        # 关键断言 4：日志含「重建索引」相关提示
        joined = " | ".join(msgs)
        assert "index" in joined.lower() or "rebuild" in joined.lower() or "recreate" in joined.lower(), (
            f"WARN 必须含索引重建提示（防历史坑：静默 INFO 让维度漂移）；实际 {joined}"
        )

    def test_no_warning_when_dim_matches(self, caplog, monkeypatch):
        """维度一致 → 不打任何 WARN（正常路径无噪音）。"""
        def _fake_urlopen(req, timeout=30):
            resp = MagicMock()
            resp.__enter__ = lambda self: resp
            resp.__exit__ = lambda self, *args: None
            resp.read = lambda: json.dumps({
                "data": [{"embedding": [0.0] * 1536}],
            }).encode("utf-8")
            return resp

        monkeypatch.setattr("keepsake.embedder.urlopen", _fake_urlopen)

        emb = OpenAIEmbedder(api_key="sk-fake-match-key",
                             base_url="http://127.0.0.1:9999/v1",
                             model="text-embedding-3-small")  # 登记 1536

        with caplog.at_level(logging.WARNING, logger="keepsake.embedder"):
            vec = emb.get_embedding("测试")

        assert vec is not None and len(vec) == 1536
        assert emb.dimension == 1536
        # 维度一致 → 不应有「returned N dims」相关 WARN
        off = [r for r in caplog.records
               if r.levelno >= logging.WARNING and "returned" in r.getMessage().lower()
               and "dims" in r.getMessage().lower()]
        assert not off, (
            f"维度一致不应打 WARN；实际 {[r.getMessage() for r in off]}"
        )


# ===========================================================================
# storage 维度冲突：拒写向量
# ===========================================================================

class TestStorageEmbedDimMismatch:
    """storage.py：embedder 维度 ≠ 线上索引 DIM 时拒绝写向量 + 显式错误。"""

    def _build_storage_with_embedder(
        self, embedder, embed_dim: int = 1536,
    ):
        """构造一个不连真实 Redis 的 RedisStorage —— 测试 _embed_enabled 闸门。

        不调 ensure_index（不连 Redis）；仅验证构造期闸门逻辑。
        """
        from keepsake.storage import RedisStorage

        # monkeypatch 掉连接初始化路径以避免真连 Redis
        with patch("keepsake.storage.RedisStorage._get_client", return_value=None):
            storage = RedisStorage(embedder=embedder, embed_dim=embed_dim)
        return storage

    def test_storage_with_known_embedder_keeps_enabled(self):
        """已知模型 embedder → _embed_enabled = True。"""
        emb = OpenAIEmbedder(api_key="sk-fake", base_url="http://x",
                             model="text-embedding-3-small")  # dim=1536
        storage = self._build_storage_with_embedder(emb, embed_dim=1536)
        assert storage._embed_enabled is True
        assert storage._has_embedder() is True

    def test_storage_with_unknown_embedder_disables_embedding(self, caplog):
        """未知模型 embedder → _embed_enabled = False（拒写向量，BM25 不受影响）。"""
        emb = OpenAIEmbedder(api_key="sk-fake", base_url="http://x",
                             model="totally-fake-model")
        assert emb.dimension == 0  # 哨兵
        assert emb._registered is False  # 显式登记状态

        with caplog.at_level(logging.ERROR, logger="keepsake.storage"):
            storage = self._build_storage_with_embedder(emb, embed_dim=1536)

        # 关键断言：闸门关闭
        assert storage._embed_enabled is False, (
            f"未知模型 embedder 必须让 _embed_enabled=False；实际 {storage._embed_enabled!r}"
        )
        assert storage._has_embedder() is False, (
            "_has_embedder 应返 False（即使 embedder 不为 None）"
        )
        # 日志必须明确提示（带 _MODEL_DIMENSIONS 修复指引）
        msgs = [rec.getMessage() for rec in caplog.records]
        assert any("_MODEL_DIMENSIONS" in m for m in msgs), (
            f"ERROR 日志必须含 '_MODEL_DIMENSIONS' 修复指引；实际 {msgs}"
        )

    def test_storage_no_embedder_keeps_disabled(self):
        """不配 embedder → _embed_enabled = False（与 BM25-only 模式一致）。"""
        from keepsake.storage import RedisStorage
        with patch("keepsake.storage.RedisStorage._get_client", return_value=None):
            storage = RedisStorage(embedder=None, embed_dim=1536)
        # embedder=None 时 _embed_enabled 默认 True（保持向后兼容），
        # 但 _has_embedder 看 embedder is None 也会 False（不写向量）
        assert storage._has_embedder() is False


# ===========================================================================
# BM25 路径不受 embedder 不可用影响（负向测试）
# ===========================================================================

class TestBM25UnaffectedByEmbedderState:
    """embedder 不可用（None dim）时，BM25 检索路径完全不受影响。

    设计：mock search_bm25 直接返结果；verify 不调 search_knn、不需要 embedder。
    """

    def test_bm25_results_returned_when_embedder_unavailable(self):
        """unknown embedder → search() 仍走 BM25-only，结果正常返回。"""
        from keepsake.storage import RedisStorage

        # 模拟已知 + 未知 两种 embedder
        bad_emb = OpenAIEmbedder(api_key="sk-fake", base_url="http://x",
                                 model="fake-model")
        assert bad_emb.dimension == 0  # 哨兵
        assert bad_emb._registered is False

        with patch("keepsake.storage.RedisStorage._get_client", return_value=None):
            # 关掉 v2 min_score 地板 —— 本测试只关心 BM25 路径不被影响
            storage = RedisStorage(embedder=bad_emb, embed_dim=1536,
                                   v2_min_score=0.0)

        # 哨兵：让 search_bm25 直接返一些固定结果
        bm25_results = [
            {"content": "碎片A", "_bm25_score": 1.0, "_sim": 1.0,
             "_key": "memory:frag:a"},
            {"content": "碎片B", "_bm25_score": 0.8, "_sim": 0.8,
             "_key": "memory:frag:b"},
        ]
        with patch.object(storage, "search_bm25", return_value=list(bm25_results)), \
             patch.object(storage, "search_knn", return_value=[]) as mock_knn:
            out = storage.search("测试查询")

        # 关键断言：search_knn 没被调用（embedder 不可用 → 不会尝试 KNN）
        assert mock_knn.call_count == 0, (
            f"embedder 不可用时 search_knn 必须不被调用；实际 {mock_knn.call_count} 次"
        )
        # BM25 结果原样返回
        assert len(out) == 2
        assert {f["content"] for f in out} == {"碎片A", "碎片B"}


# ===========================================================================
# G2 红线：resolve_dimension 未知模型路径不再返回数字常量
# ===========================================================================

class TestNoSilentFallbackToNumeric:
    """任务书 G2：grep `return _DEFAULT_DIM` 应 MET（不存在）。

    本测试是断言性等价：未知模型路径绝不能返任何数字（包括隐式通过 dict.get(..., 1536)）。
    """

    def test_no_default_dim_constant_in_module(self):
        """模块层面不应再存在 _DEFAULT_DIM 常量（已彻底根除）。"""
        from keepsake import embedder as emb_mod
        assert not hasattr(emb_mod, "_DEFAULT_DIM"), (
            "embedder.py 不应再有 _DEFAULT_DIM 常量（G2 红线）"
        )

    def test_no_silent_numeric_fallback_in_resolve_dimension(self):
        """resolve_dimension 源码扫描：不许出现「兜底数字 + 不抛」的逻辑。"""
        import inspect
        from keepsake.embedder import resolve_dimension
        src = inspect.getsource(resolve_dimension)
        # 任何「数字默认值兜底」都应被禁
        forbidden = ("_DEFAULT_DIM", "1536", "or 0", "or 1", "if dim is None: return")
        for tok in forbidden:
            assert tok not in src, (
                f"resolve_dimension 源码里不应出现 {tok!r}；"
                f"当前源码：\n{src}"
            )