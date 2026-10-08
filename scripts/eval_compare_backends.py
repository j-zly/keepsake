#!/usr/bin/env python3
"""Redis vs PostgreSQL 记忆检索**并排对照评测**。

🔴 **口径不另造**：题集、命中判定、rank@1 全部直接取自既有的
`scripts/eval_spotcheck.py`（抽查集 v2）—— `SPOTCHECK` 题集、`hits()`、
`first_rank()` 都复用它，本脚本只负责「同一份查询打到两个后端、结果并排摆一起」。
自己造一套指标 = 不可比，也没法跟历史快照对话。

## 输出
   一张并排表：每题一行，Redis 与 PG 各一列（命中数 / rank@1 / top5 / top15），
   末尾两行汇总。窄终端下自动降级成上下两段，仍然是同样的口径。

## 用法
   # 先把 Redis 里的记忆迁进 PG（见 migrate_redis_to_pg.py）
   python3 scripts/eval_compare_backends.py
   python3 scripts/eval_compare_backends.py --top 5          # 只看前 5 题
   python3 scripts/eval_compare_backends.py --backend pg     # 只跑 PG（调试用）
   python3 scripts/eval_compare_backends.py --json out.json  # 落机器可读结果

⚠️ 需要**同时**连得上 Redis 与 PG。任一端连不上就**明确报错退出**，
   绝不「跑一半、只出一半结果」—— 半张表比没有表更危险。
"""
from __future__ import annotations

import argparse
import importlib.util
import ast
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SPOTCHECK_PATH = Path(__file__).resolve().parent / "eval_spotcheck.py"


def load_spotcheck() -> Tuple[List[Tuple[str, List[str]]], Any]:
    """加载 eval_spotcheck 的题集与计分函数。

    直接 `import eval_spotcheck` 有个坑：它在**模块级**就读
    `~/.config/keepsake/config.json`，配置不在时直接抛 FileNotFoundError ——
    于是「只想借它的题集」也被绑死在「必须先配好 Redis」上。

    🔴 **绝不能为了让 import 过而去创建/改写那个 config.json**（那是用户配置，
    本脚本无权动它）。做法是：import 期间临时把 `json.load` 换成返回 `{}` 的桩，
    只影响这一小段；还不行才退到 AST 只取题集。三条路径拿到的**都是同一份
    SPOTCHECK 定义**，不另造题集、不另造指标。
    """
    spec = importlib.util.spec_from_file_location("ks_eval_spotcheck", SPOTCHECK_PATH)
    mod = importlib.util.module_from_spec(spec)

    real_load = json.load
    json.load = lambda *a, **k: {}          # 只在 exec_module 这一小段内生效
    try:
        spec.loader.exec_module(mod)
        return mod.SPOTCHECK, mod
    except Exception as e:
        print(f"⚠️  无法 import eval_spotcheck（{type(e).__name__}: {e}），"
              "改用 AST 只取题集")
    finally:
        json.load = real_load

    tree = ast.parse(SPOTCHECK_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "SPOTCHECK" for t in node.targets):
            return ast.literal_eval(node.value), None
    raise SystemExit("🔴 eval_spotcheck.py 里找不到 SPOTCHECK 题集，无法做对照评测")


# ---------------------------------------------------------------- 计分（复用 v2 口径）


def _functions_from_source(names: Tuple[str, ...]) -> Dict[str, Any]:
    """直接从 eval_spotcheck.py 的源码里取这几个函数**原函数体**执行。

    🔴 为什么不用「抄一份等价实现」兜底：抄出来的口径迟早跟原函数漂，
    而这套评测的全部价值就在于「跟历史快照可比」。所以宁可 AST 取源码原体。
    """
    src = SPOTCHECK_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    missing = set(names) - {n.name for n in wanted}
    if missing:
        raise SystemExit(f"🔴 eval_spotcheck.py 里找不到函数 {sorted(missing)}")
    # 用原文件的 imports + 这几个函数体组一个新模块 ⇒ 拿到的就是原实现
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    module = ast.Module(body=[*imports, *wanted], type_ignores=[])
    ns: Dict[str, Any] = {"__name__": "ks_eval_spotcheck_scorers"}
    exec(compile(ast.fix_missing_locations(module), str(SPOTCHECK_PATH), "exec"), ns)  # noqa: S102
    return {n: ns[n] for n in names}


