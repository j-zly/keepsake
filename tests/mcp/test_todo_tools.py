"""MCP 面上的待办工具（`todo_add` / `todo_list` / `todo_done`）。

2026-10 ks_todo：待办是一等公民 ⇒ 代理侧也有工具面。判据一条条钉死：

  ① `todo_add`：owner 缺省 = **调用方**（凭据反查，不是自报）；可以指他人，
     但那个代理**必须在册且已批准**（未批准 ⇒ 拒，不静默挂空）；
  ② `todo_list`：默认只看**开放**的；非 primary 只见 **owner=自己 ∪ creator=自己**
     （被指派者必见自己的活），primary 全量；
  ③ `todo_done` / 标废弃：**只有 owner / creator / primary 能改**，别人的一律拒；
  ④ **未批准**的凭据三工具全拒（fail-closed，与记忆工具同一道闸）。

存储用**临时 SQLite**（真 `ks_agent` + `ks_todo` 两张表）—— 不连 Redis / 生产库。
"""

from __future__ import annotations

import functools
import json
from typing import Any, Dict, List

import anyio
import pytest

from keepsake.mcp_server.server import build_server
from keepsake.mcp_server.tools import ToolError

#: 主代理（stdio 缺省身份）与两个子代理
MAIN = "mcp"
SUB = "worker-sub"
OTHER = "worker-other"
PENDING = "worker-pending"


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """临时 sqlite 配置 + 装配好的 MCP server（默认装配 ⇒ 带代理注册表与闸门）。"""
    cfg = {
        "storage": {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "todo.db")}},
        "is_primary": True,
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.delenv("KEEPSAKE_AGENT_ID", raising=False)
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL", raising=False)

    from keepsake.adapters.hermes import provider as prov_mod

    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})
    s = build_server()
    yield s
    s.agent_gate.stop()
    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})


def call(server, name: str, **arguments: Any) -> Any:
    _, structured = anyio.run(functools.partial(server.call_tool, name, arguments))
    return structured["result"]


def _as_caller(server, agent_id: str) -> None:
    """把 stdio 闸门切到另一个代理（标成「还没复核过」⇒ 下次调用必走一遍判定链）。"""
    gate = server.agent_gate
    gate.agent_id = agent_id
    gate._stdio_ok = None       # noqa: SLF001
    gate._stdio_at = 0.0        # noqa: SLF001


def _seed_agent(server, agent_id: str, name: str = "", as_primary: bool = False) -> None:
    """把代理登记进注册表并**本机批准**（`bootstrap_local`：与记忆面同一套身份链）。"""
    server.agent_gate.registry.bootstrap_local(agent_id, name or agent_id,
                                               as_primary=as_primary)


def _seed_pending(server, agent_id: str) -> None:
    """登记一个**未批准**的代理（存在但没批）。"""
    server.agent_gate.registry.register(agent_id, agent_id)


# ---------------------------------------------------------------------------
# ① todo_add
# ---------------------------------------------------------------------------

def test_todo_add_owner缺省是调用方(server):
    """🔴 归属 = **凭据反查的调用方**（不是自报的 —— 工具签名里没有 owner 必填）。"""
    _seed_agent(server, SUB, "子代理")
    _as_caller(server, SUB)
    out = call(server, "todo_add", title="补回滚手册")
    assert out["ok"] is True
    assert out["owner"] == SUB
    assert out["creator"] == SUB
    assert out["status"] == "open"
    assert out["id"] > 0


def test_todo_add_可以指别人(server):
    """🔴 可指他人（协作台账）：owner 是那个人，creator 仍是记录人。"""
    _seed_agent(server, SUB, "子代理")
    _seed_agent(server, OTHER, "另一个人")
    _as_caller(server, SUB)
    out = call(server, "todo_add", title="你来办", owner=OTHER)
    assert out["owner"] == OTHER
    assert out["creator"] == SUB


def test_todo_add_owner未批准被拒(server):
    """🔴 owner 不在册 / 未批准 ⇒ 拒（不静默挂一个空 owner）。"""
    _seed_agent(server, SUB, "子代理")
    _seed_pending(server, PENDING)
    _as_caller(server, SUB)
    with pytest.raises(ToolError) as exc:
        call(server, "todo_add", title="派给没批的人", owner=PENDING)
    assert "未批准" in str(exc.value), str(exc.value)
    with pytest.raises(ToolError):
        call(server, "todo_add", title="派给不存在的人", owner="nobody")


def test_todo_add_空标题被拒(server):
    _seed_agent(server, SUB, "子代理")
    _as_caller(server, SUB)
    with pytest.raises(ToolError):
        call(server, "todo_add", title="   ")


# ---------------------------------------------------------------------------
# ② todo_list
# ---------------------------------------------------------------------------

def test_todo_list_默认只看开放的(server):
    _seed_agent(server, SUB, "子代理")
    _as_caller(server, SUB)
    tid = call(server, "todo_add", title="一件活儿")["id"]
    call(server, "todo_done", id=tid)
    assert call(server, "todo_list") == []
    assert len(call(server, "todo_list", scope="done")) == 1
    assert len(call(server, "todo_list", scope="all")) == 1


