"""LLM 协议适配层（openai / anthropic）—— **只换 transport，不动判分逻辑与 prompt**。

## 为什么要这一层

2026-11-15 用户拍板：**每张 LLM 类卡可独立选协议**。此前三处 LLM 调用
（提炼/合并通道、检索筛选、控制台连通检测）各自硬编码「OpenAI 兼容
`{base}/chat/completions` + Bearer」，换个协议就得改三处代码。本模块把
「怎么拼请求、怎么读响应」收成一处，调用方只管传配置与消息。

## 协议

* `openai`（**缺省**）：`POST {base_url}/chat/completions`，头 `Authorization: Bearer …`，
  body = `model` + `messages` + cfg 里其余参数原样透传；
  响应取 `choices[0].message.content`。
* `anthropic`：`POST {base_url}/messages`（**base_url 约定与 openai 侧同款**：
  含版本根，如 `https://api.anthropic.com/v1`），头 `x-api-key` +
  `anthropic-version: 2023-06-01`；body 里 `system` 消息抽出来单独放
  （**只取首条 system 消息**），`messages` 只留非 system；`max_tokens` **必填**
  （缺省给 cfg 的值，再缺省 1024）；响应取 `content` 里的 text 块按序拼接。

## 兼容口径

* `cfg["protocol"]` 缺省/空 ⇒ `openai`，**行为与改造前逐字一致**（同一个 URL、
  同一套头、同一份 body、同一处取值）；
* 非法协议值 ⇒ 回落 `openai`（**绝不抛穿**：手改 config.json 写错协议名不该
  让整条通道瘫掉），告警文案由 `protocol_warning()` 出，调用方塞进检测回包；
* 凭据沿用各自现有的 key 文件机制（**不改 config 形状**）：内联 `api_key` 优先，
  其次 `key_file` / `api_key_file` 指向的文件，由本模块的 `resolve_api_key()` 统一读。

## cfg 契约

`cfg` 里属于**传输层**的键（`protocol` / `base_url` / `model` / `api_key` /
`key_file` / `api_key_file` / `request_extra`）**不进 body**；其余键**原样透传**
进 body（`request_extra` 单独合并在最后，可覆盖默认参数 —— 与 consolidator
的既有语义一致）。**调用方只把自己要发出去的参数放进 cfg**。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from .http import get_client

logger = logging.getLogger("keepsake.core.llm_http")

#: 协议取值域。
PROTOCOL_OPENAI = "openai"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOLS: Tuple[str, ...] = (PROTOCOL_OPENAI, PROTOCOL_ANTHROPIC)

#: anthropic 的 API 版本头（固定值 —— 协议的一部分，不是配置）。
ANTHROPIC_VERSION = "2023-06-01"

#: anthropic 必填 `max_tokens`，cfg 也没给时的兜底。
ANTHROPIC_DEFAULT_MAX_TOKENS = 1024

#: 默认超时（秒）—— 调用方一般都会显式给。
DEFAULT_TIMEOUT_S = 30.0

#: cfg 里属于「传输层」的键 —— **不进请求 body**。
_TRANSPORT_KEYS = frozenset({
    "protocol", "base_url", "model",
    "api_key", "key_file", "api_key_file",
    "request_extra",
})


class LlmHttpError(Exception):
    """LLM 请求失败的基类（**不抛穿调用方**：各自降级）。"""

    def __init__(self, message: str, *, status_code: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.body = str(body or "")


class LlmHttpStatusError(LlmHttpError):
    """HTTP 状态 >= 400（429 单独可辨 —— 限流值得重试，其余不）。"""


class LlmBadResponseError(LlmHttpError, ValueError):
    """响应**形状**不对（choices 为空 / content 里没有文本块）。

    刻意与 `LlmHttpStatusError` 分开：这是「模型回得不对」，不是「请求没发出去」。
    """


# --------------------------------------------------------------------------
# 协议值：归一 + 告警
# --------------------------------------------------------------------------

def normalize_protocol(value: Any) -> str:
    """协议值归一：**缺省/空 = openai**；非法值也回落 openai（不抛穿）。"""
    v = str(value or "").strip().lower()
    return v if v in PROTOCOLS else PROTOCOL_OPENAI


def protocol_warning(value: Any) -> str:
    """非法协议值的人话告警（合法值 / 缺省 ⇒ 空串）。

    调用方把它塞进检测回包 —— 手改 config.json 写错协议名要**看得见**，
    而不是安静地按 openai 跑。
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.lower() in PROTOCOLS:
        return ""
    return (f"协议 {raw} 不是合法值（只认 openai / anthropic），已按 openai 处理")


