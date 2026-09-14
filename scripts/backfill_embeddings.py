#!/usr/bin/env python3
"""Keepsake 存量记忆 embedding 回填 — 给没有 embed_bin 的碎片补向量

用法: python3 backfill_embeddings.py [--limit 500] [--dry-run]
"""
import argparse
import sys
import time

sys.path.insert(0, "/opt/fragmented-memory/src")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from keepsake.storage import RedisStorage
    # 从 config.json 读 Redis + embedder 配置
    import json, os
    cfg = {}
    p = os.path.expanduser("~/.config/keepsake/config.json")
    if os.path.exists(p):
        cfg = json.load(open(p))
    embed_cfg = cfg.get("embedder", {}) or {}
    from keepsake.embedder import create_embedder
    embedder = None
    if embed_cfg.get("provider"):
        embedder = create_embedder(
            provider=embed_cfg.get("provider", ""),
            api_key=embed_cfg.get("api_key", ""),
            base_url=embed_cfg.get("base_url", ""),
            model=embed_cfg.get("model", ""),
        )
    stor = RedisStorage(
        host=cfg.get("redis_host", "127.0.0.1"),
        port=cfg.get("redis_port", 6379),
        password=cfg.get("redis_password") or None,
        embedder=embedder,
    )
    client = stor._get_client()
    if not client:
        print("Redis 连接失败")
        return

    if not stor._has_embedder():
        print("embedder 未配置/不可用，检查 config.json embedder 段")
        return
    print(f"embedder: {stor._embedder.__class__.__name__} dim={stor._embedder.dimension}")

    # 遍历所有碎片，找缺 embed_bin 的
    # 2026-09-15：改用 pipeline 批量 HEXISTS —— 逐 key 往返在公网 Redis 上是 N+1
    #（3057 次 × ~170ms ≈ 8.7 分钟），批处理后降到秒级。
    cursor = 0
    missing = []
    total = 0
    while True:
        cursor, keys = client.scan(cursor=cursor, match="memory:frag:*", count=200)
        if keys:
            total += len(keys)
            pipe = client.pipeline()
            for k in keys:
                pipe.hexists(k, "embed_bin")
            for k, exists in zip(keys, pipe.execute()):
                if not exists:
                    missing.append(k)
        if cursor == 0:
            break

    print(f"碎片总数: {total}, 缺 embedding: {len(missing)}")
    if args.dry_run:
        print("(dry-run 结束)")
        return

    done, fail = 0, 0
    pending = []
    for k in missing[: args.limit]:
        content = client.hget(k, "content")
        if not content:
            continue
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        blob = stor._text_to_blob(content)
        if blob:
            pending.append((k, blob))
        else:
            fail += 1
        # 批量写回（每 20 条一个 pipeline，减少公网往返）
        if len(pending) >= 20:
            pipe = client.pipeline()
            for kk, bb in pending:
                pipe.hset(kk, "embed_bin", bb)
            pipe.execute()
            done += len(pending)
            pending = []
            print(f"进度: {done + fail}/{min(len(missing), args.limit)}")
        time.sleep(0.02)  # 避免打爆 ollama
    if pending:
        pipe = client.pipeline()
        for kk, bb in pending:
            pipe.hset(kk, "embed_bin", bb)
        pipe.execute()
        done += len(pending)
        pending = []

    print(f"完成: 回填 {done} 条, 失败 {fail} 条")


if __name__ == "__main__":
    main()