def make_scorers(mod: Any):
    """拿到 eval_spotcheck 的 hits / first_rank（同名同实现，不另造）。"""
    hits = getattr(mod, "hits", None) if mod else None
    first_rank = getattr(mod, "first_rank", None) if mod else None
    if hits and first_rank:
        return hits, first_rank
    got = _functions_from_source(("hits", "first_rank"))
    print("ℹ️  直接从 eval_spotcheck.py 源码取了 hits/first_rank 原实现（模块未 import）")
    return got["hits"], got["first_rank"]


# ---------------------------------------------------------------- 后端构造


def build_redis(cfg: Dict[str, Any], is_primary: bool = True):
    from keepsake.embedder import create_embedder
    from keepsake.storage import RedisStorage

    ec = dict(cfg.get("embedder") or {})
    embedder = None
    if ec.get("model"):
        embedder = create_embedder(
            provider=ec.get("provider", ""), api_key=ec.get("api_key", ""),
            base_url=ec.get("base_url", ""), model=ec.get("model", ""),
        )
    return RedisStorage(
        host=cfg.get("redis_host", "127.0.0.1"),
        port=int(cfg.get("redis_port", 6379)),
        password=cfg.get("redis_password") or None,
        embedder=embedder,
        is_primary=is_primary,
    )


def build_pg(cfg: Dict[str, Any], is_primary: bool = True):
    from keepsake.embedder import create_embedder
    from keepsake.storage_pg import PgStorage

    pg_cfg = (cfg.get("storage") or {}).get("postgres") or {}
    if not isinstance(pg_cfg, dict):
        pg_cfg = {}
    ec = dict(cfg.get("embedder") or {})
    embedder = None
    if ec.get("model"):
        embedder = create_embedder(
            provider=ec.get("provider", ""), api_key=ec.get("api_key", ""),
            base_url=ec.get("base_url", ""), model=ec.get("model", ""),
        )
    return PgStorage(
        host=str(pg_cfg.get("host", "127.0.0.1")),
        port=int(pg_cfg.get("port", 5432)),
        dbname=str(pg_cfg.get("dbname", "keepsake")),
        user=str(pg_cfg.get("user", "")),
        password=str(pg_cfg.get("password", "")),
        sslmode=str(pg_cfg.get("sslmode", "")),
        agent_id=str(cfg.get("agent_id", "")),
        embedder=embedder,
        is_primary=is_primary,
    )


def load_config(path: str) -> Dict[str, Any]:
    if not path:
        path = os.environ.get("KEEPSAKE_CONFIG",
                              os.path.expanduser("~/.config/keepsake/config.json"))
    if not os.path.exists(path):
        raise SystemExit(f"🔴 找不到配置 {path}；用 --config 指定")
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 主流程


def run_one(storage, queries, hits_fn, rank_fn) -> List[Dict[str, Any]]:
    rows = []
    for query, expect in queries:
        try:
            res = storage.search(query) or []
        except Exception as e:
            print(f"  ⚠️ 查询失败（该题记为失败，不拖垮整轮）: {query} — "
                  f"{type(e).__name__}: {e}")
            rows.append({"query": query, "ok": False, "rank": None, "n": 0,
                         "error": type(e).__name__})
            continue
        contents = [r.get("content", "") or "" for r in res]
        rows.append({
            "query": query,
            "n": len(contents),
            "top5": bool(hits_fn(contents, expect, 5)),
            "top15": bool(hits_fn(contents, expect, 15)),
            "rank": rank_fn(contents, expect),
            "error": None,
        })
    return rows


