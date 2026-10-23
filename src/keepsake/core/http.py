"""共享 HTTP 栈 —— httpx 连接池 + tenacity 重试（2026-10 ks_perf_stdlib）。

**为什么多这一层**：改造前 embedder 与 LLM 通道各写一套 `urllib.request`
（各自新建连接、各自一套超时/重试口径）。本模块把两件事收成**单一真值源**：

  * `get_client(base_url, timeout)` —— **模块级 httpx.Client 复用**：同一 base_url
    共用一个连接池 ⇒ 省掉每次请求的 TCP + TLS 握手（硅基流动 embed API 实测
    单发 ~190ms，复用后连接开销归零）；base_url 变 ⇒ 换新 client。
  * `retrying(...)` —— **tenacity 重试策略**：429（限流）与连接类异常
    （连不上/超时/网络错误）重试 3 次，退避 **1s / 2s / 4s**（指数退避）。
    其余 HTTP 状态（400/401/404/500…）**不重试** —— 请求本身有问题，
    重试只是浪费配额和时间。

纪律：
  * 只依赖 httpx + tenacity（都已是运行时依赖），不引入第三个 HTTP 库；
  * 重试耗尽 ⇒ **原异常原样上抛**（`reraise=True`），不包成 `RetryError` ——
    上游（embedder / consolidator）原有的降级分支因此一字不用改。
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, Optional, Tuple

import httpx
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

#: 可重试的 HTTP 状态码 —— 只有限流（429）。其余 4xx/5xx 不重试。
RETRY_STATUS_CODES = frozenset({429})

#: 重试次数（**不含**首次尝试）—— 3 次 ⇒ 最多 4 次尝试。
MAX_RETRIES = 3

#: 指数退避表：1s / 2s / 4s。
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 4.0

#: 客户端缓存：`{(base_url, timeout): httpx.Client}` —— 进程级、线程安全。
_CLIENTS: Dict[Tuple[str, float], httpx.Client] = {}
_CLIENT_LOCK = threading.Lock()


def get_client(base_url: str, timeout: float = 30.0) -> httpx.Client:
    """取 `base_url` 的**模块级 httpx.Client 单例**（连接池复用）。

    同一 `(base_url, timeout)` 永远拿到同一个 client ⇒ 连接/TLS 握手只付一次；
    base_url 变了自然换新 client（旧的不再复用）。

    🔴 client **不带默认 Authorization 头** —— 凭据逐请求带（同一 base_url 下
    不同调用方的 key 可以不同，共用 client 不会串号）。
    """
    key = (str(base_url), float(timeout))
    with _CLIENT_LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            client = httpx.Client(timeout=httpx.Timeout(float(timeout)))
            _CLIENTS[key] = client
        return client


def close_clients() -> None:
    """关掉并清空全部缓存 client（测试 / 进程收尾用）。"""
    with _CLIENT_LOCK:
        for client in _CLIENTS.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001 —— 收尾不该抛
                pass
        _CLIENTS.clear()


def is_retryable(exc: BaseException) -> bool:
    """这个异常值得重试吗？—— **429 与连接类异常**值得，其余不。

    * `httpx.HTTPStatusError` ⇒ 只看状态码在不在 `RETRY_STATUS_CODES`；
    * `httpx.TransportError` ⇒ 连接失败 / 连接超时 / 读超时 / 写超时 / 池超时 /
      网络错误 —— 全是「再试一次可能就好」的那一类。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRY_STATUS_CODES
    return isinstance(exc, httpx.TransportError)


def retrying(
    *,
    retries: int = MAX_RETRIES,
    extra_retryable: Optional[Callable[[BaseException], bool]] = None,
    before_sleep: Optional[Callable[[Any], None]] = None,
) -> Retrying:
    """构造 tenacity 重试器：退避 1s/2s/4s，只重试 429 与连接类异常。

    * `retries` —— 重试次数（不含首次尝试）；`0` ⇒ 只试一次（等价于不重试）。
    * `extra_retryable` —— 额外可重试判据（例：LLM 通道的坏 JSON —— 旧手写
      重试覆盖它，改造后保持不变）。
    * `before_sleep` —— 每次重试前的回调（用来留痕）。
    * `reraise=True` ⇒ 重试耗尽时**原异常原样上抛**（不包 `RetryError`）。
    """
    predicate = retry_if_exception(
        lambda exc: is_retryable(exc)
        or (extra_retryable is not None and extra_retryable(exc))
    )
    return Retrying(
        stop=stop_after_attempt(retries + 1),
        wait=wait_exponential(multiplier=BACKOFF_BASE_S,
                              min=BACKOFF_BASE_S, max=BACKOFF_MAX_S),
        retry=predicate,
        before_sleep=before_sleep,
        reraise=True,
    )
