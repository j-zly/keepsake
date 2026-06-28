#!/usr/bin/env python3
"""碎片记忆维护脚本 — 由 cron 调度，每 2h 执行一次。

执行:
  1. Consolidation（同主题碎片合并提炼）
  2. Selective Forgetting（低价值碎片清理）

输出:
  仅 maintenance 有实际动作时输出日志，无动作时静默（watchdog 模式）。
"""

import json
import os
import sys
from pathlib import Path

# config 路径
CONFIG_PATH = os.path.expanduser("~/.config/keepsake/config.json")
HERMES_ENV = os.path.expanduser("~/.hermes/.env")

# 加载 .env（gateway 的 env）
if os.path.isfile(HERMES_ENV):
    with open(HERMES_ENV) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def load_config() -> dict:
    """从 JSON 配置文件加载参数。"""
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def main():
    cfg = load_config()
    redis_host = cfg.get("redis_host", "127.0.0.1")
    redis_port = int(cfg.get("redis_port", 6379))

    try:
        from keepsake import KeepsakeProvider

        provider = KeepsakeProvider(keepsake={
            "redis_host": redis_host,
            "redis_port": redis_port,
        })
        if not provider.is_available():
            print("[memory-maintenance] keepsake provider not available")
            sys.exit(1)

        provider.initialize(session_id="cron-maintenance")
        if not provider._initialized:
            print("[memory-maintenance] initialization failed (Redis down?)")
            sys.exit(1)

        stats = provider.maintenance()
        provider.shutdown()

        # watchdog 模式：有实际动作才输出
        c = stats.get("consolidator", {})
        f = stats.get("forgetter", {})

        merged = c.get("merged", 0) or 0
        forgotten = f.get("deleted", 0) or 0
        dry_run = f.get("dry_run", True)

        if merged > 0 or forgotten > 0:
            parts = []
            if merged > 0:
                parts.append(f"合并 {merged} 条碎片")
            if forgotten > 0:
                note = " (dry-run)" if dry_run else ""
                parts.append(f"遗忘 {forgotten} 条碎片{note}")
            print(f"[memory-maintenance] {'，'.join(parts)}")
        else:
            # 无动作则静默
            sys.exit(0)

    except Exception as e:
        print(f"[memory-maintenance] error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
