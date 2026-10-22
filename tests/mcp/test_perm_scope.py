"""删除权限按归属收窄 + 子代理自荐常驻（2026-10 ks_perm_scope）。

用户拍板的口径：**子代理和主脑同质能力** —— 但都限定在「自己的」范围。

覆盖两组（对应任务书判据）：

**A 组（删除权限）**
  ① 子代理删**自己**的条目 ⇒ 过；
  ② 子代理删**别人**的条目 ⇒ 拒（「不是你的记忆」，**不回显正文**）；
  ③ **无归属的老条目**（收紧前的公共数据）⇒ 子代理可删（与检索口径一致）；
  ④ primary 删任意条目 ⇒ 过（= 主脑现状逐字不变）；
  ⑤ 子代理调 `memory_forget`（**批处理治理面**）⇒ 拒（「治理动作仅主代理可用」）。

**B 组（子代理自荐常驻）**
  ⑥ 子代理写 `core` ⇒ 落候选 **且带提名日戳**，可被 nominate 枚举到
     （此前「降级但不打戳」= 死候选，班车永远不审它）；
  ⑦ **常驻层本体它仍写不进**（没有裸 `core` 标签）。

全程 hermetic：临时 SQLite + 临时审计/备份目录，不连 Redis / PG / 网络 / 生产库。
"""

from __future__ import annotations

import functools
import json
from typing import Any, Dict, List

import anyio
import pytest

from keepsake.mcp_server.server import build_server
from keepsake.mcp_server.tools import ToolError

#: 子代理（调用方）与另一个子代理（被删的对象）。
SUB = "worker-sub"
OTHER = "worker-other"


def _build(tmp_path, monkeypatch):
    """按临时配置装配一个 server（审计/备份落 tmp；`KEEPSAKE_CONFIG` 指 tmp sqlite）。"""
    cfg = {
        "storage": {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "perm.db")}},
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
    """种三条记忆：子代理自己的 / 别人的 / **无归属的老条目**。

    归属标签要原样落库 ⇒ 用**没有 agent_id 的**存储写（存储层 store() 会把
    `agent:` 标签改写成自己的归属）。
    """
    from keepsake.core.sqlite import SqliteStorage
    from keepsake.core.shared import fragment_key_for

    reg = server.agent_gate.registry
    reg.bootstrap_local(SUB, "子代理")
    reg.bootstrap_local(OTHER, "另一个子代理")

    path = str(tmp_path / "perm.db")
    writer = SqliteStorage(path=path)
    try:
        keys: Dict[str, str] = {}
        for suffix, tags in (("自己的", f"agent:{SUB}"),
                             ("别人的", f"agent:{OTHER}"),
                             ("老条目", "legacy")):
            text = f"回滚 流程 校验 {suffix}"
            assert writer.store(text=text, tags=tags, category="probe",
                                source="probe") is True
            keys[suffix] = fragment_key_for(text)
    finally:
        writer.close()
    return keys


def _stored_tags(gov, key: str) -> List[str]:
    """落库后的标签（拿请求里的 tags 验闸门等于没验）。"""
    row = gov.agent_gate.registry.storage.get_fragment(key) or {}
    return [t.strip() for t in str(row.get("tags") or "").split(",") if t.strip()]


# ---------------------------------------------------------------------------
# A 组：删除权限按归属收窄
# ---------------------------------------------------------------------------

def test_子代理删自己的条目通过_owner(gov, tmp_path):
    """🔴 ① 子代理删**自己**的条目 ⇒ 过（同质能力，只是范围限定在「自己的」）。"""
    keys = _seed(gov, tmp_path)
    _as_caller(gov, SUB)
    out = call(gov, "memory_remove", key=keys["自己的"], confirm=True)
    assert out["removed"] is True, out
    assert gov.agent_gate.registry.storage.get_fragment(keys["自己的"]) is None


def test_子代理删他人条目被拒_owner(gov, tmp_path):
    """🔴 ② 子代理删**别人**的条目 ⇒ 拒（只回「不是你的记忆」，**不回显正文**）。"""
    keys = _seed(gov, tmp_path)
    _as_caller(gov, SUB)
    with pytest.raises(ToolError) as exc:
        call(gov, "memory_remove", key=keys["别人的"], confirm=True)
    assert "不是你的" in str(exc.value), str(exc.value)
    # 🔴 别人的条目**还在**（没被删掉）
    assert gov.agent_gate.registry.storage.get_fragment(keys["别人的"]) is not None


def test_子代理删共享代理的条目也拒_owner(gov, tmp_path):
    """🔴 「看得见」≠「能删」：开了 `share_memory` 也**不许删**别人的条目。"""
    keys = _seed(gov, tmp_path)
    gov.agent_gate.registry.set_share_memory(OTHER, True)
    _as_caller(gov, SUB)
    with pytest.raises(ToolError) as exc:
        call(gov, "memory_remove", key=keys["别人的"], confirm=True)
    assert "不是你的" in str(exc.value), str(exc.value)
    assert gov.agent_gate.registry.storage.get_fragment(keys["别人的"]) is not None


