"""通用契约的「能力声明」门面。

真正的定义在 `keepsake.domain.types`（纯数据类型层，零 IO），
本模块只是给 `adapters.generic` 侧一个就近的导入路径，
避免外部调用方为了读一个 dataclass 而依赖 `domain` 包的内部布局。

依赖方向：`adapters → domain`，反向不许。
"""

from __future__ import annotations

from ...domain.types import CapabilityNotSupported, Capabilities, KeepsakeContractError

__all__ = ["Capabilities", "KeepsakeContractError", "CapabilityNotSupported"]