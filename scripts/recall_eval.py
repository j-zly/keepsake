#!/usr/bin/env python3
"""召回质量评测器（只读）—— keepsake_recall_eval_v1

在**指定库**上跑一组探针（query → 期望子串），输出各档 Recall@k / MRR / 逐条命中 /
每次查询命中的 key 序列。

🔴 **绝对不写库**：本脚本只调只读原语（`search_bm25` / `search` / `scan_fragment_keys`），
   并且**连接本身**就是只读的 ——
     * sqlite：构造时传 `readonly=True` ⇒ 走 `file:…?mode=ro` URI，写语句由 SQLite 拒掉；
     * postgres：conninfo 追加 `options='-c default_transaction_read_only=on'`
       ⇒ 会话级只读事务，任何 INSERT/UPDATE/DELETE 都被服务端拒绝。
   评测污染不了记忆库这件事由**数据库**保证，不靠调用方自觉。

用法（合成语料）:
    python3 scripts/recall_eval.py \
        --config /tmp/ks_eval_cfg.json \
        --probes /tmp/ks_eval_probes.json \
        --k 1,5,15,50 --mode rerank --repeat 3 --out /tmp/ks_eval.json

用法（生产 PG 复跑，主脑验收用）:
    python3 scripts/recall_eval.py \
        --config ~/.config/keepsake/config.json \
        --probes /tmp/ks_eval_probes.json \
        --k 1,5,15,50 --mode rerank --out /tmp/ks_eval_prod.json

`--config` 用 keepsake 自己的配置格式（`storage.backend` / `storage.postgres.*` /
`storage.sqlite.*`）；本脚本只覆盖两个键：`top_k` = `--k` 的最大值（否则拿不到深位次）、
`bm25_limit`（不配则用 DEFAULT_BM25_LIMIT，即本单放大后的候选池）。

探针格式: `[{"query": "...", "expect_substring": "...", "tag_filter": ""}]`
命中定义: 返回结果里**存在一条**的 `content` 含 `expect_substring`（大小写敏感）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# 源码树优先（与 scripts/eval_spotcheck.py 同一套做法，见该文件顶部注释）：
# 在源码树里跑 ⇒ import 的就是正在改的代码；硬编码安装路径会让评测悄悄跑旧版本。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from keepsake.storage import (                                  # noqa: E402
    DEFAULT_BM25_LIMIT,
    resolve_backend,
    storage_from_config,
)

DEFAULT_CONFIG = os.path.expanduser("~/.config/keepsake/config.json")


# ---------------------------------------------------------------------------
# 只读装配
# ---------------------------------------------------------------------------

def _force_readonly(storage: Any, backend: str) -> List[str]:
    """把 storage 钉成**只读**，返回说明（进 JSON 输出，方便复核）。

    * sqlite：`readonly=True` ⇒ `file:…?mode=ro`。这是构造参数，故在实例上补设后
      **必须丢掉已建立的连接**（本脚本在建连之前调用，正常路径上 `_conn is None`）。
    * postgres：libpq `options='-c default_transaction_read_only=on'` 是**会话级**
      只读（`SET` 写在事务里会随 commit 失效，所以只能走 conninfo）。
      `_conninfo()` 里 `_dsn` 非空即优先 ⇒ 直接补一条 options 即可覆盖 host/port 拼法。
    * redis：`storage.py` 没有只读连接开关，退化为「只调只读方法」的纪律，
      在说明里**明确写出来**（不假装有数据库级保证）。
    """
    notes: List[str] = []
    if backend == "sqlite":
        storage._readonly = True                      # noqa: SLF001 — 评测器专用开关
        if getattr(storage, "_conn", None) is not None:  # noqa: SLF001
            storage.close()
        notes.append("sqlite: file:...?mode=ro（SQLite 层面拒写；"
                     "若跑在**旧版本代码**上该开关还不存在，只靠调用纪律）")
    elif backend == "postgres":
        ro_opt = "options='-c default_transaction_read_only=on'"
        conninfo = storage._conninfo()                # noqa: SLF001
        if "default_transaction_read_only" not in conninfo:
            storage._dsn = f"{conninfo} {ro_opt}"      # noqa: SLF001
            storage._conn = None                      # noqa: SLF001 — 强制按新 conninfo 建连
        notes.append("postgres: default_transaction_read_only=on（PG 服务端拒写）")
    else:
        notes.append("redis: 仅调用只读方法（无数据库级只读开关）")
    return notes


def _corpus_size(storage: Any) -> int:
    """碎片条数（走只读的 `scan_fragment_keys` keyset 分页；读不到就 0，不抛）。"""
    total, cursor = 0, ""
    try:
        while True:
            cursor, keys = storage.scan_fragment_keys(cursor, limit=1000)
            total += len(keys)
            if not cursor or not keys:
                break
    except Exception as e:                            # noqa: BLE001 — 统计失败不该毁掉整轮评测
        print(f"[warn] 语料条数统计失败（不影响召回指标）: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 0
    return total


# ---------------------------------------------------------------------------
# 一轮评测
# ---------------------------------------------------------------------------

def run_once(storage: Any, probes: List[Dict[str, Any]], ks: List[int],
             mode: str, with_keys: bool) -> Dict[str, Any]:
    """跑一遍全部探针。`with_keys=False` 时不收集 key 序列（确定性对照省内存）。"""
    search = storage.search if mode == "rerank" else storage.search_bm25
    top = max(ks)
    per_probe: List[Dict[str, Any]] = []
    for p in probes:
        query = str(p.get("query", ""))
        expect = str(p.get("expect_substring", ""))
        tag_filter = str(p.get("tag_filter", "") or "")
        try:
            res = search(query, tag_filter=tag_filter) or []
        except Exception as e:                        # noqa: BLE001 — 单题失败不拖垮整轮
            per_probe.append({"query": query, "expect_substring": expect,
                              "error": f"{type(e).__name__}: {e}",
                              "hit": False, "first_hit_rank": None,
                              "keys": [] if with_keys else None})
            continue
        # 只看前 top 条 —— Recall@k 的 k 档都不超过 top
        res = list(res)[:top]
        first_rank = None
        for i, frag in enumerate(res, 1):
            if expect and expect in str(frag.get("content", "")):
                first_rank = i
                break
        per_probe.append({
            "query": query,
            "expect_substring": expect,
            "hit": first_rank is not None,
            "first_hit_rank": first_rank,
            "keys": [f.get("_key") for f in res] if with_keys else None,
        })

    n = len(per_probe) or 1
    ok = [x for x in per_probe if not x.get("error")]
    metrics = {f"Recall@{k}": round(sum(1 for x in ok if x["hit"] and x["first_hit_rank"] <= k) / n, 4)
               for k in ks}
    metrics["MRR"] = round(
        sum(1.0 / x["first_hit_rank"] for x in ok if x["hit"]) / n, 4)
    return {"metrics": metrics, "probes": per_probe}


def main() -> int:
    ap = argparse.ArgumentParser(description="keepsake 召回质量评测器（只读）")
    ap.add_argument("--probes", required=True,
                    help='探针 JSON: [{"query": "...", "expect_substring": "..."}]')
    ap.add_argument("--k", default="1,5,15,50", help="召回档位，逗号分隔（默认 1,5,15,50）")
    ap.add_argument("--mode", choices=["recall", "rerank"], default="rerank",
                    help="recall=直接看 BM25 候选池(search_bm25)；rerank=走完整 search()")
    ap.add_argument("--config", default="", help="keepsake 配置 JSON（默认 ~/.config/keepsake/config.json）")
    ap.add_argument("--repeat", type=int, default=1, help="连跑几轮（>1 时逐轮比对 key 序列做确定性校验）")
    ap.add_argument("--out", default="", help="输出 JSON 路径（默认 <out>.json + <out>.txt）")
    args = ap.parse_args()

    ks = [int(x) for x in str(args.k).replace("，", ",").split(",") if x.strip()]
    if not ks:
        print("ERROR: --k 解析不出档位", file=sys.stderr)
        return 2

    probes = json.loads(Path(args.probes).read_text(encoding="utf-8"))
    if isinstance(probes, dict):
        probes = probes.get("probes", [])
    if not probes:
        print("ERROR: 探针集为空", file=sys.stderr)
        return 2

    cfg_path = args.config or os.environ.get("KEEPSAKE_CONFIG") or DEFAULT_CONFIG
    cfg: Dict[str, Any] = {}
    if Path(cfg_path).expanduser().exists():
        cfg = json.loads(Path(cfg_path).expanduser().read_text(encoding="utf-8"))
    else:
        print(f"[warn] 配置 {cfg_path} 不存在 —— 用库默认值（backend=redis）", file=sys.stderr)

    backend = resolve_backend(cfg)
    # 只覆盖两个键：top_k 取最深档（否则 Recall@50 会被 final_limit 截断成 Recall@top_k），
    # bm25_limit 不配就用模块默认（本单放大后的候选池）。其余键（filter/权重）原样透传。
    cfg["top_k"] = max(ks)
    cfg.setdefault("bm25_limit", DEFAULT_BM25_LIMIT)
    if backend == "redis":       # redis 后端需要 host/port 在顶层；sqlite/pg 在 storage.* 节
        cfg.setdefault("redis_host", "127.0.0.1")
        cfg.setdefault("redis_port", 6379)

    storage = storage_from_config(config=cfg)
    ro_notes = _force_readonly(storage, backend)

    rounds: List[Dict[str, Any]] = []
    try:
        corpus_size = _corpus_size(storage)
        for i in range(max(1, args.repeat)):
            rounds.append(run_once(storage, probes, ks, args.mode,
                                   with_keys=(args.repeat > 1 or i == 0)))
    finally:
        try:
            storage.close()
        except Exception:                           # noqa: BLE001
            pass

    base = rounds[0]
    # 确定性：多轮之间逐条比对 key 序列
    deterministic = all(
        [p.get("keys") for p in r["probes"]] == [p.get("keys") for p in base["probes"]]
        for r in rounds[1:]
    )
    result = {
        "meta": {
            "mode": args.mode,
            "ks": ks,
            "repeat": args.repeat,
            "deterministic": deterministic,
            "backend": backend,
            "config_path": cfg_path,
            "read_only": ro_notes,
            "params": {k: cfg.get(k) for k in ("top_k", "bm25_limit", "candidate_k")},
            "corpus_size": corpus_size,
            "probes": len(probes),
        },
        "metrics": base["metrics"],
        "probes": base["probes"],
    }

    out = args.out or str(Path(args.probes).with_suffix(".recall_eval.json"))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"mode={args.mode}  backend={backend}  语料={result['meta']['corpus_size']} 条  "
        f"探针={len(probes)}  params={result['meta']['params']}",
        f"只读：{'; '.join(ro_notes)}",
        "  ".join(f"{m}={v}" for m, v in result["metrics"].items()),
        f"确定性（{args.repeat} 轮 key 序列一致）：{'YES' if deterministic else 'NO'}",
        "逐条：",
    ]
    for p in base["probes"]:
        mark = "HIT " if p["hit"] else "MISS"
        rank = p["first_hit_rank"]
        lines.append(f"  [{mark}] rank={rank or '-'}  {p['query']}  ->  {p['expect_substring']}"
                     + (f"  ERR={p['error']}" if p.get("error") else ""))
    txt = "\n".join(lines)
    Path(str(out).rsplit(".", 1)[0] + ".txt").write_text(txt + "\n", encoding="utf-8")
    print(txt)
    print(f"\nJSON: {out}")
    print(f"人读: {str(out).rsplit('.', 1)[0]}.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())