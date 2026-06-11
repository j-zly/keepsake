#!/usr/bin/env python3
import sys, json, importlib.machinery
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / 'src'
sys.path.insert(0, str(SRC))

from fragmented_memory import splitter, emotion, embedder

_loader = importlib.machinery.SourceFileLoader(
    'fragmented_memory.storage',
    str(SRC / 'fragmented_memory' / 'storage.py')
)
mod = type(sys)('fragmented_memory.storage')
_loader.exec_module(mod)

cfg_p = Path('~/.config/fragmented-memory/config.json').expanduser()
cfg = json.loads(cfg_p.read_text()) if cfg_p.exists() else {}

store = mod.RedisStorage(
    host=cfg.get('redis_host', '127.0.0.1'),
    port=int(cfg.get('redis_port', 6379)),
    password=cfg.get('redis_password') or None,
    synonym_min_word_freq=int(cfg.get('synonym_min_word_freq', 10)),
    synonym_jaccard_threshold=float(cfg.get('synonym_jaccard_threshold', 0.5)),
    synonym_min_co_occurrence=int(cfg.get('synonym_min_co_occurrence', 3)),
)
result = store.discover_synonyms()

# 生成 jieba 自定义词典
dict_result = store.generate_jieba_dict()

store.close()
result["jieba_dict"] = dict_result
print(json.dumps(result, ensure_ascii=False))
