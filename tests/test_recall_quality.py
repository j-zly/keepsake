"""召回质量（合成语料）—— keepsake_recall_eval_v1 的可运行检查。

本文件 **hermetic**：语料全部在测试里现造、只落 `tmp_path` 里的临时 SQLite 文件，
**不连任何外部服务、不写任何真实记忆库、不含任何真实记忆内容**（正文全是合成的
「交易系统运维」套话与占位词）。

要钉住的两个事实（本单验收锚点）：

  1. **主题相关的长条目能稳定进候选池** —— 生产实测 bm25_limit=20 时
     `trade-platform 回测(2026-09-22 …)`（该条里「回测」出现十几次、正文 400–1600 字）
     被挤在候选窗口之外，`search_bm25("StrategyCompare")` 返 0 条，而同一条记忆条目
     `content_tsv @@ plainto_tsquery('simple','回测')` = true ⇒ 在索引里，是召回环节丢的。
     合成语料按同一形状复现：目标条目 = 长正文 + 高词频；干扰项 = 短正文 + 标签命中，
     **占满候选窗口但正文没有查询词**。断言：改后 Recall@15 ≥ 0.9。
  2. **同一查询连跑多次结果完全一致** —— 重排分是浮点连乘、同分极常见，
     `list.sort` 稳定 ⇒ 并列项的相对名次会随输入次序飘。断言：同查询连跑 3 次，
     命中的 key 序列逐字相同。

另有一条独立用例钉住**查询侧与文档侧分词口径一致**（`storage_shared.query_lexemes`）：
纯英文查询带括号时，改前 `sanitize` 之后仍是 `XG-Alpha(2026` 这种整块 —— 索引里
没有这个 lexeme ⇒ 该词恒 0 命中（生产 `search_bm25("StrategyCompare")` 即此类）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from keepsake.storage import DEFAULT_BM25_LIMIT
from keepsake.storage_shared import frozen_scoring_now
from keepsake.storage_sqlite import SqliteStorage

# ---------------------------------------------------------------------------
# 合成语料（全部现造，无任何真实记忆内容）
# ---------------------------------------------------------------------------

#: 20 个合成主题词（占位词，非真实业务词）
HEADS = ["回测", "网关", "熔断", "缓存", "对账", "风控", "撮合", "清算", "盯盘", "推仓",
         "止盈", "滑点", "因子", "样本", "权重", "基准", "净值", "回撤", "敞口", "基差"]

#: 正文填充料：同一段运维套话反复拼，长度用来制造「长条目」（生产失手条都是长条目）
FILLER = ("交易系统按日切片加载行情快照并落盘归档；运维同学在变更窗口内滚动重启撮合进程；"
          "灰度期间新旧两套配置并行运行十五分钟，比对差异后择机切换；回滚脚本默认保留七十二小时。")

#: 旧版候选池大小（本单把默认值提到 DEFAULT_BM25_LIMIT，用它当「改前」对照组）
LEGACY_BM25_LIMIT = 20

#: 每个主题配多少条干扰项 —— 必须 > LEGACY_BM25_LIMIT，否则挤不满旧候选窗口
DISTRACTORS_PER_TOPIC = 40


def make_target(idx: int, head: str) -> str:
    """目标条目：长正文 + 查询词多次出现（生产失手条的同一形状）。"""
    return (f"{head}专题条目{idx:02d}（2026-09-22 复盘）："
            + f"{head} " * 3 + FILLER * 3)


def make_distractor(idx: int, j: int) -> str:
    """干扰项：正文短、**正文里没有查询词**（查询词只出现在 tags 上）。

    🔴 这正是把候选窗口占满的那一类：FTS5 的 `bm25()` 索引的是
    `content + entities + tags` 三路合并的 `content_tok`，标签命中一样算词频，
    而 Python 侧最终打分只按 `content_tsv`（正文）算 ⇒ 它们占窗口但不得分。
    PG 侧同理（`ts_rank_cd` 只当窗口粗排，见 `_bm25_thin` 的 docstring）。

    正文必须**逐条不同**：`store()` 按内容 hash 去重 + 版本化，同内容只留一条。
    """
    return f"第{j}号通道（topic{idx:02d}）的巡检脚本路径固定不变，无需人工干预。"


def make_probes() -> list:
    """20 条探针：`query` = 主题词，`expect_substring` = 目标条目的唯一标记。"""
    return [{"query": h, "expect_substring": f"{h}专题条目{i:02d}"}
            for i, h in enumerate(HEADS)]


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> str:
    """造合成语料，返回临时库路径。**整个模块共用一份**（构造一次，读多轮）。"""
    path = str(tmp_path_factory.mktemp("recall") / "synthetic.db")
    st = SqliteStorage(path=path)
    assert st.ensure_index() is True
    for i, head in enumerate(HEADS):
        st.store(make_target(i, head), tags=f"synthetic,{head},topic{i:02d}",
                 category="fact")
        for j in range(DISTRACTORS_PER_TOPIC):
            st.store(make_distractor(i, j), tags=f"synthetic,{head}", category="note")
    st.close()
    return path


def _recall_at(path: str, ks=(1, 5, 15, 50), bm25_limit: int = DEFAULT_BM25_LIMIT,
               repeat: int = 1) -> tuple:
    """跑一遍探针，返回 `(各档 Recall@k, 逐条命中的 key 序列列表, 语料条数)`。

    🔴 **评分时钟必须冻住**（2026-10-20 修）：时衰减按「查询那一刻」求值 ⇒
    同一行两次查询分数就不同，只差 1e-7 级的近同行会互相穿过
    （`core/shared.py::scoring_now` 的说明里记着这条实测：轮间隔 1.5s 即换位）。
    不冻 ⇒ 「同查询连跑 3 次 key 序列逐字相同」是掷骰子（同款修复见
    `scripts/recall_eval.py`：评测器一直用 `frozen_scoring_now()`）。
    **断言一个字没删、没放宽** —— 只是把「不可复现」变成「可复现」。
    """
    st = SqliteStorage(path=path, bm25_limit=bm25_limit, final_limit=max(ks),
                       is_primary=True)
    try:
        probes = make_probes()
        key_seqs: list = []
        hits: dict = {k: 0 for k in ks}
        with frozen_scoring_now():
            for p in probes:
                seq = []
                rank = None
                for i, frag in enumerate(st.search_bm25(p["query"]) or [], 1):
                    seq.append(frag.get("_key"))
                    if rank is None and p["expect_substring"] in str(frag.get("content", "")):
                        rank = i
                key_seqs.append(seq)
                for k in ks:
                    if rank is not None and rank <= k:
                        hits[k] += 1
            with st._lock:                                   # noqa: SLF001 — 只读计数
                n_docs = st._db().execute(                  # noqa: SLF001
                    "SELECT COUNT(*) FROM ks_fragment").fetchone()[0]
            repeat_seqs = key_seqs if repeat <= 1 else [
                key_seqs,                                            # 占位，下面再跑 repeat-1 轮
            ]
            for _ in range(max(0, repeat - 1)):
                again = []
                for p in probes:
                    again.append([f.get("_key") for f in
                                  (st.search_bm25(p["query"]) or [])])
                repeat_seqs.append(again)
    finally:
        st.close()
    recall = {k: hits[k] / len(probes) for k in ks}
    return recall, repeat_seqs, n_docs


# ---------------------------------------------------------------------------
# 1. 召回质量：改后 Recall@15 ≥ 0.9
# ---------------------------------------------------------------------------

def test_recall_at_15_meets_bar_on_synthetic_corpus(corpus):
    """合成语料上，改后（大候选池 + 查询侧词元归一）Recall@15 必须 ≥ 0.9。"""
    recall, _, n_docs = _recall_at(corpus)
    assert n_docs >= 300, f"合成语料应 ≥300 条，实际 {n_docs}"
    assert recall[15] >= 0.9, f"Recall@15={recall[15]} < 0.9（全档：{recall}）"


def test_larger_candidate_pool_beats_legacy_pool(corpus):
    """同一份语料：旧候选池（bm25_limit=20）确实会丢，改后（默认 200）不丢。

    这条同时是「不许拍脑袋调参」的证据：把 `bm25_limit` 这个**旧配置键**拨回 20
    就能复现改前的数字，无需改动任何代码。
    """
    recall_new, _, _ = _recall_at(corpus, bm25_limit=DEFAULT_BM25_LIMIT)
    recall_old, _, _ = _recall_at(corpus, bm25_limit=LEGACY_BM25_LIMIT)
    assert recall_new[15] > recall_old[15], (
        f"放大候选池后 Recall@15 没有提升：新={recall_new[15]} 旧={recall_old[15]}")


def test_default_candidate_pool_is_at_least_200():
    """默认值必须是 ≥200 的大候选池，且旧配置键 `bm25_limit` 仍能覆盖它。"""
    from keepsake import storage_pg
    assert DEFAULT_BM25_LIMIT >= 200
    assert storage_pg.DEFAULT_BM25_LIMIT == DEFAULT_BM25_LIMIT
    assert storage_pg.DEFAULT_BM25_LIMIT >= 200


# ---------------------------------------------------------------------------
# 2. 确定性：同查询连跑 3 次，key 序列逐字相同
# ---------------------------------------------------------------------------

def test_same_query_three_runs_give_identical_key_sequence(corpus):
    """同一查询连跑 3 次，命中的 key 序列必须完全相同（tiebreaker 生效）。"""
    _, seqs, _ = _recall_at(corpus, repeat=3)
    assert len(seqs) == 3
    assert seqs[1] == seqs[0], "第 2 轮与第 1 轮 key 序列不同 —— 排序不确定"
    assert seqs[2] == seqs[0], "第 3 轮与第 1 轮 key 序列不同 —— 排序不确定"


def test_tiebreak_is_shared_by_all_backends():
    """三后端的 tiebreaker 必须走**同一份实现**（同一语义，不会各自漂移）。"""
    from keepsake import storage, storage_pg, storage_shared, storage_sqlite

    rows = [{"_key": "b", "_combined_score": 1.0}, {"_key": "a", "_combined_score": 1.0},
            {"_key": "c", "_combined_score": 2.0}, {"_combined_score": 1.0}]
    # 同分按 key 升序；无 `_key` 的行按 `or ""` 兜底 ⇒ 排在同分的 a/b 之前
    want = ["c", None, "a", "b"]
    assert [f.get("_key") for f in storage_shared.tiebreak_by_key(rows)] == want
    # 两个后端的静态方法只是薄转发，输出必须与共用实现逐条一致
    assert [f.get("_key") for f in storage_pg.PgStorage._tiebreak(rows)] == want
    assert [f.get("_key") for f in storage_sqlite.SqliteStorage._tiebreak(rows)] == want
    assert storage.tiebreak_by_key is storage_shared.tiebreak_by_key


# ---------------------------------------------------------------------------
# 3. 查询侧与文档侧分词口径一致
# ---------------------------------------------------------------------------

def test_query_terms_are_normalized_to_index_lexemes():
    """查询词必须按索引侧同一个 `\\w+` 口径归一（否则带符号整块恒 0 命中）。"""
    from keepsake.storage_shared import LEXEME_RE, query_lexemes, tsv_lexemes

    doc_lexemes = [w.lower() for w in tsv_lexemes("XG-Alpha(2026-09-22 复盘)要点")]
    # 纯英文查询走 `segment_query` 的 `query.split()` 分支（**不过 jieba**），
    # 且 sanitize 只按 `/-_.` 拆、不切括号 ⇒ `XG-Alpha(2026` 这种整块原样进 tsquery，
    # 而索引里根本没有这个 lexeme ⇒ 该词恒 0 命中（生产 `search_bm25("StrategyCompare")`
    # 返 0 条即此类）。归一后必须**逐字落在索引词元集合内**。
    q_lexemes = [w.lower() for w in query_lexemes(["XG-Alpha(2026-09-22)"])]
    assert "xg-alph(2026" not in q_lexemes        # 改前的形态，已被归一掉
    for want in ("xg", "alpha", "2026"):
        assert want in q_lexemes, f"查询侧丢了 {want}"
        assert want in doc_lexemes, f"文档侧没有 {want}（两侧口径不一致）"
    # 归一后的每个查询词元都必须能在文档侧词元里找到同形项（口径同源的硬判据）
    assert set(q_lexemes) <= set(doc_lexemes), set(q_lexemes) - set(doc_lexemes)
    assert LEXEME_RE.pattern == r"\w+"


def test_punctuation_query_recalls_target(corpus):
    """带括号的纯英文查询必须召回目标条目（口径对齐的端到端证据）。"""
    st = SqliteStorage(path=corpus, bm25_limit=DEFAULT_BM25_LIMIT, final_limit=50,
                       is_primary=True)
    try:
        res = st.search_bm25("XG-Alpha(2026-09-22 复盘)") or []
        keys = [r.get("_key") for r in res]
        assert keys, "带符号查询一条都没召回"
        assert len(keys) == len(set(keys)), "同一查询出现重复 key"
    finally:
        st.close()


# ---------------------------------------------------------------------------
# 4. 只读：评测器不许写库（由**数据库**保证，不靠调用方自觉）
# ---------------------------------------------------------------------------

def test_readonly_connection_rejects_writes(tmp_path):
    path = str(tmp_path / "ro.db")
    st = SqliteStorage(path=path, is_primary=True)
    assert st.ensure_index() is True
    st.store("只读连接测试条目", tags="synthetic")
    st.close()

    ro = SqliteStorage(path=path, readonly=True, is_primary=True)
    try:
        assert ro.search_bm25("只读连接测试条目"), "只读连接应能正常检索"
        with ro._lock:                                   # noqa: SLF001 — 只读计数
            before = ro._db().execute(                   # noqa: SLF001
                "SELECT COUNT(*) FROM ks_fragment").fetchone()[0]
        # `store()` 把写失败记 WARNING 后返回 False（不抛）—— 关键是**库没被动过**
        assert ro.store("评测器绝不许写的条目", tags="synthetic") is False
        with ro._lock:                                   # noqa: SLF001
            after = ro._db().execute(                    # noqa: SLF001
                "SELECT COUNT(*) FROM ks_fragment").fetchone()[0]
        assert after == before, "只读连接竟然写进去了"
    finally:
        ro.close()


def test_readonly_refuses_to_build_schema(tmp_path):
    """只读连接下 schema 未就绪 ⇒ 显式抛错，**不许**偷偷补建（那是写）。"""
    ro = SqliteStorage(path=str(tmp_path / "missing.db"), readonly=True)
    try:
        with pytest.raises(Exception):
            ro.search_bm25("任何查询")
    finally:
        ro.close()


# ---------------------------------------------------------------------------
# 5. 评测器本身：跑得起来 + 不含写调用
# ---------------------------------------------------------------------------

_EVAL = Path(__file__).resolve().parent.parent / "scripts" / "recall_eval.py"

#: 评测器里**出现即视为写调用**的方法名（静态自查，见 test_eval_script_is_read_only）
_WRITE_METHODS = (
    "store", "upsert_fragment", "write_fragments_batch", "update_fragment_fields",
    "delete_fragments_batch", "touch_fragment", "set_supersedes", "supersede_fragment",
    "correct_fragments", "record_feedback", "consume", "ensure_index", "import_aux_rows",
)


def test_eval_script_is_read_only():
    """静态自查：评测器里不许出现任何写方法调用。"""
    src = _EVAL.read_text(encoding="utf-8")
    code_lines = [ln for ln in src.splitlines()
                  if not ln.strip().startswith("#")]
    code = "\n".join(code_lines)
    for name in _WRITE_METHODS:
        assert f".{name}(" not in code, f"评测器里出现了写调用 .{name}("


def test_eval_script_runs_on_synthetic_corpus(corpus, tmp_path, capsys):
    """评测器能在合成语料上跑通并输出各档 Recall（改后必须 ≥ 0.9）。"""
    import subprocess
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "storage": {"backend": "sqlite", "sqlite": {"path": corpus}},
        "is_primary": True, "bm25_limit": DEFAULT_BM25_LIMIT,
    }), encoding="utf-8")
    probes = tmp_path / "probes.json"
    probes.write_text(json.dumps(make_probes(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "eval.json"

    proc = subprocess.run(
        [sys.executable, str(_EVAL), "--config", str(cfg), "--probes", str(probes),
         "--k", "1,5,15,50", "--mode", "rerank", "--repeat", "3", "--out", str(out)],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, f"评测器退出码 {proc.returncode}\n{proc.stderr[-2000:]}"
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["metrics"]["Recall@15"] >= 0.9, result["metrics"]
    assert result["meta"]["deterministic"] is True, "连跑 3 轮 key 序列不一致"
    # 冻结必须真的生效 —— 否则上面那条 deterministic 是靠运气（轮间恰好没赶上分数交叉）
    # 而不是靠机制，回归了也看不出来。
    assert result["meta"].get("frozen_now"), "评测没冻结评分时钟（meta.frozen_now 缺失）"
    assert result["meta"]["corpus_size"] >= 300