# --------------------------------------------------------------------------
# 凭据：统一读（内联优先，其次 key 文件）
# --------------------------------------------------------------------------

def resolve_api_key(cfg: Optional[Dict[str, Any]]) -> str:
    """凭据真值：内联 `api_key` 优先，其次 `key_file` / `api_key_file` 指向的文件。

    沿用各节现有的 key 文件机制（**不改 config 形状**）；读不到 ⇒ 空串
    （由调用方按「无凭据」降级）。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    key = str(cfg.get("api_key") or "").strip()
    if key:
        return key
    for name in ("key_file", "api_key_file"):
        path = str(cfg.get(name) or "").strip()
        if not path:
            continue
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.warning("keepsake: LLM key 文件读不到（%s: %s）—— 按无 key 降级",
                           path, type(e).__name__)
            continue
    return ""


# --------------------------------------------------------------------------
# 请求构造
# --------------------------------------------------------------------------

def _passthrough(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """cfg 里要进 body 的参数（传输层键与 model/messages 除外）。"""
    return {k: v for k, v in cfg.items()
            if k not in _TRANSPORT_KEYS and k not in ("model", "messages")}


def _openai_request(cfg: Dict[str, Any], messages: Sequence[Dict[str, Any]]
                    ) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """openai 兼容请求（**与改造前逐字一致**：同 URL、同头、同 body）。"""
    base = str(cfg.get("base_url") or "").rstrip("/")
    model = str(cfg.get("model") or "").strip()
    body: Dict[str, Any] = {"model": model, "messages": list(messages or [])}
    body.update(_passthrough(cfg))
    extra = cfg.get("request_extra")
    if isinstance(extra, dict) and extra:
        body.update(extra)
    headers = {
        "Authorization": f"Bearer {resolve_api_key(cfg)}",
        "Content-Type": "application/json",
    }
    return f"{base}/chat/completions", headers, body


def _anthropic_request(cfg: Dict[str, Any], messages: Sequence[Dict[str, Any]]
                       ) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """anthropic 请求：`/messages` + `x-api-key` + system 抽出 + max_tokens 必填。"""
    base = str(cfg.get("base_url") or "").rstrip("/")
    model = str(cfg.get("model") or "").strip()

    system = ""
    msgs: List[Dict[str, Any]] = []
    for m in messages or []:
        role = str(m.get("role") or "user")
        content = m.get("content")
        if role == "system":
            if not system:      # 只取**首条** system 消息
                system = content if isinstance(content, str) \
                    else json.dumps(content, ensure_ascii=False)
            continue
        msgs.append({"role": role, "content": content})

    try:
        max_tokens = int(cfg.get("max_tokens"))
    except (TypeError, ValueError):
        max_tokens = ANTHROPIC_DEFAULT_MAX_TOKENS
    if max_tokens <= 0:
        max_tokens = ANTHROPIC_DEFAULT_MAX_TOKENS

    body: Dict[str, Any] = {
        "model": model,
        "messages": msgs,
        "max_tokens": max_tokens,
    }
    if system:
        body["system"] = system
    for k, v in _passthrough(cfg).items():
        if k in ("model", "messages", "system", "max_tokens"):
            continue        # 结构性键不许被透传覆盖
        body[k] = v

    headers = {
        "x-api-key": resolve_api_key(cfg),
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    return f"{base}/messages", headers, body


def build_request(cfg: Optional[Dict[str, Any]], messages: Sequence[Dict[str, Any]]
                  ) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """按协议分叉，产出 `(url, headers, body)`（**纯函数**：不发请求）。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    if normalize_protocol(cfg.get("protocol")) == PROTOCOL_ANTHROPIC:
        return _anthropic_request(cfg, messages)
    return _openai_request(cfg, messages)


# --------------------------------------------------------------------------
# 响应归一
# --------------------------------------------------------------------------

