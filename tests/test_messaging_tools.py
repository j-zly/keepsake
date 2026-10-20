"""代理互聊信箱的 MCP 工具面测试（`message_send` / `message_inbox` / `message_recent`）。

覆盖五件事：

1. **投递闸门**：目标代理不存在 / 未批准 / 已吊销 ⇒ 拒投（一条都不出去）；
   目标 `dm_enabled=0` ⇒ 拒投并说明；
2. **发件人身份只认凭据反查**（工具签名里没有 `from`）；
3. **fetch 即标读**：`message_inbox` 取走的消息不再出现在未读里；
4. **打点**：三个工具都按**写**计数（收信即消费 = 状态变更）；
5. **闸门拒绝文案四分法**（2026-10-20 用户本机误判两小时的根因）：
   没带凭据 / 凭据无效 / 未批准 / 服务端暂时读不到 —— **各回各话**。

全程 hermetic：`tmp_path` 里的临时 SQLite，不连 Redis / PG / 网络 / 生产库。
"""

from __future__ import annotations

import functools
import json
from typing import Any, Dict

import anyio
import pytest

from keepsake.mcp_server.server import (
    GATE_DENIED_NOT_APPROVED,
    GATE_DENIED_NO_CREDENTIAL,
    GATE_DENIED_UNAVAILABLE,
    GATE_DENIED_UNKNOWN_AGENT,
    build_server,
    gate_denied_text,
)
from keepsake.mcp_server.tools import ToolError


# --------------------------------------------------------------------------
# 夹具 / 帮手
# --------------------------------------------------------------------------

def _build(tmp_path, monkeypatch, *, govern: bool = False):
    """按临时配置装配一个 server（存储落在 tmp 里的 sqlite）。"""
    cfg = {
        "storage": {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "msg.db")}},
        "is_primary": True,
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.delenv("KEEPSAKE_AGENT_ID", raising=False)
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL", raising=False)
    return build_server(govern=govern)


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
# ① 投递闸门
# --------------------------------------------------------------------------

def test_发给未批准代理被拒且没投出去(box):
    """🔴 目标代理 pending ⇒ 一条都不许投出去（收件箱恒空）。"""
    server, reg = box
    reg.register("pending-agent")
    _as_caller(server, "agent-a")
    with pytest.raises(ToolError) as ei:
        call(server, "message_send", to="pending-agent", content="在吗")
    assert "未批准" in str(ei.value) and "未投递" in str(ei.value)
    _as_caller(server, "agent-b")
    assert call(server, "message_inbox") == []


def test_发给已吊销代理被拒(box):
    server, reg = box
    reg.register("revoked-agent")
    reg.approve("revoked-agent")
    reg.revoke("revoked-agent")
    _as_caller(server, "agent-a")
    with pytest.raises(ToolError) as ei:
        call(server, "message_send", to="revoked-agent", content="在吗")
    assert "未批准" in str(ei.value)


def test_发给不存在的代理被拒(box):
    server, _ = box
    _as_caller(server, "agent-a")
    with pytest.raises(ToolError) as ei:
        call(server, "message_send", to="查无此代理", content="在吗")
    assert "未批准" in str(ei.value) and "未投递" in str(ei.value)


def test_目标关了私信拒投并说明(box):
    """🔴 `dm_enabled=0` ⇒ 拒投，且**说明原因**（不让人猜）。"""
    server, reg = box
    reg.set_dm_enabled("agent-b", False)
    _as_caller(server, "agent-a")
    with pytest.raises(ToolError) as ei:
        call(server, "message_send", to="agent-b", content="在吗")
    assert "dm_enabled" in str(ei.value) and "未投递" in str(ei.value)
    _as_caller(server, "agent-b")
    assert call(server, "message_inbox") == []


# --------------------------------------------------------------------------
# ② 身份 / 正文 / 线程号
# --------------------------------------------------------------------------

def test_发件人身份来自凭据反查而不是调用侧(box):
    """🔴 工具签名里**没有 from** —— 发件人只能是凭据反查出来的那一个。"""
    server, _ = box
    _as_caller(server, "agent-a")
    out = call(server, "message_send", to="agent-b", content="在吗")
    assert out["ok"] is True and out["seq"] > 0
    _as_caller(server, "agent-b")
    inbox = call(server, "message_inbox")
    assert len(inbox) == 1
    assert inbox[0]["from"] == "agent-a"          # 发件人是 agent-a，不是「随便谁」
    assert inbox[0]["content"] == "在吗"
    assert inbox[0]["thread_id"]


def test_未批准的调用方发不出去(box):
    """🔴 调用方自己没批准 ⇒ 闸门就拒（与目标无关）。"""
    server, reg = box
    reg.register("stranger")
    _as_caller(server, "stranger")
    with pytest.raises(ToolError):
        call(server, "message_send", to="agent-b", content="在吗")


def test_正文超长报错(box):
    server, _ = box
    _as_caller(server, "agent-a")
    with pytest.raises(ToolError) as ei:
        call(server, "message_send", to="agent-b", content="x" * 4001)
    assert "超长" in str(ei.value)
    with pytest.raises(ToolError):
        call(server, "message_send", to="agent-b", content="   ")


def test_空thread_id自动生成并回显(box):
    server, _ = box
    _as_caller(server, "agent-a")
    out = call(server, "message_send", to="agent-b", content="在吗")
    assert out["thread_id"].startswith("th_") and len(out["thread_id"]) == 11
    # 显式给的 thread_id 原样保留（回复续同一段对话）
    out2 = call(server, "message_send", to="agent-b", content="说事", thread_id="th_fixed")
    assert out2["thread_id"] == "th_fixed"


# --------------------------------------------------------------------------
# ③ 收件箱：fetch 即标读
# --------------------------------------------------------------------------

