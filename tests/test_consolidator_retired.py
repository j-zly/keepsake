"""Consolidator 退役接线断言（2026-09-09）。

覆盖（任务书验收点）：
  1. KeepsakeProvider.initialize() 之后不再持有运行期 Consolidator 接线
  2. maintenance() 返回 stats 中 `consolidator` 字段是 retired（非可执行状态）
  3. resolve_llm_channel 仍可从 keepsake.consolidator 模块正常导入（向后兼容）
  4. KeepsakeProvider 不再持有 _consolidator 属性（属性接线拆除干净）
  5. _init_pipeline 仍按需导入 _call_llm / resolve_llm_channel（v2 复用路径完好）

设计要点：
  * 不连 Redis —— 全 mock；任务书红线（禁连 180 生产 Redis）
  * 通过 monkeypatch 让 initialize() 走完全部路径但不触发真实外部依赖
  * 不动 Consolidator 类源码（任务书要求保留整文件）
"""

from __future__ import annotations

from typing import Any, Dict
import types

import pytest


# ===========================================================================
# Fixtures: fake storage + monkeypatched initialize path
# ===========================================================================

class _FakeRedisClient:
    """最小 fake redis client —— 仅 ensure_index/exists/scan 路径需要的接口。"""

    def __init__(self):
        self.exists_calls: list = []

    def ping(self) -> bool:
        return True

    def ft(self):
        raise RuntimeError("no RediSearch in fake mode")

    def exists(self, key: str) -> int:
        self.exists_calls.append(key)
        return 0


class _FakeEmbedder:
    """最小 fake embedder —— initialize 期望 create_embedder 返回有 dimension 属性的对象。"""
    dimension = 1536


class _FakeStorage:
    """替代 RedisStorage —— 仅 initialize() 触达 ensure_index / _get_client / close。"""

    def __init__(self, *args, **kwargs):
        self._client = _FakeRedisClient()
        self.closed = False
        # initialize() 在构造后会调用 ensure_index() → True
        self.ensure_index_return = True

    def ensure_index(self) -> bool:
        return self.ensure_index_return

    def _get_client(self):
        return self._client

    def close(self):
        self.closed = True


@pytest.fixture
def fake_redis_storage(monkeypatch):
    """monkeypatch RedisStorage 构造 —— 让 initialize() 不连真实 Redis。"""

    def _factory(*args, **kwargs):
        return _FakeStorage(*args, **kwargs)

    monkeypatch.setattr("keepsake.RedisStorage", _factory)

    def _embedder_factory(**kwargs):
        return _FakeEmbedder()

    monkeypatch.setattr("keepsake.create_embedder", _embedder_factory)
    return _factory


@pytest.fixture
def fake_cron_dir(tmp_path, monkeypatch):
    """monkeypatch _ensure_cron_jobs 不读 ~/.hermes/cron/jobs.json。"""
    monkeypatch.setattr(
        "keepsake.KeepsakeProvider._ensure_cron_jobs",
        staticmethod(lambda: None),
    )


@pytest.fixture
def fake_pipeline(monkeypatch):
    """monkeypatch _init_pipeline —— 不启动 daemon。"""
    monkeypatch.setattr(
        "keepsake.KeepsakeProvider._init_pipeline",
        lambda self, cfg: setattr(self, "_pipeline", None),
    )


@pytest.fixture
def provider_initialized(fake_redis_storage, fake_cron_dir, fake_pipeline, monkeypatch, tmp_path):
    """构造一个走完 initialize() 但未连 Redis 的 provider。"""
    # agent_id 必填 → 用 monkeypatch env
    monkeypatch.setenv("KEEPSAKE_AGENT_ID", "test_agent_retired")
    # config.json 路径用空 —— 不污染 ~/.config
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "no_such_config.json"))

    from keepsake import KeepsakeProvider

    p = KeepsakeProvider()
    p.initialize(session_id="test_consolidator_retired")
    return p


