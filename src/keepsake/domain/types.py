"""domain 层纯数据类型：Fragment / Hit / Capabilities 与错误类型。

本模块**零 IO、零依赖**（只用标准库 typing）：它是 `adapters/generic/protocol.py`
的数据侧地基，任何适配器都可以在不碰 core 的前提下引用。

Fragment / Hit 刻意用 `TypedDict` 而不是 dataclass：
存储层三个后端（Redis / PG / SQLite）返回的本来就是 `Dict[str, Any]`，
TypedDict 只在类型检查层面描述形状，**运行时仍是那个 dict 本身** ——
不引入一次多余的对象转换，也就不会有「取回来变成另一种类型」的行为漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, TypedDict


# ---------------------------------------------------------------------------
# 检索命中 / 碎片本体
# ---------------------------------------------------------------------------

class Fragment(TypedDict, total=False):
    """一条记忆碎片（存储层原样返回的 dict，不做转换）。

    `total=False`：字段随后端与写入路径不同而增减，缺字段是常态，
    调用方一律走 `.get()` + 默认值，不许直接下标。
    """

    key: str
    content: str
    tags: str
    category: str
    source: str
    fragment_type: str
    created: str
    updated_at: str
    sentiment_score: str
    sentiment_label: str
    feedback_score: str
    touch_count: str


#: 一条推送事件的**摘要**（消费者交给宿主的形状）。
#:
#: 🔴 **没有正文**：帧里只有元数据（谁发来的 / 第几条 / 未读几条）——
#: 私信正文永远走信箱本体（`message_inbox`），进推送层就等于进了日志。
#:
#: 用**函数式**写法而不是 class 体：`from` 是 Python 关键字，写不成注解。
#: 键名与线上帧逐字一致（`event` / `seq` / `from` / `unread`），
#: 宿主可以直接把帧映射成这个形状，不用二次翻译。
PushEvent = TypedDict("PushEvent", {
    "event": str,     # "dm"（有人投了私信）/ "dm-nudge"（人工点了提醒）
    "seq": int,       # 信箱序号
    "from": str,      # 最近一个发件人
    "unread": int,    # 当前未读条数
})


class Hit(TypedDict, total=False):
    """一条检索命中：在 Fragment 之上叠了排序信号。

    `_` 前缀字段是检索期算出来的**临时**信号（不入库），
    与 `storage_shared` 的重排函数输出一一对应。
    """

    key: str
    content: str
    tags: str
    # 排序信号
    _combined_score: float
    _sim: float
    _weights: Dict[str, Any]
    _knn_score: Optional[float]


# ---------------------------------------------------------------------------
# 能力声明
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Capabilities:
    """适配器对外的能力声明（`MemoryProvider.capabilities()` 的返回值）。

    调用方据此决定「能不能问后端要 KNN」这类问题，而不是 try/except 猜。
    `version` 是**契约版本**（"v1"），与 keepsake 包版本无关 ——
    升级契约要升它，升级实现不许动它。
    """

    core_tier: bool = False
    """是否有常驻核心层（core tag 每轮全量注入上下文）。"""

    knn: bool = False
    """检索是否含向量（KNN）通道；False = 只有 BM25 全文。"""

    dry_run_merge: bool = False
    """merge() 是否支持 dry_run（只统计不写）；False = 调用方不许传 dry_run。"""

    push_consumer: bool = False
    """是否带**推送消费者**（2026-10 ks_push_consumer）：provider 侧会挂一条
    SSE 长连接，收私信元数据帧。

    True = 宿主可以注册 `on_push_event(fn)` 拿事件摘要；
    False = 只剩寄生层的对话轮提醒（既有行为，逐字不变）。"""

    notify_outlet: bool = False
    """是否带**通知出口**（2026-10 ks_notify_outlet）：收到未读私信帧时，
    旁路 POST 一个可配置的渠道 webhook（例：飞书群机器人），把人叫醒。

    🔴 载荷**只含元数据**（未读条数 / 发件人）—— **永不含正文**；
    🔴 同水位按 `notify_min_interval_s` 限频（缺省 120s），水位升高立刻再喊；
    🔴 通道侧失败只记 warning（fail-open），绝不重试轰炸、绝不碰水位。

    True = **能**发（是否真的发由 `push_consumer.notify_url` 决定，空 ⇒ 零外呼）；
    False = 没有任何外呼出口。"""

    idempotent_write: bool = False
    """写路径是否支持**请求幂等键**（2026-10 ks_idem）：调用方带同一个键重试
    （典型场景 = MCP 调用超时、客户端没收到响应）⇒ 服务端**不落第二条**，
    回首次落库的结果。

    True = 能带幂等键；False = **不支持**（调用方带了会被显式拒绝，
    **绝不静默降级成「照写不误」** —— 那会让调用方误以为重试安全）。

    🔴 **契约版本仍是 "v1"**：新增一个**默认 False 的布尔位**是兼容扩展
    （老实现方不声明它 ⇒ 取默认 False，语义正确）；按老规矩，升契约版本
    要改的是 `version`，不是加字段。"""

    version: str = "v1"
    """契约版本号。"""


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------

class KeepsakeContractError(Exception):
    """契约层错误基类：实现违反 `adapters/generic/protocol.py` 的失败语义时抛出。"""


class CapabilityNotSupported(KeepsakeContractError):
    """调用方要了实现方 `Capabilities` 里没声明的能力（如对 BM25-only 后端要 KNN）。"""


__all__ = [
    "Fragment",
    "Hit",
    "PushEvent",
    "Capabilities",
    "KeepsakeContractError",
    "CapabilityNotSupported",
]