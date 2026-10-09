"""PG 侧 BM25 打分的可运行自检（无真库也能跑）。

背景（2026-10 ks_pg_bm25 单）：PG 侧原来用 `ts_rank_cd` 当相关性分。它是
**覆盖密度**，输出离散且大量并列 —— 同分簇挤在一起，归一化后 `_sim` 这一维
失去分辨率，名次被时间衰减/情绪/热词这些**非相关性**维度决定。本单把打分换成
Python 侧的真 BM25（k1=1.2 / b=0.75，对齐 RediSearch），**召回不变**。

这些断言钉的是三件最容易悄悄坏掉的事：
  1. BM25 的**数值**（手算常量，别让它无声漂走）；
  2. 可分辨性 —— 词频/长度不同 ⇒ 必须给出不同分（ts_rank_cd 在这里给的是同一个值）；
  3. 语料统计**没有 N+1** —— df 是一次聚合拿齐的，不随候选条数线性增长往返。

无真库依赖：连接被 `_FakeCursor` 顶掉，只看发出的 SQL 与算出来的分。
"""
from __future__ import annotations

import math
from typing import Any, List

import pytest

from keepsake.storage_pg import BM25_B, BM25_K1, PgStorage, _parse_tfs, bm25_score
from keepsake.storage_shared import SEARCH_FIELDS


# --------------------------------------------------------------------------
# 1. BM25 数值：手算常量
# --------------------------------------------------------------------------

def test_bm25_matches_hand_computed_value():
    """单个词元：score = idf * tf*(k1+1) / (tf + k1*(1-b+b*dl/avgdl))。

    取 N=10 / df=2 / tf=3 / dl=20 / avgdl=20，k1=1.2 / b=0.75：
      idf   = ln(1 + (10-2+0.5)/(2+0.5)) = ln(4.2)
      denom = 3 + 1.2*(1-0.75+0.75*20/20) = 3 + 1.2
    """
    n_docs, df, tf, dl, avgdl = 10.0, 2.0, 3.0, 20.0, 20.0
    want_idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
    want = want_idf * (tf * (BM25_K1 + 1.0)) / (tf + BM25_K1 * (1.0 - BM25_B + BM25_B * dl / avgdl))
    got = bm25_score({"部署": tf}, dl, n_docs, avgdl, {"部署": df})
    assert got == pytest.approx(want, rel=1e-12)
    assert got == pytest.approx(want_idf * 3 * 2.2 / 4.2, rel=1e-12)


def test_bm25_is_zero_on_degenerate_inputs():
    """缺统计（avgdl=0 / N=0 / df 缺失）必须返回 0 而不是抛错或除零。"""
    assert bm25_score({"a": 1}, 10, 10, 0.0, {"a": 2}) == 0.0
    assert bm25_score({"a": 1}, 10, 0, 5.0, {"a": 2}) == 0.0
    assert bm25_score({"a": 1}, 10, 10, 5.0, {}) == 0.0
    assert bm25_score({}, 10, 10, 5.0, {"a": 2}) == 0.0


# --------------------------------------------------------------------------
# 2. 可分辨性 —— 本单的核心判据（改造前 ts_rank_cd 在这里是全并列）
# --------------------------------------------------------------------------

def test_bm25_separates_docs_that_coverage_density_cannot():
    """三条文档命中的**查询词集合完全一样**（ts_rank_cd 给同一个分），
    只有词频和长度不同 ⇒ BM25 必须给出三个互不相同的分。

    这就是缺陷本身：`_sim` 只有在这一维有分辨率，时间衰减/情绪这些
    非相关性维度才有资格做「相关性同分时」的 tie-break，而不是独自决定名次。
    """
    dfs = {"前端": 3.0, "部署": 3.0}
    n_docs, avgdl = 10.0, 20.0
    short_few = bm25_score({"前端": 1, "部署": 1}, 8, n_docs, avgdl, dfs)     # 短、词频低
    short_many = bm25_score({"前端": 5, "部署": 4}, 8, n_docs, avgdl, dfs)    # 同样短、词频高
    long_same = bm25_score({"前端": 1, "部署": 1}, 60, n_docs, avgdl, dfs)    # 同样词频、文档长

    assert len({round(short_few, 12), round(short_many, 12), round(long_same, 12)}) == 3, (
        "同一覆盖集上的三篇文档必须拿到三个不同的 BM25 分"
    )
    assert short_many > short_few > long_same, (
        "词频高的应更高；词频相同时短的应更高（长度归一化生效）"
    )