# ===========================================================================
# Test 1: initialize() 之后不再持有运行期 Consolidator 接线
# ===========================================================================

class TestConsolidatorWiringRemoved:
    """KeepsakeProvider.initialize 路径里 Consolidator 的接线已被摘除。"""

    def test_initialize_does_not_construct_consolidator(
        self, fake_redis_storage, fake_cron_dir, fake_pipeline, monkeypatch, tmp_path,
    ):
        """initialize() 走完后，self._consolidator 属性不应再被设置。

        旧版本会 `self._consolidator = Consolidator(...)`；退役后该路径消失。
        """
        monkeypatch.setenv("KEEPSAKE_AGENT_ID", "test_agent_retired")
        monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "no_such_config.json"))

        from keepsake import KeepsakeProvider

        p = KeepsakeProvider()
        p.initialize(session_id="test_no_consolidator_init")

        # 关键断言：不再持有 _consolidator 运行期引用
        assert not hasattr(p, "_consolidator"), (
            "Consolidator 退役后 KeepsakeProvider 不应再有 _consolidator 属性，"
            f"实际属性集合：{sorted(a for a in vars(p) if a.startswith('_'))}"
        )
        # 健壮性：Forgetter 仍在运行
        assert p._forgetter is not None, "Forgetter 不应被一起摘掉"

    def test_consolidator_class_still_importable_for_revive(
        self, provider_initialized,
    ):
        """Consolidator 类源码保留 —— 仍可从 keepsake.consolidator 模块导入（未来复活）。"""
        from keepsake.consolidator import Consolidator
        assert Consolidator is not None
        assert hasattr(Consolidator, "consolidate"), (
            "Consolidator 源码应保留 consolidate 方法（任务书：类整文件保留）"
        )


# ===========================================================================
# Test 2: maintenance() 返回 stats 中 consolidator 字段是 retired
# ===========================================================================

