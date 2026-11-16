"""同义词「巨型组」防线（2026-11 synonym_giant_group_guard）。

生产事故：`ks_synonym` 被定时同义词总线合图失控熔毁 —— 3688 组里 1045 组同义词数
>50，最大一组 840 个同义词（组头 task/ssh/agent/the）。检索层按组做查询扩展，
巨型组把 BM25 查询式撑成几百词 ⇒ 搜索精度实际塌方。

本文件钉死三道防线（闸门对象）：
  1. 检索侧：规模超上限的组**整体跳过**（不扩展、不截断）；
  2. 总线侧：合并前并查集探测，目标组规模将超上限 ⇒ **整次合并跳过**并计数；
  3. 正常规模的组**不受影响**（防线不许把正常同义词也毙了）。

外加一条：同义并入必须**同时**满足共现次数下限（改前 Jaccard 那条路径形同虚设）。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Set

from keepsake.core.shared import (
    MAX_SYN_GROUP,
    MIN_COOCCURRENCES,
    _expand_terms,
    discover_synonym_pairs,
    merge_synonym_maps_guarded,
)


def _components(merged: Dict[str, set]) -> List[Set[str]]:
    """合并结果 → 连通分量列表（组 = 同义词图上的连通分量，按词数计）。"""
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for term, syns in merged.items():
        for syn in syns:
            union(term, syn)
    groups: Dict[str, Set[str]] = {}
    for node in list(parent):
        groups.setdefault(find(node), set()).add(node)
    return list(groups.values())


def _chain(words: Iterable[str]) -> Dict[str, set]:
    """`w0-w1-w2-…` 链式泛连（同义词图的一条链 ⇒ 一个大连通分量）。"""
    ws = list(words)
    out: Dict[str, set] = {}
    for i, w in enumerate(ws):
        syns: Set[str] = set()
        if i > 0:
            syns.add(ws[i - 1])
        if i < len(ws) - 1:
            syns.add(ws[i + 1])
        out[w] = syns
    return out


# ---------------------------------------------------------------- 检索侧防御

def test_巨型组整组跳过不扩展():
    """🔴 生产形态复现：100 个同义词/组 ⇒ 该组**一个词都不许扩**。

    截断成 8 个不算数（换个姿势继续炸检索）—— 整组跳过。
    """
    giant = {"task": {f"syn{i}" for i in range(100)}}
    assert _expand_terms(["task"], giant) == ["task"], "巨型组必须整组跳过扩展"

    # 组外的普通词照常扩展（防线不许误伤）
    normal = {"task": {f"syn{i}" for i in range(100)}, "普通词": {"别名"}}
    out = _expand_terms(["普通词"], normal)
    assert set(out) == {"普通词", "别名"}, out


def test_正常规模的组照常扩展():
    """3 个词的合法组正常扩展命中（防线的另一半：别把正常同义词也毙了）。"""
    syn_map = {"记忆": {"回忆", "remember"}}
    out = _expand_terms(["记忆"], syn_map)
    assert set(out) == {"记忆", "回忆", "remember"}, out
    # 组规模恰好等于上限 ⇒ 仍应扩展（上限是「>」不是「>=」）
    at_limit = {"词": {f"s{i}" for i in range(MAX_SYN_GROUP)}}
    assert len(_expand_terms(["词"], at_limit)) == MAX_SYN_GROUP + 1


# ---------------------------------------------------------------- 总线侧上限

def test_超上限的合并被跳过且没有组超过上限():
    """喂一条 20 词的链式泛连 ⇒ 必须有合并被跳过，且**没有任何组超过上限**。"""
    merged, skipped = merge_synonym_maps_guarded({}, _chain(f"w{i}" for i in range(20)))

    assert skipped > 0, "超上限的合并一次都没跳过 ⇒ 上限没生效"
    for group in _components(merged):
        assert len(group) <= MAX_SYN_GROUP, f"组规模超上限: {sorted(group)}"
    # 每词的同义表长度也跟着受限（组内最多 7 个同伴）
    for term, syns in merged.items():
        assert len(syns) <= MAX_SYN_GROUP, (term, sorted(syns))


def test_上限可覆写且正常组合不被牵连():
    """上限是参数（可覆写）；正常规模的组合照常并进来。"""
    merged, skipped = merge_synonym_maps_guarded({}, _chain(["甲", "乙", "丙"]),
                                                 max_group=MAX_SYN_GROUP)
    assert skipped == 0
    assert merged["甲"] == {"乙"} and merged["乙"] == {"甲", "丙"}

    small, skipped_small = merge_synonym_maps_guarded({}, _chain(["x", "y", "z", "w"]),
                                                      max_group=2)
    assert skipped_small > 0, "上限调小后超限的合并必须被跳过"
    for group in _components(small):
        assert len(group) <= 2, sorted(group)


# ---------------------------------------------------------------- 可信度门槛

def test_同义并入必须同时满足共现下限():
    """共现够但 Jaccard 不够 ⇒ **不成对**（改前走「共现」路径被并入）。

    这正是 hub 式泛连的入口：'the' 与任意低频词共现几次就成同义 ⇒ 链式串成
    840 词/组的巨型组。
    """
    word_freq = {"配置": 100, "部署": 100}
    pair = tuple(sorted(("配置", "部署")))     # 共现计数键按词序（与 accumulate_co_occurrence 同口径）
    weak = {pair: 3}                           # Jaccard = 3/197 ≈ 0.015 < 0.5
    m, groups = discover_synonym_pairs(word_freq, weak, 1, 0.5, 3)
    assert groups == 0 and m == {}, m

    strong = {pair: 100}                       # Jaccard = 1.0，共现 100 ≥ 门槛
    m2, groups2 = discover_synonym_pairs(word_freq, strong, 1, 0.5, 3)
    assert groups2 == 1 and m2["配置"] == {"部署"}, m2


def test_共现门槛字段无效时用兜底常量():
    """门槛字段形同虚设（<=1）⇒ 用 `MIN_COOCCURRENCES` 顶替（取值出处见常量注释）。"""
    assert MIN_COOCCURRENCES == 5
    pair = tuple(sorted(("配置", "部署")))
    # Jaccard 拉满（两词共现于全部语料），只让「共现下限」起决定作用
    m, groups = discover_synonym_pairs({"配置": 2, "部署": 2}, {pair: 2}, 1, 0.5, 1)
    assert groups == 0 and m == {}, "字段 = 1（共现一次即算同义）⇒ 实际门槛应是 5"

    m2, groups2 = discover_synonym_pairs({"配置": 5, "部署": 5}, {pair: 5}, 1, 0.5, 1)
    assert groups2 == 1 and m2["配置"] == {"部署"}, "共现 5 次 ⇒ 兜底门槛下成立"
