#!/usr/bin/env python3
"""同义词自动发现 — 给 cron 调用用。

2026-11：改走 `storage_from_config()` 统一入口（Redis 后端已移除）。
同义词阈值等参数由工厂从同一份 config.json 读取，不再手搓构造。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from keepsake.core.factory import storage_from_config

storage = storage_from_config()
result = storage.discover_synonyms()
storage.close()
print(json.dumps(result, ensure_ascii=False))
