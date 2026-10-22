"""MCP 远端写入的**归属章**（2026-10 ks_owner_stamp）。

缺陷：外部代理经 MCP `memory_add` 写的记忆，归属打的是 **provider 进程默认身份**
（main-brain），不是**凭据反查出的调用方** ⇒ 它自己 get/search 全不中（「写了 key
查不到」），写的记忆还混进主脑全库。

覆盖四件事（任务书判据）：

  ① 远端调用方写 ⇒ 落库 `agent:<调用方>`（不含 `agent:<进程默认>`），且**它自己**
     search / get 命中自己的条目；
  ② 自报 `agent:main-brain` ⇒ 被覆写回真实身份（**不许自报归属**）；
  ③ 本机路径（caller 空）⇒ 归属仍落**进程默认**（回归钉）；
  ④ `core_direct` 语义回归（非 primary 写 core 仍落候选；primary 仍直写）。

全程 hermetic：临时 SQLite + 临时审计/备份目录，不连 Redis / PG / 网络 / 生产库。
"""

from __future__ import annotations

import functools
import json
from typing import Any, Dict, List

import anyio
import pytest

from keepsake.mcp_server.factory import default_provider
from keepsake.mcp_server.server import build_server

#: 进程默认身份（生产 config 顶层 `agent_id` 就是它）。
PROCESS_AGENT = "main-brain"
#: 远端代理（凭据反查出来的调用方）。
REMOTE_AGENT = "zly-hermes"

#: 一条能被中文分词检索命中的探针文本（与既有 MCP 用例同款写法）。
PROBE = "外部代理 写入 归属 探针"


# ---------------------------------------------------------------------------
# 夹具：临时 sqlite + keepsake 默认装配（agent_id = 进程默认身份）
# ---------------------------------------------------------------------------

@pytest.fixture()
def provider(tmp_path, monkeypatch):
    """按临时配置装配 provider（**进程默认身份 = main-brain**）。

    归属章的判据需要「进程默认身份」在场：修复前远端代理写的记忆被打成
    `agent:main-brain`，修复后打成 `agent:<调用方>` —— 没有这个默认身份就
    看不出差别。
    """
    cfg = {
        "agent_id": PROCESS_AGENT,
        "storage": {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "owner.db")}},
        "is_primary": True,
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.delenv("KEEPSAKE_AGENT_ID", raising=False)
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL", raising=False)
    return default_provider()


@pytest.fixture()
def gov(provider):
    """挂了闸门的 MCP server（治理面开着 —— 远端代理要在注册表里有一行）。"""
    from keepsake.adapters.hermes import provider as prov_mod

    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})      # noqa: SLF001
    server = build_server(provider, govern=True)
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


def _stored_tags(gov, key: str) -> List[str]:
    """落库后的标签（`memory_add` 返回的是**请求里**的 tags —— 拿它验等于没验）。"""
    row = gov.agent_gate.registry.storage.get_fragment(key) or {}
    return [t.strip() for t in str(row.get("tags") or "").split(",") if t.strip()]


# ---------------------------------------------------------------------------
# ① 远端调用方写 ⇒ 归属按凭据反查的调用方打，且它自己搜得到
# ---------------------------------------------------------------------------

def test_远端代理写的记忆归它自己(gov):
    gov.agent_gate.registry.bootstrap_local(REMOTE_AGENT, "外部代理")
    _as_caller(gov, REMOTE_AGENT)

    key = call(gov, "memory_add", text=PROBE, tags="lesson")["key"]
    tags = _stored_tags(gov, key)

    assert f"agent:{REMOTE_AGENT}" in tags, tags
    assert f"agent:{PROCESS_AGENT}" not in tags, tags


def test_远端代理自己搜得到取得到(gov):
    """🔴 修复前这条全红：归属打成 main-brain ⇒ 可见性收紧后它自己「查不到」。"""
    gov.agent_gate.registry.bootstrap_local(REMOTE_AGENT, "外部代理")
    _as_caller(gov, REMOTE_AGENT)

    key = call(gov, "memory_add", text=PROBE, tags="lesson")["key"]

    hits = call(gov, "memory_search", query=PROBE)["results"]
    assert any(h["key"] == key for h in hits), hits
    assert call(gov, "memory_get", key=key)["key"] == key


# ---------------------------------------------------------------------------
# ② 自报归属 ⇒ 覆写回真实身份（不许自报归属）
# ---------------------------------------------------------------------------

def test_自报归属被覆写回真实身份(gov):
    gov.agent_gate.registry.bootstrap_local(REMOTE_AGENT, "外部代理")
    _as_caller(gov, REMOTE_AGENT)

    key = call(gov, "memory_add", text=PROBE, tags=f"agent:{PROCESS_AGENT},lesson")["key"]
    tags = _stored_tags(gov, key)

    assert f"agent:{REMOTE_AGENT}" in tags, tags
    assert f"agent:{PROCESS_AGENT}" not in tags, tags


# ---------------------------------------------------------------------------
# ③ 本机路径（caller 空）⇒ 归属仍落进程默认（回归钉）
# ---------------------------------------------------------------------------

def test_本机直写仍落进程默认归属(provider):
    """provider 直写（没有调用方身份）⇒ 归属章仍是**进程默认身份**。"""
    key = provider.add(PROBE, tags="lesson")
    row = provider._require_storage().get_fragment(key) or {}    # noqa: SLF001
    tags = [t.strip() for t in str(row.get("tags") or "").split(",") if t.strip()]

    assert f"agent:{PROCESS_AGENT}" in tags, tags


def test_存储层默认落进程默认归属(gov):
    """存储层直写（不传 owner）⇒ 同上（逐字不变的老路径）。"""
    from keepsake.core.shared import fragment_key_for

    storage = gov.agent_gate.registry.storage
    assert storage.store(text=PROBE, tags="lesson") is True
    row = storage.get_fragment(fragment_key_for(PROBE)) or {}
    tags = [t.strip() for t in str(row.get("tags") or "").split(",") if t.strip()]

    assert f"agent:{PROCESS_AGENT}" in tags, tags


# ---------------------------------------------------------------------------
# ④ core_direct 语义回归（归属章改动不许动写闸）
# ---------------------------------------------------------------------------

def test_非primary写core仍落候选(gov):
    gov.agent_gate.registry.bootstrap_local(REMOTE_AGENT, "外部代理")
    _as_caller(gov, REMOTE_AGENT)

    key = call(gov, "memory_add", text="子代理 想记 常驻", tags="core")["key"]
    tags = _stored_tags(gov, key)

    assert "core_candidate" in tags, tags
    assert "core" not in tags, tags
    assert f"agent:{REMOTE_AGENT}" in tags, tags


def test_primary写core仍直写(gov):
    key = call(gov, "memory_add", text="主脑 要记 常驻", tags="core")["key"]
    tags = _stored_tags(gov, key)

    assert "core" in tags, tags
    assert "core_candidate" not in tags, tags
    assert f"agent:{PROCESS_AGENT}" in tags, tags