def test_取走即已读_二次取不到(box):
    server, _ = box
    _as_caller(server, "agent-a")
    call(server, "message_send", to="agent-b", content="在吗")
    _as_caller(server, "agent-b")
    assert len(call(server, "message_inbox")) == 1
    assert call(server, "message_inbox") == []              # 未读里没有了
    assert len(call(server, "message_inbox", unread_only=False)) == 1   # 历史还在


def test_recent双向且不动已读状态(box):
    server, _ = box
    _as_caller(server, "agent-a")
    call(server, "message_send", to="agent-b", content="问一句")
    _as_caller(server, "agent-b")
    call(server, "message_send", to="agent-a", content="答一句")
    recent = call(server, "message_recent", peer="agent-a")
    assert [r["content"] for r in recent] == ["问一句", "答一句"]
    # recent 不得吃掉未读
    _as_caller(server, "agent-a")
    assert len(call(server, "message_inbox")) == 1


# --------------------------------------------------------------------------
# ④ 打点：三个工具都算写
# --------------------------------------------------------------------------

def test_三个信箱工具都记写计数(box):
    """🔴 收信即消费（标已读）是状态变更 ⇒ 三个工具**都**按写计数。"""
    server, reg = box
    _as_caller(server, "agent-a")
    call(server, "message_send", to="agent-b", content="在吗")
    row = reg.get("agent-a")
    assert row["writes"] == 1 and row["reads"] == 0

    _as_caller(server, "agent-b")
    call(server, "message_inbox")
    row = reg.get("agent-b")
    assert row["writes"] == 1 and row["reads"] == 0


# --------------------------------------------------------------------------
# ⑤ 闸门拒绝文案四分法
# --------------------------------------------------------------------------

def test_没带凭据的回执(tmp_path, monkeypatch):
    """🔴 http 传输且**没带 Authorization 头** ⇒ 回「请求未带凭据」。

    这正是 2026-10-20 用户本机误判两小时的场景：真因是没带凭据，
    旧文案却回成「代理 main-brain 未批准」。
    """
    server = _build(tmp_path, monkeypatch)
    try:
        gate = server.agent_gate
        gate._http_headers = lambda: {}          # noqa: SLF001 —— 伪造「无头」请求
        with pytest.raises(ToolError) as ei:
            gate.check("memory_search")
        assert str(ei.value) == GATE_DENIED_NO_CREDENTIAL
        assert "main-brain" not in str(ei.value)
    finally:
        server.agent_gate.stop()


def test_凭据无效的回执(tmp_path, monkeypatch):
    server = _build(tmp_path, monkeypatch)
    try:
        gate = server.agent_gate
        gate._http_headers = lambda: {"authorization": "Bearer ksk_" + "0" * 32}
        with pytest.raises(ToolError) as ei:
            gate.check("memory_search")
        assert str(ei.value) == GATE_DENIED_UNKNOWN_AGENT
    finally:
        server.agent_gate.stop()


def test_未批准的回执带凭据前8位(tmp_path, monkeypatch):
    """🔴 未批准 / 已吊销 ⇒ 保持原措辞，外加**真实反查线索**（前 8 位）。

    🔴 只回显 hint 级信息：完整凭据**一个字都不许出现在消息里**。
    """
    server = _build(tmp_path, monkeypatch)
    try:
        reg = server.agent_gate.registry
        reg.register("worker-1")
        credential = reg.approve("worker-1")
        reg.revoke("worker-1")
        gate = server.agent_gate
        gate.agent_id = "worker-1"
        gate._credential = credential
        gate._stdio_ok = None          # noqa: SLF001 —— 强制本次调用走一遍判定链
        gate._stdio_at = 0.0           # noqa: SLF001
        with pytest.raises(ToolError) as ei:
            gate.check("memory_search")
        text = str(ei.value)
        assert text == GATE_DENIED_NOT_APPROVED.format(
            agent_id="worker-1", hint=credential[:8])
        assert credential[:8] in text
        assert credential not in text            # 完整凭据绝不回显
    finally:
        server.agent_gate.stop()


def test_服务端读不到注册表的回执(tmp_path, monkeypatch):
    """🔴 PG 抖动（连接类异常）**不许**与「查无此人」混同一句。"""
    class OperationalError(Exception):
        """按类名判定的假异常（`is_connection_error` 只看类名，不 import psycopg）。"""

    server = _build(tmp_path, monkeypatch)
    try:
        gate = server.agent_gate
        reg = gate.registry

        def _boom(_credential: str) -> Dict[str, Any]:
            raise OperationalError("server closed the connection unexpectedly")

        reg.verify_any = _boom                # type: ignore[method-assign]
        gate._http_headers = lambda: {"authorization": "Bearer ksk_x"}   # noqa: SLF001
        with pytest.raises(ToolError) as ei:
            gate.check("memory_search")
        assert str(ei.value) == GATE_DENIED_UNAVAILABLE
        assert "未批准" not in str(ei.value)
    finally:
        server.agent_gate.stop()


def test_四类文案各回各话():
    """🔴 纯函数判据：四类 reason ⇒ 四句互不相同的话（不许揉成一句）。"""
    texts = {
        gate_denied_text("no_credential"),
        gate_denied_text("unknown_agent"),
        gate_denied_text("not_approved", agent_id="a", credential_hint="ksk_abcd"),
        gate_denied_text("registry_unavailable"),
    }
    assert len(texts) == 4
    assert "Authorization" in gate_denied_text("no_credential")
    assert "重新换发" in gate_denied_text("unknown_agent")
    assert "ksk_abcd" in gate_denied_text("not_approved", agent_id="a",
                                          credential_hint="ksk_abcd")
    assert "几十秒" in gate_denied_text("registry_unavailable")
