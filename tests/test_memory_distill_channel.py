"""cron/memory_distill.py —— LLM 通道配置化单元测试。

任务书 ks_distill_channel 覆盖（G1/G2/G3/G4）:
  1. unconfigured channel (valid=False) → 主流程跳过且不推 watermark
  2. build_chat_request：URL 拼接（带/不带尾斜杠）/ Bearer 头 / model 取自 channel
     / 无任何硬编码模型名字面
  3. 响应解析：正常 choices 提取；```json 围栏仍能从 content 提取数组
  4. api_key 不出现在任何日志输出（caplog）
  5. 全 mock urlopen，零网络（任务书红线）

设计要点:
  * 全 mock 无网络（任务书红线）
  * 测试用假端点 http://127.0.0.1:9/v1 + 假 key（任务书红线）
  * sys.path 加 cron/ 目录拉起 memory_distill 模块
  * argparse 走 monkeypatch sys.argv 防止 pytest args 漏到主进程
"""

from __future__ import annotations

import json
import logging
import os
import sys
from unittest.mock import MagicMock
from urllib.error import URLError

import pytest

# 把 cron/ 加到 path —— memory_distill.py 是脚本而非包
_CRON_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cron"
)
if _CRON_DIR not in sys.path:
    sys.path.insert(0, _CRON_DIR)

import memory_distill  # noqa: E402


# ===========================================================================
# Fixtures
# ===========================================================================

FAKE_KEY = "FAKE_KEY_DO_NOT_LEAK_42"
FAKE_BASE_URL = "http://127.0.0.1:9/v1"

FAKE_CHANNEL = {
    "base_url": FAKE_BASE_URL,
    "model": "fake-model-1",
    "api_key": FAKE_KEY,
    "source": "test",
    "key_file": "",
    "valid": True,
}


@pytest.fixture
def fake_channel():
    return dict(FAKE_CHANNEL)


@pytest.fixture
def clean_argv(monkeypatch):
    """主流程用 argparse —— 屏蔽 pytest 的 -q 之类参数。"""
    monkeypatch.setattr(sys, "argv", ["memory_distill.py"])


@pytest.fixture
def enable_conf(monkeypatch):
    """绕开真实 ~/scripts/memory_distill.conf，强制走 enabled=True。"""
    monkeypatch.setattr(memory_distill, "load_conf", lambda: {})


# ===========================================================================
# Test 1: unconfigured channel → 跳过且不推 watermark
# ===========================================================================


class TestUnconfiguredChannelSkip:
    def test_unconfigured_skips_and_holds_watermark(
        self, capsys, monkeypatch, clean_argv, enable_conf
    ):
        """valid=False → main() 打 skip 文案 → return → watermark 写函数未被调用。"""
        unconfigured = {
            "base_url": "",
            "model": "",
            "api_key": "",
            "source": "unconfigured",
            "key_file": "",
            "valid": False,
        }
        monkeypatch.setattr(
            memory_distill, "resolve_llm_channel_cached", lambda: unconfigured
        )

        # watermark 写函数打 sentinel
        watermark_written: list = []
        monkeypatch.setattr(
            memory_distill, "write_watermark", lambda mid: watermark_written.append(mid)
        )

        # distill 也绝不该被调（前置拦截）
        distill_calls = []
        monkeypatch.setattr(
            memory_distill,
            "distill",
            lambda *a, **k: (distill_calls.append(1) or []),
        )

        # store_to_keepsake 也绝不该被调
        store_calls = []
        monkeypatch.setattr(
            memory_distill,
            "store_to_keepsake",
            lambda items, dry: (store_calls.append(1) or 0),
        )

        # 不抛穿 = exit 0
        rc = memory_distill.main()
        assert rc is None

        out = capsys.readouterr().out
        assert "llm channel unconfigured" in out
        assert "skip" in out.lower()
        assert "watermark held" in out

        # 关键负向断言：watermark 写函数未被调用、distill/store 也都未被调
        assert watermark_written == []
        assert distill_calls == []
        assert store_calls == []

    def test_unconfigured_returns_normally_for_cron(
        self, monkeypatch, clean_argv, enable_conf
    ):
        """unconfigured 时 main() 走正常 return 路径（cron 报绿、不抛异常）。"""
        monkeypatch.setattr(
            memory_distill,
            "resolve_llm_channel_cached",
            lambda: {
                "base_url": "",
                "model": "",
                "api_key": "",
                "source": "unconfigured",
                "key_file": "",
                "valid": False,
            },
        )
        # 不应抛任何异常
        result = memory_distill.main()
        assert result is None  # 等同 sys.exit(0)

    def test_valid_channel_proceeds_past_channel_gate(
        self, monkeypatch, clean_argv, enable_conf, fake_channel, capsys
    ):
        """valid=True → 通过通道闸门（distill/store 不应被拦在 unconfigured 之前）。"""
        monkeypatch.setattr(
            memory_distill, "resolve_llm_channel_cached", lambda: fake_channel
        )

        # 让 get_recent_messages 返空（避免真 SQL 触发）
        monkeypatch.setattr(
            memory_distill, "get_recent_messages", lambda *a, **k: ("", 0)
        )

        # 当 conv 为空 → 走到「无新对话」分支，watermark 仍不动
        watermark_written: list = []
        monkeypatch.setattr(
            memory_distill, "write_watermark", lambda mid: watermark_written.append(mid)
        )

        memory_distill.main()
        out = capsys.readouterr().out
        assert "llm channel unconfigured" not in out
        assert "无新对话" in out
        assert watermark_written == []


