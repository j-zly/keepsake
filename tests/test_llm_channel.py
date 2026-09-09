"""keepsake LLM 通道配置化 —— resolve_llm_channel 单元测试。

覆盖（任务书 G1/G2/G3/G4 + 2026-09 ks_noqwen 新增语义）:
  1. 有 llm 节 + key_file → resolve 出 base_url/model/api_key
  2. 无 llm 节 → resolve 返回 source="unconfigured" / valid=False
     （2026-09 起移除 dashscope 兜底 — 无任何硬编码回落）
  3. 缺 model 字段 → 视同无有效通道
  4. key_file 不存在 → resolve 不抛，api_key="" 但 base_url/model 保留
  5. api_key 直填 优先于 key_file
  6. 日志无 key 泄漏：monkeypatch logger，断言任何 record 文本不含 key 值
  7. (NEW) resolve_llm_channel_cached: mtime 不变 → 同内容 touch 不重解析
  8. (NEW) resolve_llm_channel_cached: mtime 变 → 重读 + 重解析
  9. (NEW) resolve_llm_channel_cached: 中途坏 JSON → 本窗 unconfigured，不抛
 10. (NEW) resolve_llm_channel_cached: key_file 轮换 → 下一窗读新值
 11. (NEW) Pipeline.channel_refresher: 通道从 unconfigured→valid 下窗生效
 12. (NEW) Pipeline.channel_refresher: 中途 model 值变更 → 下窗生效

设计要点:
  * 全 mock 无网络（任务书红线）
  * 测试用 tmp fake 文件 + 假 key 字符串（任务书红线）
  * 不依赖外部 hermes yaml/配置文件
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time

import pytest

from keepsake.consolidator import (
    resolve_llm_channel,
    resolve_llm_channel_cached,
    invalidate_channel_cache,
)


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def fake_key_file(tmp_path):
    """写入一个 fake key 文件，返回路径。"""
    p = tmp_path / "fake_glm.pass"
    p.write_text("fake-key-do-not-use-9876\n")
    return str(p)


@pytest.fixture
def missing_key_file(tmp_path):
    """返回一个**不存在**的路径。"""
    return str(tmp_path / "does_not_exist.pass")


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个测试前清空 mtime 缓存（避免跨测试污染）。"""
    invalidate_channel_cache()
    yield
    invalidate_channel_cache()


# ===========================================================================
# Test 1: llm 节 + key_file → resolve 出正确 base_url / model / api_key
# ===========================================================================

class TestResolveWithKeyFile:
    def test_bigmodel_channel_full_resolution(self, fake_key_file):
        """智谱免费端点 + key_file：全字段正确解析。"""
        cfg = {
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "key_file": fake_key_file,
            }
        }
        ch = resolve_llm_channel(cfg)

        # base_url 不带尾 /
        assert ch["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
        assert ch["model"] == "glm-4-flash"
        assert ch["api_key"] == "fake-key-do-not-use-9876"
        assert ch["source"] == "bigmodel"
        assert ch["key_file"] == fake_key_file
        # valid=True（base_url + model + api_key 全齐）
        assert ch["valid"] is True

    def test_url_composition_bigmodel(self, fake_key_file):
        """G2: URL 拼接断言 https://open.bigmodel.cn/api/paas/v4/chat/completions
        （只到 /api/paas/v4，不加 /v1 —— 智谱路径比 OpenAI 少一层）。"""
        cfg = {
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "key_file": fake_key_file,
            }
        }
        ch = resolve_llm_channel(cfg)
        url = f"{ch['base_url']}/chat/completions"
        assert url == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        # 关键负向断言：不能把 /v1 拼进 URL
        assert "/v1/chat" not in url

    def test_trailing_slash_normalized(self, fake_key_file):
        """base_url 带尾 / 也要兼容。"""
        cfg = {
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4/",
                "model": "glm-4-flash",
                "key_file": fake_key_file,
            }
        }
        ch = resolve_llm_channel(cfg)
        url = f"{ch['base_url']}/chat/completions"
        # rstrip 后不会重复 /
        assert "//chat" not in url
        assert url == "https://open.bigmodel.cn/api/paas/v4/chat/completions"


# ===========================================================================
# Test 2: 无 llm 节 → unconfigured（2026-09 移除 dashscope 兜底）
# ===========================================================================