class TestMaintenanceStatsRetired:
    """maintenance() 不再触发 Consolidator 执行，stats["consolidator"] = retired。"""

    def test_maintenance_consolidator_field_is_retired(
        self, fake_redis_storage, fake_cron_dir, fake_pipeline, monkeypatch, tmp_path,
    ):
        """provider.maintenance() 返回 stats["consolidator"]["status"] == "retired"。"""
        monkeypatch.setenv("KEEPSAKE_AGENT_ID", "test_agent_retired")
        monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "no_such_config.json"))

        from keepsake import KeepsakeProvider

        p = KeepsakeProvider()
        p.initialize(session_id="test_maintenance_retired")

        # 让 Forgetter.forget() 返回一些可观测的 stats（确认 main() 中 forgetter 路径还在跑）
        if p._forgetter is not None:
            p._forgetter.forget = lambda: {"deleted": 0, "dry_run": True, "scanned": 0}

        stats = p.maintenance()

        # 关键断言：consolidator 字段是 retired（不是 skipped / 不是任何可执行状态）
        assert "consolidator" in stats, (
            "maintenance() 应保留 consolidator 键供 cron/health 探针观测"
        )
        cstat = stats["consolidator"]
        assert cstat.get("status") == "retired", (
            f"Consolidator 已退役，应报 retired；实际 {cstat}"
        )
        # 不应再被遗忘器语义污染
        assert "merged" not in cstat, (
            "退役字段不应再含 merged（运行期指标），实际 {cstat}"
        )

    def test_maintenance_does_not_call_consolidator_consolidate(
        self, fake_redis_storage, fake_cron_dir, fake_pipeline, monkeypatch, tmp_path,
    ):
        """maintenance() 路径里不应再出现 Consolidator.consolidate 调用。

        在旧版代码中 maintenance() 会执行 self._consolidator.consolidate()；
        退役后该路径消失。验证方法：monkeypatch 一个会爆的 Fake Consolidator 并确认
        实例从未被构造（也就无从触发其 .consolidate）。
        """
        monkeypatch.setenv("KEEPSAKE_AGENT_ID", "test_agent_retired")
        monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "no_such_config.json"))

        # 哨兵：一旦 Consolidator 被构造就抛错
        from keepsake import consolidator as cm

        class _SentinelConsolidator:
            def __init__(self, *a, **kw):
                raise AssertionError(
                    "Consolidator.__init__ 被触发 → 运行期接线未摘干净"
                )

            def consolidate(self):
                raise AssertionError(
                    "Consolidator.consolidate 被触发 → 运行期接线未摘干净"
                )

        monkeypatch.setattr(cm, "Consolidator", _SentinelConsolidator)
        # 还要让属性层面拿不到（防止从 keepsake 顶层 import 旧名）
        import keepsake
        if hasattr(keepsake, "Consolidator"):
            monkeypatch.delattr(keepsake, "Consolidator")

        from keepsake import KeepsakeProvider

        p = KeepsakeProvider()
        p.initialize(session_id="test_no_consolidator_call")

        if p._forgetter is not None:
            p._forgetter.forget = lambda: {"deleted": 0, "dry_run": True, "scanned": 0}

        # maintenance() 不应触发任何 Consolidator 构造/调用
        stats = p.maintenance()
        assert stats["consolidator"]["status"] == "retired"

    def test_forgetter_still_runs_in_maintenance(
        self, fake_redis_storage, fake_cron_dir, fake_pipeline, monkeypatch, tmp_path,
    ):
        """Forgetter 不应被一并摘掉 —— maintenance() 中 forgetter 应有结果。"""
        monkeypatch.setenv("KEEPSAKE_AGENT_ID", "test_agent_retired")
        monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "no_such_config.json"))

        from keepsake import KeepsakeProvider

        p = KeepsakeProvider()
        p.initialize(session_id="test_forgetter_alive")

        sentinel_stats = {"deleted": 5, "dry_run": False, "scanned": 7}
        p._forgetter.forget = lambda: sentinel_stats

        stats = p.maintenance()
        assert stats["forgetter"] == sentinel_stats, (
            f"Forgetter 应在 maintenance() 中跑，实际 stats={stats['forgetter']}"
        )


# ===========================================================================
# Test 3: resolve_llm_channel 仍可从 keepsake.consolidator 模块正常导入
# ===========================================================================

class TestResolveLLMChannelStillImportable:
    """resolve_llm_channel / _call_llm / Consolidator 类源码仍可从 consolidator 模块导入。"""

    def test_resolve_llm_channel_importable(self):
        """向后兼容：resolve_llm_channel 仍从 keepsake.consolidator 导出。"""
        from keepsake.consolidator import resolve_llm_channel
        # 行为不变：传 None 仍返回 dashscope 兜底 dict
        ch = resolve_llm_channel(None)
        assert "base_url" in ch
        assert "model" in ch
        assert "api_key" in ch

    def test_call_llm_importable(self):
        """v2 pipeline 复用的 _call_llm 仍可从 consolidator 模块导入。"""
        from keepsake.consolidator import _call_llm
        assert callable(_call_llm)

    def test_module_constants_intact(self):
        """模块常量 DASHSCOPE_BASE / DEFAULT_LLM_MODEL 保留（外部测试 + 文档示例引用）。"""
        from keepsake import consolidator as cm
        assert cm.DASHSCOPE_BASE.startswith("https://")
        assert isinstance(cm.DEFAULT_LLM_MODEL, str)
        assert len(cm.DEFAULT_LLM_MODEL) > 0

    def test_resolve_llm_channel_used_by_initialize_pipeline(
        self, fake_redis_storage, fake_cron_dir, fake_pipeline, monkeypatch, tmp_path,
    ):
        """initialize() 路径里的 resolve_llm_channel 单独 import 应无副作用。"""
        monkeypatch.setenv("KEEPSAKE_AGENT_ID", "test_agent_retired")
        monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "no_such_config.json"))

        # 哨兵：监控 resolve_llm_channel 是否被 initialize() 调用
        from keepsake import consolidator as cm
        orig = cm.resolve_llm_channel
        called: Dict[str, Any] = {"count": 0}

        def _spy(cfg):
            called["count"] += 1
            return orig(cfg)

        monkeypatch.setattr(cm, "resolve_llm_channel", _spy)

        from keepsake import KeepsakeProvider

        p = KeepsakeProvider()
        p.initialize(session_id="test_resolve_called")

        # initialize() 中调用过 resolve_llm_channel（用于 llm_pipeline 的 channel 解析）
        assert called["count"] >= 1, (
            "initialize() 路径仍应调用 resolve_llm_channel 用于 v2 pipeline 配置"
        )