# ===========================================================================
# Test 2: build_chat_request 纯函数
# ===========================================================================


class TestBuildChatRequest:
    def test_url_without_trailing_slash(self, fake_channel):
        """base_url 不带尾 / → 拼成 .../v1/chat/completions。"""
        url, body, headers = memory_distill.build_chat_request(fake_channel, "hi")
        assert url == "http://127.0.0.1:9/v1/chat/completions"

    def test_url_with_trailing_slash(self, fake_channel):
        """base_url 带尾 / → rstrip 后不出现 //。"""
        ch = dict(fake_channel, base_url="http://127.0.0.1:9/v1/")
        url, _, _ = memory_distill.build_chat_request(ch, "hi")
        assert url == "http://127.0.0.1:9/v1/chat/completions"
        assert "//chat" not in url

    def test_bearer_header_uses_api_key(self, fake_channel):
        _, _, headers = memory_distill.build_chat_request(fake_channel, "hi")
        assert headers["Authorization"] == f"Bearer {fake_channel['api_key']}"
        assert headers["Content-Type"] == "application/json"

    def test_model_taken_from_channel_not_hardcoded(self, fake_channel):
        """model 字段取自 channel dict，函数内不出现任何硬编码常量。"""
        ch = dict(fake_channel, model="channel-specific-model-xyz")
        _, body, _ = memory_distill.build_chat_request(ch, "hi")
        payload = json.loads(body)
        assert payload["model"] == "channel-specific-model-xyz"
        # 负向断言：不应出现任何 OLLAMA/MODEL 旧硬编码字面
        assert "qwen" not in payload["model"].lower()
        assert "ollama" not in json.dumps(payload).lower()

    def test_messages_user_role_single_turn(self, fake_channel):
        _, body, _ = memory_distill.build_chat_request(fake_channel, "prompt 内容")
        payload = json.loads(body)
        assert payload["messages"] == [{"role": "user", "content": "prompt 内容"}]

    def test_temperature_and_max_tokens(self, fake_channel):
        _, body, _ = memory_distill.build_chat_request(fake_channel, "x")
        payload = json.loads(body)
        assert payload["temperature"] == 0.2
        # r2: 1024 → 4096，预算不足让思考段吃掉 → 截断 0 条
        assert payload["max_tokens"] == 4096

    def test_default_body_has_no_thinking_field(self, fake_channel):
        """默认 body 不含厂商特异字段（保持 OpenAI 兼容通道通用）。"""
        _, body, _ = memory_distill.build_chat_request(fake_channel, "hi")
        payload = json.loads(body)
        # 不应自动塞入 thinking / extra 字段
        assert "thinking" not in payload
        # 标准字段都还在
        assert payload["model"] == fake_channel["model"]
        assert payload["temperature"] == 0.2

    def test_extra_body_merged_into_body(self, fake_channel):
        """传 extra_body 时合并进 body（厂商特异字段走配置注入）。"""
        extra = {"thinking": {"type": "disabled"}, "top_p": 0.9}
        _, body, _ = memory_distill.build_chat_request(fake_channel, "hi", extra_body=extra)
        payload = json.loads(body)
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["top_p"] == 0.9
        # 标准字段不被覆盖
        assert payload["max_tokens"] == 4096
        assert payload["temperature"] == 0.2

    def test_extra_body_none_does_not_merge(self, fake_channel):
        """extra_body=None → 不合并任何字段（默认调用方不传也安全）。"""
        _, body, _ = memory_distill.build_chat_request(fake_channel, "hi", extra_body=None)
        payload = json.loads(body)
        assert "thinking" not in payload
        assert "top_p" not in payload

    def test_extra_body_overrides_defaults(self, fake_channel):
        """extra_body 字段与默认冲突时以 extra 为准（调用方意图优先）。"""
        # 注意：max_tokens 在 extra 中覆盖（极端场景）
        _, body, _ = memory_distill.build_chat_request(
            fake_channel, "hi", extra_body={"max_tokens": 2048}
        )
        payload = json.loads(body)
        assert payload["max_tokens"] == 2048  # extra 覆盖默认 4096

    def test_no_hardcoded_model_in_source(self, fake_channel):
        """防御性：源码扫描 —— build_chat_request 不接受也不允许出现任何旧硬编码模型名。"""
        ch = dict(fake_channel, model="")  # 即便空 model 也透传
        _, body, _ = memory_distill.build_chat_request(ch, "hi")
        payload = json.loads(body)
        blob = json.dumps(payload).lower()
        # 常见历史硬编码模型前缀都不应自动注入
        for forbidden in ["qwen3", "qwen2", "gpt-3.5", "gpt-4"]:
            assert forbidden not in blob, (
                f"硬编码模型名泄漏: {forbidden} 出现在 {payload}"
            )