def test_bm25_is_monotonic_in_tf_and_against_length():
    """词频单调递增、长度单调惩罚 —— BM25 的两条基本性质。"""
    dfs, n_docs, avgdl = {"x": 2.0}, 20.0, 10.0
    scores = [bm25_score({"x": t}, 10, n_docs, avgdl, dfs) for t in (1, 2, 3, 8, 20)]
    assert scores == sorted(scores)
    lens = [bm25_score({"x": 2}, dl, n_docs, avgdl, dfs) for dl in (2, 10, 50, 200)]
    assert lens == sorted(lens, reverse=True)


def test_bm25_never_goes_negative_for_ubiquitous_terms():
    """df 接近 N 时用 RediSearch 的 `ln(1 + ...)` 变体 ⇒ idf 恒正。

    用 Lucene 的 `ln((N-df+0.5)/(df+0.5))` 变体时 df=N 会让 idf 变负，
    于是「每篇都含的查询词」会把**长文档**顶上来。这里必须钉死。
    """
    for df in (1.0, 5.0, 19.0, 20.0):
        assert bm25_score({"x": 1}, 100, 20.0, 10.0, {"x": df}) > 0.0, f"df={df}"


# --------------------------------------------------------------------------
# 3. 统计来源：tf / doclen 的解析与「不 N+1」
# --------------------------------------------------------------------------

def test_parse_tfs_handles_text_array_and_text_literal():
    assert _parse_tfs(["前端:3", "部署:1"]) == {"前端": 3.0, "部署": 1.0}
    assert _parse_tfs("{前端:3,部署:1}") == {"前端": 3.0, "部署": 1.0}
    assert _parse_tfs(None) == {}
    assert _parse_tfs([]) == {}
    assert _parse_tfs(["坏值"]) == {}, "没有 ':' 的项必须丢掉而不是让整条查询炸掉"


class _FakeCursor:
    """记录 SQL 与参数的假游标；每次 execute 顺次取 FakeDb 队列里的下一份结果。"""

    def __init__(self, db: "FakeDb"):
        self.db = db

    def execute(self, sql, params=None):
        self.db.statements.append((" ".join(sql.split()), list(params or [])))
        self.db.rows = self.db.queue.pop(0) if self.db.queue else []

    def fetchall(self):
        return self.db.rows

    def fetchone(self):
        return self.db.rows[0] if self.db.rows else None


class FakeDb:
    """结果队列按「execute 的发生次序」消费 —— 一次 `_ro()` 里的两条 SQL 也能各拿各的。"""

    def __init__(self, queue):
        self.statements: List[Any] = []
        self.queue = list(queue)       # 每个元素 = 一次 fetch 的结果集
        self.rows: List[tuple] = []

    def ro(self):
        return _Ctx(self)


class _Ctx:
    def __init__(self, db: FakeDb):
        self.db = db

    def __enter__(self):
        self.cur = _FakeCursor(self.db)
        return self.cur

    def __exit__(self, *exc):
        return False


def _row(key, doclen, tfs):
    """一行**薄候选**（2026-10 ks_pgr 之后候选阶段不再回正文）。

    列序与 `_bm25_thin` 的 SELECT 对上：
      key, chash, created, sentiment_score, feedback_score, corrected, consumed,
      hot_w, attn_w, doclen, tfs, ts_rank_cd（最后一列是粗排分，会被 BM25 顶掉）。
    正文在 `_hydrate_thin()` 的第二条 SQL 里按 key 取回（`_frag_row`）。
    """
    return (key, f"{key}-hash", "2026-01-01T00:00:00+00:00", "0", "0", 0, 0,
            0.0, 0.0, doclen, tfs, 1.8)


def _frag_row(key, content):
    """一行 `_hydrate_thin` 的取回结果（FRAGMENT_COLUMNS 列序）。"""
    return (key, content, "shared", "", "", "2026-01-01T00:00:00+00:00",
            "0", "", "0", "", "fact", "", "", "", "", "", "")


def _fake_pg(monkeypatch, queue):
    """造一个不连库的 PgStorage；结果队列按 execute 次序消费。

    典型队列：同义词表([]) → 候选行 → 统计行。
    """
    pg = PgStorage(dsn="postgresql://unused/unused", is_primary=True,
                   bm25_limit=50, final_limit=50)
    db = FakeDb(queue)
    monkeypatch.setattr(pg, "_ro", db.ro)   # noqa: SLF001 — 测试内顶掉连接
    return pg, db