def test_无归属老条目子代理可删_owner(gov, tmp_path):
    """🔴 ③ **无归属的老条目**（收紧前的公共数据）⇒ 子代理可删（与检索口径一致）。"""
    keys = _seed(gov, tmp_path)
    _as_caller(gov, SUB)
    out = call(gov, "memory_remove", key=keys["老条目"], confirm=True)
    assert out["removed"] is True, out
    assert gov.agent_gate.registry.storage.get_fragment(keys["老条目"]) is None


def test_primary删任意条目通过_owner(gov, tmp_path):
    """🔴 ④ primary 删任意条目 ⇒ 过（= 主脑现状逐字不变）。"""
    keys = _seed(gov, tmp_path)
    out = call(gov, "memory_remove", key=keys["别人的"], confirm=True)
    assert out["removed"] is True, out
    assert gov.agent_gate.registry.storage.get_fragment(keys["别人的"]) is None


def test_子代理调forget被拒_govern(gov, tmp_path):
    """🔴 ⑤ `memory_forget` 是**批处理治理面** ⇒ 收归 primary-only。"""
    _seed(gov, tmp_path)
    _as_caller(gov, SUB)
    with pytest.raises(ToolError) as exc:
        call(gov, "memory_forget", dry_run=True)
    assert "治理动作仅主代理可用" in str(exc.value), str(exc.value)


def test_primary调forget不被新闸拦住_govern(gov, tmp_path):
    """primary 调 forget ⇒ **不被权限闸拦**（= 主脑现状逐字不变）。"""
    _seed(gov, tmp_path)
    out = call(gov, "memory_forget", dry_run=True)
    assert out["dry_run"] is True, out


# ---------------------------------------------------------------------------
# B 组：子代理自荐常驻 = 与主脑同流程
# ---------------------------------------------------------------------------

def test_子代理写core落候选带提名戳且可被nominate枚举(gov, tmp_path):
    """🔴 ⑥ 子代理写 `core` ⇒ 候选 **+ 提名日戳**，可被 nominate 枚举到。

    此前子代理的候选**没有提名日** ⇒ 班车规则二压根不审它（死候选）。
    现在与主脑候选**同一条审核/班车链**（人工批准 / 热度自动过，一视同仁）。
    """
    from keepsake.core import tiering as tiering_mod

    _seed(gov, tmp_path)
    _as_caller(gov, SUB)
    key = call(gov, "memory_add", text="子代理 想记 常驻", tags="core")["key"]

    tags = _stored_tags(gov, key)
    assert "core_candidate" in tags, tags
    assert any(t.startswith("tier:auto-hot:") for t in tags), tags
    # 提名日**可解析**（班车规则二据此判「挂多久了」）
    row = gov.agent_gate.registry.storage.get_fragment(key) or {}
    assert tiering_mod.nominated_on(row) is not None, row
    # 可被 nominate（候选审核队列）枚举到
    queue = gov.agent_gate.registry.storage.list_by_tag(
        tiering_mod.CANDIDATE_TAG) or []
    assert any(str(r.get("key")) == key for r in queue), queue


def test_子代理写core仍进不了常驻本体(gov, tmp_path):
    """🔴 ⑦ 子代理**写不进 core 本体**（写闸其它语义不动）。"""
    _seed(gov, tmp_path)
    _as_caller(gov, SUB)
    key = call(gov, "memory_add", text="子代理 想记 常驻 2", tags="core")["key"]
    tags = _stored_tags(gov, key)
    assert "core" not in tags, tags
    assert "core_candidate" in tags, tags


# ---------------------------------------------------------------------------
# 判据本体（`govern.visibility.can_remove_fragment`）—— 唯一那份口径
# ---------------------------------------------------------------------------

def test_删除判据本体():
    """判据四条一次钉死：primary 全量 / 自己的过 / 他人的拒 / 无归属的过。"""
    from keepsake.govern.visibility import can_remove_fragment

    # primary ⇒ 全量（= 主脑现状逐字不变）
    assert can_remove_fragment("worker-other", "worker-sub", is_primary=True) is True
    assert can_remove_fragment("", "worker-sub", is_primary=True) is True
    # 非 primary ⇒ 自己的过、他人的拒
    assert can_remove_fragment("worker-sub", "worker-sub", is_primary=False) is True
    assert can_remove_fragment("worker-other", "worker-sub", is_primary=False) is False
    # 无归属的老条目 ⇒ 过（收紧前的公共数据，与检索口径一致）
    assert can_remove_fragment("", "worker-sub", is_primary=False) is True
    # 判不出归属（调用方 id 为空）⇒ 非 primary 也不许删别人的
    assert can_remove_fragment("worker-other", "", is_primary=False) is False
    assert can_remove_fragment("", "", is_primary=False) is True

