"""MCP 面上的记忆共享（`ks_share_memory`）：治理工具 + 检索可见性。

覆盖四件事：

1. **治理面**：`agent_list` 回包带 `share_memory`；`agent_set_share` 开关往返
   （仅 primary 可调 —— 非 primary 拿到的是治理面那条拒绝文案）；
2. **检索可见性**：子代理调用方**只看得见自己 + 已共享代理**的记忆；
3. **直取**：未共享代理的条目按「不存在」回（存在性也不泄露）；
4. **primary 不受影响**：primary 调用方仍是全量可见（现状逐字不变）。

全程 hermetic：临时 SQLite + 临时审计/备份目录，不连 Redis / PG / 网络。
"""

from __future__ import annotations

import functools
import json
from typing import Any, Dict, List

import anyio
import pytest

from keepsake.mcp_server.server import build_server
from keepsake.mcp_server.tools import ToolError

QUERY = "回滚 流程 校验"


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


def _seed(server, tmp_path) -> Dict[str, str]:
    """种三条记忆（分别归属三个代理）+ 一个已批准的第三方代理。

    归属标签要原样落库 ⇒ 用**没有 agent_id 的**存储写（存储层 store() 会把
    `agent:` 标签改写成自己的归属）。
    """
    from keepsake.core.sqlite import SqliteStorage
    from keepsake.core.shared import fragment_key_for

    reg = server.agent_gate.registry
    reg.bootstrap_local("worker-sub", "子代理")
    reg.bootstrap_local("worker-other", "另一个子代理")

    path = str(tmp_path / "gov.db")
    writer = SqliteStorage(path=path)
    try:
        keys: Dict[str, str] = {}
        for suffix, tags in (("甲", "agent:worker-sub"),
                             ("乙", "agent:worker-other"),
                             ("丙", "agent:worker-sub,shared")):
            text = f"{QUERY} {suffix}"
            assert writer.store(text=text, tags=tags, category="probe",
                                source="probe") is True
            keys[suffix] = fragment_key_for(text)
    finally:
        writer.close()
    return keys


def _suffixes(hits: List[Dict[str, Any]]) -> set:
    return {str(h.get("content", ""))[-1] for h in hits}


# ---------------------------------------------------------------------------
# ① 治理面
# ---------------------------------------------------------------------------

def test_agent_list带share_memory字段(gov):
    reg = gov.agent_gate.registry
    reg.bootstrap_local("worker-sub", "子代理")
    items = call(gov, "agent_list")["items"]
    row = next(a for a in items if a["agent_id"] == "worker-sub")
    assert "share_memory" in row
    assert row["share_memory"] is False


def test_agent_set_share开关往返(gov):
    reg = gov.agent_gate.registry
    reg.bootstrap_local("worker-sub", "子代理")
    out = call(gov, "agent_set_share", agent_id="worker-sub", enabled=True)
    assert out["ok"] is True and out["share_memory"] is True and out["changed"] is True
    assert reg.share_memory("worker-sub") is True
    again = call(gov, "agent_set_share", agent_id="worker-sub", enabled=True)
    assert again["changed"] is False
    off = call(gov, "agent_set_share", agent_id="worker-sub", enabled=False)
    assert off["share_memory"] is False and off["changed"] is True


def test_非primary调不了治理工具(gov):
    reg = gov.agent_gate.registry
    reg.bootstrap_local("worker-sub", "子代理")
    _as_caller(gov, "worker-sub")
    with pytest.raises(ToolError) as exc:
        call(gov, "agent_set_share", agent_id="worker-sub", enabled=True)
    assert "治理工具仅主代理可用" in str(exc.value)
    assert reg.share_memory("worker-sub") is False


# ---------------------------------------------------------------------------
# ② 检索可见性
# ---------------------------------------------------------------------------

