#!/usr/bin/env python3
"""把 `kind:pending` 记忆条目迁成**待办**（2026-10 ks_todo 一等公民）。

## 为什么
   待办此前一直用 `kind:pending` 记忆条目凑合：盘点 12 条里 9 条烂账
   （做完没销、过期没人知）—— 待办是**状态机**（开放 / 已完成 / 已废弃），
   不是一条「记着点」的碎片。本脚本把老条目搬进 `ks_todo`。

## 映射口径
   * **标题** = 正文**前 60 字**；
   * **正文（detail）** = **原文**（一条不删 —— 判不出结构的条目也不能丢内容）；
   * **归属（owner）** = 条目上的 `agent:<id>` 标签（该代理**在册且已批准**时）；
     判不出 / 不合格 ⇒ 落到**主代理**；再判不出 ⇒ 空归属（无人认领）；
   * **状态** = 一律 `open`（老条目没有状态机，判不了完成没完成 —— 宁可开着，
     也不替人把没干的活标成干完了）；
   * **到期日** = 空（老条目没有到期日）。

## 原条目怎么处理
   打上 `migrated-to-todo` 标签，**不删**。删了就查不回来「当初为什么记这条」。

## 幂等
   判据 = 条目上有没有 `migrated-to-todo` 标签。重复跑第二遍 `migrated=0`。

## 默认 dry-run
   **默认只读不算写**（`--write` 才真写）。dry-run 也读库（否则算不出该迁几条）。

## 用法
   python3 scripts/migrate_pending_to_todo.py                # dry-run
   python3 scripts/migrate_pending_to_todo.py --write        # 真迁
   python3 scripts/migrate_pending_to_todo.py --dry          # 显式声明 dry-run
   python3 scripts/migrate_pending_to_todo.py --config /path/config.json

输出（stdout 唯一一行 JSON）：
   {"scanned": N, "migrated": M, "skipped": K, "dry_run": true}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from keepsake.govern.registry import ROLE_PRIMARY, STATUS_APPROVED  # noqa: E402
from keepsake.govern.todo import TodoStore  # noqa: E402
from keepsake.govern.visibility import fragment_owner  # noqa: E402
from keepsake.storage import storage_from_config  # noqa: E402

#: 老待办的标签（迁移判据）
PENDING_TAG = "kind:pending"

#: 迁移完给原条目打的标（**幂等判据**）
MIGRATED_TAG = "migrated-to-todo"

#: 标题取正文前 N 字（正文全文进 detail —— 一条不丢）
TITLE_CHARS = 60

#: 扫描分页大小
SCAN_LIMIT = 500


def _tags_of(raw: Any) -> List[str]:
    """tags 字段 → 标签列表（逗号分隔，兼容 `|` 分隔的老写法）。"""
    text = str(raw or "").replace("|", ",")
    return [t.strip() for t in text.split(",") if t.strip()]


def _has_tag(raw: Any, tag: str) -> bool:
    """逗号边界上的**完整标签相等**（`kind:pending-x` 不算 `kind:pending`）。"""
    return tag in _tags_of(raw)


def _primary_agent(registry: Any) -> str:
    """主代理 id（注册表里 role=primary 的那位；查不到 ⇒ 空串）。"""
    for row in registry.list_agents() or []:
        if row.get("role") == ROLE_PRIMARY:
            return str(row.get("agent_id") or "")
    return ""


def _owner_of(registry: Any, tags: Any) -> str:
    """归属代理：条目上的 `agent:<id>`（**在册且已批准**时）⇒ 否则主代理 ⇒ 否则空。

    🔴 判「已批准」是有意义的：待办的 owner 必须是能真接活的代理 ——
    挂一个已吊销的代理名���，那条待办永远没人认领。
    """
    agent = fragment_owner(tags)
    if agent:
        row = registry.get(agent)
        if row and row.get("status") == STATUS_APPROVED:
            return agent
    return _primary_agent(registry)


def _scan_pending(storage: Any) -> List[Dict[str, Any]]:
    """扫出所有 `kind:pending` 条目（`[{key, content, tags}]`，**含已迁过的**）。

    🔴 扫描走 `scan_fragment_keys` + `get_fragments_batch` 两个**通用原语**
    （与合并 / 遗忘同一条读路径）⇒ Redis / SQLite / PG 三后端同款。

    已迁过的**也扫出来**（`migrated=True` 标上）：`scanned` 要报「库里有几条
    kind:pending」，`skipped` 要报「其中几条已经迁过了」—— 两数分开才看得出
    「这遍真迁了几条」。
    """
    out: List[Dict[str, Any]] = []
    cursor = ""
    while True:
        cursor, keys = storage.scan_fragment_keys(cursor=cursor, limit=SCAN_LIMIT)
        if keys:
            docs = storage.get_fragments_batch(keys)
            for key in keys:
                doc = docs.get(key) or {}
                tags = doc.get("tags", "")
                if not _has_tag(tags, PENDING_TAG):
                    continue
                out.append({"key": key, "content": str(doc.get("content") or ""),
                            "tags": str(tags or ""),
                            "migrated": _has_tag(tags, MIGRATED_TAG)})
        if not cursor or not keys:
            break
    return out


def migrate(storage: Any, registry: Any, todos: TodoStore, *,
            write: bool = False) -> Dict[str, Any]:
    """把 `kind:pending` 条目迁成待办。

    返回 `{scanned, migrated, skipped, dry_run}`：
    * `scanned` —— 扫到的 `kind:pending` 条目数（含已迁过的）；
    * `migrated` —— 这次**新迁**的条数（dry-run 里是「本该迁」的条数）；
    * `skipped` —— 已迁过 / 迁不了的条数。
    """
    todos.ensure_schema()
    rows = _scan_pending(storage)
    scanned = len(rows)
    migrated = 0
    skipped = 0
    for row in rows:
        tags = row["tags"]
        if row.get("migrated") or _has_tag(tags, MIGRATED_TAG):
            skipped += 1
            continue
        content = row["content"]
        title = content[:TITLE_CHARS].strip() or "（无标题的待办）"
        owner = _owner_of(registry, tags)
        if write:
            todos.add(title, content, owner, owner, "")
            new_tags = ",".join(_tags_of(tags) + [MIGRATED_TAG])
            storage.update_fragment_fields(row["key"], {"tags": new_tags})
        migrated += 1
    return {"scanned": scanned, "migrated": migrated, "skipped": skipped,
            "dry_run": not write}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="把 kind:pending 记忆条目迁成待办（默认 dry-run）")
    ap.add_argument("--write", action="store_true", help="真写库（**默认只 dry-run**）")
    ap.add_argument("--dry", action="store_true",
                    help="显式声明 dry-run（默认行为，写它只是把意图写明）")
    ap.add_argument("--config", default=None, help="keepsake 配置 json 路径")
    args = ap.parse_args(argv)

    storage = storage_from_config(args.config)
    try:
        if not storage.ensure_index():
            print("[error] 存储后端未就绪（ensure_index 失败），不迁移", file=sys.stderr)
            return 2
        from keepsake.govern.registry import Registry

        registry = Registry(storage)
        registry.ensure_schema()
        todos = TodoStore(storage)
        result = migrate(storage, registry, todos, write=bool(args.write))
    finally:
        close = getattr(storage, "close", None)
        if callable(close):
            close()

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