def test_todo_list_非primary只见与自己有关的(server):
    """🔴 可见 = owner ∪ creator（**被指派者必见自己的活**）。

    三条分别落在三个象限：owner 是我 / creator 是我 / 两样都不是我。
    """
    _seed_agent(server, SUB, "子代理")
    _seed_agent(server, OTHER, "另一个人")

    # 别人自己记、自己干的一条（owner=creator=OTHER ⇒ SUB 看不见）
    _as_caller(server, OTHER)
    call(server, "todo_add", title="别人的私活")
    # 派给 SUB 的一条（owner=SUB, creator=OTHER ⇒ **被指派者必见**）
    call(server, "todo_add", title="派给我的活", owner=SUB)
    # SUB 自己记的一条（owner=creator=SUB）
    _as_caller(server, SUB)
    call(server, "todo_add", title="我自己的活")

    titles = {r["title"] for r in call(server, "todo_list", scope="all")}
    assert titles == {"派给我的活", "我自己的活"}
    assert "别人的私活" not in titles


def test_todo_list_primary全量(server):
    """🔴 primary 看得到全部（治理面：主脑要看到所有待办）。"""
    _seed_agent(server, SUB, "子代理")
    _as_caller(server, SUB)
    call(server, "todo_add", title="子代理的活儿")
    _seed_agent(server, MAIN, "主代理", as_primary=True)
    _as_caller(server, MAIN)
    titles = {r["title"] for r in call(server, "todo_list", scope="all")}
    assert "子代理的活儿" in titles


def test_todo_list_非法scope被拒(server):
    _seed_agent(server, SUB, "子代理")
    _as_caller(server, SUB)
    with pytest.raises(ToolError):
        call(server, "todo_list", scope="随便")


def test_todo_list_带due_state(server):
    """🔴 到期态由**后端**给（前端不自己算日期）。"""
    import datetime as _dt
    _seed_agent(server, SUB, "子代理")
    _as_caller(server, SUB)
    old = (_dt.datetime.now(_dt.timezone.utc).date()
           - _dt.timedelta(days=5)).isoformat()
    call(server, "todo_add", title="欠账的一件", due=old)
    rows = call(server, "todo_list", scope="all")
    assert rows[0]["due_state"] == "overdue"


# ---------------------------------------------------------------------------
# ③ todo_done / 标废弃
# ---------------------------------------------------------------------------

def test_todo_done_只有归属人能改(server):
    """🔴 别人的待办**改不了**（owner / creator / primary 才能改）。"""
    _seed_agent(server, SUB, "子代理")
    _seed_agent(server, OTHER, "另一个人")
    _as_caller(server, SUB)
    tid = call(server, "todo_add", title="我的活儿", owner=SUB)["id"]
    _as_caller(server, OTHER)
    with pytest.raises(ToolError) as exc:
        call(server, "todo_done", id=tid)
    assert "与你无关" in str(exc.value), str(exc.value)


def test_todo_done_creator也能改(server):
    """🔴 记录人（creator）也能改 —— 被派活的人与派活的人都能销账。"""
    _seed_agent(server, SUB, "子代理")
    _seed_agent(server, OTHER, "另一个人")
    _as_caller(server, SUB)
    tid = call(server, "todo_add", title="派给他的", owner=OTHER)["id"]
    _as_caller(server, OTHER)
    out = call(server, "todo_done", id=tid)
    assert out["ok"] is True and out["status"] == "done"


def test_todo_done_primary能改任何人的(server):
    _seed_agent(server, SUB, "子代理")
    _as_caller(server, SUB)
    tid = call(server, "todo_add", title="子代理的活儿", owner=SUB)["id"]
    _seed_agent(server, MAIN, "主代理", as_primary=True)
    _as_caller(server, MAIN)
    out = call(server, "todo_done", id=tid)
    assert out["ok"] is True


def test_todo_done_标废弃与重新打开(server):
    _seed_agent(server, SUB, "子代理")
    _as_caller(server, SUB)
    tid = call(server, "todo_add", title="一件事")["id"]
    assert call(server, "todo_done", id=tid, status="obsolete")["status"] == "obsolete"
    assert call(server, "todo_done", id=tid, status="open")["status"] == "open"


def test_todo_done_不存在被拒(server):
    _seed_agent(server, SUB, "子代理")
    _as_caller(server, SUB)
    with pytest.raises(ToolError):
        call(server, "todo_done", id=999999)


# ---------------------------------------------------------------------------
# ④ 闸门与工具面
# ---------------------------------------------------------------------------

def test_未批准的凭据三工具全拒(server):
    """🔴 fail-closed：未批准 ⇒ 全部拒绝（与记忆工具同一道闸）。"""
    _seed_pending(server, PENDING)
    _as_caller(server, PENDING)
    with pytest.raises(ToolError):
        call(server, "todo_add", title="没批的人写的")
    with pytest.raises(ToolError):
        call(server, "todo_list")
    with pytest.raises(ToolError):
        call(server, "todo_done", id=1)


def test_工具面含三个待办工具(server):
    names = {t.name for t in server.list_tools()}
    assert {"todo_add", "todo_list", "todo_done"} <= names


def test_写口登记(server):
    """🔴 `todo_add` / `todo_done` 算写（状态变更）；`todo_list` 是纯读。"""
    from keepsake.mcp_server.server import GATE_WRITE_TOOLS

    assert "todo_add" in GATE_WRITE_TOOLS
    assert "todo_done" in GATE_WRITE_TOOLS
    assert "todo_list" not in GATE_WRITE_TOOLS
