#!/usr/bin/env python3
"""ks_hot_topic_stopwords — 一次性清理 Redis 中污染的热门话题词。

背景：2026-09 ks_hot_topic_stopwords 修复前，`extract_keywords` 的 jieba 分支
会漏过 jieba HMM 切英文产出的 2 字母 ASCII 碎渣（in/an/ce/be/dd/em 等），
这些碎渣被 zincrby 进 HOT_TOPIC_SET/DAILY/WEEKLY，还污染检索 hot 权重。
修复后新写入不会再有此类污染，但历史 zset/hset 里仍残留。

本脚本：
  * 读 config.json 连 Redis（host/port/password — 密码不硬编码）
  * 扫描三个 zset + last_seen hash
  * 列出/删除「纯 ASCII 且 len<3」或「在 _ENG_FUNCTION_WORDS 内」的 member
  * --dry-run 默认开（只打印将删清单），--yes 才真删

用法：
    python3 scripts/cleanup_hot_topics.py             # 默认 dry-run
    python3 scripts/cleanup_hot_topics.py --yes       # 真删
    python3 scripts/cleanup_hot_topics.py --json      # 干跑结果输出 JSON
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# 让脚本不依赖外部安装也能 import keepsake.splitter 的虚词表
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from keepsake.splitter import _ENG_FUNCTION_WORDS  # noqa: E402
from keepsake.storage import (  # noqa: E402
    HOT_TOPIC_DAILY,
    HOT_TOPIC_LAST_SEEN,
    HOT_TOPIC_SET,
    HOT_TOPIC_WEEKLY,
)


# 三个 zset + 一个 hash 都属于 hot topic 体系，一起扫
HOT_ZSETS = [HOT_TOPIC_SET, HOT_TOPIC_DAILY, HOT_TOPIC_WEEKLY]

# 纯 ASCII 短 token 判据（≤ 3 字母的英文碎渣 + 单字母）
_ASCII_FRAGMENT = re.compile(r"^[A-Za-z]+$")


def _is_polluted(member: str) -> bool:
    """判断是否属于本次清理目标：纯 ASCII 短词 或 英文虚词。"""
    if not member:
        return False
    lower = member.lower()
    # 虚词表
    if lower in _ENG_FUNCTION_WORDS:
        return True
    # 纯 ASCII 短词（≤ 3 字母） — jieba 碎渣 + 单字母噪音
    if _ASCII_FRAGMENT.match(member) and len(member) < 3:
        return True
    return False


def _load_config() -> dict:
    """读 config.json — 路径优先级同 keepsake 包：env KEEPSAKE_CONFIG → ~/.config/keepsake/config.json。"""
    default_path = "~/.config/keepsake/config.json"
    path_str = os.environ.get("KEEPSAKE_CONFIG") or default_path
    path = Path(path_str).expanduser()
    if not path.exists():
        print(f"[ERROR] config.json 不存在: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[ERROR] config.json 读取失败: {path} ({e})", file=sys.stderr)
        sys.exit(1)


def _connect_redis(cfg: dict):
    """按 cfg 里的 redis_host/port/password 连接。失败时给清晰报错并退出。"""
    try:
        import redis  # noqa: F401
    except ImportError:
        print(
            "[ERROR] redis 库未安装；本脚本需要 redis-py 才能连 Redis。\n"
            "       pip install redis",
            file=sys.stderr,
        )
        sys.exit(1)

    host = cfg.get("redis_host", "127.0.0.1")
    port = int(cfg.get("redis_port", 6379))
    password = cfg.get("redis_password") or None
    try:
        client = redis.Redis(
            host=host, port=port, password=password,
            socket_connect_timeout=3, socket_timeout=3,
        )
        client.ping()
        return client
    except Exception as e:
        print(
            f"[ERROR] Redis 连接失败 {host}:{port} ({type(e).__name__}: {e})",
            file=sys.stderr,
        )
        sys.exit(1)


def scan_polluted(client) -> dict:
    """扫三个 zset + last_seen hash，列出每个 key 下要清的 member 列表。"""
    plan: dict = {}
    for zkey in HOT_ZSETS:
        members = []
        try:
            raw = client.zrange(zkey, 0, -1)
        except Exception as e:
            print(f"[WARN] zrange {zkey} 失败: {e}", file=sys.stderr)
            raw = []
        for m in raw:
            if isinstance(m, bytes):
                m = m.decode("utf-8", errors="replace")
            if _is_polluted(m):
                members.append(m)
        if members:
            plan[zkey] = members

    # last_seen hash
    try:
        raw_hash = client.hgetall(HOT_TOPIC_LAST_SEEN) or {}
    except Exception as e:
        print(f"[WARN] hgetall {HOT_TOPIC_LAST_SEEN} 失败: {e}", file=sys.stderr)
        raw_hash = {}
    polluted_fields = []
    for f in raw_hash:
        if isinstance(f, bytes):
            f = f.decode("utf-8", errors="replace")
        if _is_polluted(f):
            polluted_fields.append(f)
    if polluted_fields:
        plan[HOT_TOPIC_LAST_SEEN] = polluted_fields

    return plan


def execute_cleanup(client, plan: dict, *, apply: bool) -> int:
    """dry-run: 打印清单；apply: 真删。返回实际删除/将要删除的 member 总数。"""
    total = 0
    for key, members in plan.items():
        if not members:
            continue
        is_zset = key in HOT_ZSETS
        verb = "ZREM" if is_zset else "HDEL"
        print(f"\n[{key}] {verb} {len(members)} 项：")
        for m in members:
            print(f"  - {m!r}")
            if apply:
                try:
                    if is_zset:
                        client.zrem(key, m)
                    else:
                        client.hdel(key, m)
                except Exception as e:
                    print(f"    [WARN] 删 {m!r} 失败: {e}", file=sys.stderr)
        total += len(members)
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="清理 Redis 中污染的热门话题词（一次性脚本）",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="实际执行删除（默认 dry-run，只打印清单）",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="将扫描结果以 JSON 输出到 stdout（仍走 dry-run）",
    )
    args = parser.parse_args(argv)

    cfg = _load_config()
    client = _connect_redis(cfg)

    plan = scan_polluted(client)

    if args.json:
        # JSON 输出：dry-run 始终为 True（即使 --yes 也只是把计划 dump 出来）
        print(json.dumps(
            {"dry_run": True, "plan": plan, "total": sum(len(v) for v in plan.values())},
            ensure_ascii=False, indent=2,
        ))
        return 0

    total = sum(len(v) for v in plan.values())
    if total == 0:
        print("[OK] 三个 zset + last_seen hash 中没有发现污染词；无需清理。")
        return 0

    apply = bool(args.yes)
    if not apply:
        print(
            "[DRY-RUN] 即将删除以下 member（共 {n} 项）。"
            "\n         用 --yes 才真删。".format(n=total)
        )
    else:
        print(f"[APPLY] 开始清理 {total} 项污染 member...")

    deleted = execute_cleanup(client, plan, apply=apply)

    if apply:
        print(f"\n[DONE] 已清理 {deleted} 项污染词。")
    else:
        print(f"\n[DRY-RUN DONE] 共 {deleted} 项污染词等待清理（加 --yes 执行）。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
