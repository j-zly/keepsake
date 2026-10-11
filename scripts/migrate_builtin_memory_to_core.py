#!/usr/bin/env python3
"""把 Hermes 的常驻记忆（MEMORY.md / USER.md）**原文**迁移成 core 碎片。

## 为什么
   常驻记忆原先由 Hermes 本体逐轮全量注入（约 8 万字符），keepsake 只做按查询
   召回的检索层 —— 两套记忆并存，同一件事记两遍、还可能不一致。
   本脚本把常驻记忆收进碎片库并打上 `core` 标签，此后由
   `KeepsakeProvider.system_prompt_block()` 每轮原文全量注入。

## 条目切分
   按**单独一行** `§` 切（Markdown 里 `§` 单独成行是既有分节惯例；
   行内出现的 `§` 不切）。每条去掉首尾空行后**原文**入库。

## 幂等
   判据 = 库里已有一条 core 碎片的 content 与本条**逐字相同**。
   不靠 key（key 是内容 hash，改一个字就是新 key ⇒ 不幂等）。
   所以重复跑第二次必然 inserted=0。

## 默认 dry-run
   只读库算「本该写几条」，不写任何东西。**只有显式加 `--write` 才真写库。**
   dry-run 也读库（否则算不出 skipped），但只走 `list_by_tag` 这一个读原语。

## 用法
   python3 scripts/migrate_builtin_memory_to_core.py --source MEMORY.md --source USER.md
   python3 scripts/migrate_builtin_memory_to_core.py --source MEMORY.md --write

输出（stdout 唯一一行 JSON）：
   {"parsed": N, "inserted": M, "skipped": K}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from keepsake.storage import storage_from_config  # noqa: E402

#: 条目分隔符：单独一行、内容恰为这个符号的行。
SEPARATOR = "§"

#: 迁移进来的条目一律打这个 tags（与 `on_memory_write` 的 core 写入同款）。
CORE_TAGS = "core,memory"


def split_entries(text: str) -> list[str]:
    """按单独一行 `§` 切条目；去掉首尾空行，丢弃空条目。"""
    out, cur = [], []
    for line in text.splitlines():
        if line.strip() == SEPARATOR:
            out.append("\n".join(cur).strip())
            cur = []
        else:
            cur.append(line)
    out.append("\n".join(cur).strip())
    return [e for e in out if e]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把 MEMORY.md / USER.md 原文迁成 core 碎片")
    ap.add_argument("--source", action="append", required=True, metavar="MD",
                    help="常驻记忆 md 路径，可重复传")
    ap.add_argument("--write", action="store_true",
                    help="真写库（**默认只 dry-run**）")
    ap.add_argument("--config", default=None, help="keepsake 配置 json 路径")
    args = ap.parse_args(argv)

    storage = storage_from_config(args.config)
    try:
        if not storage.ensure_index():
            print("[error] 存储后端未就绪（ensure_index 失败），不迁移", file=sys.stderr)
            return 2
        try:
            existing = {f.get("content") for f in storage.list_by_tag("core")}
        except NotImplementedError as e:
            print(f"[error] 当前后端未实现 list_by_tag，无法判重: {e}", file=sys.stderr)
            return 2

        parsed = inserted = skipped = 0
        for src in args.source:
            path = Path(src).expanduser()
            if not path.exists():
                print(f"[error] 源文件不存在: {path}", file=sys.stderr)
                return 2
            for entry in split_entries(path.read_text(encoding="utf-8")):
                parsed += 1
                if entry in existing:
                    skipped += 1
                    continue
                if args.write:
                    storage.store(text=entry, tags=CORE_TAGS, category="core",
                                  source="migrate_builtin_memory", fragment_type="core")
                existing.add(entry)     # 同一文件里重复条目也要幂等
                inserted += 1
    finally:
        storage.close()

    print(json.dumps({"parsed": parsed, "inserted": inserted, "skipped": skipped},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())