#!/usr/bin/env python3
"""Keepsake 记忆检索抽查集 v2 — 带度量纪律（防自欺）

相对 v1 的四项升级（纪律来源：技能 trustworthy-evaluation）：
  1. **oracle 过滤坏题**：期望关键词若在库内根本查不到，该题是坏题 → 标 unanswerable，不计入分母。
  2. **两档都报**：`top5`（严格）与 `top15`（放宽，≈「允许再看一批」）。只报一档会掩盖「排第几」的信息。
  3. **噪声地板**：同一批题跑两遍看抖动（确定性）；再加一组**只改措辞**的对照臂。
     实测地板 ≈ 同义改写带来的百分点差，小于地板的「提升」一律当平局。
  4. **赢/输题数**：与上一次快照逐题对比，输出 won/lost 明细，而不是只给一个百分比。

另外：单条查询失败不拖垮整轮（逐题 try/except）；结果快照落在仓库**之外**（`--out`），
仓库里只留代码与题集——真实会话/检索结果不进仓库。

用法:
  python3 eval_spotcheck.py                 # 跑并打印
  python3 eval_spotcheck.py --save          # 跑并落快照（供下次对比）
  python3 eval_spotcheck.py --baseline X.json
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, "/opt/fragmented-memory/src")

CFG = json.load(open(os.path.expanduser("~/.config/keepsake/config.json")))
DEFAULT_OUT_DIR = os.path.expanduser("~/ks_eval_runs")

# 抽查集: (查询, 期望关键词列表) — 基于记忆库真实内容
SPOTCHECK = [
    ("服务器密码", ["密码", "Redis"]),
    ("gost 代理怎么配", ["gost", "8443", "202"]),
    ("部署流程是什么", ["部署", "rsync", "88"]),
    # 2026-09-20 移除「FLUX 模型在哪下载」：oracle 判为坏题——FLUX/flux-fp8/Kijai 在 keepsake
    # 记忆库里 0 命中（该知识在技能库不在记忆库），拿它评测 keepsake 召回等于用错题库。
    ("缠论怎么分析", ["缠论"]),
    ("数据库密码", ["密码", "MySQL", "MongoDB"]),
    ("202 服务器上有什么", ["202", "生产", "quartz"]),
    ("记忆插件状态", ["记忆", "keepsake", "Keepsake"]),
    ("定时任务有哪些", ["定时", "cron", "提炼"]),
    ("语义检索怎么实现的", ["语义", "embedding", "检索"]),
    ("怎么给 88 派活", ["88", "agent-worker", "任务"]),
    ("生图怎么弄", ["ComfyUI", "生图", "pipeline"]),
    ("怎么备份数据", ["备份", "数据库"]),
    ("量化回测", ["回测", "量化", "缠论"]),
    ("ClickHouse 数据", ["ClickHouse", "clickhouse"]),
    ("内存 swap 优化", ["swappiness", "swap"]),
    ("前端部署到哪", ["前端", "/opt/web", "web"]),
    ("项目放在哪个目录", ["claude_user", "/home/claude_user"]),
    ("记忆怎么自动提炼", ["提炼", "memory_distill", "qwen"]),
    ("PVE 上有什么服务", ["PVE", "容器", "ComfyUI"]),
    ("记忆提炼怎么跑的", ["提炼", "qwen", "distill"]),
    ("RRF 融合排序是什么", ["RRF", "融合", "排序"]),
    ("记忆去重任务", ["去重", "dedup"]),
    ("主脑的 IP 是什么", ["主脑", "ip", "IP"]),
    ("Redis 怎么守护的", ["Redis", "守护", "Restart"]),
    ("state.db 怎么恢复的", ["state.db", "恢复", "损坏"]),
    ("抖音视频分析", ["抖音", "视频", "王洋"]),
    ("创业计划书", ["创业", "计划", "数字遗产"]),
    ("agent-worker 是什么", ["agent-worker", "调度", "任务"]),
    ("embedding 模型用的什么", ["nomic", "embedding", "768"]),
]

# 措辞对照臂：只改问法、不改意图（用来量噪声地板）。
# 覆盖前 12 题，其余题不参与措辞对照（分母按实际参评题数算）。
PARAPHRASE = {
    "服务器密码": "服务器的密码是多少",
    "gost 代理怎么配": "gost 代理的配置方法",
    "部署流程是什么": "部署的流程是怎样的",
    "FLUX 模型在哪下载": "从哪里下载 FLUX 模型",
    "缠论怎么分析": "如何用缠论做分析",
    "数据库密码": "数据库的密码",
    "202 服务器上有什么": "202 这台服务器上跑了什么",
    "记忆插件状态": "记忆插件现在什么状态",
    "定时任务有哪些": "有哪些定时任务",
    "语义检索怎么实现的": "语义检索是如何实现的",
    "怎么给 88 派活": "如何给 88 派活",
    "生图怎么弄": "生图要怎么做",
}


def build_storage():
    from keepsake.storage import RedisStorage
    from keepsake.embedder import create_embedder

    ec = CFG.get("embedder", {}) or {}
    embedder = None
    if ec.get("provider"):
        embedder = create_embedder(
            provider=ec.get("provider", ""),
            api_key=ec.get("api_key", ""),
            base_url=ec.get("base_url", ""),
            model=ec.get("model", ""),
        )
    return RedisStorage(
        host=CFG.get("redis_host", "127.0.0.1"),
        port=CFG.get("redis_port", 6379),
        password=CFG.get("redis_password") or None,
        embedder=embedder,
        is_primary=True,
    )


def search_safe(stor, query):
    """单条失败不拖垮整轮。"""
    try:
        res = stor.search(query)
        return [r.get("content", "") or "" for r in (res or [])]
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 查询失败（已跳过该题）: {query} — {type(exc).__name__}: {exc}")
        return None


def hits(contents, expect, top_n):
    top = [c.lower() for c in contents[:top_n]]
    return [k for k in expect if any(k.lower() in c for c in top)]


def first_rank(contents, expect):
    """第一个命中期望关键词的结果排在第几（1 基）；未命中返回 None。

    这是**有区分度**的指标：top5 通过率接近饱和时（27/29 与 27/29 无差别），
    排名仍能区分「排第 1」和「排第 4」。
    """
    for i, c in enumerate(contents, start=1):
        low = c.lower()
        if any(k.lower() in low for k in expect):
            return i
    return None


def oracle_ok(stor, expect):
    """oracle：直接拿期望关键词查，库内真的存在才说明这题可答。"""
    for kw in expect:
        got = search_safe(stor, kw)
        if got and any(kw.lower() in c.lower() for c in got[:15]):
            return True, kw
    return False, ""


def run_arm(stor, queries, top_n, label):
    rows = []
    for query, expect in queries:
        contents = search_safe(stor, query)
        if contents is None:
            rows.append({"query": query, "ok": False, "error": True, "hit": [], "rank": None})
            continue
        hk = hits(contents, expect, top_n)
        rows.append({"query": query, "ok": bool(hk), "hit": hk,
                     "rank": first_rank(contents, expect), "n_results": len(contents)})
    passed = sum(1 for r in rows if r["ok"])
    ranks = [r["rank"] for r in rows if r.get("rank")]
    r1 = sum(1 for r in ranks if r == 1)
    med = sorted(ranks)[len(ranks) // 2] if ranks else 0
    print(f"[{label}] {passed}/{len(rows)} ({passed / max(len(rows), 1) * 100:.1f}%)"
          f" · rank@1 {r1}/{len(ranks)} · 中位排名 {med}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true", help="落快照供下次逐题对比")
    ap.add_argument("--baseline", default="", help="上一次快照 json 路径")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR, help="快照目录（必须在仓库之外）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if os.path.abspath(args.out).startswith(os.path.abspath(os.path.dirname(__file__))):
        sys.exit("--out 不能落在仓库内（真实检索结果不进仓库）")

    stor = build_storage()
    t0 = time.time()

    # ---- 1. oracle 过滤坏题 ----
    answerable, bad = [], []
    for query, expect in SPOTCHECK:
        ok, kw = oracle_ok(stor, expect)
        (answerable if ok else bad).append((query, expect))
        if not ok:
            print(f"  ⚠️ 坏题（期望关键词库内查不到，不计入分母）: {query} → {expect}")
    print(f"oracle: {len(answerable)}/{len(SPOTCHECK)} 题可答；剔除 {len(bad)} 题坏题\n")

    # ---- 2. 两档 + 重跑稳定性（重跑 3 次 → 量出本抽查集自己的抖动地板）----
    print("=== 主臂 ===")
    strict = run_arm(stor, answerable, 5, "top5  #1")
    relaxed = run_arm(stor, answerable, 15, "top15")
    runs = [strict, run_arm(stor, answerable, 5, "top5  #2"),
            run_arm(stor, answerable, 5, "top5  #3")]
    flap = [a["query"] for a, b in zip(runs[0], runs[1]) if a["ok"] != b["ok"]]
    print(f"重跑抖动题数: {len(flap)}" + (f" → {flap}" if flap else ""))

    def _rate(rows, key):
        if key == "pass":
            return sum(1 for r in rows if r["ok"]) / max(len(rows), 1) * 100
        rk = [r["rank"] for r in rows if r.get("rank")]
        return sum(1 for x in rk if x == 1) / max(len(rk), 1) * 100

    pass_rates = [_rate(r, "pass") for r in runs]
    rank_rates = [_rate(r, "rank") for r in runs]
    floor_pass = max(pass_rates) - min(pass_rates)
    floor_rank_rerun = max(rank_rates) - min(rank_rates)
    print(f"重跑地板：通过率 {floor_pass:.1f} 点（{['%.1f' % x for x in pass_rates]}）"
          f" · rank@1 {floor_rank_rerun:.1f} 点（{['%.1f' % x for x in rank_rates]}）")
    s_pass = sum(1 for r in strict if r["ok"])
    r_pass = sum(1 for r in relaxed if r["ok"])

    # ---- 3. 措辞对照臂 → 噪声地板 ----
    para_queries = [(q, dict(answerable).get(q) or dict(SPOTCHECK)[q]) for q in PARAPHRASE
                    if q in dict(answerable)]
    print("\n=== 措辞对照臂（只改问法）===")
    para = run_arm(stor, [(PARAPHRASE[q], e) for q, e in para_queries], 5, "top5 改写")
    orig_subset = [r for r in strict if r["query"] in {q for q, _ in para_queries}]
    p_orig = sum(1 for r in orig_subset if r["ok"])
    p_para = sum(1 for r in para if r["ok"])
    saturated = (p_orig == len(orig_subset) and p_para == len(para))
    floor = abs(p_orig - p_para) / max(len(orig_subset), 1) * 100
    # 子集饱和（两边全对）时通过率量不出地板，改用 rank@1 差当地板——排名仍有区分度。
    rk_orig = [r["rank"] for r in orig_subset if r.get("rank")]
    rk_para = [r["rank"] for r in para if r.get("rank")]
    r1_orig = sum(1 for x in rk_orig if x == 1)
    r1_para = sum(1 for x in rk_para if x == 1)
    floor_rank = abs(r1_orig - r1_para) / max(len(rk_orig), 1) * 100
    print(f"原问法 {p_orig}/{len(orig_subset)} vs 改写 {p_para}/{len(para)}"
          f" → 通过率地板 {'n/a（子集饱和，两边全对）' if saturated else f'{floor:.1f} 个百分点'}")
    print(f"rank@1 原问法 {r1_orig}/{len(rk_orig)} vs 改写 {r1_para}/{len(rk_para)}"
          f" → 措辞地板 {floor_rank:.1f} 个百分点")
    # 最终地板 = 重跑抖动与措辞敏感度取大者（通过率指标与排名指标各自独立）
    floor = max(floor_pass, floor_rank_rerun, floor_rank)
    print(f"→ **本抽查集地板 = {floor:.1f} 个百分点**"
          f"（重跑通过率 {floor_pass:.1f} / 重跑 rank@1 {floor_rank_rerun:.1f} / 措辞 {floor_rank:.1f}）")

    # ---- 4. 与基线逐题对比（赢/输）----
    won_lost = None
    if args.baseline and os.path.exists(args.baseline):
        base = json.load(open(args.baseline))
        bmap = {r["query"]: r["ok"] for r in base.get("strict", [])}
        won = [r["query"] for r in strict if r["ok"] and bmap.get(r["query"]) is False]
        lost = [r["query"] for r in strict if (not r["ok"]) and bmap.get(r["query"]) is True]
        won_lost = {"won": won, "lost": lost}
        # 口径：本次 vs 基线的**同一指标**（都用 top5 通过率），不拿子集比全集
        cur_rate = s_pass / max(len(strict), 1) * 100
        base_txt = str(base.get("strict_top5", "?"))
        try:
            bn, bd = (int(x) for x in base_txt.split("/"))
            base_rate = bn / max(bd, 1) * 100
        except Exception:
            base_rate = float("nan")
        delta = cur_rate - base_rate
        print(f"\n=== 对基线（{os.path.basename(args.baseline)}）===")
        print(f"top5: {base_txt} → {s_pass}/{len(strict)}，变化 {delta:+.1f} 个百分点")
        print(f"赢 {len(won)} 题 / 输 {len(lost)} 题" + ("" if (won or lost) else "（逐题无变化）"))
        if won:
            print(f"  赢: {won}")
        if lost:
            print(f"  输: {lost}")
        verdict = "平局" if abs(delta) <= floor else ("提升" if delta > 0 else "退化")
        print(f"判定：{verdict}（|{delta:+.1f}| vs 地板 {floor:.1f} 个百分点）")

    # ---- 汇总 ----
    s_ranks = [r["rank"] for r in strict if r.get("rank")]
    summary = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "oracle": {"answerable": len(answerable), "bad": [q for q, _ in bad]},
        "strict_top5": f"{s_pass}/{len(strict)}",
        "relaxed_top15": f"{r_pass}/{len(relaxed)}",
        "rank_at_1": f"{sum(1 for x in s_ranks if x == 1)}/{len(s_ranks)}",
        "median_rank": (sorted(s_ranks)[len(s_ranks) // 2] if s_ranks else 0),
        "noise_floor_pp": round(floor, 1),
        "rerun_flap": flap,
        "won_lost": won_lost,
        "elapsed_s": round(time.time() - t0, 1),
    }
    print("\n=== 汇总 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.save:
        os.makedirs(args.out, exist_ok=True)
        path = os.path.join(args.out, f"spotcheck-{time.strftime('%Y%m%d-%H%M%S')}.json")
        json.dump({**summary, "strict": strict, "relaxed": relaxed, "paraphrase": para},
                  open(path, "w"), ensure_ascii=False, indent=1)
        print(f"\n快照: {path}")
        latest = os.path.join(args.out, "latest.json")
        json.dump({**summary, "strict": strict, "relaxed": relaxed, "paraphrase": para},
                  open(latest, "w"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
