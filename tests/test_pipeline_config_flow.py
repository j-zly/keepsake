"""回归（keepsake ks_pipefix）：_init_pipeline 必须读合并后的 cfg。

事故（2026-09-09 生产实锤）：
  * e45eefd 之前 dashscope 兜底会掩盖「inline 空 + 文件有 llm 节」场景
  * e45eefd 根除兜底后暴露：`_init_pipeline` 用 `resolve_llm_channel(self._config)`
    拿的是 Hermes 传入的 inline 配置（无 llm 节），结果是 `source=unconfigured`，
    pipeline 永不启动 → 日志「v2 pipeline disabled (llm channel unconfigured)」
  * 单测全喂合并后 cfg，测不到真实「inline 空 + 文件有节」场景

修复契约：
  * initialize() 须把合并完成的 cfg 存 `self._resolved_config`（deepcopy）
  * _init_pipeline 须读 `self._resolved_config` 解析 channel
  * 文件路径重读链（channel_refresher / resolve_llm_channel_cached）不受影响

本测试不连 Redis/网络（任务书红线），全 mock；只用 FAKE key 字符串。
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List

import pytest


# ===========================================================================
# Fixtures: fake storage + monkeypatched initialize path
# 与 test_consolidator_retired.py 模式保持一致，但本测试用 fake Pipeline
# 替身，捕获构造参数，便于断言「_init_pipeline 解析的 channel 是否用了合并后 cfg」。
# ===========================================================================

class _FakeRedisClient:
    def ping(self) -> bool:
        return True

    def ft(self):
        raise RuntimeError("no RediSearch in fake mode")

    def exists(self, key: str) -> int:
        return 0


class _FakeEmbedder:
    dimension = 1536


class _FakeStorage:
    """替代 RedisStorage —— 仅 initialize() 触达 ensure_index / _get_client / close。"""

    def __init__(self, *args, **kwargs):
        self._client = _FakeRedisClient()
        self.ensure_index_return = True

    def ensure_index(self) -> bool:
        return self.ensure_index_return

    def _get_client(self):
        return self._client

    def close(self):
        pass


class _FakePipeline:
    """Pipeline 替身 —— 记录构造参数 + 不启 daemon 线程。

    设计要点：
      * KeepsakeProvider._init_pipeline 走 Pipeline(...) 构造 → 抓到本替身即可
      * start() 留空 → 不启线程 → 不污染 sys.exit 时的清理
      * llm_fn 是 functools.partial(_call_llm, channel=...) → 通过 .keywords
        拿回 channel 字典，便于断言 source/base_url
    """

    instances: List["_FakePipeline"] = []

    def __init__(self, storage=None, *, llm_fn=None, model: str = "",
                 window_pairs: int = 4, window_seconds: float = 30.0,
                 max_calls_per_window: int = 8, update_top_k: int = 5,
                 recent_context_size: int = 8, gate_fallback=None,
                 channel_refresher=None):
        self.storage = storage
        self.llm_fn = llm_fn
        self.model = model
        self.window_pairs = window_pairs
        self.window_seconds = window_seconds
        self.max_calls_per_window = max_calls_per_window
        self.update_top_k = update_top_k
        self.recent_context_size = recent_context_size
        self.gate_fallback = gate_fallback
        self.channel_refresher = channel_refresher
        self._started = False
        _FakePipeline.instances.append(self)

    def start(self) -> None:
        self._started = True

    def stop(self, *, drain: bool = True, timeout: float = 5.0) -> None:
        self._started = False


@pytest.fixture
def fake_storage(monkeypatch):
    """monkeypatch RedisStorage 构造 —— 让 initialize() 不连真实 Redis。"""
    monkeypatch.setattr("keepsake.RedisStorage", lambda *a, **kw: _FakeStorage(*a, **kw))
    monkeypatch.setattr(
        "keepsake.create_embedder",
        lambda **kw: _FakeEmbedder(),
    )


@pytest.fixture
def fake_pipeline_class(monkeypatch):
    """monkeypatch Pipeline 类 —— 让 _init_pipeline 走 fake，避免启 daemon。"""
    _FakePipeline.instances.clear()
    # __init__.py 里 `from .pipeline import Pipeline, ...`，所以两个路径都要 patch
    monkeypatch.setattr("keepsake.Pipeline", _FakePipeline)
    monkeypatch.setattr("keepsake.pipeline.Pipeline", _FakePipeline)


@pytest.fixture
def fake_cron(monkeypatch):
    monkeypatch.setattr(
        "keepsake.KeepsakeProvider._ensure_cron_jobs",
        staticmethod(lambda: None),
    )


def _make_provider_with_file_config(
    monkeypatch,
    tmp_path,
    file_cfg: Dict[str, Any],
    *,
    inline_cfg: Dict[str, Any] = None,
) -> Any:
    """构造一个走完 initialize() 的 provider，file_cfg 由临时 JSON 文件喂入。

    inline_cfg 默认为空 dict —— 模拟 Hermes 不带 llm 节传入。
    """
    # agent_id 必须 —— 用 env
    monkeypatch.setenv("KEEPSAKE_AGENT_ID", "test_pipefix_agent")
    # 写一份临时 config.json 给 _load_json_config 读
    cfg_file = tmp_path / "keepsake_config.json"
    cfg_file.write_text(json.dumps(file_cfg, ensure_ascii=False))
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))

    from keepsake import KeepsakeProvider

    p = KeepsakeProvider(**(inline_cfg or {}))
    p.initialize(session_id="test_pipefix")
    return p


# ===========================================================================
# 回归 1: inline={} + 文件含有效 llm 节 → pipeline 必须启动且 channel=bigmodel
# ===========================================================================

class TestPipelineUsesMergedConfig:
    """核心回归：_init_pipeline 必须读合并后的 self._resolved_config。

    inline 为空（Hermes 不带 llm 节）+ config.json 含有效 llm 节 →
    修复前：channel=unconfigured, pipeline=None（生产事故）
    修复后：channel=bigmodel, pipeline 非 None
    """

    def test_inline_empty_file_has_llm_starts_pipeline_bigmodel(
        self, fake_storage, fake_pipeline_class, fake_cron, monkeypatch, tmp_path,
    ):
        """inline={} + 文件 llm 节完整 → pipeline 必启 + source=bigmodel。"""
        file_cfg = {
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "api_key": "fake-inline-key-PIPEFIX-001",
            },
        }

        p = _make_provider_with_file_config(
            monkeypatch, tmp_path, file_cfg, inline_cfg={},
        )

        # 1) 合并后的 cfg 已落 self._resolved_config（关键修复点）
        assert hasattr(p, "_resolved_config"), (
            "修复点 1 缺失：initialize() 必须把合并 cfg 存到 self._resolved_config，"
            f"实际属性：{sorted(a for a in vars(p) if not a.startswith('__'))}"
        )
        assert "llm" in p._resolved_config
        assert p._resolved_config["llm"]["model"] == "glm-4-flash"

        # 2) 合并后 cfg 必须是 deepcopy（防外部改污染）
        p._resolved_config["llm"]["model"] = "MUTATED"
        # 再算一次应不受影响 —— 直接对比 inline
        from keepsake import KeepsakeProvider as _KP
        fresh_cfg = _KP._resolve_config(p._config)
        assert fresh_cfg["llm"]["model"] != "MUTATED", (
            "self._resolved_config 必须是 deepcopy，外部修改不应影响 resolve 链"
        )
        # 回滚，免得影响下游断言
        p._resolved_config["llm"]["model"] = "glm-4-flash"

        # 3) Pipeline 实例被构造（非 None 即走过 _init_pipeline 启动分支）
        assert len(_FakePipeline.instances) == 1, (
            "修复前 _init_pipeline 走 unconfigured 分支提前 return，"
            "Pipeline(...) 不会被构造。修复后必须构造 1 次。"
        )
        assert _FakePipeline.instances[0]._started is True

        # 4) channel 来源是 bigmodel（host 命中）
        # llm_fn 是 functools.partial(_call_llm, channel=...) → 从 .keywords 拿 channel
        import functools
        assert isinstance(_FakePipeline.instances[0].llm_fn, functools.partial), (
            "llm_fn 应是 functools.partial 包裹的 _call_llm，"
            f"实际类型：{type(_FakePipeline.instances[0].llm_fn)}"
        )
        channel = _FakePipeline.instances[0].llm_fn.keywords["channel"]
        assert channel["valid"] is True, (
            f"channel 必须 valid（修复前会被错判为 unconfigured）：{channel}"
        )
        assert channel["source"] == "bigmodel", (
            f"channel.source 应识别为 bigmodel，实际 {channel.get('source')}"
        )
        assert channel["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
        assert channel["model"] == "glm-4-flash"
        assert channel["api_key"] == "fake-inline-key-PIPEFIX-001"

        # 5) 关键反向断言：日志/返回值里绝对不能出现 dashscope/qwen-plus 兜底串
        channel_blob = json.dumps(channel, ensure_ascii=False).lower()
        assert "dashscope" not in channel_blob
        assert "aliyuncs" not in channel_blob
        assert "qwen-plus" not in channel_blob

    def test_inline_empty_file_llm_missing_keeps_pipeline_none(
        self, fake_storage, fake_pipeline_class, fake_cron, monkeypatch, tmp_path,
    ):
        """inline={} + 文件无 llm 节 → pipeline=None（unconfigured 语义保持）。

        修复后 _init_pipeline 仍能识别「合并后无 llm 节」→ 走 unconfigured 早 return。
        不能因为本次修复反而把这条「无配置 = 不启」的契约搞坏。
        """
        file_cfg = {
            "redis_host": "127.0.0.1",
            # 故意无 llm 节
        }

        p = _make_provider_with_file_config(
            monkeypatch, tmp_path, file_cfg, inline_cfg={},
        )

        # Pipeline 不该被构造（unconfigured 早 return）
        assert _FakePipeline.instances == [], (
            "无 llm 节时 _init_pipeline 应早 return；"
            f"实际构造了 {len(_FakePipeline.instances)} 次 Pipeline"
        )
        # self._pipeline 必须仍是 None（保持旧契约）
        assert getattr(p, "_pipeline", "MISSING") is None, (
            f"无 llm 节时 self._pipeline 应为 None；实际 {p._pipeline!r}"
        )
        # self._resolved_config 应仍被赋值（修复一致性）
        assert hasattr(p, "_resolved_config"), (
            "修复点 1 一致性：即使无 llm 节，self._resolved_config 也应存在"
        )
        assert p._resolved_config.get("llm", {}) == {}, (
            "无 llm 节时 _resolved_config['llm'] 应为空 dict，"
            f"实际 {p._resolved_config.get('llm')}"
        )


# ===========================================================================
# 回归 2: inline 含 llm 节 + 文件无 → 以 inline 为准（验证合并链未坏）
# ===========================================================================

class TestPipelineMergingChainStillWorks:
    """验证修复未破坏既有合并链：inline 优先 + 文件兜底。"""

    def test_inline_llm_wins_over_file_when_both_present(
        self, fake_storage, fake_pipeline_class, fake_cron, monkeypatch, tmp_path,
    ):
        """inline 含 llm 节 + 文件含 llm 节 → 合并后 inline 胜出。

        验证 _resolve_config 的 _deep_merge 顺序未受修复影响。
        """
        file_cfg = {
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "api_key": "file-key-SHOULD-LOSE",
            },
        }
        inline_cfg = {
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "api_key": "inline-key-SHOULD-WIN",
            },
        }

        p = _make_provider_with_file_config(
            monkeypatch, tmp_path, file_cfg, inline_cfg=inline_cfg,
        )

        assert len(_FakePipeline.instances) == 1
        ch = _FakePipeline.instances[0].llm_fn.keywords["channel"]
        assert ch["valid"] is True
        assert ch["api_key"] == "inline-key-SHOULD-WIN", (
            "inline 应胜过 file —— _deep_merge 顺序若被破坏会暴露"
        )

    def test_key_file_path_in_file_cfg_resolves_via_merged_cfg(
        self, fake_storage, fake_pipeline_class, fake_cron, monkeypatch, tmp_path,
    ):
        """inline={} + 文件 llm.key_file → 应读到 key 文件内容（验证 _resolved_config 真被用）。"""
        key_file = tmp_path / "glm.pass"
        key_file.write_text("fake-key-from-file-PIPEFIX\n")
        file_cfg = {
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "key_file": str(key_file),
            },
        }

        p = _make_provider_with_file_config(
            monkeypatch, tmp_path, file_cfg, inline_cfg={},
        )

        assert len(_FakePipeline.instances) == 1
        ch = _FakePipeline.instances[0].llm_fn.keywords["channel"]
        assert ch["valid"] is True
        assert ch["api_key"] == "fake-key-from-file-PIPEFIX"
        assert ch["source"] == "bigmodel"


# ===========================================================================
# 回归 3: log 反向断言 —— 修复后不允许「v2 pipeline disabled (unconfigured)」
# ===========================================================================

class TestNoUnconfiguredWarningWhenFileHasLLM:
    """修复后若 file 有 llm 节，绝不允许再触发 unconfigured warning。"""

    def test_no_unconfigured_warning_when_file_llm_valid(
        self, fake_storage, fake_pipeline_class, fake_cron, monkeypatch, tmp_path, caplog,
    ):
        """inline={} + 文件含有效 llm 节 → 日志里不应再出现「v2 pipeline disabled」。"""
        import logging as _logging
        file_cfg = {
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "api_key": "fake-key-PIPEFIX-LOG",
            },
        }

        with caplog.at_level(_logging.WARNING, logger="keepsake"):
            _make_provider_with_file_config(
                monkeypatch, tmp_path, file_cfg, inline_cfg={},
            )

        offending = [
            rec for rec in caplog.records
            if "v2 pipeline disabled" in rec.getMessage()
        ]
        assert not offending, (
            "修复前会打『v2 pipeline disabled (llm channel unconfigured, source=unconfigured)』；"
            f"修复后 file 有 llm 节时绝不应再出现；实际命中 {len(offending)} 条："
            f"{[r.getMessage() for r in offending]}"
        )


# ===========================================================================
# 回归 4: log 反向断言 —— 修复不应引入新 key 泄漏
# ===========================================================================

class TestResolvedConfigNoKeyLeak:
    """_resolved_config 持有合并后 cfg（带 key）→ logger 必须不打 key 值。"""

    def test_no_api_key_value_in_any_log(
        self, fake_storage, fake_pipeline_class, fake_cron, monkeypatch, tmp_path, caplog,
    ):
        import logging as _logging
        fake_key = "fake-key-PIPEFIX-NOLEAK-007"
        file_cfg = {
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "api_key": fake_key,
            },
        }

        with caplog.at_level(_logging.DEBUG, logger="keepsake"):
            _make_provider_with_file_config(
                monkeypatch, tmp_path, file_cfg, inline_cfg={},
            )

        leaked = [
            rec.getMessage() for rec in caplog.records
            if fake_key in rec.getMessage()
        ]
        assert not leaked, (
            f"修复后 _resolved_config 带 key 值，logger 不应泄露：{leaked[:3]}"
        )
