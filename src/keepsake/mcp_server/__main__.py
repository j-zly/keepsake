"""`python -m keepsake.mcp_server` —— CLI 入口。

    python -m keepsake.mcp_server                                   # stdio（默认）
    python -m keepsake.mcp_server --transport http --port 8299      # 只听 127.0.0.1

参数解析与启动逻辑一行都不在这里复制，全部转发给 `server.main()`
（`__main__` 是 `python -m` 的硬约定；不做任何事的话包就起不来）。
"""

from __future__ import annotations

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())