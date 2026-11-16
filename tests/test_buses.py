"""班车业务函数包（``keepsake.buses``）的契约护栏。

四条纪律（包头注）：

* **B1** 每个班车模块都有 ``run()``，且它是可调用对象；
* **B2** 契约：``run() -> (ok, 人读摘要)`` —— 第一个是布尔，第二个是**一句
  人话**（落 ``ks_bus_state.detail``），不是 JSON dump；
* **B3** **模块级零副作用**：import 不许连库 / 发网络 / 起子进程
  （把 socket / subprocess 打成炸弹，import 后不许炸）；
* **B4** 依赖走 keepsake 既有 core（不自己搓 httpx / psycopg 直连）。

四条班车各钉一条 import+契约 smoke —— 班车的静默失败模式不是「报错」，
而是「到点一声不吭」（模块 import 失败、run 抛异常、返回形状不对，
页面上一律只显示「上次失败」或干脆没记录）。
"""

from __future__ import annotations

import importlib
import socket
import subprocess
from typing import Any, Dict

import pytest

BUS_MODULES = ["distill", "maintenance", "synonyms", "tiering"]


def _import(name: str):
    return importlib.import_module(f"keepsake.buses.{name}")


# ---------------------------------------------------------------- B1 / B2

@pytest.mark.parametrize("name", BUS_MODULES)
def test_每个班车模块都有run且可调用(name):
    mod = _import(name)
    assert hasattr(mod, "run"), f"{name} 缺 run()（班车唯一入口）"
    assert callable(mod.run)


@pytest.mark.parametrize("name", BUS_MODULES)
def test_run契约返回布尔与人话摘要(name, monkeypatch):
    """run() → (bool, str)：第一个是布尔，第二个是**一句话**（不是 JSON / None）。"""
    ok, summary = _run_one(name, monkeypatch)
    assert isinstance(ok, bool), f"{name}: ok 必须是布尔（得到 {type(ok)}）"
    assert isinstance(summary, str) and summary.strip(), f"{name}: 摘要不能是空串"
    assert not summary.strip().startswith("{"), f"{name}: 摘要要是人话，不是 JSON dump"


def _run_one(name: str, monkeypatch) -> tuple:
    """跑一条班车的 run()（**全程零外部动作**：存储 / LLM / 引擎都被替身接管）。"""
    from keepsake.core import factory

    if name == "distill":
        # 通道未配置 ⇒ 本轮跳过（不调 LLM、不连库）—— 与生产「没配通道」同一条路。
        from keepsake.core.ops import consolidator
        monkeypatch.setattr(consolidator, "resolve_llm_channel_cached",
                            lambda *a, **k: {"valid": False})
        return _import(name).run()

    if name == "tiering":
        # 分级默认关死 ⇒ 跳过（连存储都不构造）。
        from keepsake.core import tiering
        monkeypatch.setattr(tiering, "resolve_tiering",
                            lambda *a, **k: {"enabled": False})
        return _import(name).run()

    if name == "synonyms":
        monkeypatch.setattr(factory, "load_config", lambda *a, **k: {})
        monkeypatch.setattr(factory, "storage_from_config", lambda *a, **k: _FakeStore())
        return _import(name).run()

    # maintenance：预演（dry-run）且两个引擎都替身化 ⇒ 零写、零外部动作。
    from keepsake.core.ops import consolidator as cons_mod
    from keepsake.core.ops import forgetter as forg_mod
    monkeypatch.setattr(factory, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(factory, "storage_from_config", lambda *a, **k: _FakeStore())
    monkeypatch.setattr(cons_mod, "Consolidator", lambda *a, **k: _FakeConsolidator())
    monkeypatch.setattr(forg_mod, "Forgetter", lambda *a, **k: _FakeForgetter())
    return _import(name).run(dry_run=True)


class _FakeStore:
    """假存储（synonyms 班车用）：只回答那两个原语。"""

    def discover_synonyms(self, rebuild: bool = False) -> Dict[str, Any]:
        return {"discovered_groups": 3, "total_terms": 12,
                "scanned_fragments": 40, "rebuild": rebuild, "degraded": []}

    def generate_jieba_dict(self, output_path=None) -> Dict[str, Any]:
        return {"written_terms": 25, "total_candidates": 40,
                "output_path": "/tmp/jieba_dict.txt", "degraded": []}

    def close(self) -> None:
        pass


class _FakeConsolidator:
    def consolidate(self, dry_run: bool = False) -> Dict[str, Any]:
        return {"scanned": 10, "groups_found": 1, "merged": 0, "skipped": 0,
                "errors": 0, "dry_run": dry_run, "would_merge": 2}


class _BoomConsolidator:
    """故意炸的合并引擎（测红报口径）。"""

    def consolidate(self, dry_run: bool = False) -> Dict[str, Any]:
        raise RuntimeError("合并引擎炸了（测试剧本）")


class _FakeForgetter:
    def forget(self, force: bool = False) -> Dict[str, Any]:
        return {"scanned": 10, "candidates": 4, "deleted": 0,
                "skipped_protected": 0, "dry_run": not force}


# ---------------------------------------------------------------- B3 模块级零副作用

@pytest.mark.parametrize("name", BUS_MODULES)
def test_模块级零副作用_import不发网络不起子进程(name, monkeypatch):
    """🔴 import 本模块**不许**连库 / 发网络 / 起子进程。

    做法：把 socket / subprocess 打成炸弹（碰一下就炸），再**重新 import**
    一遍 —— 模块级代码要是偷偷干了什么，这里当场红。
    """
    def _boom(*a, **k):
        raise AssertionError(f"模块级副作用：import {name} 时碰了网络 / 子进程")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)

    mod = importlib.reload(_import(name))
    assert callable(mod.run)