def _summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ranks = [r["rank"] for r in rows if r.get("rank")]
    return {
        "命中数": f"{sum(1 for r in rows if r['top5'])}/{len(rows)}",
        "top15": f"{sum(1 for r in rows if r['top15'])}/{len(rows)}",
        "rank@1": f"{sum(1 for r in ranks if r == 1)}/{len(ranks) if ranks else 0}",
        "中位排名": sorted(ranks)[len(ranks) // 2] if ranks else 0,
        "失败题": sum(1 for r in rows if r.get("error")),
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Redis vs PG 检索并排对照评测")
    ap.add_argument("--config", default=os.environ.get("KEEPSAKE_CONFIG", ""),
                    help="keepsake config.json 路径")
    ap.add_argument("--backend", choices=["both", "redis", "pg"], default="both",
                    help="跑哪一端（默认 both）")
    ap.add_argument("--top", type=int, default=0, help="只跑前 N 题（0 = 全部）")
    ap.add_argument("--json", default="", help="把结果落成 json（供后续脚本处理）")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    spotcheck, mod = load_spotcheck()
    hits_fn, rank_fn = make_scorers(mod)
    queries = spotcheck[:args.top] if args.top else spotcheck
    print(f"题集：eval_spotcheck.py v2 的 {len(queries)} 题（口径未另造）\n")

    results: Dict[str, List[Dict[str, Any]]] = {}
    backends: Dict[str, Any] = {}
    if args.backend in ("both", "redis"):
        backends["Redis"] = build_redis(cfg)
        if backends["Redis"].health_check() is not True:
            raise SystemExit("🔴 Redis 连不上 —— 对照评测必须两端都出结果，"
                             "只跑一半比不跑更危险")
    if args.backend in ("both", "pg"):
        backends["PG"] = build_pg(cfg)
        if backends["PG"].health_check() is not True:
            raise SystemExit("🔴 PostgreSQL 连不上（先跑 migrate_redis_to_pg.py 灌数据）")

    t0 = time.time()
    try:
        for name, storage in backends.items():
            print(f"▶ 跑 {name} …", flush=True)
            results[name] = run_one(storage, queries, hits_fn, rank_fn)
    finally:
        for storage in backends.values():
            storage.close()

    names = list(results)
    _print_table(queries, results, names)

    print()
    summaries = {}
    for name in names:
        summaries[name] = _summary(results[name])
        s = summaries[name]
        print(f"{name:<8} 命中(取) {s['命中数']}  ·  top15 {s['top15']}  ·  "
              f"rank@1 {s['rank@1']}  ·  中位排名 {s['中位排名']}  ·  失败 {s['失败题']} 题")

    if len(names) == 2:
        a, b = names
        both = sum(1 for x, y in zip(results[a], results[b]) if x["top5"] and y["top5"])
        only_a = sum(1 for x, y in zip(results[a], results[b]) if x["top5"] and not y["top5"])
        only_b = sum(1 for x, y in zip(results[a], results[b]) if y["top5"] and not x["top5"])
        print(f"\n逐题对比（top5 口径）：两端都命中 {both} 题 · "
              f"仅 {a} 命中 {only_a} 题 · 仅 {b} 命中 {only_b} 题")
        for x, y in zip(results[a], results[b]):
            if x["top5"] != y["top5"]:
                print(f"  {'▶' if x['top5'] else '  '} {a:<5} | {'▶' if y['top5'] else '  '} "
                      f"{b:<5} | {x['query']}")

    print(f"\n耗时 {time.time() - t0:.1f}s")
    print("📌 解读提醒：话题榜/注意力/同义词**未**随迁移搬进 PG ⇒ PG 侧重排加权走"
          "默认值，比**命中集合**可以，比**分数/名次**要留神。")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"queries": [q for q, _ in queries], "results": results,
             "summaries": summaries}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"结果已落盘：{args.json}")
    return 0


def _print_table(queries, results: Dict[str, List[Dict[str, Any]]], names: List[str]) -> None:
    """并排表。列太多时降级成上下两段（同样的口径，只是排版不同）。"""
    wide = 26 * len(names) + 34
    if wide > 120:
        print("─" * 100)
        for name in names:
            print(f"【{name}】")
            for q, row in zip(queries, results[name]):
                flag = "✓" if row["top5"] else ("·" if not row["error"] else "✗")
                print(f"  {flag} rank={str(row['rank'] or '-'):>3}  命中={row['n']:>2}  {q}")
            print()
        return
    header = f"{'查询':<22}" + "".join(f"{n + '(取/rank@1)':<26}" for n in names)
    print(header)
    print("─" * len(header))
    for i, (q, _) in enumerate(queries):
        cells = ""
        for name in names:
            row = results[name][i]
            mark = "✓" if row["top5"] else ("·" if not row["error"] else "✗")
            cells += f"{mark} {row['n']:>2}条 rank{str(row['rank'] or '-'):>3}".ljust(26)
        print(f"{q:<22}{cells}")


if __name__ == "__main__":
    sys.exit(main())
