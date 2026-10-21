"""通用适配层：与具体宿主无关的记忆契约（Protocol / Capabilities）。

依赖方向：只准 `adapters.hermes → adapters.generic`；本包**绝不 import
任何 Hermes 的东西**，否则契约就被宿主绑死了。
"""

from __future__ import annotations

from .capabilities import CapabilityNotSupported, Capabilities, KeepsakeContractError, PushEvent
from .protocol import MemoryProvider, PushEventConsumer

__all__ = [
    "MemoryProvider",
    "PushEventConsumer",
    "PushEvent",
    "Capabilities",
    "KeepsakeContractError",
    "CapabilityNotSupported",
]