# ---------------------------------------------------------------- 种子 ↔ 包 的接线

def test_四条种子func都指向可import且可调用的run():
    """BUS_JOBS 的 func 引用必须真的能 import 且 callable（否则到点一声不吭）。"""
    from keepsake.core.bus import BUS_JOBS

    for j in BUS_JOBS:
        mod_name, _, attr = j["func"].partition(":")
        assert mod_name.startswith("keepsake.buses."), j
        fn = getattr(importlib.import_module(mod_name), attr)
        assert callable(fn), j["func"]


# ---------------------------------------------------------------- 维护班车：口径平移

def test_maintenance_预演摘要同壳脚本口径(monkeypatch):
    """预演摘要沿用原壳脚本的 watchdog 措辞：「合并 N 条碎片，遗忘 M 条碎片 (dry-run)」。"""
    from keepsake.core import factory
    from keepsake.core.ops import consolidator as cons_mod
    from keepsake.core.ops import forgetter as forg_mod

    monkeypatch.setattr(factory, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(factory, "storage_from_config", lambda *a, **k: _FakeStore())
    monkeypatch.setattr(cons_mod, "Consolidator", lambda *a, **k: _FakeConsolidator())
    monkeypatch.setattr(forg_mod, "Forgetter", lambda *a, **k: _FakeForgetter())

    ok, summary = _import("maintenance").run(dry_run=True)
    assert ok is True
    assert "合并 2 条碎片" in summary, summary
    assert "遗忘 4 条碎片" in summary, summary
    assert "(dry-run)" in summary, summary


def test_maintenance_出错时红报口径同壳脚本(monkeypatch):
    """🔴 出错 ⇒ 抛出的消息以 ``[memory-maintenance] error:`` 开头（壳脚本同款），
    由调度层捕获落 ok=False。"""
    from keepsake.core import factory
    from keepsake.core.ops import consolidator as cons_mod
    from keepsake.core.ops import forgetter as forg_mod

    monkeypatch.setattr(factory, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(factory, "storage_from_config", lambda *a, **k: _FakeStore())
    monkeypatch.setattr(cons_mod, "Consolidator", lambda *a, **k: _BoomConsolidator())
    monkeypatch.setattr(forg_mod, "Forgetter", lambda *a, **k: _FakeForgetter())

    with pytest.raises(RuntimeError) as ei:
        _import("maintenance").run(dry_run=True)
    assert str(ei.value).startswith("[memory-maintenance] error:"), str(ei.value)