def test_search_bm25_fetches_corpus_stats_in_one_aggregate_not_per_document(monkeypatch):
    """🔴 **不 N+1**：df / N / avgdl 必须一条聚合 SQL 拿齐，
    往返次数不随候选条数增长。

    这条断言直接对着「别为每篇文档单独查一次 df」那条要求 ——
    写成 N+1 的实现在这里必然红（statements 数会随候选数线性涨）。
    """
    rows = [_row(f"k{i}", 20, ["前端:2", "部署:2"]) for i in range(30)]
    frags = [_frag_row(f"k{i}", f"前端部署到 /opt/web 第{i}版") for i in range(30)]
    # 六趟：同义词（空表）、热词榜（空）、注意力榜（空）、候选召回、语料统计、取回正文
    pg, db = _fake_pg(monkeypatch, [[], [], [], rows,
                                    [(50.0, 1000.0, {"前端": 5.0, "部署": 5.0})], frags])

    out = pg.search_bm25("前端部署")
    assert out, "应当返回候选"

    # 语料统计（df / N / avgdl）必须**只有一条**聚合 SQL —— 逐篇查 df 就是 N+1，
    # 候选从 1 条涨到 30 条时这里的计数会跟着涨。
    stats_calls = [(q, ps) for q, ps in db.statements if "GROUP BY t.lexeme" in q]
    assert len(stats_calls) == 1, (
        f"df/N/avgdl 必须一条聚合拿齐，实际发了 {len(stats_calls)} 条 —— "
        f"候选 30 条时按篇查 df 就是 N+1（实测 {len(db.statements)} 趟 SQL）"
    )
    stat, stat_params = stats_calls[0]
    assert stat.startswith("WITH corpus AS ("), "统计查询必须是一条（CTE + 子查询）的聚合语句"
    assert "json_object_agg(lexeme, df)" in stat, "df 必须聚合成一张 {词: df} 映射一次性取回"
    assert "cardinality(t.positions)" in stat, "doclen 必须取自 tsvector 的 positions"
    # 查询词一次传齐（`= ANY(...)`），不是一个词一趟
    lexemes_param = next(p for p in stat_params if isinstance(p, list))
    assert len(lexemes_param) == len(set(lexemes_param)), "查询词应一次传齐"


def test_search_bm25_scores_are_bm25_not_rank_cd(monkeypatch):
    """端到端：跑真实 `search_bm25`，检查 `_bm25_score` 等于手算 BM25，
    且**不再是全并列**（候选集相同、只有打分换了）。"""
    rows = [_row("k1", 20, ["前端:1", "部署:1"]),
            _row("k2", 26, ["前端:4", "部署:4"])]
    frags = [_frag_row("k1", "前端部署到 /opt/web"),
             _frag_row("k2", "前端部署到 /opt/web 前端部署 前端部署 前端部署")]
    pg, _db = _fake_pg(monkeypatch, [[], [], [], rows,
                                      [(2.0, 46.0, {"前端": 2.0, "部署": 2.0})], frags])

    out = pg.search_bm25("前端部署")
    assert {f["_key"] for f in out} == {"k1", "k2"}

    scores = {f["_key"]: f["_bm25_score"] for f in out}
    assert scores["k1"] == pytest.approx(
        bm25_score({"前端": 1.0, "部署": 1.0}, 20.0, 2.0, 23.0, {"前端": 2.0, "部署": 2.0}),
        rel=1e-12)
    assert scores["k2"] == pytest.approx(
        bm25_score({"前端": 4.0, "部署": 4.0}, 26.0, 2.0, 23.0, {"前端": 2.0, "部署": 2.0}),
        rel=1e-12)
    assert scores["k2"] > scores["k1"], "词频高 4 倍的那条必须排在前面（ts_rank_cd 分不出）"
    assert scores["k1"] != scores["k2"], "两条同覆盖文档绝不能同分"


def test_empty_query_and_sanitized_to_zero_terms_return_empty(monkeypatch):
    """空查询 / sanitize 后零词：明确返回空列表，不静默、也不打库。"""
    pg, db = _fake_pg(monkeypatch, [])

    assert pg.search_bm25("") == []
    assert pg.search_bm25("   ") == []
    assert pg.search_bm25("&& --") == [], "纯符号会被 sanitize 清空 ⇒ 空列表"
    assert not [x for x, _ in db.statements if "ts_rank_cd" in x or "corpus" in x], (
        "空查询/零词都不该发候选或统计 SQL")