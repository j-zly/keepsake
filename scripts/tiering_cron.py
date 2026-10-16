#!/usr/bin/env python3
"""常驻层分级班车的 cron 入口：连库 → `run_tiering()` 一轮 → stdout 打 stats JSON。

用法（互斥且必选，与 `scripts/kb_ingest.py` 同一套 argparse 口吻）：

    python3 scripts/tiering_cron.py --dry     # 预演：只算不写（观察期人肉跑几天）
    python3 scripts/tiering_cron.py --run     # 实跑：按 config 的阈值提名 / 升 / 降

**`tiering.enabled=false`（缺省）时两条都直接 `exit 0` + 打 `{"skipped":"disabled"}`** ——
cron 不报红。开关的语义与纪律见 `keepsake/core/tiering.py` 模块 docstring。

🔴 **本脚本不挂 cron、不改 crontab**：调度是运维接线的事（主脑做）。脚本自身无
定时器、无线程 —— 跑完就退出（`run_tiering` 也是纯同步）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

# 仓库没装进 site-packages 时（cron 直接跑脚本）把 src 挂上，与 kb_ingest.py 同款
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from keepsake.core.tiering import (  # noqa: E402 —— 路径补齐后才能 import
    resolve_tiering,
    run_tiering,
)

__all__ = ["load_config", "main"]


def load_config() -> Dict[str, Any]:
    """读整份 config.json（**读一次**）：路径与 provider 注入路径同一条解析规则。

    连 provider 的 `config_path()` 而不是自己判 env：读写两端各写一份 env 判断，
    迟早出现「写的是 A 文件、班车读的是 B 文件」。
    """
    from keepsake.adapters.hermes.provider import config_path

    path = config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        print(json.dumps({"skipped": "config_unreadable", "path": str(path),
                          "error": str(e)}, ensure_ascii=False))
        raise SystemExit(2)
    if not isinstance(raw, dict):
        print(json.dumps({"skipped": "config_not_object", "path": str(path)},
                         ensure_ascii=False))
        raise SystemExit(2)
    return raw


def main(argv: Any = None) -> int:
    ap = argparse.ArgumentParser(description="常驻层分级班车（提名 / 自动升 / 压力降）")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry", action="store_true", help="只算不写（观察期用这个）")
    group.add_argument("--run", action="store_true", help="实跑（按 config 阈值写库）")
    args = ap.parse_args(argv)
    dry_run = bool(args.dry)

    cfg = load_config()
    tiering = resolve_tiering(cfg)
    if not tiering["enabled"]:
        # 缺省关死：两条都干净退出，cron 不红（照 spec）
        print(json.dumps({"skipped": "disabled", "dry_run": dry_run,
                          "reason": "tiering.enabled=false"}, ensure_ascii=False))
        return 0

    from keepsake.storage import storage_from_config

    # 同一份 cfg 交给存储层构造（少一次读盘，也保证班车与检索看的是同一个库）
    storage = storage_from_config(config=cfg)
    stats = run_tiering(storage, cfg, dry_run=dry_run)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
