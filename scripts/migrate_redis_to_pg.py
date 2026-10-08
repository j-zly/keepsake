#!/usr/bin/env python3
"""把 Redis 里的记忆副本迁移到 PostgreSQL —— **对 Redis 严格只读**。

🔴 **只读红线（本脚本最硬的约束）**
   本文件只用 SCAN / HGETALL / PING 这几个读命令，**不存在任何写 Redis 的调用**。
   启动时 `assert_no_redis_writes()` 用 AST 静态自检：把源码里所有方法调用名收集起来，
   跟「写命令黑名单」对撞，出现任何一个就直接退出 —— 防止以后有人顺手加一行
   `client.set(...)` 而没人发现。这是自检，不依赖 pytest。

## 干什么
   SCAN `memory:frag:*` → pipeline HGETALL → 直写 PG `ks_fragment`。
   tsvector（jieba 分词）+ GIN 索引、embedding（embedder）+ HNSW 索引，
   一律由 `PgStorage.upsert_fragment()` 负责算 —— 脚本不重复实现一遍分词逻辑。

## 幂等
   主键就是 Redis 的 key，`ON CONFLICT (key) DO UPDATE` ⇒ 重复跑是覆盖更新，
   **不会**产生第二行。
   刻意**不走 `store()`**：store() 遇同内容会把旧版另起 `:<epoch>` 新键（在线写入的
   去重语义）。迁移要的是「Redis 里什么样，PG 里就什么样」。

## 用法
   python3 scripts/migrate_redis_to_pg.py --dry-run     # 只读、只打印计划
   python3 scripts/migrate_redis_to_pg.py --limit 100   # 先迁 100 条试水
   python3 scripts/migrate_redis_to_pg.py                # 全量
   python3 scripts/migrate_redis_to_pg.py --config /path/to/config.json

⚠️ 88 上没有到 Redis 的通路，本单**不连真 Redis** 跑；脚本按「主脑在能连通的环境
   执行」设计。凭据一律来自 keepsake 的 config.json / `KEEPSAKE_CONFIG`，
   不硬编码、不打印、不进日志。

📌 **本单未覆盖**（评测前要知道）：话题榜 / 注意力榜 / 同义词表 / 实体时间线
   **没有**一起搬。后果：PG 侧的重排加权会退到默认值（热词 1.0、注意力 1.0），
   同义词扩展为空 ⇒ **BM25 命中率可比、KNN 可比，但排序分不可直接比**。
   主脑做对照评测时请只比「命中集合」而非「分数」，或先补搬辅助表。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# ---------------------------------------------------------------- 只读红线自检

#: 出现即视为写 Redis 的命令 —— 源码里有就直接拒绝启动。
#: 只收 **Redis 命令名**（都是 `client.<命令>()` 的形式）。
#: 刻意**不**收 `execute`（pipeline 的 flush 方法，本身不是命令；本脚本队列里
#: 只有 HGETALL）与 `close`/`ping` —— 否则自检会永远误报，红线也就没人看了。
FORBIDDEN_REDIS_COMMANDS = frozenset({
    "set", "setex", "setnx", "getset", "append", "incr", "incrby", "decr", "decrby",
    "hset", "hmset", "hdel", "hincrby", "hincrbyfloat", "hsetnx",
    # delete/unlink 是 DEL 的两个合法别名，两个都得禁（漏一个就等于没禁）
    "del", "delete", "unlink", "rename", "renamenx", "expire", "pexpire", "expireat",
    "persist",
    # execute_command：redis-py 的低层逃逸口，命令名是字符串参数，静态审不出来
    "execute_command",
    "zadd", "zrem", "zincrby", "zremrangebyscore", "zremrangebyrank",
    "sadd", "srem", "smove", "sunion", "sunionstore",
    "flushdb", "flushall", "eval", "evalsha", "script",
    "ft_create", "ft_drop", "ft_add", "ft_del", "ft_search", "ftalter", "ftalterindex",
})


def _redis_receiver_names(tree: ast.AST) -> set:
    """找出「可能持有 Redis 连接」的变量名。

    判据：被赋值为 `redis.Redis(...)` / `*.Redis(...)` / `*.pipeline()` 的名字，
    以及形参名里带 client/conn/pipe/redis 的。目的是让红线只盯着真正的连接对象，
    而不是把 `rows.append(...)` 也当成写 Redis。
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            val = node.value
            is_redis = (isinstance(val, ast.Call)
                        and isinstance(val.func, ast.Attribute)
                        and val.func.attr in ("Redis", "StrictRedis", "pipeline"))
            if not is_redis:
                continue
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.arg):
            low = node.arg.lower()
            if any(k in low for k in ("client", "conn", "pipe", "redis")):
                names.add(node.arg)
    return names


