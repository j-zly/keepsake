"""SSE 事件端点（`GET /events`）—— 私信实时推送的**快车道**。

2026-10 ks_dm_sse。参照**飞书 WebSocket 长连**模型：连接方向永远
**客户端 → 服务端**（NAT / 家宽免疫，客户端零端口）。这里用 **SSE** 而不是
WebSocket：浏览器与 Python 客户端都能零依赖读（`curl -N` 就够），而 MCP 的
http 传输本来就只听回环地址。

--------------------------------------------------------------------------
四层递降（三层实时性 + 一层兜底）
--------------------------------------------------------------------------
    SSE 长连接（这一层） → 重连追帧 → unread_dm 水位（寄生提醒 / 主动查）

* **SSE**：别人给我投私信时，服务端**顺这条流**推一帧元数据（`dm` 事件）。
* **重连追帧**：刚建流成功就补推一帧当前未读摘要（`seq` / `from` / `unread`）——
  客户端带不带 `last_seq` 都推这一帧（有未读就让宿主立刻知道）。
* **unread_dm 水位**：检索顺路带的未读数（老三层，一分没改）。

--------------------------------------------------------------------------
三条纪律
--------------------------------------------------------------------------
1. **只元数据**：帧里绝不带正文。正文永远走信箱本体（`message_inbox` 拿）。
2. **建流要过三道闸**：带 `Authorization: Bearer <ksk_凭据>` → 注册表**反查**
   代理 → `status=approved` **且** `push_enabled=1`。任一不满足 ⇒ 401 / 403，
   **不建流**。
3. **心跳 + 挂起上限**：每 25s 写一行 `: ping` 注释帧保活；挂起总超时默认
   30 分钟，到点**服务端主动断**（客户端自会重连）。每个心跳**复核一次**建流
   资格 ⇒ **吊销 / 关推送的生效边界 = 一个心跳间隔（≤25s）**。

🔴 **单代理最多 2 条并发流**（新的踢旧的，见 `govern/events.py`）——
挂太多流等于把一台机器的资源摊给一个代理。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, AsyncIterator, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from ..govern.events import BUS, EVENT_DM, STOP, frame
from ..govern.messaging import Messaging

logger = logging.getLogger("keepsake.mcp_server.events")

__all__ = ["register_events_route", "EVENTS_PATH", "HEARTBEAT_S", "MAX_STREAM_S"]

#: SSE 端点的路径（挂在 MCP 的 streamable-http 应用上 ⇒ 同进程同端口）
EVENTS_PATH = "/events"

#: 心跳间隔（秒）—— 每 25s 写一行 `: ping` 注释帧保活
HEARTBEAT_S = 25.0

#: 挂起总超时（秒）—— 默认 30 分钟到点服务端主动断（客户端自会重连）
MAX_STREAM_S = 30 * 60.0

#: 环境变量（可改；测试用它把两个数字调小，不必真等 25 分钟）
ENV_HEARTBEAT = "KEEPSAKE_SSE_HEARTBEAT_S"
ENV_MAX_STREAM = "KEEPSAKE_SSE_MAX_STREAM_S"


def _env_float(name: str, default: float) -> float:
    """环境变量 → float（非法值回落默认，不让一个坏配置把流打死）。"""
    try:
        value = float(str(os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _bearer(raw: Any) -> str:
    """`Authorization: Bearer <凭据>` → 凭据本体（其它形态一律当没带）。"""
    text = str(raw or "").strip()
    return text[len("Bearer "):].strip() if text.startswith("Bearer ") else ""


def _denied(status: int, error: str, message: str) -> JSONResponse:
    """拒绝建流（🔴 不回显任何凭据内容）。"""
    return JSONResponse(status_code=status,
                        content={"detail": {"error": error, "message": message}})


def _snapshot(registry: Any, agent_id: str) -> Dict[str, Any]:
    """这个代理的未读水位快照（`{count, max_seq, last_from}`；读不到就当没有）。"""
    try:
        return dict(Messaging(registry.storage).unread_snapshot(agent_id))
    except Exception as e:      # noqa: BLE001 —— 快照失败不许把建流打死
        logger.warning("keepsake: 未读快照失败（%s: %s）", type(e).__name__, e)
        return {"count": 0, "max_seq": 0, "last_from": ""}


def _still_allowed(registry: Any, agent_id: str, credential: str) -> bool:
    """这条流**还该不该活着**（每个心跳复核一次）。

    🔴 **吊销生效边界 = 一个心跳间隔（25s）**：凭据被吊销 / `push_enabled`
    被关之后，已挂着的那条流**最多再活 25s** —— 不复核的话，吊销要等到
    30 分钟的挂起上限才生效，那不叫吊销。
    """
    try:
        res = dict(registry.verify_any(credential))
        if not res.get("ok") or str(res.get("agent_id") or "") != agent_id:
            return False
        return bool(registry.push_enabled(agent_id))
    except Exception as e:      # noqa: BLE001 —— 读不到注册表 ⇒ 按不放行处理
        logger.warning("keepsake: SSE 复核失败（%s: %s）", type(e).__name__, e)
        return False


async def _event_stream(registry: Any, agent_id: str, credential: str) -> AsyncIterator[str]:
    """一条挂着的 SSE 流：追帧 → 收帧 → 心跳 → 到点自断。"""
    loop = asyncio.get_running_loop()
    sub = BUS.subscribe(agent_id, loop)
    heartbeat = _env_float(ENV_HEARTBEAT, HEARTBEAT_S)
    max_stream = _env_float(ENV_MAX_STREAM, MAX_STREAM_S)
    deadline = time.monotonic() + max_stream
    try:
        # ---- 重连追帧：建流成功立刻补一帧当前未读摘要 ----
        # 🔴 客户端带不带 last_seq 都推这一帧 —— 有未读就让宿主立刻知道
        #   （漏掉它 = 宿主要等到下一条私信才知道自己错过了多少）。
        snap = _snapshot(registry, agent_id)
        if int(snap.get("count") or 0) > 0:
            yield frame(EVENT_DM, {
                "seq": int(snap.get("max_seq") or 0),
                "from": str(snap.get("last_from") or ""),
                "unread": int(snap.get("count") or 0),
            })
        while True:
            try:
                item = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                if time.monotonic() >= deadline:
                    break                       # 挂起总超时 ⇒ 服务端主动断
                # 复核：凭据被吊销 / 推送被关 ⇒ 这条流到此为止（≤ 一个心跳间隔）
                if not _still_allowed(registry, agent_id, credential):
                    logger.info("keepsake: 代理 %s 的 SSE 流复核不通过，主动断开",
                                agent_id)
                    break
                yield ": ping\n\n"              # 保活（注释帧，客户端忽略）
                continue
            if item is STOP or not isinstance(item, str):
                break                           # 被新的踢掉 / 服务端收尾
            yield item
            if time.monotonic() >= deadline:
                break
    finally:
        BUS.unsubscribe(sub)                     # 断流摘除（含客户端断开）


def register_events_route(server: Any, gate: Any) -> None:
    """把 `GET /events` 挂到 MCP 服务上（streamable-http 同进程同端口）。

    `gate` 为 None（provider 没装配注册表）⇒ 端点**存在但一律拒绝**：
    没有注册表就没有「这是谁」可判 —— 与治理面同一纪律（不让调用方以为
    订阅通道凭空可用）。
    """
    registry = getattr(gate, "registry", None) if gate is not None else None

    @server.custom_route(EVENTS_PATH, methods=["GET"])
    async def _events(request: Request) -> Response:
        if registry is None:
            return _denied(404, "no_registry",
                           "本服务没有代理注册表 —— 订阅通道不可用")
        credential = _bearer(request.headers.get("authorization"))
        if not credential:
            return _denied(401, "no_credential",
                           "请求未带凭据（检查客户端的 Authorization 头）")
        try:
            res = dict(registry.verify_any(credential))
        except Exception as e:      # noqa: BLE001 —— 分类后给一句话，不带内部细节
            logger.warning("keepsake: SSE 建流校验异常（%s: %s）",
                           type(e).__name__, e)
            return _denied(401, "verify_error", "凭据校验失败，请稍后重试")
        if not res.get("ok"):
            # 未批准 / 已吊销 ⇒ 403（凭据本身没问题，是**这个代理**不能订阅）；
            # 凭据无效 / 查无此人 ⇒ 401。
            reason = str(res.get("reason") or "")
            if reason == "not_approved":
                return _denied(403, "not_approved",
                               "代理未批准/已吊销，不能订阅事件流")
            return _denied(401, reason or "unknown_agent",
                           "凭据无效或已更换（到控制台重新换发）")
        agent_id = str(res.get("agent_id") or "")
        if not registry.push_enabled(agent_id):
            return _denied(403, "push_disabled",
                           "该代理未开启私信实时推送（到控制台开启后再连）")
        return StreamingResponse(
            _event_stream(registry, agent_id, credential),
            media_type="text/event-stream",
            headers={
                # 🔴 `no-cache` + `X-Accel-Buffering: no`：反代（Nginx）默认会
                #   缓冲响应 ⇒ 帧会卡在反代里直到缓冲满 —— 那就不是长连接了。
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
