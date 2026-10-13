#!/usr/bin/env python3
"""非 Hermes 的 MCP 客户端示例 —— stdio 传输，一趟跑通 search / core_list / add。

证明的是「任何 MCP 客户端连上就能用」：这里既不是 Hermes，也没有任何
keepsake 内部符号，只用官方 SDK 的 `ClientSession` + `stdio_client`，
把 `python -m keepsake.mcp_server` 当成任意 MCP 服务端拉起来。

    .venv/bin/python examples/mcp_client_demo.py          # 结尾打印 DEMO_OK

自带一个 tmp 目录里的 SQLite 配置（`KEEPSAKE_CONFIG` 注入子进程），
**不连生产库、不写仓库任何文件**。
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _server_env(tmp: pathlib.Path) -> dict[str, str]:
    """子进程环境：临时 sqlite 配置 + 本仓库的 src 放上 PYTHONPATH。"""
    cfg = {"storage": {"backend": "sqlite", "sqlite": {"path": str(tmp / "demo.db")}}}
    cfg_file = tmp / "config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

    env = dict(os.environ)
    env["KEEPSAKE_CONFIG"] = str(cfg_file)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _text(result) -> str:
    """CallToolResult 的 content 拼回纯文本。"""
    return "".join(getattr(block, "text", "") for block in result.content)


async def _run() -> None:
    with tempfile.TemporaryDirectory(prefix="keepsake-mcp-demo-") as tmpdir:
        tmp = pathlib.Path(tmpdir)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "keepsake.mcp_server", "--transport", "stdio"],
            env=_server_env(tmp),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = sorted(t.name for t in (await session.list_tools()).tools)
                print("tools:", tools)

                caps = json.loads(_text(await session.call_tool("memory_capabilities", {})))
                print("capabilities:", caps)

                added = json.loads(_text(await session.call_tool(
                    "memory_add", {"text": "MCP 契约探针", "tags": "probe,mcp"})))
                print("add ->", added)

                found = json.loads(_text(await session.call_tool(
                    "memory_search", {"query": "MCP 契约探针"})))
                print("search ->", found["count"], "hit(s):",
                      [r["key"] for r in found["results"]])

                core = json.loads(_text(await session.call_tool("memory_core_list", {})))
                print("core_list ->", core["count"], "item(s)")

                resources = [str(r.uri) for r in (await session.list_resources()).resources]
                print("resources:", resources)

                # 写语义与 P2 一致：不带 confirm 必须报错，不是 ok
                denied = await session.call_tool(
                    "memory_remove", {"key": added["key"], "confirm": False})
                assert denied.isError, "未带 confirm 的删除必须报错"
                print("remove(confirm=False) -> isError:", denied.isError)

    print("DEMO_OK")


if __name__ == "__main__":
    asyncio.run(_run())