def assert_no_redis_writes(script_path: Optional[Path] = None) -> List[str]:
    """🔴 只读红线自检：源码里不得对 Redis 连接调用任何写命令。

    为什么不是「全文 grep 命令名」：那样 `rows.append(...)`、`set(...)` 全会误报，
    误报的红线等于没有红线。这里只审**「对 Redis 连接的」属性调用** ——
    先用 `_redis_receiver_names()` 把连接对象认出来，再看它们身上调了什么方法。

    另加一条低层逃逸口检查：redis-py 的 `execute_command("SET", ...)` 什么方法名都
    藏得住，所以裸函数调用里出现 `execute_command` 也直接拒绝。
    """
    path = script_path or Path(__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    receivers = _redis_receiver_names(tree)

    called: List[str] = []
    bad: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                and func.value.id in receivers:
            name = func.attr.lower()
            called.append(name)
            if name in FORBIDDEN_REDIS_COMMANDS:
                bad.append(name)
        elif isinstance(func, ast.Name) and func.id == "execute_command":
            # 低层逃逸口：命令名是字符串参数，静态审不出来 ⇒ 一律拒绝使用
            bad.append("execute_command(...)")

    if bad:
        raise SystemExit(
            f"🔴 只读红线被打破：脚本对 Redis 连接调用了 {sorted(set(bad))}。"
            "本脚本对 Redis 只能读。"
        )
    return sorted(set(called))


# ---------------------------------------------------------------- 配置 / 客户端


def load_config(path: str = "") -> Dict[str, Any]:
    """读 keepsake 配置（与 storage_from_config 同一份）。"""
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    default = os.path.expanduser("~/.config/keepsake/config.json")
    if not os.path.exists(default):
        raise SystemExit(
            f"找不到 keepsake 配置 {default}；用 --config 指定，或设 KEEPSAKE_CONFIG 环境变量"
        )
    return json.loads(Path(default).read_text(encoding="utf-8"))


def build_redis(cfg: Dict[str, Any]):
    """只读 Redis 客户端。

    🔴 刻意用裸 `redis.Redis` 而不是 `RedisStorage`：后者的 `store()` 是写路径，
    迁移时误用就违反只读红线了。
    """
    import redis  # 延迟 import：没装 redis 时仍可 import 本模块做语法检查

    return redis.Redis(
        host=cfg.get("redis_host", "127.0.0.1"),
        port=int(cfg.get("redis_port", 6379)),
        password=cfg.get("redis_password") or None,
        decode_responses=False,          # 保持 bytes，hash 字段值原样搬
        socket_connect_timeout=5,
        socket_timeout=30,
    )


def build_embedder(cfg: Dict[str, Any]):
    from keepsake.embedder import create_embedder

    ec = dict(cfg.get("embedder") or {})
    if not ec.get("model"):
        print("⚠️ 配置里没有 embedder 段 → 迁过去的碎片没有向量，PG 侧 KNN 不可用"
              "（BM25 不受影响）。")
        return None
    return create_embedder(
        provider=ec.get("provider", "openai"),
        api_key=ec.get("api_key", ""),
        base_url=ec.get("base_url", ""),
        model=ec.get("model", ""),
    )


def build_pg(cfg: Dict[str, Any], embedder: Any, embed_dim: int):
    from keepsake.storage_pg import PgStorage

    pg_cfg = (cfg.get("storage") or {}).get("postgres") or {}
    if not isinstance(pg_cfg, dict):
        pg_cfg = {}
    storage = PgStorage(
        host=str(pg_cfg.get("host", "127.0.0.1")),
        port=int(pg_cfg.get("port", 5432)),
        dbname=str(pg_cfg.get("dbname", "keepsake")),
        user=str(pg_cfg.get("user", "")),
        password=str(pg_cfg.get("password", "")),
        sslmode=str(pg_cfg.get("sslmode", "")),
        agent_id=str(cfg.get("agent_id", "")),
        embedder=embedder,
        embed_dim=embed_dim,
    )
    storage.ensure_index()
    return storage


# ---------------------------------------------------------------- 读 Redis（只读）


def _s(v: Any) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v or "")


