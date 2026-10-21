"""未读私信提醒（**服务端**侧）—— `memory_search` 的响应顺路带 `unread_dm`。

2026-10 ks_dm_hint：私信是**拉取式**的，接收方不主动拉就不知道有信。
已否决 cron 哨兵 ⇒ 定案：把未读数**寄生在每轮必然发生的检索**上
（零新组件、零定时器、零额外请求），插件把这一行渲染成提醒。

本文件锁死三件事：

1. `memory_search` 的响应里**有** `unread_dm`（不是可有可无的附加字段）；
2. 投一条给该代理 ⇒ 计数 +1；读掉 ⇒ 回 0（口径 = `to_id=调用方 AND read_at IS NULL`）；
3. **消息表读失败 ⇒ 检索本体照常返回** —— 提醒是附加值，fail-open，
   一个计数查询不该有能力让检索瘫掉。

全程 hermetic：`tmp_path` 里的临时 SQLite，不连 Redis / PG / 网络 / 生产库。
"""

from __future__ import annotations

import functools
import json
from typing import Any

import anyio
import pytest

from keepsake.govern.messaging import TABLE
from keepsake.mcp_server.server import build_server


# --------------------------------------------------------------------------
# 夹具 / 帮手（与 tests/test_messaging_tools.py 同一套装配）
# --------------------------------------------------------------------------

def _build(tmp_path, monkeypatch):
    """按临时配置装配一个 server（存储落在 tmp 里的 sqlite）。"""
    cfg = {
        "storage": {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "dm.db")}},
        "is_primary": True,
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.delenv("KEEPSAKE_AGENT_ID", raising=False)
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL", raising=False)
    return build_server(govern=False)


@pytest.fixture()
def box(tmp_path, monkeypatch):
    """一个 server + 注册表（两个已批准代理：发件人 agent-a / 收件人 agent-b）。"""
    from keepsake.adapters.hermes import provider as prov_mod

    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})      # noqa: SLF001
    server = _build(tmp_path, monkeypatch)
    reg = server.agent_gate.registry
    reg.bootstrap_local("agent-a", "发件人")
    reg.bootstrap_local("agent-b", "收件人")
    yield server, reg
    server.agent_gate.stop()
    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})      # noqa: SLF001


def call(server, name: str, **arguments: Any) -> Any:
    _, structured = anyio.run(functools.partial(server.call_tool, name, arguments))
    return structured["result"]


def _as_caller(server, agent_id: str) -> None:
    """把 stdio 闸门切到另一个代理（标成「还没复核过」⇒ 下次调用必走一遍判定链）。"""
    gate = server.agent_gate
    gate.agent_id = agent_id
    gate._stdio_ok = None       # noqa: SLF001
    gate._stdio_at = 0.0        # noqa: SLF001


# --------------------------------------------------------------------------
# ① 响应带未读数，且随收信 / 读信变化
# --------------------------------------------------------------------------

def test_搜索响应带未读私信数且随收信变化(box):
    """🔴 响应里必须有 `unread_dm`；投一条 +1、读掉回 0。"""
    server, _ = box
    _as_caller(server, "agent-b")
    res = call(server, "memory_search", query="探针")
    assert "unread_dm" in res, res
    assert res["unread_dm"] == 0

    _as_caller(server, "agent-a")
    call(server, "message_send", to="agent-b", content="在吗")

    _as_caller(server, "agent-b")
    res = call(server, "memory_search", query="探针")
    assert res["unread_dm"] == 1, res

    call(server, "message_inbox")          # 取走即算已读
    res = call(server, "memory_search", query="探针")
    assert res["unread_dm"] == 0, res


def test_别人的信不算我的未读(box):
    """🔴 口径 = `to_id=调用方` —— 收件人不是自己 ⇒ 不进我的未读数。"""
    server, _ = box
    _as_caller(server, "agent-a")
    call(server, "message_send", to="agent-b", content="在吗")
    _as_caller(server, "agent-b")
    assert call(server, "memory_search", query="探针")["unread_dm"] == 1
    _as_caller(server, "agent-a")
    assert call(server, "memory_search", query="探针")["unread_dm"] == 0


# --------------------------------------------------------------------------
# ② 消息表读失败 ⇒ 检索本体照常返回（提醒 fail-open）
# --------------------------------------------------------------------------

def test_消息表读失败时检索本体照常返回(box):
    """🔴 提醒是**附加值**：信箱表读炸了，检索结果必须照常给。

    造的是**真失败**（把 `ks_message` 表删掉 ⇒ count 查询直接抛
    `no such table`），不是 mock 一个异常 —— 这样「读失败」这条路径
    走的是与线上一致的异常分类。
    """
    server, reg = box
    # 先让信箱 schema 就绪（否则 ensure_schema 会把表建回来，测不到「读失败」）
    _as_caller(server, "agent-a")
    call(server, "message_send", to="agent-b", content="在吗")

    with reg.storage._write() as cur:                          # noqa: SLF001
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")

    _as_caller(server, "agent-b")
    res = call(server, "memory_search", query="探针")
    assert "results" in res, res            # 检索本体照常返回
    assert res["unread_dm"] == 0, res       # 提醒 fail-open（读不到 ⇒ 0）