def _text_of(content: Any) -> str:
    """openai 侧 content：字符串直接用；块列表（部分兼容厂商）按 text 块拼。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text") or ""))
            elif isinstance(blk, str):
                parts.append(blk)
        return "".join(parts)
    return str(content)


def _normalize_response(payload: Any, protocol: str) -> Tuple[str, str]:
    """响应 → `(正文, 结束原因)`；形状不对 ⇒ `LlmBadResponseError`。"""
    if not isinstance(payload, dict):
        raise LlmBadResponseError("响应不是 JSON 对象")

    if protocol == PROTOCOL_ANTHROPIC:
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise LlmBadResponseError("响应里没有 content 块")
        parts = [str(b.get("text") or "") for b in blocks
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts), str(payload.get("stop_reason") or "")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmBadResponseError("响应里没有 choices")
    first = choices[0] if isinstance(choices[0], dict) else {}
    msg = first.get("message")
    msg = msg if isinstance(msg, dict) else {}
    return _text_of(msg.get("content")), str(first.get("finish_reason") or "")


# --------------------------------------------------------------------------
# 发请求
# --------------------------------------------------------------------------

def _timeout_of(cfg: Dict[str, Any], timeout: Optional[float]) -> float:
    if timeout is not None:
        try:
            return float(timeout)
        except (TypeError, ValueError):
            pass
    try:
        return float(cfg.get("timeout_s"))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


def _read_json(resp: Any) -> Any:
    """读响应体：优先 `resp.json()`；没有这个方法就用 `resp.content` 解码。

    （httpx.Response 两者都有；测试替身常常只给其中一种。）
    """
    reader = getattr(resp, "json", None)
    if callable(reader):
        return reader()
    content = getattr(resp, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    return json.loads(content)


def chat_completion_detail(cfg: Optional[Dict[str, Any]],
                           messages: Sequence[Dict[str, Any]],
                           *, timeout: Optional[float] = None,
                           client: Optional[httpx.Client] = None,
                           body_bytes: bool = False) -> Dict[str, Any]:
    """发一条 chat 请求 → `{"text", "finish_reason", "protocol"}`。

    * `client` 缺省 ⇒ 用 `core.http.get_client` 的模块级单例（连接池复用）；
      调用方也可以把自己的 client 传进来（测试 / 自带超时口径）。
    * `body_bytes=True` ⇒ 请求体预编码成字节再发（`content=`）—— 提炼通道
      **逐字保持**它今天发的那份字节；默认 False = 交给 httpx 用 `json=` 编码。
    * 失败一律**抛**（`LlmHttpStatusError` / `LlmBadResponseError` /
      `json.JSONDecodeError` / httpx 的传输异常），由调用方各自降级 ——
      筛选要 fail-open 放行、提炼要重试、控制台要结构化回包，口径各不相同。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    protocol = normalize_protocol(cfg.get("protocol"))
    url, headers, body = build_request(cfg, messages)
    base = str(cfg.get("base_url") or "").rstrip("/")
    t = _timeout_of(cfg, timeout)
    cl = client if client is not None else get_client(base, t)

    if body_bytes:
        resp = cl.post(url, content=json.dumps(body).encode("utf-8"),
                       headers=headers)
    else:
        resp = cl.post(url, json=body, headers=headers)
    status = int(getattr(resp, "status_code", 200) or 200)
    if status >= 400:
        raise LlmHttpStatusError(f"HTTP {status}: {url}", status_code=status,
                                 body=str(getattr(resp, "text", "") or ""))
    payload = _read_json(resp)     # 非法 JSON ⇒ json.JSONDecodeError / ValueError
    text, finish = _normalize_response(payload, protocol)
    return {"text": text, "finish_reason": finish, "protocol": protocol}


def chat_completion(cfg: Optional[Dict[str, Any]],
                    messages: Sequence[Dict[str, Any]],
                    *, timeout: Optional[float] = None,
                    client: Optional[httpx.Client] = None,
                    body_bytes: bool = False) -> str:
    """发一条 chat 请求 → 模型正文（字符串）。

    三处 LLM 调用（提炼/合并、检索筛选、控制台检测）**全部走这里**。
    """
    return chat_completion_detail(cfg, messages, timeout=timeout,
                                  client=client,
                                  body_bytes=body_bytes)["text"]