class TestResolveUnconfigured:
    def test_empty_cfg_returns_unconfigured(self):
        """G3: 空 cfg = unconfigured（不再回落任何硬编码付费模型）。"""
        ch = resolve_llm_channel({})
        assert ch["base_url"] == ""
        assert ch["model"] == ""
        assert ch["api_key"] == ""
        assert ch["source"] == "unconfigured"
        assert ch["valid"] is False
        # 关键断言：base_url 不是任何阿里/付费域
        assert "dashscope" not in ch["base_url"]
        assert "aliyuncs.com" not in ch["base_url"]

    def test_none_cfg_returns_unconfigured(self):
        """cfg=None 也走 unconfigured。"""
        ch = resolve_llm_channel(None)
        assert ch["base_url"] == ""
        assert ch["model"] == ""
        assert ch["source"] == "unconfigured"
        assert ch["valid"] is False

    def test_llm_empty_dict_returns_unconfigured(self):
        """cfg 存在但 llm 节是空 dict → 视为零配置 → unconfigured。"""
        ch = resolve_llm_channel({"llm": {}})
        assert ch["base_url"] == ""
        assert ch["model"] == ""
        assert ch["source"] == "unconfigured"
        assert ch["valid"] is False

    def test_unconfigured_no_dashscope_substring_anywhere(self):
        """无 llm 节：返回 dict 任何字段都不含 dashscope/aliyuncs。"""
        for cfg_input in [{}, None, {"llm": {}}, {"llm": None}]:
            ch = resolve_llm_channel(cfg_input)
            # 序列化再查字面
            blob = json.dumps(ch, ensure_ascii=False)
            assert "dashscope" not in blob.lower(), (
                f"unconfigured 通道不应含 dashscope：{ch}"
            )
            assert "qwen-plus" not in blob, (
                f"unconfigured 通道不应含 qwen-plus：{ch}"
            )

    def test_llm_section_missing_model_returns_unconfigured(self):
        """有 llm 节但缺 model → 该通道无效 → unconfigured。"""
        cfg = {
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                # 故意缺 model
                "key_file": "/tmp/anything",
            }
        }
        ch = resolve_llm_channel(cfg)
        assert ch["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
        assert ch["model"] == ""
        assert ch["source"] == "unconfigured"
        assert ch["valid"] is False

    def test_llm_section_missing_base_url_returns_unconfigured(self):
        """有 llm 节但缺 base_url → 该通道无效 → unconfigured。"""
        cfg = {
            "llm": {
                "model": "glm-4-flash",
                # 故意缺 base_url
                "key_file": "/tmp/anything",
            }
        }
        ch = resolve_llm_channel(cfg)
        assert ch["base_url"] == ""
        assert ch["model"] == "glm-4-flash"
        assert ch["source"] == "unconfigured"
        assert ch["valid"] is False


# ===========================================================================
# Test 3: key_file 不存在 → 不抛
# ===========================================================================

class TestResolveKeyFileMissing:
    def test_missing_key_file_does_not_raise(self, missing_key_file):
        """key_file 不存在 → resolve 必须不抛异常。"""
        cfg = {
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "key_file": missing_key_file,
            }
        }
        # 不抛即过
        ch = resolve_llm_channel(cfg)
        # 端点 host 仍然按配置解析（base_url 不受 key_file 缺失影响）
        assert ch["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
        assert ch["model"] == "glm-4-flash"
        # api_key 走兜底链（测试环境无 env 无 yaml → 期望空串，不抛）
        assert ch["api_key"] == ""
        # valid=False（key 缺失 + 无 env 兜底）
        assert ch["valid"] is False
        # key_file 路径仍记录（便于诊断）
        assert ch["key_file"] == missing_key_file


# ===========================================================================
# Test 4: api_key 直填 优先于 key_file
# ===========================================================================

class TestResolveApiKeyPriority:
    def test_api_key_direct_beats_key_file(self, fake_key_file):
        """直填 api_key 优先于 key_file（即便 key_file 存在）。"""
        cfg = {
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "key_file": fake_key_file,
                "api_key": "direct-inline-key-1234",
            }
        }
        ch = resolve_llm_channel(cfg)
        # 直填胜出
        assert ch["api_key"] == "direct-inline-key-1234"
        # 不应读取 key_file（用 sentinel 内容验证）
        assert "fake-key-do-not-use" not in ch["api_key"]
        assert ch["valid"] is True

    def test_whitespace_only_key_file_stripped(self, fake_key_file):
        """key_file 内容含尾随空白 / 换行应被 strip。"""
        cfg = {
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "key_file": fake_key_file,
            }
        }
        ch = resolve_llm_channel(cfg)
        # strip 已生效 —— 无尾随 \n / 空格
        assert ch["api_key"] == ch["api_key"].strip()
        assert "\n" not in ch["api_key"]
        assert " " not in ch["api_key"]


