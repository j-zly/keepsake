"""`kind:pending` → 待办 的迁移脚本测试（2026-10 ks_todo）。

判据：

1. **默认 dry-run**：只算不写（库与条目一个字节都不动）；
2. **映射口径**：标题 = 正文前 60 字、正文 = **原文**（一条不丢）、
   归属 = 条目上的 `agent:<id>`（在册且已批准时）否则主代理、状态一律 open；
3. **原条目不删**，只打 `migrated-to-todo` 标；
4. **幂等**：第二遍 `migrated=0`（判据 = 那个标签）。

存储用**临时 SQLite** —— 不连 Redis / PG / 生产库。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from keepsake.govern.registry import Registry
from keepsake.govern.todo import STATUS_OPEN, TodoStore

#: 脚本不在包内（scripts/ 是独立目录）—— 按路径加载它。
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_pending_to_todo.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("migrate_pending_to_todo", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["migrate_pending_to_todo"] = module
    spec.loader.exec_module(module)
    return module


migrate_mod = _load_script()


@pytest.fixture()
def env(tmp_path):
    """临时 SQLite 存储 + 注册表 + 待办存储。"""
    from keepsake.core.sqlite import SqliteStorage

    storage = SqliteStorage(path=str(tmp_path / "migrate.db"))
    assert storage.ensure_index() is True
    registry = Registry(storage)
    registry.ensure_schema()
    todos = TodoStore(storage)
    assert todos.ensure_schema() is True
    yield storage, registry, todos
    storage.close()


def _seed_pending(storage, text: str, tags: str) -> str:
    """写一条记忆条目（`kind:pending`），返回它的 key。"""
    assert storage.store(text=text, tags=tags) is True
    for row in storage.scan_fragment_keys(limit=500)[1]:
        doc = storage.get_fragment(row)
        if doc and doc.get("content") == text:
            return row
    raise AssertionError("没找到刚写进去的条目")


# --------------------------------------------------------------------------
# ① 默认 dry-run
# --------------------------------------------------------------------------

def test_默认dry_run_只算不写(env):
    """🔴 默认只读不算写：待办一条没建，原条目的标签一个没动。"""
    storage, registry, todos = env
    registry.register("main", "主代理")
    registry.approve("main")
    registry.set_role("main", "primary")
    key = _seed_pending(storage, "把回滚手册补齐", "kind:pending,conversation")

    result = migrate_mod.migrate(storage, registry, todos, write=False)
    assert result == {"scanned": 1, "migrated": 1, "skipped": 0, "dry_run": True}
    assert todos.summary()["total"] == 0, "dry-run 不许建待办"
    assert "migrated-to-todo" not in storage.get_fragment(key)["tags"]


def test_dry显式声明与默认同行为(env):
    storage, registry, todos = env
    registry.register("main", "主代理")
    registry.approve("main")
    registry.set_role("main", "primary")
    _seed_pending(storage, "一条待办", "kind:pending")
    assert migrate_mod.migrate(storage, registry, todos, write=False)["dry_run"] is True


# --------------------------------------------------------------------------
# ② 映射口径
# --------------------------------------------------------------------------

def test_映射口径_标题前60字正文原文状态open(env):
    """🔴 标题 = 正文前 60 字；正文 = **原文**（一条不丢）；状态一律 open。"""
    storage, registry, todos = env
    registry.register("main", "主代理")
    registry.approve("main")
    registry.set_role("main", "primary")
    long_text = "甲" * 100
    _seed_pending(storage, long_text, "kind:pending")

    migrate_mod.migrate(storage, registry, todos, write=True)
    rows, total = todos.list_rows()
    assert total == 1
    row = rows[0]
    assert row["title"] == "甲" * 60
    assert row["detail"] == long_text, "原文必须一条不丢地进 detail"
    assert row["status"] == STATUS_OPEN, "老条目判不了完成没完成 ⇒ 一律 open"


def test_映射口径_归属取agent标签(env):
    """🔴 归属 = 条目上的 `agent:<id>`（该代理**已批准**时）。"""
    storage, registry, todos = env
    registry.register("main", "主代理")
    registry.approve("main")
    registry.set_role("main", "primary")
    registry.register("worker-1", "工人")
    registry.approve("worker-1")
    _seed_pending(storage, "派给工人的活", "kind:pending,agent:worker-1")

    migrate_mod.migrate(storage, registry, todos, write=True)
    rows, _ = todos.list_rows()
    assert rows[0]["owner"] == "worker-1"
    assert rows[0]["creator"] == "worker-1"


def test_映射口径_未批准的agent标签落到主代理(env):
    """🔴 已吊销/未批准的代理名挂上去 = 永远没人认领 ⇒ 落到主代理。"""
    storage, registry, todos = env
    registry.register("main", "主代理")
    registry.approve("main")
    registry.set_role("main", "primary")
    registry.register("worker-pending", "还没批")
    _seed_pending(storage, "派给没批的人的活", "kind:pending,agent:worker-pending")

    migrate_mod.migrate(storage, registry, todos, write=True)
    rows, _ = todos.list_rows()
    assert rows[0]["owner"] == "main"


def test_判不出归属时落到主代理(env):
    storage, registry, todos = env
    registry.register("main", "主代理")
    registry.approve("main")
    registry.set_role("main", "primary")
    _seed_pending(storage, "没标归属的活", "kind:pending")
    migrate_mod.migrate(storage, registry, todos, write=True)
    rows, _ = todos.list_rows()
    assert rows[0]["owner"] == "main"


# --------------------------------------------------------------------------
# ③ 原条目不删 + 幂等
# --------------------------------------------------------------------------

def test_原条目不删只打标(env):
    """🔴 原条目**不删**（删了就查不回来「当初为什么记这条」），只打标。"""
    storage, registry, todos = env
    registry.register("main", "主代理")
    registry.approve("main")
    registry.set_role("main", "primary")
    key = _seed_pending(storage, "一条待办", "kind:pending,conversation")

    migrate_mod.migrate(storage, registry, todos, write=True)
    doc = storage.get_fragment(key)
    assert doc is not None, "原条目不许删"
    assert "kind:pending" in doc["tags"], "原标签不许丢"
    assert "migrated-to-todo" in doc["tags"], "迁移标必须打上"


def test_幂等_第二遍不迁(env):
    """🔴 判据 = `migrated-to-todo` 标签 ⇒ 重复跑第二遍 `migrated=0`。"""
    storage, registry, todos = env
    registry.register("main", "主代理")
    registry.approve("main")
    registry.set_role("main", "primary")
    _seed_pending(storage, "一条待办", "kind:pending")

    first = migrate_mod.migrate(storage, registry, todos, write=True)
    assert first["migrated"] == 1
    second = migrate_mod.migrate(storage, registry, todos, write=True)
    assert second["migrated"] == 0
    assert second["skipped"] == 1
    assert todos.summary()["total"] == 1, "不许迁出第二条待办"


def test_非pending条目不碰(env):
    """🔴 没有 `kind:pending` 标签的条目一律不碰（判据是**逗号边界的完整标签相等**）。"""
    storage, registry, todos = env
    registry.register("main", "主代理")
    registry.approve("main")
    registry.set_role("main", "primary")
    _seed_pending(storage, "普通记忆", "conversation")
    _seed_pending(storage, "像但不是", "kind:pending-extra")

    result = migrate_mod.migrate(storage, registry, todos, write=True)
    assert result["scanned"] == 0
    assert todos.summary()["total"] == 0
