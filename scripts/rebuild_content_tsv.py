#!/usr/bin/env python3
"""Keepsake 存量 `content_tsv` 重建 — 把老行的检索索引重算成新口径（英文词干化）

## 为什么需要这个脚本（2026-10 ks_en_stem）

英文检索改成 Porter2 词干化之后，**索引侧**（`content_tsv`）与**查询侧**
（`query_lexemes`）必须逐字同形（对称铁律）。新写入的碎片走的就是新口径
（`PgStorage.tsv_expr_params` / `SqliteStorage._fts_tokens`），但**存量行**的
`content_tsv` 还是老口径（未词干化）⇒ 两边对不齐 ⇒ 那批数据在重建前
只能靠查询侧保留的「原形」命中，变形词（running / runs）召不回来。

⇒ 这个脚本按**与写路径同一个函数**重算全库 `content_tsv`（口径单点，不另写一份）。

## 用法

```bash
# 0) 先干跑（只报告，不写库）—— 任何时候都可以先跑这个
python3 scripts/rebuild_content_tsv.py --backend pg --dsn "$KEEPSAKE_PG_DSN" --dry

# 1) 真写（PG）：--apply 才会落库
python3 scripts/rebuild_content_tsv.py --backend pg --dsn "$KEEPSAKE_PG_DSN" --apply

# 2) SQLite 后端
python3 scripts/rebuild_content_tsv.py --backend sqlite --sqlite-path /path/keepsake.db --apply
```

🔴 **默认不写库**：`--dry` 是显式开关，不给 `--apply` 就只报告。
🔴 **不许连生产库做迁移**（除非主脑明确下令）：本脚本**只交付 + 自测在测试库**。
   生产重建由主脑收口时亲跑（见任务书的收口说明）。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Tuple

# 让脚本能从仓库源码目录 import keepsake（部署机上源码在 /opt/fragmented-memory/src）
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _pg_rebuild(dsn: str, dry: bool, limit: int, batch: int) -> Tuple[int, int]:
    """PG：重算 `ks_fragment.content_tsv`。返回 `(需重算条数, 已重算条数)`。"""
    from keepsake.core.pg import TSV_SETWEIGHT_SQL, PgStorage, tsv_expr_params

    store = PgStorage(dsn=dsn, is_primary=True)
    need = 0
    done = 0
    try:
        with store._ro() as cur:                      # noqa: SLF001 — 脚本内部读
            cur.execute(
                "SELECT f.key, f.content, COALESCE(f.entities,''), COALESCE(f.tags,'') "
                "FROM ks_fragment f ORDER BY f.key"
            )
            rows = cur.fetchall()
        if limit > 0:
            rows = rows[:limit]
        # 新口径的词元串（与写路径同一个函数算出来）
        new_tokens: List[Tuple] = []
        for key, content, entities, tags in rows:
            new_tokens.append((key, *tsv_expr_params(content or "", entities or "", tags or "")))

        # 🔴 「哪些行真的变了」由 **PG 自己**判：`to_tsvector('simple', …)` 与
        #    写路径那句 `TSV_SETWEIGHT_SQL` 逐字相同 ⇒ 直接比 tsvector。
        #    为什么不在 Python 侧比词元集合：PG 的分词器与我们的 `\w+` 口径在
        #    **下划线**上不一致（"trade_api" → PG 给 trade+api，我们给 trade_api）
        #    ⇒ Python 侧比会永远判「变了」，重跑不幂等。
        with store._tx() as cur:                      # noqa: SLF001 — 脚本内部写
            cur.execute("CREATE TEMP TABLE IF NOT EXISTS ks_rebuild_new ("
                        "key text PRIMARY KEY, tok_a text, tok_b text) ON COMMIT DROP")
            cur.execute("TRUNCATE ks_rebuild_new")
            for i in range(0, len(new_tokens), max(1, batch)):
                cur.executemany("INSERT INTO ks_rebuild_new (key, tok_a, tok_b) "
                                "VALUES (%s, %s, %s) ON CONFLICT (key) DO NOTHING",
                                new_tokens[i:i + max(1, batch)])
            cur.execute(
                "SELECT n.key FROM ks_rebuild_new n JOIN ks_fragment f ON f.key = n.key "
                "WHERE (setweight(to_tsvector('simple', n.tok_a), 'A') "
                "       || setweight(to_tsvector('simple', n.tok_b), 'B')) "
                "       IS DISTINCT FROM f.content_tsv ORDER BY n.key"
            )
            changed = [r[0] for r in cur.fetchall()]
        need = len(changed)
        changed_set = set(changed)
        # UPDATE 的参数顺序 = **词元串在前、key 在后**（SQL 里 `TSV_SETWEIGHT_SQL`
        # 的两个占位符在 `WHERE key = %s` 之前）—— 顺序写反会把 key 当正文写进索引。
        updates = [(tok_a, tok_b, key) for key, tok_a, tok_b in new_tokens
                   if key in changed_set]
        print(f"[pg] 扫描 {len(rows)} 行，需要重算 {need} 行")
        if dry:
            print("[pg] --dry：未写库")
            return need, done
        with store._tx() as cur:                      # noqa: SLF001 — 脚本内部写
            for i in range(0, len(updates), max(1, batch)):
                chunk = updates[i:i + max(1, batch)]
                cur.executemany(
                    f"UPDATE ks_fragment SET content_tsv = {TSV_SETWEIGHT_SQL} WHERE key = %s",
                    chunk,
                )
                done += len(chunk)
        print(f"[pg] 已重算 {done} 行")
        return need, done
    finally:
        store.close()


def _sqlite_rebuild(path: str, dry: bool, limit: int) -> Tuple[int, int]:
    """SQLite：重算 `content_tsv` **并同步 FTS5 虚表**（本后端有两份索引）。"""
    from keepsake.core.sqlite import SqliteStorage

    store = SqliteStorage(path=path, is_primary=True)
    need = 0
    done = 0
    try:
        with store._lock:
            cur = store._db().cursor()
            cur.execute(
                "SELECT key, content, COALESCE(entities,''), COALESCE(tags,''), "
                "       COALESCE(content_tsv,'') FROM ks_fragment ORDER BY key"
            )
            rows = cur.fetchall()
        if limit > 0:
            rows = rows[:limit]
        updates = []
        for key, content, entities, tags, old_tsv in rows:
            new_tok = store._fts_tokens(content or "", entities or "", tags or "")  # noqa: SLF001
            if (old_tsv or "") == new_tok:
                continue
            need += 1
            updates.append((key, content or "", entities or "", tags or "", new_tok))
        print(f"[sqlite] 扫描 {len(rows)} 行，需要重算 {need} 行")
        if dry:
            print("[sqlite] --dry：未写库")
            return need, done
        with store._write() as cur:                          # noqa: SLF001 — 脚本内部写
            for key, _c, _e, _t, tok in updates:
                cur.execute(
                    "UPDATE ks_fragment SET content_tsv = ? WHERE key = ?", (tok, key))
                store._fts_write(cur, key, tok)              # noqa: SLF001
                done += 1
        print(f"[sqlite] 已重算 {done} 行（含 FTS5 虚表同步）")
        return need, done
    finally:
        store.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="重算存量 content_tsv（英文词干化口径）")
    ap.add_argument("--backend", choices=("pg", "sqlite"), default="pg")
    ap.add_argument("--dsn", default=os.environ.get("KEEPSAKE_PG_DSN", ""))
    ap.add_argument("--sqlite-path", default=os.environ.get("KEEPSAKE_SQLITE_PATH", ""))
    ap.add_argument("--dry", action="store_true", help="只报告不写库（默认行为）")
    ap.add_argument("--apply", action="store_true", help="真的写库")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少行（0 = 全部）")
    ap.add_argument("--batch", type=int, default=200, help="PG 每批多少行")
    args = ap.parse_args()

    dry = not args.apply
    if args.backend == "pg":
        if not args.dsn:
            print("缺 DSN：用 --dsn 或环境变量 KEEPSAKE_PG_DSN", file=sys.stderr)
            return 2
        need, done = _pg_rebuild(args.dsn, dry, args.limit, args.batch)
    else:
        if not args.sqlite_path:
            print("缺库路径：用 --sqlite-path 或环境变量 KEEPSAKE_SQLITE_PATH",
                  file=sys.stderr)
            return 2
        need, done = _sqlite_rebuild(args.sqlite_path, dry, args.limit)

    print(f"完成：需重算 {need} 行，实际重算 {done} 行"
          f"{'（dry-run，未写库）' if dry else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