# ===========================================================================
# Test 4: KeepsakeProvider 不再持有 _consolidator 属性（属性接线拆除干净）
# ===========================================================================

class TestNoConsolidatorAttributeOnProvider:
    """Provider 实例上不应残留 _consolidator 属性（包括默认值类变量）。"""

    def test_class_level_no_consolidator_annotation(
        self, fake_redis_storage, fake_cron_dir, fake_pipeline, monkeypatch, tmp_path,
    ):
        """KeepsakeProvider 类层面不应再声明 _consolidator 类属性。"""
        from keepsake import KeepsakeProvider

        # 直接检查类字典（避免 instance 属性干扰）
        cls_dict = vars(KeepsakeProvider)
        assert "_consolidator" not in cls_dict, (
            "KeepsakeProvider 类层面不应再有 _consolidator 属性，"
            f"实际属性：{[k for k in cls_dict if k.startswith('_')]}"
        )

    def test_instance_level_no_consolidator_after_init(
        self, provider_initialized,
    ):
        """Provider 实例初始化完成后不应持有 _consolidator。"""
        p = provider_initialized
        # 健壮性：实例字典里没有
        assert "_consolidator" not in vars(p), (
            f"Provider 实例不应有 _consolidator；实例属性：{sorted(vars(p))}"
        )


# ===========================================================================
# Test 5: _init_pipeline 仍按需导入 _call_llm / resolve_llm_channel（v2 复用完好）
# ===========================================================================

class TestV2PipelineStillUsesConsolidatorHelpers:
    """v2 pipeline 仍依赖 consolidator 模块的 _call_llm / resolve_llm_channel —— 不误删。"""

    def test_pipeline_imports_resolve_llm_channel_from_consolidator(self):
        """pipeline 模块或 __init__._init_pipeline 仍从 consolidator 取 resolve_llm_channel。"""
        import inspect
        from keepsake import KeepsakeProvider
        src = inspect.getsource(KeepsakeProvider._init_pipeline)
        # _init_pipeline 内部仍需 resolve_llm_channel 与 _call_llm
        assert "resolve_llm_channel" in src, (
            "_init_pipeline 仍应引用 resolve_llm_channel（v2 pipeline channel 解析）"
        )
        assert "_call_llm" in src, (
            "_init_pipeline 仍应引用 _call_llm（v2 pipeline LLM 调用）"
        )
        # 应仍从 consolidator 模块 import
        assert "consolidator" in src, (
            "_init_pipeline 应仍从 keepsake.consolidator 模块 import helpers"
        )

    def test_consolidator_module_file_unchanged_on_disk(self):
        """consolidator.py 文件物理存在且 Consolidator 类定义仍在。"""
        import os
        from keepsake import consolidator as cm

        path = cm.__file__
        assert os.path.exists(path), f"consolidator.py 应保留：{path}"

        # 类定义仍在源码里
        src = open(path).read()
        assert "class Consolidator:" in src, (
            "Consolidator 类源码应整文件保留（任务书：日后想复活再议）"
        )
        assert "def consolidate(self)" in src, (
            "Consolidator.consolidate 方法应保留"
        )