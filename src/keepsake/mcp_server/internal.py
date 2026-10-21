"""内部投递端点（`POST /internal/publish`）—— 控制台 → MCP 的**跨进程**投递。

2026-10 ks_nudge_xproc。SSE 流挂在 **MCP 进程**的内存总线上，而控制台
（keepsake-console，8200）是**另一个进程** ⇒ 控制台点「触发提醒」时，
它自己那台总线**永远没有订阅者** ⇒ 提醒恒 `delivered=false`（对用户就是坏功能）。

本模块补上那条**缺的一米**：控制台把「给谁推什么帧」POST 到 MCP 进程，
由**挂着流的那台进程**去推。回包 `{delivered: bool}` 是 BUS 的**真实答案**
（有没有订阅者），控制台照实转述 —— 不谎报已送达。

--------------------------------------------------------------------------
两道闸（都要过才推）
--------------------------------------------------------------------------
1. **只接受回环来源**：`request.client.host ∈ {127.0.0.1, ::1}`。
   Nginx 不转发本路径 ⇒ 公网不可达；即便有人把端口暴露出去，外网来源也进不来。
2. **共享密钥头** `X-Internal-Token`（值从 env `KEEPSAKE_INTERNAL_TOKEN` 读）。
   🔴 **未配置该 env ⇒ 端点一律 503**（fail-closed）：宁可不投递，也不裸奔。

推的仍然是**只有元数据的帧**（`seq` / `from` / `unread`）—— 正文永远走信箱本体，
与进程内 `publish_nudge` 逐字同款。
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Dict, Tuple

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..govern.events import (BUS, EVENT_DM, EVENT_DM_NUDGE,
                             INTERNAL_TOKEN_HEADER, internal_token)

logger = logging.getLogger("keepsake.mcp_server.internal")

__all__ = ["register_internal_route", "publish_core",
           "INTERNAL_PATH", "LOOPBACK_HOSTS"]

#: 内部投递端点的路径（挂在 MCP 的 streamable-http 应用上 ⇒ 同进程同端口）。
INTERNAL_PATH = "/internal/publish"

#: 允许的来源（回环）。`::ffff:127.0.0.1` 是 IPv4-mapped IPv6 的写法，
#: 某些栈会以这个形态报 client.host —— 一样是本机，放行。
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")

#: 「body 不是合法 JSON」的哨兵（与「body 是 null」区分开）。
INVALID_JSON = object()


def _as_int(value: Any) -> int:
    """任意值 → int（坏了就 0 —— 帧里的元数据字段不许把端点打死）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def publish_core(host: str, token_header: str,
                 body: Any) -> Tuple[int, Dict[str, Any]]:
    """内部投递的**核心逻辑**（同步；HTTP 路由与测试都走它 ⇒ 同一份判定）。

    返回 `(status_code, payload)`：

    * **200** → `{"delivered": bool}`（BUS 真实有无订阅者的答案）；
    * **4xx/5xx** → `{"detail": {"error": ..., "message": ...}}`（🔴 不回显密钥）。
    """
    # ---- 闸 ①：共享密钥没配 ⇒ 端点一律 503（fail-closed）----
    expected = internal_token()
    if not expected:
        return 503, {"detail": {"error": "internal_disabled",
                                "message": "内部投递未启用（未配置 KEEPSAKE_INTERNAL_TOKEN）"}}
    # ---- 闸 ②：只接受回环来源 ----
    if str(host or "") not in LOOPBACK_HOSTS:
        return 403, {"detail": {"error": "forbidden_source",
                                "message": "内部投递端点只接受回环地址来源"}}
    # ---- 闸 ③：共享密钥头（常数时间比较）----
    got = str(token_header or "").strip()
    if not got or not hmac.compare_digest(got.encode("utf-8"),
                                          expected.encode("utf-8")):
        return 401, {"detail": {"error": "invalid_token",
                                "message": "共享密钥不对或没带"}}
    # ---- body ----
    if body is INVALID_JSON or not isinstance(body, dict):
        return 400, {"detail": {"error": "bad_body",
                                "message": "请求体必须是 JSON 对象"}}
    agent_id = str(body.get("agent_id") or "").strip()
    if not agent_id:
        return 400, {"detail": {"error": "no_agent", "message": "缺 agent_id"}}
    kind = str(body.get("kind") or EVENT_DM_NUDGE).strip()
    if kind not in (EVENT_DM, EVENT_DM_NUDGE):
        # 🔴 kind 是**帧的 event 名**：不许调用方注入任意事件名
        return 400, {"detail": {"error": "bad_kind",
                                "message": "kind 只允许 dm / dm-nudge"}}
    data: Dict[str, Any] = {
        "seq": _as_int(body.get("seq")),
        "from": str(body.get("from") or ""),
        "unread": _as_int(body.get("unread")),
    }
    # ---- 推帧（**只有元数据**）----
    streams = BUS.publish(agent_id, kind, data)
    return 200, {"delivered": bool(streams > 0)}


def register_internal_route(server: Any) -> None:
    """把 `POST /internal/publish` 挂到 MCP 服务上（streamable-http 同进程同端口）。"""

    @server.custom_route(INTERNAL_PATH, methods=["POST"])
    async def _publish(request: Request) -> Response:
        try:
            body: Any = await request.json()
        except Exception:       # noqa: BLE001 —— 不是 JSON ⇒ 400，不带内部细节
            body = INVALID_JSON
        client = getattr(request, "client", None)
        status, payload = publish_core(
            str(getattr(client, "host", "") or ""),
            str(request.headers.get(INTERNAL_TOKEN_HEADER.lower()) or ""),
            body,
        )
        return JSONResponse(status_code=status, content=payload)
