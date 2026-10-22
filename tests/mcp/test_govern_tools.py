"""MCP 治理面测试（`agent_*` 五工具，仅 primary 放行）。

覆盖四件事：

1. **闸门**：已批准但只是 sub 的代理 ⇒ 五个工具全拒（文案带 `role=sub`）；
   pending 代理 ⇒ 同样拒，但文案带 `status=pending`，与前者**不混同**；
   registry 里的 primary（本机 bootstrap，无凭据）⇒ 放行。
2. **业务语义**：list/approve 往返、revoke 的 confirm 双向、
   `set_role` 的全库唯一 primary、exclude 开关 —— 与控制台同一套 `Registry` 方法。
3. **审计**：每个治理写（含被拒绝的）留一行，`reason` 首段恒为 `source=mcp`，
   且**凭据明文不进审计**。
4. **工具面兼容性**：`govern` 默认关（既有 8 工具契约逐字不动），CLI 入口恒开。

全程 hermetic：`tmp_path` 里的临时 SQLite + 临时审计/备份目录，
不连 Redis / PG / 网络 / 生产库，也不改主脑的真实 config。
"""

from __future__ import annotations

import functools
import json
import pathlib
from typing import Any, Dict, List

import anyio
import pytest

from keepsake.mcp_server.server import build_server
from keepsake.mcp_server.tools import (GOVERN_TOOLS, KB_TOOLS, MESSAGE_TOOLS,
                                        TODO_TOOLS, ToolError)

MEMORY_TOOLS = {
    "memory_capabilities", "memory_search", "memory_get", "memory_core_list",
    "memory_add", "memory_remove", "memory_merge", "memory_forget",
}


# ---------------------------------------------------------------------------
# 夹具 / 帮手
# ---------------------------------------------------------------------------

def _build(tmp_path, monkeypatch, *, govern: bool = True):
    """按临时配置装配一个 server（审计/备份落 tmp；`KEEPSAKE_CONFIG` 指 tmp sqlite）。"""
    cfg = {
        "storage": {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "gov.db")}},
        # is_primary=true 只是首次引导的种子；之后角色一律由注册表说了算
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
def gov(tmp_path, monkeypatch):
    """主代理 `mcp`（本机 bootstrap ⇒ approved + primary）视角的治理面。"""
    from keepsake.adapters.hermes import provider as prov_mod

    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})      # noqa: SLF001 进程级缓存
    server = _build(tmp_path, monkeypatch)
    yield server
    server.agent_gate.stop()
    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})      # noqa: SLF001


def call(server, name: str, **arguments: Any) -> Dict[str, Any]:
    _, structured = anyio.run(functools.partial(server.call_tool, name, arguments))
    return structured["result"]


def _as_caller(server, agent_id: str) -> None:
    """把 stdio 闸门切到另一个代理（标成「还没复核过」⇒ 下次调用必走一遍判定链）。"""
    gate = server.agent_gate
    gate.agent_id = agent_id
    gate._stdio_ok = None       # noqa: SLF001
    gate._stdio_at = 0.0        # noqa: SLF001