# ===========================================================================
# Test 3: 响应解析 —— 正常 choices + ```json 围栏
# ===========================================================================


class TestDistillResponseParsing:
    def _patch_urlopen(self, monkeypatch, payload):
        """把 urlopen 换成返回 payload 的 fake。"""
        fake_resp = MagicMock()
        fake_resp.read = lambda: json.dumps(payload).encode()
        monkeypatch.setattr(
            memory_distill.urllib.request, "urlopen", lambda req, **kw: fake_resp
        )

    def test_normal_choices_extract(self, monkeypatch, fake_channel):
        """正常 choices 提取 → 数组中的 dict 全部带回。"""
        self._patch_urlopen(
            monkeypatch,
            {
                "choices": [
                    {
                        "message": {
                            "content": '[{"content": "用户偏好 Vim 编辑器", '
                            '"category": "preference", "tags": "vim,editor"}]'
                        }
                    }
                ]
            },
        )
        items = memory_distill.distill("对话正文" * 50, 4000, fake_channel)
        assert len(items) == 1
        assert items[0]["content"] == "用户偏好 Vim 编辑器"
        assert items[0]["category"] == "preference"
        assert items[0]["tags"] == "vim,editor"

    def test_json_fence_still_extracts(self, monkeypatch, fake_channel):
        """```json code fence 围裹仍能从 content 提取数组。"""
        fenced = (
            "```json\n"
            '[{"content": "项目用 Python 3.11 做后端", "category": "fact"}]\n'
            "```"
        )
        self._patch_urlopen(
            monkeypatch,
            {"choices": [{"message": {"content": fenced}}]},
        )
        items = memory_distill.distill("对话" * 50, 4000, fake_channel)
        assert len(items) == 1
        assert items[0]["content"] == "项目用 Python 3.11 做后端"
        assert items[0]["category"] == "fact"

    def test_short_content_filtered(self, monkeypatch, fake_channel):
        """<10 字内容被滤掉（既有行为保持）。"""
        self._patch_urlopen(
            monkeypatch,
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '[{"content": "嗯"}, '
                                '{"content": "用户偏好 Vim 编辑器超过 Emacs"}]'
                            )
                        }
                    }
                ]
            },
        )
        items = memory_distill.distill("对话" * 50, 4000, fake_channel)
        # 短的被滤，长的保留
        assert len(items) == 1
        assert "Vim" in items[0]["content"]

    def test_no_json_array_returns_empty(self, monkeypatch, fake_channel):
        """content 不含 JSON 数组 → 空列表，不抛。"""
        self._patch_urlopen(
            monkeypatch,
            {"choices": [{"message": {"content": "今天天气真好"}}]},
        )
        items = memory_distill.distill("对话" * 50, 4000, fake_channel)
        assert items == []

    def test_empty_choices_returns_empty(self, monkeypatch, fake_channel):
        """choices=[] → 走空路径 → 返 []。"""
        self._patch_urlopen(monkeypatch, {"choices": []})
        items = memory_distill.distill("对话" * 50, 4000, fake_channel)
        assert items == []

    def test_urlopen_error_returns_empty(self, monkeypatch, fake_channel, capsys):
        """urlopen 抛错 → 返回 []，日志含 host 不含 key。"""
        monkeypatch.setattr(
            memory_distill.urllib.request,
            "urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(URLError("connection refused")),
        )
        items = memory_distill.distill("对话" * 50, 4000, fake_channel)
        assert items == []
        err = capsys.readouterr().out
        assert "llm 调用失败" in err
        assert "127.0.0.1:9" in err  # host 出现
        assert fake_channel["api_key"] not in err  # api_key 不出现

    def test_truncation_when_conversation_too_long(self, monkeypatch, fake_channel, capsys):
        """>max_chars 时末尾截断 —— 取 conversation[-max_chars:]。"""
        captured = {}

        def fake_urlopen(req, **kw):
            captured["body"] = req.data
            fake_resp = MagicMock()
            fake_resp.read = lambda: json.dumps(
                {"choices": [{"message": {"content": "[]"}}]}
            ).encode()
            return fake_resp

        monkeypatch.setattr(memory_distill.urllib.request, "urlopen", fake_urlopen)
        long_conv = "X" * 5000
        memory_distill.distill(long_conv, 1000, fake_channel)
        # prompt 应只含最近 1000 字
        sent = json.loads(captured["body"])
        prompt_text = sent["messages"][0]["content"]
        # DISTILL_PROMPT 模板替换后会保留对话部分（最后 1000 X）
        assert prompt_text.endswith("X" * 1000)
        assert len(prompt_text) < 5000  # 截断生效

    def test_truncated_response_logs_and_returns_empty(
        self, monkeypatch, fake_channel, capsys
    ):
        """r2 截断防御：响应只有 `[` 无 `]` → 打 JSON extract failed 日志 + 返 []。

        真实场景：max_tokens 1024 + 思考段 → JSON 数组被截断 → text.rfind(']')=-1
        → 不推 watermark，但日志必须记录 finish_reason 便于排查预算/截断。
        """
        fake_resp = MagicMock()
        # 模拟截断：finish_reason=length，content 只有 '[' 开头没 ']' 收尾
        fake_resp.read = lambda: json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": '[{"content": "用户偏好 Vim 编辑器"}, '},
                    }
                ]
            }
        ).encode()
        monkeypatch.setattr(
            memory_distill.urllib.request,
            "urlopen",
            lambda req, **kw: fake_resp,
        )

        items = memory_distill.distill("对话" * 50, 4000, fake_channel)
        assert items == []  # 截断 → 0 条

        out = capsys.readouterr().out
        assert "JSON extract failed" in out
        assert "finish=length" in out  # finish_reason 透传进日志
        assert "len=" in out  # len 字段
        # api_key 仍不出现在日志
        assert fake_channel["api_key"] not in out

    def test_channel_request_extra_consumed_in_distill(
        self, monkeypatch, fake_channel
    ):
        """channel["request_extra"] 字段被 distill() 消费 → 请求体含之。

        不改 consolidator 也能让厂商特异字段经 config.json llm.request_extra 注入；
        若 consolidator 后续透传该字段，本链路即生效；当前测试用 mock channel 直接验证。
        """
        captured = {}

        def fake_urlopen(req, **kw):
            captured["body"] = req.data
            fake_resp = MagicMock()
            fake_resp.read = lambda: json.dumps(
                {"choices": [{"message": {"content": "[]"}}]}
            ).encode()
            return fake_resp

        monkeypatch.setattr(memory_distill.urllib.request, "urlopen", fake_urlopen)

        # mock channel 含 request_extra（厂商特异参数走该字段注入）
        ch_with_extra = dict(fake_channel)
        ch_with_extra["request_extra"] = {"thinking": {"type": "disabled"}}
        memory_distill.distill("对话" * 50, 4000, ch_with_extra)

        sent = json.loads(captured["body"])
        assert sent["thinking"] == {"type": "disabled"}
        # 标准字段不被覆盖
        assert sent["max_tokens"] == 4096

    def test_channel_without_request_extra_no_thinking_field(
        self, monkeypatch, fake_channel
    ):
        """channel 无 request_extra 字段 → 不注入 thinking（默认 OpenAI 兼容 body）。"""
        captured = {}

        def fake_urlopen(req, **kw):
            captured["body"] = req.data
            fake_resp = MagicMock()
            fake_resp.read = lambda: json.dumps(
                {"choices": [{"message": {"content": "[]"}}]}
            ).encode()
            return fake_resp

        monkeypatch.setattr(memory_distill.urllib.request, "urlopen", fake_urlopen)

        # fake_channel 本身没 request_extra
        assert "request_extra" not in fake_channel
        memory_distill.distill("对话" * 50, 4000, fake_channel)

        sent = json.loads(captured["body"])
        assert "thinking" not in sent  # 没注入，保持通道通用

    def test_json_load_failure_logs_finish_reason(
        self, monkeypatch, fake_channel, capsys
    ):
        """截断致 json.loads 抛 → 仍打 JSON extract failed 日志（与无 ] 同分支）。"""
        fake_resp = MagicMock()
        fake_resp.read = lambda: json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        # 形似有 ] 但 JSON 本身不合法（缺引号）
                        "message": {"content": '[{"content": unterminated'},
                    }
                ]
            }
        ).encode()
        monkeypatch.setattr(
            memory_distill.urllib.request,
            "urlopen",
            lambda req, **kw: fake_resp,
        )
        items = memory_distill.distill("对话" * 50, 4000, fake_channel)
        assert items == []
        out = capsys.readouterr().out
        assert "JSON extract failed" in out
        assert "finish=length" in out


