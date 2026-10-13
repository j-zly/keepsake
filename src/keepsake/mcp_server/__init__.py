"""keepsake 的通用 MCP 服务（`keepsake_p4_mcp_generic_v1`）。

把记忆能力暴露成**标准 MCP**，不绑任何宿主：
`server.py` 只认 `adapters/generic/protocol.MemoryProvider` 协议，
别的 agent 实现同一套协议后，`build_server(my_provider)` 直接复用全部工具与资源。

    from keepsake.mcp_server.server import build_server
    build_server(MyProvider()).run("stdio")     # 任何 MCP 客户端可连

本包**零处** `import agent.*` / `hermes`（见 GATES G2）——
默认 provider 的装配在 `factory.py`，那一行是从 keepsake 自己的实现取，
不是宿主耦合。Hermes 插件路径（`adapters/hermes/provider.py`）零改动。
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_server", "main"]


def __getattr__(name: str) -> Any:
    """懒导出（PEP 562）。

    不在 `__init__` 里 eager import —— 否则 `python -m keepsake.mcp_server.server`
    会先导入包、再执行同一个模块，runpy 每次都告警一圈（stdio 传输下这串
    噪音就在客户端眼前）。
    """
    if name in __all__:
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")