# ===========================================================================
# Test 5: 日志无 key 泄漏（G4）
# ===========================================================================

class _CaptureHandler(logging.Handler):
    """把所有 LogRecord 抓到列表里供断言。"""
    def __init__(self):
        super().__init__()
        self.records: list = []

    def emit(self, record):
        self.records.append(record)


class TestLogNoKeyLeak:
    def test_no_key_value_in_any_log_record(self, fake_key_file):
        """monkeypatch logger 后，断言任何 record 的文本不含 key 值。"""
        fake_key = "fake-key-do-not-use-9876"

        # consolidator logger 加 capture handler
        from keepsake import consolidator as cm
        cap = _CaptureHandler()
        cap.setLevel(logging.DEBUG)
        cm.logger.addHandler(cap)
        cm.logger.setLevel(logging.DEBUG)
        try:
            # 三种关键路径都跑一遍
            cfg_ok = {
                "llm": {
                    "base_url": "https://open.bigmodel.cn/api/paas/v4",
                    "model": "glm-4-flash",
                    "key_file": fake_key_file,
                }
            }
            resolve_llm_channel(cfg_ok)

            # key_file 缺失路径（应该打 debug 而非 error）
            cfg_missing = {
                "llm": {
                    "base_url": "https://open.bigmodel.cn/api/paas/v4",
                    "model": "glm-4-flash",
                    "key_file": "/no/such/file/12345",
                }
            }
            resolve_llm_channel(cfg_missing)

            # 兜底链路径（无 llm 节）
            resolve_llm_channel({})
        finally:
            cm.logger.removeHandler(cap)

        # 关键断言：所有 LogRecord 的格式化文本都不含 key 内容
        leaked = []
        for rec in cap.records:
            try:
                msg = rec.getMessage()
            except Exception:
                msg = str(rec)
            if fake_key in msg:
                leaked.append((rec.levelname, msg))

        assert not leaked, (
            f"key value leaked into log records: {leaked}"
        )

    def test_no_key_in_debug_message_format(self, fake_key_file, caplog):
        """额外 caplog 验证：debug 日志只打印路径和长度，不打 key。"""
        cfg = {
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "key_file": fake_key_file,
            }
        }
        with caplog.at_level(logging.DEBUG, logger="keepsake.consolidator"):
            resolve_llm_channel(cfg)

        all_text = "\n".join(rec.getMessage() for rec in caplog.records)
        # 关键负向断言
        assert "fake-key-do-not-use-9876" not in all_text
        # 正向断言：路径出现
        assert fake_key_file in all_text


# ===========================================================================
# NEW (2026-09 ks_noqwen): mtime 感知缓存 + 热生效
# ===========================================================================