def _audit_lines(tmp_path) -> List[Dict[str, Any]]:
    files = sorted(pathlib.Path(tmp_path / "audit").glob("audit-*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]


def _roles(reg) -> Dict[str, str]:
    return {a["agent_id"]: a["role"] for a in reg.list_agents()}


# ---------------------------------------------------------------------------
# ① 工具面
# ---------------------------------------------------------------------------

def test_治理面工具齐全_记忆工具不受影响(gov):
    names = {t.name for t in gov.list_tools()}
    assert set(GOVERN_TOOLS) <= names
    assert MEMORY_TOOLS <= names


def test_治理面默认不挂_既有八工具契约不动(tmp_path, monkeypatch):
    """`build_server()` 默认不开治理面：宿主枚举工具面看到的仍是记忆工具 + 信箱工具。

    🔴 2026-10 ks_agent_messaging：**信箱那 3 个与治理面无关**（所有已批准代理
    都能用），所以默认装配下工具面 = 记忆 8 个 + 信箱 3 个；
    治理那 5 个**仍然不挂**（本单没有放宽治理面的默认可见性）。
    🔴 2026-10 ks_kb_agents：**知识库那 4 个同样与治理面无关**（所有已批准代理
    都能用，归属 / 可见性 / 删除与记忆同一把尺子）⇒ 默认装配下也在工具面里。
    🔴 2026-10 ks_todo：**待办那 3 个同样与治理面无关**（所有已批准代理都能用）⇒
    默认装配下也在工具面里（工具面 15 → 18）。
    """
    server = _build(tmp_path, monkeypatch, govern=False)
    try:
        names = {t.name for t in server.list_tools()}
        assert names == (MEMORY_TOOLS | set(MESSAGE_TOOLS) | set(KB_TOOLS)
                         | set(TODO_TOOLS))
        assert not (set(GOVERN_TOOLS) & names)
    finally:
        server.agent_gate.stop()


def test_无注册表的provider没有闸门():
    """自造 provider（压根不碰 keepsake 存储）⇒ 不挂闸门，治理面自然也不可用。"""
    from keepsake.mcp_server.server import AgentGate

    assert AgentGate.for_provider(object(), None) is None


# ---------------------------------------------------------------------------
# ② 闸门：只有 primary 能进
# ---------------------------------------------------------------------------

def test_已批准的sub_五个治理工具全拒且文案带role(gov):
    reg = gov.agent_gate.registry
    reg.bootstrap_local("worker-sub", "工人")        # 本机通道 ⇒ approved + role=sub
    _as_caller(gov, "worker-sub")

    args = {
        "agent_list": {},
        "agent_approve": {"agent_id": "worker-sub"},
        "agent_revoke": {"agent_id": "worker-sub", "confirm": "worker-s"},
        "agent_set_role": {"agent_id": "worker-sub", "role": "primary", "confirm": "worker-sub"},
        "agent_set_exclude": {"agent_id": "worker-sub", "exclude": True},
    }
    for name, kwargs in args.items():
        with pytest.raises(ToolError) as exc:
            call(gov, name, **kwargs)
        text = str(exc.value)
        assert "治理工具仅主代理可用" in text
        assert "role=sub" in text and "status=approved" in text


def test_pending代理_拒绝文案带status_与sub可分(gov):
    reg = gov.agent_gate.registry
    reg.register("worker-pending", "待审工人")
    _as_caller(gov, "worker-pending")

    with pytest.raises(ToolError) as exc:
        call(gov, "agent_list")
    text = str(exc.value)
    assert "治理工具仅主代理可用" in text
    assert "status=pending" in text


def test_主代理可过闸(gov):
    """`mcp` 是本机 bootstrap 的 primary（stdio 无凭据）⇒ agent_list 放行。"""
    out = call(gov, "agent_list")
    assert out["count"] == 1 and out["items"][0]["agent_id"] == "mcp"
    assert out["items"][0]["role"] == "primary"


# ---------------------------------------------------------------------------
# ③ 业务语义（primary 视角）
# ---------------------------------------------------------------------------

def test_列代理与批准往返(gov):
    reg = gov.agent_gate.registry
    reg.register("worker-1", "工人")

    out = call(gov, "agent_approve", agent_id="worker-1")
    cred = out["credential"]
    assert cred.startswith("ksk_") and out["credential_hint"] == cred[:8]

    # 库里只有 sha256，明文不落库（与控制台同一纪律）；对外形状也不含摘要
    row = reg.get("worker-1")
    assert row["status"] == "approved"
    assert "credential_hash" not in row
    assert reg.verify("worker-1", cred)["ok"] is True    # 凭据能对上 ⇒ 存的是它的摘要
    assert reg.verify("worker-1", cred + "0")["ok"] is False

    item = next(i for i in call(gov, "agent_list")["items"] if i["agent_id"] == "worker-1")
    assert item["credential_hint"] == cred[:8]
    assert "credential_hash" not in item


def test_吊销confirm双向(gov):
    reg = gov.agent_gate.registry
    reg.register("worker-2", "工人")
    cred = call(gov, "agent_approve", agent_id="worker-2")["credential"]

    with pytest.raises(ToolError) as exc:                 # 不给 confirm
        call(gov, "agent_revoke", agent_id="worker-2")
    assert "前 8 位" in str(exc.value)

    with pytest.raises(ToolError):                        # 少一位也不行
        call(gov, "agent_revoke", agent_id="worker-2", confirm="worker-2"[:7])
    assert reg.get("worker-2")["status"] == "approved"   # 被拒的写不许改数据

    out = call(gov, "agent_revoke", agent_id="worker-2", confirm="worker-2"[:8])
    assert out["status"] == "revoked"
    assert reg.verify("worker-2", cred)["ok"] is False   # 旧凭据立即作废


def test_改角色_全库唯一primary经MCP路径复证(gov):
    reg = gov.agent_gate.registry
    reg.register("worker-alpha", "工人甲")
    reg.register("worker-beta", "工人乙")

    with pytest.raises(ToolError):                        # confirm 必须是完整 agent_id
        call(gov, "agent_set_role", agent_id="worker-alpha", role="primary",
             confirm="worker-alpha"[:8])
    with pytest.raises(ToolError):                        # 角色只能是枚举内的值
        call(gov, "agent_set_role", agent_id="worker-alpha", role="root",
             confirm="worker-alpha")
    assert _roles(reg) == {"mcp": "primary", "worker-alpha": "sub", "worker-beta": "sub"}

    out = call(gov, "agent_set_role", agent_id="worker-alpha", role="primary",
               confirm="worker-alpha")
    assert out["demoted"] == ["mcp"]
    assert _roles(reg) == {"mcp": "sub", "worker-alpha": "primary", "worker-beta": "sub"}
    assert list(_roles(reg).values()).count("primary") == 1


def test_exclude开关往返(gov):
    reg = gov.agent_gate.registry
    reg.register("worker-5", "工人")

    assert call(gov, "agent_set_exclude", agent_id="worker-5", exclude=True)["changed"] is True
    assert reg.get("worker-5")["exclude_from_primary"] is True
    assert call(gov, "agent_set_exclude", agent_id="worker-5", exclude=True)["changed"] is False
    assert call(gov, "agent_set_exclude", agent_id="worker-5", exclude=False)["changed"] is True


def test_未知代理_报错不静默(gov):
    with pytest.raises(ToolError) as exc:
        call(gov, "agent_approve", agent_id="不存在")
    assert "注册表里没有这个代理" in str(exc.value)


# ---------------------------------------------------------------------------
# ④ 审计：source=mcp，且凭据明文不进审计
# ---------------------------------------------------------------------------

def test_审计留痕来源恒为mcp且无凭据明文(gov, tmp_path):
    reg = gov.agent_gate.registry
    reg.register("worker-6", "工人")
    cred = call(gov, "agent_approve", agent_id="worker-6")["credential"]
    call(gov, "agent_set_exclude", agent_id="worker-6", exclude=True)
    with pytest.raises(ToolError):                    # 被拒绝的写也要留痕（ok=false）
        call(gov, "agent_revoke", agent_id="worker-6", confirm="错")

    lines = _audit_lines(tmp_path)
    assert lines, "治理写必须留痕"
    assert all(l["reason"].startswith("source=mcp; ") for l in lines)
    assert {"agents.approve", "agents.exclude", "agents.revoke"} <= {l["action"] for l in lines}
    assert [l["ok"] for l in lines if l["action"] == "agents.revoke"] == [False]
    assert cred not in json.dumps(lines, ensure_ascii=False)      # 明文绝不进审计


def test_记忆工具不混进治理审计(tmp_path, monkeypatch):
    """治理面开着也不影响记忆工具：它只记注册表活动，不落审计行。"""
    server = _build(tmp_path, monkeypatch)
    try:
        call(server, "memory_add", text="一条记忆", tags="")
    finally:
        server.agent_gate.stop()
    assert _audit_lines(tmp_path) == []
