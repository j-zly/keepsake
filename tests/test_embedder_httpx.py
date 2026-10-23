"""ks_perf_stdlib —— embedder 换 httpx + tenacity 后的行为钉死（2026-10）。

覆盖任务书验收点：
  1. **正常批量向量**：一次请求拿到向量（httpx 假响应，零网络）；
  2. **429 重试后成功**：第一次 429 → 退避后重试 → 拿到向量；
  3. **重试耗尽走原降级路径**：4 次尝试（首次 + 3 次重试）后退避表 1s/2s/4s，
     最终**返回 None**（与改造前逐字一致的降级链：debug 日志 + None）；
  4. **400 不重试直接上抛**：只打一次请求；HTTPStatusError 原样上抛到既有
     `except Exception` 分支 ⇒ 仍然返回 None（**既有降级语义一字未改**）。

设计要点：
  * 全 mock httpx（monkeypatch `httpx.Client`）+ 清空客户端缓存 ⇒ 零网络、
    零真实连接；
  * `time.sleep` 被换成记录器 ⇒ 断言退避表是 1s/2s/4s，且测试不真的睡 7 秒；
  * 既有断言（`tests/test_embedder_dimensions.py`）**一字未改** —— 本文件只
    新增 httpx/tenacity 路径的断言。
"""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest

from keepsake.core import http as http_mod
from keepsake.embedder import OpenAIEmbedder

# 登记模型（维度已知 ⇒ 不会触发「维度不符」告警）
_MODEL = "bge-m3"
_DIM = 1024
_BASE_URL = "https://embed.example.com/v1"


def _vector_response(status_code: int = 200, dim: int = _DIM) -> httpx.Response:
    """造一个 httpx 真响应（带 request —— raise_for_status 需要它）。"""
    request = httpx.Request("POST", f"{_BASE_URL}/embeddings")
    return httpx.Response(status_code,
                          json={"data": [{"embedding": [0.1] * dim}]},
                          request=request)


@pytest.fixture
def fake_httpx(monkeypatch):
    """把 `httpx.Client` 换成假客户端 —— 返回调用记录器。

    * `state["responses"]` —— 每次 post 取一个；**只剩最后一个时反复用它**
      （⇒ 「一直 429」这种场景好写）；
    * `calls` —— 每次 post 记一笔（url / body / headers）；
    * 客户端缓存同时清空（`core.http._CLIENTS`）⇒ 每个用例都拿到干净连接池。
    """
    calls: List[Dict[str, Any]] = []
    built: List[Any] = []
    state: Dict[str, Any] = {"responses": []}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            built.append(self)

        def post(self, url, content=None, headers=None, **kwargs):
            calls.append({"url": url, "content": content, "headers": headers or {}})
            queue = state["responses"]
            if not queue:
                raise AssertionError("假客户端没有可返回的响应")
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    monkeypatch.setattr(http_mod, "_CLIENTS", {})
    return SimpleNamespace(calls=calls, built=built, state=state)


@pytest.fixture
def sleeps(monkeypatch):
    """把 `time.sleep` 换成记录器（tenacity 的退避表断言用；不真的睡）。"""
    recorded: List[float] = []

    def _fake_sleep(seconds):
        recorded.append(float(seconds))

    monkeypatch.setattr(time, "sleep", _fake_sleep)
    return recorded


def _embedder() -> OpenAIEmbedder:
    return OpenAIEmbedder(api_key="sk-fake-test-key",
                          base_url=_BASE_URL, model=_MODEL)


# ---------------------------------------------------------------------------
# 1. 正常批量向量
# ---------------------------------------------------------------------------

