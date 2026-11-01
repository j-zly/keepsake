"""排除闸的**范围**（2026-10-31 ks_exclscope，#133 真根因）—— MCP 面端到端。

`ks_agent.exclude_from_primary` 管的是「这个代理的条目**要不要进主脑上下文**」，
**不是**「这个代理不能检索自己的条目」。修前 provider 的检索出口不分调用方地
全局剔除 ⇒ 开了开关的代理连**自己**名下条目都搜不到（#133 现象：get 能取、
search 零召回）。

本文件从 MCP 面验这条口径（调用方身份由闸门按凭据/本机通道反查，不是自报）：

* 子代理调用方（role=sub）⇒ **搜得到**自己名下条目；
* 主脑调用方（role=primary）⇒ 照旧**搜不到**（排除对主脑侧仍然生效）。

全程 hermetic：临时 SQLite + 临时审计/备份目录，不连 Redis / PG / 网络 / 生产库。
"""

from __future__ import annotations

import functools
import json
from typing import Any, Dict

import anyio
import pytest

from keepsake.mcp_server.server import build_server

QUERY = "回滚 流程 校验"
#: 开了 exclude_from_primary 的子代理
SUB_AGENT = "worker-sub"


def _build(tmp_path, monkeypatch):
    """按临时配置装配一个 server（审计/备份落 tmp；`KEEPSAKE_CONFIG` 指 tmp sqlite）。"""
    cfg = {
        "storage": {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "gov.db")}},
        "is_primary": True,
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.delenv("KEEPSAKE_AGENT_ID", raising=False)
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL", raising=False)
    return build_server(govern=True)


@pytest.fixture()
def gov(tmp_path, monkeypatch):
    """server + 一个开了排除开关的子代理 + 它名下的一条记忆。

    🔴 注册表必须**在 server 起来之前**写好：运行时排除表是 provider 初始化时
    从注册表组装的（`provider._init_governance`）—— 开关晚开要等下一次初始化
    才进排除表（与控制台开关的既有口径一致）。
    """
    from keepsake.core.sqlite import SqliteStorage
    from keepsake.govern.registry import Registry

    path = str(tmp_path / "gov.db")
    seed = SqliteStorage(path=path)
    try:
        assert seed.ensure_index() is True
        reg = Registry(seed)
        assert reg.ensure_schema() is True
        reg.bootstrap_local(SUB_AGENT, "子代理")      # 批准，但**不**上位（它是 sub）
        assert reg.set_exclude(SUB_AGENT, True) is True
    finally:
        seed.close()

    server = _build(tmp_path, monkeypatch)
    try:
        # 归属标签要原样落库 ⇒ 用**没有 agent_id 的**存储写
        writer = SqliteStorage(path=path)
        try:
            assert writer.store(text=f"{QUERY} 己", tags=f"agent:{SUB_AGENT}",
                                category="probe", source="probe") is True
        finally:
            writer.close()
        yield server
    finally:
        server.agent_gate.stop()


def _call(server, name: str, **arguments: Any) -> Dict[str, Any]:
    _, structured = anyio.run(functools.partial(server.call_tool, name, arguments))
    return structured["result"]


def _as_caller(server, agent_id: str) -> None:
    """把 stdio 闸门切到另一个代理（标成「还没复核过」⇒ 下次调用必走一遍判定链）。"""
    gate = server.agent_gate
    gate.agent_id = agent_id
    gate._stdio_ok = None       # noqa: SLF001
    gate._stdio_at = 0.0        # noqa: SLF001


def test_子代理搜得到自己的条目(gov):
    """🔴 #133：开了 `exclude_from_primary` 的代理**自己**必须搜得到自己的条目。

    修前（全局剔除）：`count == 0` —— get 能取、search 零召回。
    """
    _as_caller(gov, SUB_AGENT)
    out = _call(gov, "memory_search", query=QUERY, limit=20)
    assert out["count"] == 1, out
    assert out["results"][0]["content"].endswith("己"), out


def test_主脑侧照旧搜不到被排除代理的条目(gov):
    """排除闸对主脑侧**逐字不变**：主脑上下文里不出现被排除代理的条目。"""
    out = _call(gov, "memory_search", query=QUERY, limit=20)
    assert out["count"] == 0, out
