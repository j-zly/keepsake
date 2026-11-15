"""LLM 协议适配层（`core/llm_http.py`）—— 双协议的请求形状与响应归一。

**不连真网**：`httpx.MockTransport` 顶上（请求路径 / 头 / body 全部可断言）。

本文件钉死的事：

  ① **openai 侧逐字不变**：同一个 URL、同一套头、同一份 body、同一个取值处
     —— 缺省协议（不写 `protocol`）与写 `protocol="openai"` **零差别**；
  ② **anthropic 侧形状对**：走 `/messages`、带 `x-api-key` + `anthropic-version`、
     system 消息被抽出来单独放、`max_tokens` 必填；
  ③ **响应归一**：两种协议都归一成一段字符串（anthropic 的 text 块按序拼接）；
  ④ **非法协议值回落 openai**（不抛穿），并给出看得见的告警文案；
  ⑤ **凭据统一读**：内联 `api_key` 优先，其次 key 文件（路径由配置给）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx
import pytest

from keepsake.core import llm_http

BASE = "https://api.example.invalid/v1"
MESSAGES = [
    {"role": "system", "content": "你是检索结果筛选器，只输出 JSON。"},
    {"role": "user", "content": "ping"},
]


def _capture(payload: Any, status: int = 200
             ) -> Tuple[List[Dict[str, Any]], Any]:
    """返回 `(录到的请求, handler)`：handler 直接回一个 JSON 回包。"""
    seen: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({
            "url": str(request.url),
            "path": request.url.path,
            "method": request.method,
            "headers": dict(request.headers),
            "body": json.loads(request.content.decode("utf-8")),
        })
        return httpx.Response(status, json=payload)

    return seen, handler


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _openai_cfg(**over: Any) -> Dict[str, Any]:
    cfg = {
        "base_url": BASE,
        "model": "M3.1-Flash",
        "api_key": "fake-key-for-test",
        "temperature": 0,
        "max_tokens": 4096,
    }
    cfg.update(over)
    return cfg


def _anthropic_cfg(**over: Any) -> Dict[str, Any]:
    cfg = {
        "base_url": BASE,
        "model": "claude-sonnet-5",
        "api_key": "fake-key-for-test",
        "protocol": "anthropic",
        "max_tokens": 1024,
        "temperature": 0,
    }
    cfg.update(over)
    return cfg


# ---------------------------------------------------------------- openai 侧

def test_openai_请求形状与现状逐字一致():
    """同一个 URL / 同一套头 / 同一份 body —— 与引入这层之前逐字相同。"""
    seen, handler = _capture({"choices": [{"message": {"content": "pong"}}]})
    out = llm_http.chat_completion(_openai_cfg(), MESSAGES,
                                   client=_client(handler))
    assert out == "pong"
    assert len(seen) == 1
    req = seen[0]
    assert req["method"] == "POST"
    assert req["url"] == f"{BASE}/chat/completions"
    assert req["headers"]["authorization"] == "Bearer fake-key-for-test"
    # body 只有 model + messages + 采样参数（不多不少）
    assert req["body"] == {
        "model": "M3.1-Flash",
        "messages": MESSAGES,
        "temperature": 0,
        "max_tokens": 4096,
    }


def test_openai_缺省协议零变化():
    """🔴 不写 protocol（老配置）= 写 openai，一字不差。"""
    seen_a, handler_a = _capture({"choices": [{"message": {"content": "x"}}]})
    llm_http.chat_completion(_openai_cfg(), MESSAGES, client=_client(handler_a))
    seen_b, handler_b = _capture({"choices": [{"message": {"content": "x"}}]})
    llm_http.chat_completion(_openai_cfg(protocol="openai"), MESSAGES,
                              client=_client(handler_b))
    assert seen_a[0]["url"] == seen_b[0]["url"]
    assert seen_a[0]["headers"]["authorization"] == \
        seen_b[0]["headers"]["authorization"]
    assert seen_a[0]["body"] == seen_b[0]["body"]


def test_openai_响应取choices里的正文():
    seen, handler = _capture(
        {"choices": [{"finish_reason": "stop", "message": {"content": "你好"}}]})
    out = llm_http.chat_completion_detail(_openai_cfg(), MESSAGES,
                                          client=_client(handler))
    assert out["text"] == "你好"
    assert out["finish_reason"] == "stop"
    assert out["protocol"] == "openai"


def test_openai_尾斜杠不重复拼():
    seen, handler = _capture({"choices": [{"message": {"content": "x"}}]})
    llm_http.chat_completion(_openai_cfg(base_url=BASE + "/"), MESSAGES,
                              client=_client(handler))
    assert seen[0]["url"] == f"{BASE}/chat/completions"


# ---------------------------------------------------------------- anthropic 侧

def test_anthropic_请求形状():
    """走 /messages，头是 x-api-key + anthropic-version，system 被抽出来。"""
    seen, handler = _capture({"content": [{"type": "text", "text": "pong"}]})
    out = llm_http.chat_completion(_anthropic_cfg(), MESSAGES,
                                   client=_client(handler))
    assert out == "pong"
    req = seen[0]
    assert req["method"] == "POST"
    assert req["url"] == f"{BASE}/messages"
    assert req["headers"]["x-api-key"] == "fake-key-for-test"
    assert req["headers"]["anthropic-version"] == "2023-06-01"
    body = req["body"]
    assert body["model"] == "claude-sonnet-5"
    # system 单独一条，不混在 messages 里
    assert body["system"] == "你是检索结果筛选器，只输出 JSON。"
    assert body["messages"] == [{"role": "user", "content": "ping"}]
    # max_tokens 必填
    assert body["max_tokens"] == 1024
    assert body["temperature"] == 0


def test_anthropic_max_tokens缺省给1024():
    seen, handler = _capture({"content": [{"type": "text", "text": "x"}]})
    cfg = _anthropic_cfg()
    cfg.pop("max_tokens")
    llm_http.chat_completion(cfg, MESSAGES, client=_client(handler))
    assert seen[0]["body"]["max_tokens"] == 1024


def test_anthropic_只取首条system消息():
    seen, handler = _capture({"content": [{"type": "text", "text": "x"}]})
    msgs = [{"role": "system", "content": "第一条"},
            {"role": "system", "content": "第二条"},
            {"role": "user", "content": "hi"}]
    llm_http.chat_completion(_anthropic_cfg(), msgs, client=_client(handler))
    body = seen[0]["body"]
    assert body["system"] == "第一条"
    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_anthropic_响应text块按序拼接():
    payload = {"content": [{"type": "text", "text": "甲"},
                           {"type": "thinking", "text": "（跳过）"},
                           {"type": "text", "text": "乙"}],
               "stop_reason": "end_turn"}
    seen, handler = _capture(payload)
    out = llm_http.chat_completion_detail(_anthropic_cfg(), MESSAGES,
                                          client=_client(handler))
    assert out["text"] == "甲乙"
    assert out["finish_reason"] == "end_turn"
    assert out["protocol"] == "anthropic"


# ---------------------------------------------------------------- 回落与告警

def test_非法协议值回落openai且带告警():
    """🔴 手改 config.json 写错协议名 ⇒ 按 openai 跑（不抛穿）+ 告警看得见。"""
    seen, handler = _capture({"choices": [{"message": {"content": "x"}}]})
    out = llm_http.chat_completion(_openai_cfg(protocol="bogus"), MESSAGES,
                                   client=_client(handler))
    assert out == "x"
    assert seen[0]["url"] == f"{BASE}/chat/completions"
    assert llm_http.normalize_protocol("bogus") == "openai"
    warn = llm_http.protocol_warning("bogus")
    assert warn and "不是合法值" in warn


def test_协议归一与告警的边界():
    assert llm_http.normalize_protocol("") == "openai"
    assert llm_http.normalize_protocol(None) == "openai"
    assert llm_http.normalize_protocol("Anthropic") == "anthropic"
    assert llm_http.protocol_warning("") == ""
    assert llm_http.protocol_warning("anthropic") == ""
    assert llm_http.protocol_warning("openai") == ""


# ---------------------------------------------------------------- 失败口径

def test_状态错抛状态异常_带状态码与错误体():
    seen, handler = _capture({"error": "boom"}, status=429)
    with pytest.raises(llm_http.LlmHttpStatusError) as ei:
        llm_http.chat_completion(_openai_cfg(), MESSAGES, client=_client(handler))
    assert ei.value.status_code == 429
    assert "boom" in ei.value.body


def test_形状不对抛坏响应异常():
    seen, handler = _capture({"choices": []})
    with pytest.raises(llm_http.LlmBadResponseError):
        llm_http.chat_completion(_openai_cfg(), MESSAGES, client=_client(handler))


def test_非法JSON抛解码异常():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="{ 这不是 json".encode("utf-8"))

    with pytest.raises(ValueError):
        llm_http.chat_completion(_openai_cfg(), MESSAGES, client=_client(handler))


# ---------------------------------------------------------------- 凭据

def test_凭据优先内联其次key文件(tmp_path: Path):
    key_file = tmp_path / "llm.key"
    key_file.write_text("from-file", encoding="utf-8")
    # 内联优先
    assert llm_http.resolve_api_key(
        {"api_key": "inline", "key_file": str(key_file)}) == "inline"
    # 其次 key 文件
    assert llm_http.resolve_api_key({"key_file": str(key_file)}) == "from-file"
    # api_key_file 也认（筛选节那套字段名）
    assert llm_http.resolve_api_key({"api_key_file": str(key_file)}) == "from-file"
    # 都没有 ⇒ 空串（调用方按「无凭据」降级）
    assert llm_http.resolve_api_key({}) == ""
    assert llm_http.resolve_api_key({"key_file": str(tmp_path / "nope.key")}) == ""


def test_凭据从key文件读出来进请求头(tmp_path: Path):
    key_file = tmp_path / "llm.key"
    key_file.write_text("file-key", encoding="utf-8")
    seen, handler = _capture({"choices": [{"message": {"content": "x"}}]})
    llm_http.chat_completion(_openai_cfg(api_key="", key_file=str(key_file)),
                              MESSAGES, client=_client(handler))
    assert seen[0]["headers"]["authorization"] == "Bearer file-key"
