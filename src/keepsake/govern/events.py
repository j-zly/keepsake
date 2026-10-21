"""私信实时推送的**进程内事件总线**（SSE 快车道）。

2026-10 ks_dm_sse。私信实时性的**第三层**（最上面那层）：

    SSE 长连接  →  重连追帧  →  unread_dm 水位（寄生提醒 / 主动查）

客户端拨一条 SSE 长连接（连接方向永远**客户端 → 服务端**：NAT / 家宽免疫、
客户端零端口），服务端在 `message_send` 落库 COMMIT 成功后**顺原流**推一帧
**元数据**（`seq` / `from` / `unread`）—— **没有正文**。

--------------------------------------------------------------------------
三条纪律
--------------------------------------------------------------------------
1. **只元数据**：帧里绝不带正文。正文永远走信箱本体（`message_inbox` 拿）——
   推正文等于把私信复制进日志、代理缓存与代理进程内存里。
2. **进程内**：`dict[agent_id → list[Subscription]]`。🔴 控制台进程与 MCP 进程
   **分开跑**时，控制台的 nudge 推不到 MCP 侧挂着的那条流 —— 所以补了
   **跨进程投递**（2026-10 ks_nudge_xproc）：控制台 POST 内部端点
   `POST /internal/publish`（MCP 进程侧，见 `mcp_server/internal.py`），
   由**那台进程里的总线**推帧，回包带 `delivered`（BUS 真实有无订阅者）。
   `ponytail:` 真要更通用的跨进程（不止 nudge），把这一层换成 Redis pub/sub
   即可 —— 订阅端还是 `asyncio.Queue`，换的只是「谁把帧送进来」。
3. **静默丢弃**：没有挂着的连接 = 这一帧直接丢（不重试、不轰炸）。
   补偿在下一层（重连追帧 + `unread_dm` 水位），不在这一层做。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Any, Dict, List

logger = logging.getLogger("keepsake.govern.events")

__all__ = [
    "BUS", "EventBus", "Subscription",
    "EVENT_DM", "EVENT_DM_NUDGE", "STOP",
    "MAX_STREAMS_PER_AGENT", "QUEUE_MAXSIZE",
    "ENV_INTERNAL_TOKEN", "INTERNAL_TOKEN_HEADER", "internal_token",
    "frame", "publish_dm", "publish_nudge",
]

#: 帧的 `event:` 名（两类）
EVENT_DM = "dm"                # 有人给我投了私信（message_send 落库后推的那一帧）
EVENT_DM_NUDGE = "dm-nudge"    # 人工在控制台点「触发提醒」

#: 单个代理最多几条并发流（**新的踢旧的** —— 客户端自己会重连）
MAX_STREAMS_PER_AGENT = 2

#: 每条流的队列深度。满了就丢**最旧**的一帧：实时通道宁可丢帧，
#: 也不许把发送方（`message_send`）堵住。
QUEUE_MAXSIZE = 64

#: 停止哨兵（塞进队列 ⇒ 那条流自己收尾）
STOP = object()

# --------------------------------------------------------------------------
# 跨进程投递的**共享契约**（2026-10 ks_nudge_xproc）
# --------------------------------------------------------------------------

#: 共享密钥的环境变量名 —— 控制台进程与 MCP 进程读**同一个**。
#: 🔴 **未配置 ⇒ 内部端点一律 503**（fail-closed）：宁可不投递，也不裸奔。
ENV_INTERNAL_TOKEN = "KEEPSAKE_INTERNAL_TOKEN"

#: 共享密钥的请求头名（内部端点只认这一个头）
INTERNAL_TOKEN_HEADER = "X-Internal-Token"


def internal_token() -> str:
    """共享密钥本体（**只从环境变量读**；空串 = 未配置 = 端点禁用）。

    🔴 真值**永不**进代码 / 配置 / 日志 —— 只在这两个进程的环境里。
    """
    return (os.environ.get(ENV_INTERNAL_TOKEN) or "").strip()


def frame(event: str, data: Dict[str, Any]) -> str:
    """一帧 SSE：`event: <名>\\n` + `data: <json>\\n\\n`（帧结束是那个空行）。"""
    return "event: %s\ndata: %s\n\n" % (
        event, json.dumps(data, ensure_ascii=False))


class Subscription:
    """一条挂着的流（= 一个客户端连接）。

    `queue` 与 `loop` 一起存：`publish` 可能来自**另一个线程**
    （MCP 的工具跑在线程池里、控制台的写端点跑在 FastAPI 的线程池里），
    所以塞帧一律走 `loop.call_soon_threadsafe`（跨线程安全，且同线程也安全）。
    """

    __slots__ = ("agent_id", "queue", "loop", "closed")

    def __init__(self, agent_id: str, loop: asyncio.AbstractEventLoop) -> None:
        self.agent_id = agent_id
        self.loop = loop
        self.queue: "asyncio.Queue" = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.closed = False

    def stop(self) -> None:
        """叫这条流收尾（被新的踢掉 / 服务端到点主动断）。"""
        self.closed = True
        self.offer(STOP)

    def offer(self, item: Any) -> None:
        """把一帧塞进队列（**跨线程安全**；队列满 ⇒ 丢最旧的一帧）。"""
        try:
            self.loop.call_soon_threadsafe(self._put, item)
        except RuntimeError:
            # 事件循环已经关了 ⇒ 这条流已经没了（静默丢弃，不当错误）
            pass

    def _put(self, item: Any) -> None:
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()      # 丢最旧的一帧
                self.queue.put_nowait(item)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass


class EventBus:
    """进程内总线：`dict[agent_id → list[Subscription]]`（建流注册 / 断流摘除）。"""

    def __init__(self) -> None:
        self._subs: Dict[str, List[Subscription]] = {}
        self._lock = threading.Lock()

    # -- 订阅 ----------------------------------------------------------
    def subscribe(self, agent_id: str, loop: asyncio.AbstractEventLoop) -> Subscription:
        """注册一条流；超过上限时**踢掉最老的**（新的踢旧的）。"""
        aid = (agent_id or "").strip()
        sub = Subscription(aid, loop)
        kicked: List[Subscription] = []
        with self._lock:
            subs = self._subs.setdefault(aid, [])
            subs.append(sub)
            while len(subs) > MAX_STREAMS_PER_AGENT:
                kicked.append(subs.pop(0))
        for old in kicked:
            logger.info("keepsake: 代理 %s 的 SSE 流超限（%d 条），踢掉最老的一条",
                        aid, MAX_STREAMS_PER_AGENT)
            old.stop()
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """摘除一条流（断流 / 超时 / 服务端主动断）。"""
        with self._lock:
            subs = self._subs.get(sub.agent_id) or []
            if sub in subs:
                subs.remove(sub)
            if not subs:
                self._subs.pop(sub.agent_id, None)

    def stream_count(self, agent_id: str) -> int:
        """这个代理当前挂着几条流（0 = 没人连着 ⇒ 推了也是丢）。"""
        with self._lock:
            return len(self._subs.get((agent_id or "").strip()) or [])

    # -- 推 ------------------------------------------------------------
    def publish(self, agent_id: str, event: str, data: Dict[str, Any]) -> int:
        """推一帧给该代理的所有挂流；返回**挂着的流数**（0 = 静默丢弃）。"""
        aid = (agent_id or "").strip()
        if not aid:
            return 0
        with self._lock:
            subs = list(self._subs.get(aid) or [])
        if not subs:
            return 0
        text = frame(event, data)
        for sub in subs:
            sub.offer(text)
        return len(subs)


#: 进程内**唯一**那台总线（MCP 进程与控制台进程各有一台 —— 见模块 docstring 纪律 2）
BUS = EventBus()


def publish_dm(agent_id: str, seq: int, from_id: str, unread: int) -> int:
    """推一帧 `dm`（有人给我投了私信）。返回挂着的流数。"""
    return BUS.publish(agent_id, EVENT_DM, {
        "seq": int(seq or 0),
        "from": str(from_id or ""),
        "unread": int(unread or 0),
    })


def publish_nudge(agent_id: str, unread: int, from_id: str = "", seq: int = 0) -> int:
    """推一帧 `dm-nudge`（人工触发的提醒）。同样**只元数据**，不带正文。"""
    return BUS.publish(agent_id, EVENT_DM_NUDGE, {
        "seq": int(seq or 0),
        "from": str(from_id or ""),
        "unread": int(unread or 0),
    })