class TestNormalEmbedding:
    def test_returns_vector_and_calls_once(self, fake_httpx):
        """200 → 拿到向量，只打一次请求。"""
        fake_httpx.state["responses"] = [_vector_response(200)]
        vec = _embedder().get_embedding("一段中文")
        assert vec is not None and len(vec) == _DIM
        assert vec[0] == 0.1
        assert len(fake_httpx.calls) == 1

    def test_request_carries_auth_and_model(self, fake_httpx):
        """请求头与 body 与改造前一致（Bearer + model/input）。"""
        fake_httpx.state["responses"] = [_vector_response(200)]
        _embedder().get_embedding("一段中文")
        call = fake_httpx.calls[0]
        assert call["url"] == f"{_BASE_URL}/embeddings"
        assert call["headers"]["Authorization"] == "Bearer sk-fake-test-key"
        body = json.loads(call["content"].decode("utf-8"))
        assert body["model"] == _MODEL
        assert body["input"] == "一段中文"

    def test_client_is_reused_across_calls(self, fake_httpx):
        """连接池复用：两次 embed 只造**一个** client（省 TLS 握手）。"""
        fake_httpx.state["responses"] = [_vector_response(200)]
        emb = _embedder()
        emb.get_embedding("一")
        emb.get_embedding("二")
        assert len(fake_httpx.built) == 1
        assert len(fake_httpx.calls) == 2


# ---------------------------------------------------------------------------
# 2. 429 重试后成功
# ---------------------------------------------------------------------------

class TestRetryOn429:
    def test_429_then_success(self, fake_httpx, sleeps):
        """第一次 429 → 退避 1s → 重试成功。"""
        fake_httpx.state["responses"] = [_vector_response(429), _vector_response(200)]
        vec = _embedder().get_embedding("一段中文")
        assert vec is not None and len(vec) == _DIM
        assert len(fake_httpx.calls) == 2
        assert sleeps == [1.0], f"退避表应是 1s/2s/4s；实际 {sleeps}"

    def test_connection_error_then_success(self, fake_httpx, sleeps):
        """连接类异常（ConnectError）同样重试。"""
        fake_httpx.state["responses"] = [
            httpx.ConnectError("connection refused"), _vector_response(200),
        ]
        vec = _embedder().get_embedding("一段中文")
        assert vec is not None
        assert len(fake_httpx.calls) == 2
        assert sleeps == [1.0]


# ---------------------------------------------------------------------------
# 3. 重试耗尽 → 原降级路径
# ---------------------------------------------------------------------------

class TestRetryExhaustedFallsBack:
    def test_returns_none_after_three_retries(self, fake_httpx, sleeps, caplog):
        """429 一直不变 → 首次 + 3 次重试全打 → 退避 1s/2s/4s → 返回 None。"""
        fake_httpx.state["responses"] = [_vector_response(429)]
        with caplog.at_level(logging.DEBUG, logger="keepsake.embedder"):
            vec = _embedder().get_embedding("一段中文")
        assert vec is None, "重试耗尽后必须走原降级路径（返回 None）"
        assert len(fake_httpx.calls) == 4, "首次 + 3 次重试 = 4 次尝试"
        assert sleeps == [1.0, 2.0, 4.0], f"退避表应是 1s/2s/4s；实际 {sleeps}"

    def test_keeps_original_failure_trace(self, fake_httpx, sleeps, caplog):
        """降级留痕不变：`embedder: request failed: <原异常>`（debug 一行）。"""
        fake_httpx.state["responses"] = [_vector_response(429)]
        with caplog.at_level(logging.DEBUG, logger="keepsake.embedder"):
            _embedder().get_embedding("一段中文")
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "embedder: request failed" in text
        assert "429" in text, f"原异常文案应保留；实际 {text}"


# ---------------------------------------------------------------------------
# 4. 400 不重试 —— 原异常上抛到既有 handler
# ---------------------------------------------------------------------------

class TestClientErrorNotRetried:
    def test_400_called_once_and_returns_none(self, fake_httpx, sleeps, caplog):
        """400 是「请求本身错」⇒ 不重试（只打一次），异常原样上抛 ⇒ 仍返 None。"""
        fake_httpx.state["responses"] = [_vector_response(400)]
        with caplog.at_level(logging.DEBUG, logger="keepsake.embedder"):
            vec = _embedder().get_embedding("一段中文")
        assert vec is None, "既有降级语义：返回 None（不改）"
        assert len(fake_httpx.calls) == 1, "400 不该重试"
        assert sleeps == [], "400 不该退避"
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "embedder: request failed" in text
        assert "400" in text, f"HTTPStatusError 原样上抛（文案保留）；实际 {text}"
