#!/usr/bin/env python3
"""同义词自动发现 — 给 cron 调用用。"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fragmented_memory.storage import RedisStorage

config_path = Path("~/.config/fragmented-memory/config.json").expanduser()
if config_path.exists():
    with open(config_path) as f:
        cfg = json.load(f)
else:
    cfg = {}

storage = RedisStorage(
    host=cfg.get("redis_host", "127.0.0.1"),
    port=int(cfg.get("redis_port", 6379)),
    password=cfg.get("redis_password") or None,
    synonym_min_word_freq=int(cfg.get("synonym_min_word_freq", 10)),
    synonym_jaccard_threshold=float(cfg.get("synonym_jaccard_threshold", 0.5)),
    synonym_min_co_occurrence=int(cfg.get("synonym_min_co_occurrence", 3)),
)
result = storage.discover_synonyms()
storage.close()
print(json.dumps(result, ensure_ascii=False))