def test_子代理只看得见自己的记忆(gov, tmp_path):
    _seed(gov, tmp_path)
    _as_caller(gov, "worker-sub")
    hits = call(gov, "memory_search", query=QUERY)["results"]
    who = _suffixes(hits)
    assert "甲" in who and "丙" in who, who
    assert "乙" not in who, who


def test_共享打开后子代理看得见对方的记忆(gov, tmp_path):
    _seed(gov, tmp_path)
    reg = gov.agent_gate.registry
    reg.set_share_memory("worker-other", True)
    _as_caller(gov, "worker-sub")
    hits = call(gov, "memory_search", query=QUERY)["results"]
    who = _suffixes(hits)
    assert "乙" in who, who


def test_共享关回去立刻看不见(gov, tmp_path):
    _seed(gov, tmp_path)
    reg = gov.agent_gate.registry
    reg.set_share_memory("worker-other", True)
    _as_caller(gov, "worker-sub")
    assert "乙" in _suffixes(call(gov, "memory_search", query=QUERY)["results"])
    reg.set_share_memory("worker-other", False)
    assert "乙" not in _suffixes(call(gov, "memory_search", query=QUERY)["results"])


def test_primary调用方仍是全量可见(gov, tmp_path):
    _seed(gov, tmp_path)
    hits = call(gov, "memory_search", query=QUERY)["results"]
    who = _suffixes(hits)
    assert {"甲", "乙", "丙"} <= who, who


# ---------------------------------------------------------------------------
# ③ 直取：未共享代理的条目按「不存在」回
# ---------------------------------------------------------------------------

def test_直取未共享代理的条目报不存在(gov, tmp_path):
    keys = _seed(gov, tmp_path)
    _as_caller(gov, "worker-sub")
    with pytest.raises(ToolError) as exc:
        call(gov, "memory_get", key=keys["乙"])
    assert "不存在" in str(exc.value)


def test_直取共享代理的条目取得到(gov, tmp_path):
    keys = _seed(gov, tmp_path)
    gov.agent_gate.registry.set_share_memory("worker-other", True)
    _as_caller(gov, "worker-sub")
    out = call(gov, "memory_get", key=keys["乙"])
    assert out["key"] == keys["乙"]


# ---------------------------------------------------------------------------
# ④ 写 core：人工显式直写**只对 primary 凭据生效**（2026-10 ks_share_tighten）
# ---------------------------------------------------------------------------

def _stored_tags(gov, key: str) -> List[str]:
    """落库后的标签（`memory_add` 返回的是**请求里**的 tags —— 拿它验闸门等于没验）。"""
    row = gov.agent_gate.registry.storage.get_fragment(key) or {}
    return [t.strip() for t in str(row.get("tags") or "").split(",") if t.strip()]


def test_子代理写core落候选不进常驻(gov, tmp_path):
    """🔴 外部代理带 `core` 标签进来 ⇒ 不许直写常驻层。

    落 `core_candidate` **且带提名日戳**（2026-10 ks_perm_scope：子代理自荐
    常驻 = 与主脑候选**同一条审核/班车链** —— 此前「降级但不打戳」等于死候选，
    班车永远不审它）。**常驻层本体它仍写不进**（没有裸 `core` 标签）。
    """
    _seed(gov, tmp_path)
    _as_caller(gov, "worker-sub")
    key = call(gov, "memory_add", text="子代理要记住的事", tags="core")["key"]
    tags = _stored_tags(gov, key)
    assert "core_candidate" in tags, tags
    assert "core" not in tags, tags
    assert any(t.startswith("tier:auto-hot:") for t in tags), tags


def test_primary写core仍直写(gov):
    """primary 凭据 ⇒「人在对话里明确要求记这一条」照旧：直写常驻层。"""
    key = call(gov, "memory_add", text="主脑要记住的事", tags="core")["key"]
    tags = _stored_tags(gov, key)
    assert "core" in tags, tags
    assert "core_candidate" not in tags, tags
