#!/usr/bin/env python3
"""discover_synonyms —— 全量扫碎片找同义词组并写 Redis Hash。

# 2026-09 ks_retr 降噪要点（与 storage.discover_synonyms 同步）：
#   * 纯 ASCII 短词（<3 字）过滤（jieba 切英文的碎块）
#   * 内置 denoise stopwords 黑名单（eg/us/too/no/of/to/in/...）
#   * 中文对至少一方长度≥2
#   * 每词条同义表上限 8（防 hub 式泛连）
#   * --rebuild → DEL hash 后重建（洗掉历史累积碎渣；默认增量保留手动）

# 增量 vs 全量结论：storage.discover_synonyms 是**增量** —— 加载现有
# hash → 跳过手动已存在项 → 仅追加新发现。所以日常 8h cron 是增量补全，
# 不会重写手动添加。要彻底重写需 --rebuild。

# 用法：
#   python discover_synonyms.py            # 增量
#   python discover_synonyms.py --rebuild  # 清 hash 再全建
"""

import sys
import json
import argparse
import importlib.machinery
from pathlib import Path
from typing import List, Dict

SRC = Path(__file__).resolve().parent.parent / 'src'
sys.path.insert(0, str(SRC))

from keepsake import splitter, emotion, embedder

_loader = importlib.machinery.SourceFileLoader(
    'keepsake.storage',
    str(SRC / 'keepsake' / 'storage.py')
)
mod = type(sys)('keepsake.storage')
_loader.exec_module(mod)


def build_kwargs(argv: List[str]) -> Dict[str, bool]:
    """把 argv 解析为 kwargs dict —— 纯函数，便于直测。

    当前支持的 flag：
      --rebuild    清空 keepsake:synonyms 后全量重建（默认 False = 增量）

    未知 flag：交回 argparse 默认行为（SystemExit(2)，error 到 stderr）。
    """
    parser = argparse.ArgumentParser(
        description="Scan fragments and discover synonym groups.",
        prog="discover_synonyms.py",
    )
    parser.add_argument(
        '--rebuild',
        action='store_true',
        help='清空 keepsake:synonyms 后全量重建（洗历史累积碎渣；默认增量）',
    )
    ns = parser.parse_args(argv)
    return {"rebuild": ns.rebuild}


def main() -> int:
    kwargs = build_kwargs(sys.argv[1:])

    cfg_p = Path('~/.config/keepsake/config.json').expanduser()
    cfg = json.loads(cfg_p.read_text()) if cfg_p.exists() else {}

    store = mod.RedisStorage(
        host=cfg.get('redis_host', '127.0.0.1'),
        port=int(cfg.get('redis_port', 6379)),
        password=cfg.get('redis_password') or None,
        synonym_min_word_freq=int(cfg.get('synonym_min_word_freq', 10)),
        synonym_jaccard_threshold=float(cfg.get('synonym_jaccard_threshold', 0.5)),
        synonym_min_co_occurrence=int(cfg.get('synonym_min_co_occurrence', 3)),
    )
    result = store.discover_synonyms(rebuild=kwargs["rebuild"])

    # 生成 jieba 自定义词典（即使没动 synonyms 也重生成 —— discover 顺手做的事）
    dict_result = store.generate_jieba_dict()

    store.close()
    result["jieba_dict"] = dict_result
    result["mode"] = "rebuild" if kwargs["rebuild"] else "incremental"
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