def iter_fragments(client, batch: int = 200, limit: int = 0) -> Iterator[List[Dict[str, Any]]]:
    """SCAN + pipeline HGETALL，逐批 yield 碎片 dict 列表（只读）。

    yield 的是 `{"key": <redis key>, <hash 字段>: <bytes 或 str>, ...}`。
    """
    scanned = 0
    cursor = 0
    while True:
        cursor, keys = client.scan(cursor=cursor, match="memory:frag:*", count=batch)
        if keys:
            pipe = client.pipeline()
            for k in keys:
                pipe.hgetall(k)
            rows = []
            for key, raw in zip(keys, pipe.execute()):
                scanned += 1
                if not raw:
                    continue
                rows.append({"key": _s(key), **{_s(fk): fv for fk, fv in raw.items()}})
            if rows:
                yield rows
        if limit and scanned >= limit:
            return
        if cursor == 0:
            return


# ---------------------------------------------------------------- 主流程


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Redis → PostgreSQL 记忆迁移（对 Redis 只读）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--config", default=os.environ.get("KEEPSAKE_CONFIG", ""),
                    help="keepsake config.json 路径（默认 ~/.config/keepsake/config.json）")
    ap.add_argument("--dry-run", action="store_true", help="只读 Redis、只打印计划，不写 PG")
    ap.add_argument("--limit", type=int, default=0, help="最多迁多少条碎片（0 = 全部）")
    ap.add_argument("--batch", type=int, default=200, help="每批条数（默认 200）")
    ap.add_argument("--progress-every", type=int, default=500, help="每多少条打一次进度")
    args = ap.parse_args(argv)

    used = assert_no_redis_writes()
    print(f"🔒 只读自检通过：源码里未出现任何 Redis 写命令（检测到调用 {used}）")

    cfg = load_config(args.config)
    redis_host = f"{cfg.get('redis_host', '127.0.0.1')}:{cfg.get('redis_port', 6379)}"
    pg_cfg = (cfg.get("storage") or {}).get("postgres") or {}
    pg_host = (f"{pg_cfg.get('host', '127.0.0.1')}:{pg_cfg.get('port', 5432)}"
               f"/{pg_cfg.get('dbname', 'keepsake')}")
    print(f"Redis（只读）← {redis_host}    PG（写）→ {pg_host}")

    client = build_redis(cfg)
    try:
        client.ping()
    except Exception as e:
        # 错连必须**明确失败**，不能静默当成「没数据可迁」
        raise SystemExit(
            f"🔴 连不上 Redis {redis_host}：{type(e).__name__}: {e}。"
            "迁移中止 —— 这是「没连上」，不是「迁了 0 条」。"
        )

    embedder = build_embedder(cfg)
    embed_dim = embedder.dimension if embedder is not None else 1536

    pg = None
    if not args.dry_run:
        pg = build_pg(cfg, embedder, embed_dim)
    else:
        print("（dry-run：不会连接 PG，也不会写入任何一行）")

    t0 = time.time()
    total = moved = 0
    try:
        batch: List[Dict[str, Any]] = []
        for rows in iter_fragments(client, batch=args.batch, limit=args.limit):
            batch.extend(rows)
            if len(batch) >= args.batch:
                total += len(batch)
                moved += _flush(pg, batch, dry_run=False)
                batch = []
                if total % max(args.progress_every, 1) < args.batch:
                    rate = total / max(time.time() - t0, 0.001)
                    print(f"  … 已读 {total} 条，迁入 {moved} 条（{rate:.0f} 条/秒）")
        if batch:
            total += len(batch)
            moved += _flush(pg, batch, dry_run=False)
    finally:
        if pg is not None:
            pg.close()

    mode = "完成（dry-run，未写入任何一行）" if args.dry_run else "完成"
    print(f"\n✅ 迁移{mode}：Redis 读到 {total} 条 → PG 写入 {moved} 条，"
          f"耗时 {time.time() - t0:.1f}s")
    if args.dry_run:
        print("   去掉 --dry-run 才会真正落库；重复执行是幂等的（按 key 覆盖，不新增行）。")
    return 0


def _flush(pg, batch: List[Dict[str, Any]], dry_run: bool = False) -> int:
    if pg is None or dry_run:
        return len(batch)
    return sum(1 for frag in batch if pg.upsert_fragment(frag))


if __name__ == "__main__":
    sys.exit(main())
