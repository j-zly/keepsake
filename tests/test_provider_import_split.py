"""provider.py 宿主符号导入拆分（2026-11 provider_base_import_split）。

钉死三件事（每条都是「一行炸不许连坐另一行」的回归护栏）：

  a) `agent.memory_provider` 不可导入 ⇒ **只**降级基类，`tools.registry`
     仍可用 ⇒ tool_error 仍是官方那份（**不连坐**）；
  b) `tools.registry` 不可导入 ⇒ 基类**仍是官方版**（**互不牵连**，
     这是修复前被打破的那条：tools.registry 一炸 → 官方基类被打成假桩 →
     Hermes 装载器 `issubclass` 判 False → provider 装不上）；
  c) 任一降级路径 ⇒ caplog 有 **error** 记录（静默即红：原先整个 except
     无日志，坏了一整天没人看见）。

构造方式：monkeypatch `sys.modules`（把目标模块置 None ⇒ import 抛
ImportError），**不依赖真实缺件环境**。
"""

from __future__ import annotations

import logging
import sys
import types

from keepsake.adapters.hermes import provider


def _install_agent_stub(monkeypatch):
    """装一个「官方版」agent.memory_provider（哨兵类 ⇒ 可按身份核对）。"""
    mod = types.ModuleType("agent.memory_provider")

    class OfficialMemoryProvider:
        def __init__(self, *args, **kwargs):
            pass

    mod.MemoryProvider = OfficialMemoryProvider
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.memory_provider", mod)
    return OfficialMemoryProvider


def _install_tools_stub(monkeypatch):
    """装一个「官方版」tools.registry（哨兵 tool_error ⇒ 可按身份核对）。"""
    mod = types.ModuleType("tools.registry")

    def official_tool_error(msg):
        return f"[official_tool_error] {msg}"

    mod.tool_error = official_tool_error
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.registry", mod)
    return official_tool_error


def _break_agent(monkeypatch):
    """让 `from agent.memory_provider import ...` 抛 ImportError。"""
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.memory_provider", None)


def _break_tools(monkeypatch):
    """让 `from tools.registry import ...` 抛 ImportError。"""
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.registry", None)


def test_agent_missing_does_not_take_tools_down(monkeypatch):
    """a) agent 挂 ⇒ 只降级基类；tool_error 仍来自官方（不连坐）。"""
    official_tool_error = _install_tools_stub(monkeypatch)
    _break_agent(monkeypatch)

    memory_provider, tool_error = provider._import_host_symbols()

    assert tool_error is official_tool_error, "tools 不该被 agent 的失败连坐"
    # 基类确实降了桩（且是 provider 模块内的本地桩，不是官方类）
    assert memory_provider.__module__ == provider.__name__


def test_tools_missing_does_not_take_agent_down(monkeypatch):
    """b) tools 挂 ⇒ **基类仍是官方版**（修复前被连坐打断的那条）。"""
    official_memory_provider = _install_agent_stub(monkeypatch)
    _break_tools(monkeypatch)

    memory_provider, tool_error = provider._import_host_symbols()

    assert memory_provider is official_memory_provider, \
        "tools.registry 的失败不许连坐官方基类（Hermes 装载器要认这个基类）"
    # tool_error 降级成本地等价 stub：同签名、同返回形状
    assert tool_error("boom") == "[tool_error] boom"


def test_fallback_is_never_silent(caplog, monkeypatch):
    """c) 降级路径必须吭声（error 级）；静默即红。"""
    _break_agent(monkeypatch)
    _break_tools(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="keepsake.adapter.hermes"):
        provider._import_host_symbols()

    errors = [r for r in caplog.records
              if r.levelno >= logging.ERROR
              and "keepsake adapter" in r.getMessage()]
    assert errors, "降级路径没有 error 日志（静默 = 坏了没人看见）"
    messages = [r.getMessage() for r in errors]
    assert any("agent.memory_provider" in m for m in messages), messages
    assert any("tools.registry" in m for m in messages), messages
    assert any("不可用" in m and "已走本地降级" in m for m in messages), messages


def test_normal_env_is_silent(caplog, monkeypatch):
    """正常环境（两处都可用）⇒ 零降级、零 error 输出。"""
    _install_agent_stub(monkeypatch)
    _install_tools_stub(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="keepsake.adapter.hermes"):
        memory_provider, tool_error = provider._import_host_symbols()

    assert memory_provider.__name__ == "OfficialMemoryProvider"
    assert tool_error("x") == "[official_tool_error] x"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], \
        "正常环境不该有 error 日志"
