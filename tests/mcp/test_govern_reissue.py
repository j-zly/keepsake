"""MCP 治理面：已批准代理再 approve 必须**显式 reissue**（重复批准不许静默换发）。

**为什么单开一张单**：`tests/mcp/test_govern_tools.py` 是只读文件（属主 root），
改不动 —— 治理面「同语义」那几条用例在这里补齐，**不复制**那张单里的夹具
以外的逻辑（判定在后端原语 `Registry.approve` 里，两条路共用同一份）。

覆盖三条：

1. **同语义**：已 approved 的代理再 `agent_approve` ⇒ 拒（`ToolError`），
   旧凭据原样还能用（没被顶掉）；
2. **换发要显式**：`reissue=true` ⇒ 换一条新凭据，旧的那条立即作废；
3. **审计有痕**：被拒的那次留一行 `ok=false` 的痕（`reason` 含 `already_approved`）。

全程 hermetic：`tmp_path` 里的临时 SQLite + 临时审计/备份目录，
不连 Redis / PG / 网络 / 生产库。
"""

from __future__ import annotations

import functools
import json
import pathlib
from typing import Any, Dict, List

import anyio
import pytest

from keepsake.mcp_server.server import build_server
from keepsake.mcp_server.tools import ToolError


def _build(tmp_path, monkeypatch):
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
    return build_server(govern=True)


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


def _audit_lines(tmp_path) -> List[Dict[str, Any]]:
    files = sorted(pathlib.Path(tmp_path / "audit").glob("audit-*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]


def test_已批准代理再批准_拒且旧凭据没被顶掉(gov):
    """🔴 治理工具对已批准代理「再批准一次」是误伤线上代理的第三条路。

    以前这里会静默签发新凭据把旧的顶掉 —— 线上代理瞬间全断，而操作者（主代理）
    甚至人类都毫无感知。现在默认拒（旧凭据原样还能用）。
    """
    reg = gov.agent_gate.registry
    reg.register("worker-7", "工人")
    cred = call(gov, "agent_approve", agent_id="worker-7")["credential"]

    with pytest.raises(ToolError) as exc:                  # 不带 reissue ⇒ 拒
        call(gov, "agent_approve", agent_id="worker-7")
    assert "已批准" in str(exc.value)
    assert "reissue" in str(exc.value)
    # 一个字节都没动：旧凭据照旧能用（反查也还认得出它）
    assert reg.verify("worker-7", cred)["ok"] is True
    assert reg.verify_any(cred)["agent_id"] == "worker-7"
    assert reg.get("worker-7")["status"] == "approved"


def test_治理面换发要显式reissue_旧的立即作废(gov):
    """显式 `reissue=true` ⇒ 换发：旧的那条当场作废，新的可用。"""
    reg = gov.agent_gate.registry
    reg.register("worker-8", "工人")
    cred = call(gov, "agent_approve", agent_id="worker-8")["credential"]

    out = call(gov, "agent_approve", agent_id="worker-8", reissue=True)
    assert out["credential"] != cred
    assert reg.verify("worker-8", cred)["ok"] is False      # 旧的立即作废
    assert reg.verify_any(cred)["ok"] is False              # 反查也认不出它了
    assert reg.verify("worker-8", out["credential"])["ok"] is True
    assert reg.verify_any(out["credential"])["agent_id"] == "worker-8"


def test_治理面重复批准被拒_审计留痕(gov, tmp_path):
    """🔴 被拒的治理写也要留痕：action=agents.approve / ok=false / reason 含 already_approved。"""
    reg = gov.agent_gate.registry
    reg.register("worker-9", "工人")
    call(gov, "agent_approve", agent_id="worker-9")
    with pytest.raises(ToolError):
        call(gov, "agent_approve", agent_id="worker-9")

    lines = _audit_lines(tmp_path)
    rejected = [l for l in lines if l["action"] == "agents.approve" and not l["ok"]]
    assert rejected, "被拒的批准必须留一行痕"
    assert "already_approved" in rejected[-1]["reason"]
    assert rejected[-1]["reason"].startswith("source=mcp; ")
    # 凭据明文绝不进审计
    assert cred_not_in_lines(lines)


def cred_not_in_lines(lines: List[Dict[str, Any]]) -> bool:
    """审计里不许出现任何 `ksk_` 明文（凭据纪律的反钉）。"""
    text = json.dumps(lines, ensure_ascii=False)
    return "ksk_" not in text