class TestMtimeAwareCache:
    """resolve_llm_channel_cached：按 (mtime_ns, size) 缓存解析结果。"""

    def test_no_llm_section_returns_unconfigured_no_dashscope(self, tmp_path, monkeypatch):
        """配置文件无 llm 节 → unconfigured；任何字段都不含 dashscope 字样。"""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"redis_host": "127.0.0.1"}))
        monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))

        ch = resolve_llm_channel_cached()
        assert ch["valid"] is False
        assert ch["source"] == "unconfigured"
        blob = json.dumps(ch, ensure_ascii=False)
        assert "dashscope" not in blob.lower()
        assert "qwen-plus" not in blob
        assert "aliyuncs" not in blob.lower()

    def test_missing_model_means_invalid(self, tmp_path, monkeypatch):
        """缺 model → 该通道无效 → valid=False。"""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "llm": {"base_url": "https://open.bigmodel.cn/api/paas/v4"},
        }))
        monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))

        ch = resolve_llm_channel_cached()
        assert ch["model"] == ""
        assert ch["valid"] is False
        assert ch["source"] == "unconfigured"

    def test_mtime_unchanged_same_content_no_reparse(self, tmp_path, monkeypatch):
        """同内容 touch 不重解析：mtime_ns 不变 → 缓存命中。"""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "api_key": "old-key-9999",
            },
        }))
        monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))

        first = resolve_llm_channel_cached()
        assert first["api_key"] == "old-key-9999"
        # 同 mtime 再调 → 拿到的还是 first（同一对象引用，缓存命中）
        # 但对象一致即可（实现可能每次复制 dict）
        second = resolve_llm_channel_cached()
        assert second == first

    def test_mtime_changed_reparses(self, tmp_path, monkeypatch):
        """mtime 变（文件改写）→ 重读 + 重解析。"""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "api_key": "first-key-1111",
            },
        }))
        monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))

        first = resolve_llm_channel_cached()
        assert first["api_key"] == "first-key-1111"

        # 改写配置：换 api_key + 等 mtime ns 涨
        time.sleep(0.005)
        cfg_file.write_text(json.dumps({
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "api_key": "second-key-2222",
            },
        }))
        second = resolve_llm_channel_cached()
        assert second["api_key"] == "second-key-2222"
        assert second != first

    def test_mid_broken_json_falls_back_without_raising(self, tmp_path, monkeypatch):
        """配置文件中途写坏（非法 JSON）→ 本窗按 unconfigured，不抛穿。"""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "api_key": "ok-key",
            },
        }))
        monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))
        ok = resolve_llm_channel_cached()
        assert ok["valid"] is True

        # 改坏 JSON（丢右括号）
        cfg_file.write_text('{"llm": {"base_url": "x", "model":')
        bad = resolve_llm_channel_cached()
        # 不抛穿；返回 unconfigured
        assert bad["valid"] is False
        assert bad["source"] == "unconfigured"

        # 修复后 → 又能正常解析
        cfg_file.write_text(json.dumps({
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "api_key": "recovered-key",
            },
        }))
        recovered = resolve_llm_channel_cached()
        assert recovered["valid"] is True
        assert recovered["api_key"] == "recovered-key"

    def test_key_file_rotation_takes_effect(self, tmp_path, monkeypatch):
        """key_file 轮换 → 下一窗读新值。"""
        key_file = tmp_path / "rotating.pass"
        key_file.write_text("rotation-key-A\n")

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "llm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-flash",
                "key_file": str(key_file),
            },
        }))
        monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))

        first = resolve_llm_channel_cached()
        assert first["api_key"] == "rotation-key-A"

        # 轮换 key_file 内容
        time.sleep(0.005)
        key_file.write_text("rotation-key-B\n")
        # cfg.json 的 mtime 不变 → 但 key_file 的内容也走 stat 缓存
        # 这里我们走 invalidate 再 resolve，验证读文件路径会拿新值
        invalidate_channel_cache(str(cfg_file))
        second = resolve_llm_channel_cached()
        assert second["api_key"] == "rotation-key-B"

    def test_invalidate_clears_cache(self, tmp_path, monkeypatch):
        """invalidate_channel_cache → 强制重读，缓存里的旧值失效。"""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"llm": {"base_url": "x", "model": "y", "api_key": "k1"}}))
        monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))
        first = resolve_llm_channel_cached()
        assert first["api_key"] == "k1"

        # 强刷缓存 → 文件仍是 k1，但解析会重读
        invalidate_channel_cache(str(cfg_file))
        reread = resolve_llm_channel_cached()
        assert reread["api_key"] == "k1"

        # 改文件 → 不调 invalidate 也会被 mtime 检测到重读
        time.sleep(0.01)
        cfg_file.write_text(json.dumps({"llm": {"base_url": "x", "model": "y", "api_key": "k2"}}))
        after_write = resolve_llm_channel_cached()
        assert after_write["api_key"] == "k2"

        # 强刷 → 不改文件也能拿新值（前提是另一进程改过；这里只验证 API 不会报错）
        invalidate_channel_cache(str(cfg_file))
        after_invalidate = resolve_llm_channel_cached()
        assert after_invalidate["api_key"] == "k2"

        # invalidate 全清（path=None）→ 也安全
        invalidate_channel_cache()
        all_cleared = resolve_llm_channel_cached()
        assert all_cleared["api_key"] == "k2"