# ===========================================================================
# Test 4: api_key 不出现在任何日志输出
# ===========================================================================


class TestApiKeyNotInLogs:
    def test_api_key_absent_from_distill_error_log(
        self, monkeypatch, fake_channel, capsys
    ):
        """distill() 异常日志绝不打印 api_key 值（仅 host）。"""
        monkeypatch.setattr(
            memory_distill.urllib.request,
            "urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(URLError("boom")),
        )
        memory_distill.distill("对话" * 50, 4000, fake_channel)
        out = capsys.readouterr().out
        assert fake_channel["api_key"] not in out
        assert "127.0.0.1:9" in out  # host 应出现（便于诊断）

    def test_api_key_absent_from_main_unconfigured_log(
        self, capsys, monkeypatch, clean_argv, enable_conf
    ):
        """unconfigured skip 路径打印也不含 api_key（即便 channel 字段异常带 key）。"""
        # 即使 api_key 非空，valid=False 也不该把它打到 stdout
        leak_ch = {
            "base_url": "http://127.0.0.1:9/v1",
            "model": "fake",
            "api_key": "LEAKY_KEY_PRESENT_BUT_UNCONFIGURED",
            "source": "test",
            "key_file": "",
            "valid": False,  # ← 关键：valid 仍是 False（缺 model/base_url 等）
        }
        monkeypatch.setattr(
            memory_distill, "resolve_llm_channel_cached", lambda: leak_ch
        )
        memory_distill.main()
        out = capsys.readouterr().out
        assert "LEAKY_KEY_PRESENT_BUT_UNCONFIGURED" not in out

    def test_api_key_absent_from_caplog_entire_suite(
        self, monkeypatch, fake_channel, caplog
    ):
        """caplog 抓取整轮 → 任何 logger record 都不应含 api_key。"""
        # 路径 1：urlopen 异常
        monkeypatch.setattr(
            memory_distill.urllib.request,
            "urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(URLError("boom")),
        )
        with caplog.at_level(logging.DEBUG):
            memory_distill.distill("对话" * 50, 4000, fake_channel)

            # 路径 2：正常响应
            fake_resp = MagicMock()
            fake_resp.read = lambda: json.dumps(
                {"choices": [{"message": {"content": "[]"}}]}
            ).encode()
            monkeypatch.setattr(
                memory_distill.urllib.request,
                "urlopen",
                lambda req, **kw: fake_resp,
            )
            memory_distill.distill("对话" * 50, 4000, fake_channel)

        all_text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert fake_channel["api_key"] not in all_text, (
            f"api_key 泄漏到 caplog: {[r.getMessage() for r in caplog.records]}"
        )

    def test_no_forbidden_literals_in_source_file(self):
        """源码静态扫描：cron/memory_distill.py 不含任何任务书禁字面。"""
        forbidden = [
            "api.minimaxi.com",
            "MiniMax-M3",
            "qwen3:8b",
            "127.0.0.1:11434",
            "OLLAMA",
        ]
        path = memory_distill.__file__
        with open(path, encoding="utf-8") as f:
            content = f.read()
        for needle in forbidden:
            assert needle not in content, (
                f"cron/memory_distill.py 含禁字面 {needle!r}（任务书红线）"
            )