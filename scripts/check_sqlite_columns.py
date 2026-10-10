#!/usr/bin/env python3
"""列对齐检查 —— SQLite 后端 vs PostgreSQL 后端（ks_fragment）。

用法: python3 scripts/check_sqlite_columns.py [--verbose]

**零连接、零外部服务**：在临时目录里建一个空 SQLite 库（走 `ensure_index()` 的
真实建表路径），再与 PG 侧的列真相（`storage_pg` 里的常量）逐列比对。

基准列集（PG 版，同一份常量，不在本脚本里复制）：
  * `FRAGMENT_COLUMNS`   —— 碎片字段（检索结果形状）
  * `MAINTENANCE_COLUMNS`—— 合并/遗忘维护列（level / consumed_by / consumed_at）
  * `embed_bin` / `SEARCH_COLUMNS` —— 二进制向量列与检索列

退出码：0 = PG 基准列全覆盖（允许 SQLite 多出本后端自有列）；1 = 有缺失。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from keepsake.storage_pg import (  # noqa: E402
    FRAGMENT_COLUMNS,
    MAINTENANCE_COLUMNS,
    SEARCH_COLUMNS,
)
from keepsake.storage_sqlite import ALL_COLUMNS, SqliteStorage  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="逐列打印")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "colcheck.db")
        store = SqliteStorage(path=path)
        if not store.ensure_index():
            print("FAIL: 空库 ensure_index() 返回 False")
            return 1
        conn = sqlite3.connect(path)
        sqlite_cols = {r[1] for r in conn.execute("PRAGMA table_info(ks_fragment)")}
        conn.close()
        store.close()

    pg_cols = set(FRAGMENT_COLUMNS) | set(MAINTENANCE_COLUMNS) | set(SEARCH_COLUMNS) | {
        "embed_bin"
    }
    missing = sorted(pg_cols - sqlite_cols)
    extra = sorted(sqlite_cols - pg_cols)

    print(f"PG 基准列数   : {len(pg_cols)}")
    print(f"SQLite 实际列数: {len(sqlite_cols)}")
    print(f"SQLite 缺 PG 列: {missing or '（无）'}")
    print(f"SQLite 多出列  : {extra or '（无）'}")
    if args.verbose:
        for c in sorted(sqlite_cols):
            mark = "PG" if c in pg_cols else "本后端附加"
            print(f"  {c:<20} {mark}")
    # 声明列集 == 实际建出来的列集（声明与建表不同源就会在这里露馅）
    if set(ALL_COLUMNS) != sqlite_cols:
        print("FAIL: ALL_COLUMNS 声明与实际建表不一致：",
              sorted(set(ALL_COLUMNS) ^ sqlite_cols))
        return 1
    if missing:
        print("FAIL: 未覆盖 PG 基准列")
        return 1
    print("OK: PG 基准列全覆盖")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