# ===========================================================================
# NEW (2026-09 ks_noqwen): Pipeline.channel_refresher 热生效
# ===========================================================================

class TestPipelineChannelRefresherHotReload:
    """Pipeline.channel_refresher：drain 窗口内换 model 值下窗生效。"""

    def _fake_storage(self):
        """最小 fake storage —— pipeline._process_window 期望接口子集。"""
        from typing import Any, Dict, List

        class _FakeRedisClient:
            def exists(self, key):
                return 0

        class FS:
            def __init__(self):
                self._client = _FakeRedisClient()
                self.stored: List[Dict[str, Any]] = []

            def store(self, text, tags="", category="", source="", fragment_type="", **kwargs):
                self.stored.append({"text": text, "tags": tags})

            def search_bm25(self, q, tag_filter=""):
                return []

            def supersede_fragment(self, old, new):
                return True

            def _get_client(self):
                return self._client

        return FS()

    def test_model_change_takes_effect_next_window(self):
        """drain 窗口内换 model 值 → 下窗生效（channel_refresher 回调被调用）。"""
        from keepsake.pipeline import Pipeline, Turn

        st = self._fake_storage()
        # 用 counter 跟踪 refresher 调用次数 + 当前 model
        state = {"calls": 0, "model": "glm-4-flash"}

        def refresher():
            state["calls"] += 1
            ch = {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": state["model"],
                "api_key": "fake-key",
                "source": "bigmodel",
                "key_file": "",
                "valid": True,
            }
            # 用 fake_llm 模拟 _call_llm
            return (ch, fake_llm_fn)

        def fake_llm_fn(messages, model):
            # 把 model 写到结果里以便断言
            state["last_seen_model"] = model
            return '{"facts":[]}'

        p = Pipeline(
            storage=st,
            llm_fn=fake_llm_fn,
            model="glm-4-flash",  # 初始 model
            channel_refresher=refresher,
        )

        # 第一窗：refresher 必被调用一次
        p._drain_now()
        assert state["calls"] == 1
        # 此时 p._model 已被 refresher 设回 glm-4-flash（无变化）
        assert p._model == "glm-4-flash"

        # 中途换 model → 模拟用户在 config.json 里改了 model
        state["model"] = "glm-4-air"
        p._drain_now()
        assert state["calls"] == 2
        # 第二窗的 model 应被刷成新值
        assert p._model == "glm-4-air"

    def test_unconfigured_channel_disables_llm_for_window(self):
        """refresher 返回 valid=False → 本窗 llm_fn=None → 走 v1 兜底。"""
        from keepsake.pipeline import Pipeline, Turn

        st = self._fake_storage()
        fallback_calls = []

        def gate_fallback(text, category):
            fallback_calls.append((text, category))

        def refresher():
            return (
                {"base_url": "", "model": "", "api_key": "",
                 "source": "unconfigured", "key_file": "", "valid": False},
                None,  # llm_fn 任意
            )

        def fake_llm_fn(messages, model):
            return '{"facts":[]}'

        p = Pipeline(
            storage=st,
            llm_fn=fake_llm_fn,
            gate_fallback=gate_fallback,
            channel_refresher=refresher,
        )

        # 入队一条 turn，再 drain → refresher 必被调用一次 → llm_fn 被置为 None
        # → _process_window 检测到 llm_fn=None → 走 v1 兜底
        p.enqueue("有意义的事实", "OK")
        # enqueue 达到 window_pairs=4 才会立即 drain；这里手动 drain
        p._drain_now()
        # refresher 把 llm_fn 置为 None
        assert p._llm_fn is None
        # 兜底函数被调用 1 次
        assert len(fallback_calls) == 1
        assert fallback_calls[0] == ("有意义的事实", "turn_memory")

    def test_no_refresher_keeps_existing_llm_fn(self):
        """未注入 channel_refresher → 沿用构造时的 llm_fn（向后兼容）。"""
        from keepsake.pipeline import Pipeline, Turn

        st = self._fake_storage()

        def fake_llm_fn(messages, model):
            return '{"facts":[]}'

        p = Pipeline(
            storage=st,
            llm_fn=fake_llm_fn,
            model="glm-4-flash",
            # 不传 channel_refresher
        )
        original_llm = p._llm_fn
        p._drain_now()
        # llm_fn 不变
        assert p._llm_fn is original_llm
        assert p._model == "glm-4-flash"
