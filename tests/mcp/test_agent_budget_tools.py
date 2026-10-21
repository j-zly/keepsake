"""MCP 面上的 per-agent 注入预算（`ks_agent_budget`）。

锁死两件事：

1. **`memory_core_list` 按调用方的预算截** —— 调用方在注册表里配了自己的
   `core_budget_chars` ⇒ 它的 core 列表按**它**的预算截；没配 ⇒ 走全局；
2. **primary 不受影响**（全局够大时一条都不截）—— 现状逐字不变。

全程 hermetic：临时 SQLite + 临时审计/备份目录，不连 Redis / PG / 网络。
"""

from __future__ import annotations

import functools
import json
from typing import Any, Dict

import anyio
import pytest

from keepsake.mcp_server.server import build_server

#: 每条常驻条目的正文长度（预算 100 时只装得下一条）。
ITEM_CHARS = 60

#: 三条常驻条目（末字用来认身份：甲 / 乙 / 丙）。
CORE_ITEMS = ("甲", "乙", "丙")


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
    from keepsake.adapters.hermes import provider as prov_mod

    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})      # noqa: SLF001
    server = _build(tmp_path, monkeypatch)
    _seed_core(tmp_path)
    yield server
    server.agent_gate.stop()
    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})      # noqa: SLF001


def _seed_core(tmp_path) -> None:
    """种三条常驻条目。

    归属标签要原样落库 ⇒ 用**没有 agent_id 的**存储写（存储层 store() 会把
    `agent:` 标签改写成自己的归属）。这里写的是**没有归属**的老数据 ——
    它对谁都可见（core 层可见性过滤的那一档）。
    """
    from keepsake.core.sqlite import SqliteStorage

    writer = SqliteStorage(path=str(tmp_path / "gov.db"))
    try:
        for suffix in CORE_ITEMS:
            text = f"{suffix}" + "内" * (ITEM_CHARS - 1)
            assert writer.store(text=text, tags="core", category="probe",
                                source="probe") is True
    finally:
        writer.close()


def call(server, name: str, **arguments: Any) -> Dict[str, Any]:
    _, structured = anyio.run(functools.partial(server.call_tool, name, arguments))
    return structured["result"]


def read_resource(server, uri: str) -> str:
    contents = list(anyio.run(functools.partial(server.read_resource, uri)))
    return "".join(getattr(c, "content", "") for c in contents)


def _as_caller(server, agent_id: str) -> None:
    """把 stdio 闸门切到另一个代理（标成「还没复核过」⇒ 下次调用必走一遍判定链）。"""
    gate = server.agent_gate
    gate.agent_id = agent_id
    gate._stdio_ok = None       # noqa: SLF001
    gate._stdio_at = 0.0        # noqa: SLF001


def _suffixes(rows: Dict[str, Any]) -> set:
    return {c for item in rows["items"] for c in str(item["content"]) if c in CORE_ITEMS}


def test_调用方的预算只截自己的core列表(gov):
    """🔴 worker-sub 配了预算 100 ⇒ 它的 core 列表只剩装得下的那一条。"""
    reg = gov.agent_gate.registry
    reg.bootstrap_local("worker-sub", "子代理")
    assert reg.set_core_budget_chars("worker-sub", 100) is True

    _as_caller(gov, "worker-sub")
    rows = call(gov, "memory_core_list")
    assert rows["count"] == 1, rows
    assert _suffixes(rows) == {"甲"}, rows
    # 截断必须**看得见**（不许静默丢条目）+ 回带这次用的预算
    assert rows.get("truncated") is True, rows
    assert "本次列出 1 条" in str(rows.get("note") or ""), rows
    assert rows.get("budget") == 100, rows


def test_没配预算的调用方走全局(gov):
    """🔴 没配（NULL）⇒ 走全局 ⇒ 一条都不截（现状不变）。"""
    reg = gov.agent_gate.registry
    reg.bootstrap_local("worker-sub", "子代理")

    _as_caller(gov, "worker-sub")
    rows = call(gov, "memory_core_list")
    assert rows["count"] == 3, rows
    assert _suffixes(rows) == {"甲", "乙", "丙"}, rows
    assert not rows.get("truncated"), rows


def test_primary不受影响(gov):
    """🔴 primary 没配预算 ⇒ 照旧全量（现状逐字不变）。"""
    rows = call(gov, "memory_core_list")
    assert rows["count"] == 3, rows


def test_core资源按调用方的预算截(gov):
    """🔴 `memory://core`（= 每轮注入的那份渲染）按**调用方**的预算截。"""
    reg = gov.agent_gate.registry
    reg.bootstrap_local("worker-sub", "子代理")
    assert reg.set_core_budget_chars("worker-sub", 100) is True

    _as_caller(gov, "worker-sub")
    text = read_resource(gov, "memory://core")
    assert "core 层已截断" in text, text
    assert "本次注入 1 条" in text, text
