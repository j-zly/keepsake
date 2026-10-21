"""通用契约的「能力声明」门面。

真正的定义在 `keepsake.domain.types`（纯数据类型层，零 IO），
本模块只是给 `adapters.generic` 侧一个就近的导入路径，
避免外部调用方为了读一个 dataclass 而依赖 `domain` 包的内部布局。

依赖方向：`adapters → domain`，反向不许。

--------------------------------------------------------------------------
通知出口（notify outlet）—— 能力声明 `Capabilities.notify_outlet`
--------------------------------------------------------------------------
`push_consumer` 只把私信元数据帧送到**宿主进程内**（水位失效 + 摘要回调），
于是**空闲期**（用户不冒泡、不发起对话）提醒就浮不出来。通知出口是那一段的
补齐：收到未读私信帧时，适配器**旁路** POST 一个可配置的渠道 webhook
（例：飞书群机器人），把人叫醒。

契约三条（实现侧必须逐条落地，声明了这一条能力即等于承诺）：

1. 🔴 **只含元数据**（未读条数 / 发件人）—— **永不含正文**。正文永远走信箱
   本体（`message_inbox`）；正文进推送层 = 正文进了日志与外部渠道。
2. ⚠️ **同水位限频**：未读数不变 ⇒ 窗口内（`notify_min_interval_s`，缺省
   120s）不重复喊；水位升高 ⇒ 立刻再喊（不让人干等窗口）。
3. 🔴 **fail-open**：通道侧失败只记 warning —— 绝不重试轰炸、绝不影响
   私信水位与记忆主路径。

载荷形状由适配器一侧可配（`generic` / `feishu` 等），**渠道适配写在适配器里**，
契约层只声明「有这个出口」与「载荷只含元数据」两件事。
"""

from __future__ import annotations

from ...domain.types import (
    CapabilityNotSupported,
    Capabilities,
    KeepsakeContractError,
    PushEvent,
)

__all__ = ["Capabilities", "KeepsakeContractError", "CapabilityNotSupported", "PushEvent"]