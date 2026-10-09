"""PG 检索快照（TTL 语料统计 / 加权信号 / 同义词）的行为测试。

🔴 为什么要有这个文件：快照是「拿陈旧换速度」，唯一能兜住「不许陈旧到影响
断言」这条红利的，就是**写路径必须立刻让快照失效**。这个性质退化（写完还读
旧快照）不会让任何现有测试变红 —— 只会让线上悄悄少召回一条。所以在这里钉死。

凭据规则与 `test_storage_pg.py` 同款：DSN 只从 `~/.keepsake_pg_test.env` 读，
读不到 / 没装 psycopg → **skip，不 fail**；只连 `keepsake_test`，绝不碰生产。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

from keepsake.storage_pg import _SNAPSHOTS, PgStorage

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST = "8.140.192.91"
ALLOWED_DBNAME = "keepsake_test"

_WROTE: list[str] = []


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


def _dsn_is_safe(dsn: str) -> bool:
    return bool(dsn) and ALLOWED_HOST in dsn and ALLOWED_DBNAME in dsn


psycopg = pytest.importorskip("psycopg", reason="未装 psycopg（pip install 'psycopg[binary]'）")
DSN = _read_dsn()
if not _dsn_is_safe(DSN):
    pytest.skip(f"无测试库凭据或指向非 {ALLOWED_HOST}/{ALLOWED_DBNAME}，跳过 PG 快照测试",
                allow_module_level=True)


@pytest.fixture(scope="module")
def pg():
    """建表 + 实例；凭据写进实例但绝不打印。

    `is_primary=True`：默认 False 时检索的 WHERE 会带上「只搜 agent/shared」的
    标签过滤，而本模块写入的碎片不带 agent 标签 ⇒ 必然零结果（与快照无关）。
    """
    st = PgStorage(dsn=DSN, connect_timeout=30, is_primary=True)
    st.ensure_index()
    yield st
    for k in _WROTE:
        try:
            with st._tx() as cur:      # noqa: SLF001 — 测试自己清理自己写的行
                cur.execute("DELETE FROM ks_fragment WHERE key = %s", (k,))
        except Exception:               # noqa: BLE001 — 清理失败不该让测试变红
            pass
    _SNAPSHOTS.clear()


def _remember(st: PgStorage, text: str, **kw) -> str:
    st.store(text, **kw)
    key = st._fragment_key_for(text)    # noqa: SLF001 — 同一套 key 算法
    _WROTE.append(key)
    return key


def test_snapshot_hit_avoids_resending_the_query(pg):
    """TTL 内第二次调用**不发 SQL**；关掉 TTL（0）则必须发 —— 快照开关真的接上了。"""
    pg._snap_drop("attn")            # noqa: SLF001
    pg.match_attention("回测 未来函数")

    sent = {"n": 0}
    orig = PgStorage._ro

    def counting_ro(self):
        ctx = orig(self)
        return _Counting(ctx, sent)

    PgStorage._ro = counting_ro
    try:
        pg.match_attention("回测 未来函数")     # TTL 内 → 命中快照
        assert sent["n"] == 0, "TTL 内第二次调用还发了 SQL，快照没生效"

        pg._snapshot_ttl_s = 0.0               # noqa: SLF001 — 关掉快照
        try:
            pg.match_attention("回测 未来函数")  # → 必须实查
            assert sent["n"] == 1, "TTL=0 时仍走了快照，关不掉"
        finally:
            pg._snapshot_ttl_s = 60.0           # noqa: SLF001
    finally:
        PgStorage._ro = orig


def test_store_invalidates_every_snapshot_group(pg):
    """store() 写 ks_fragment + 累加热词/注意力 ⇒ **全部**快照分组必须立刻作废。

    这是「不许陈旧到影响断言」的那条红线：新写入后立刻可见，不等 TTL。

    预热**逐个显式调用**各快照的读取方，而不是只跑一次 search_bm25 ——
    后者在空语料 / 无候选时压根不会走到重排里的 match_attention，
    用例就会变成「有没有候选」而不是「写路径有没有失效快照」的断言。
    """
    pg.search_bm25("回测 未来函数 一根 bar")      # → n_avgdl / dfs / syn
    pg.match_attention("回测 未来函数")            # → attn
    pg.match_hot_topics("回测 未来函数")           # → hot
    assert _SNAPSHOTS.get(pg._target_key()), "预热后应当有快照"  # noqa: SLF001
    groups = {k[0] for k in _SNAPSHOTS[pg._target_key()]}          # noqa: SLF001
    assert {"n_avgdl", "dfs", "attn", "hot", "syn"} <= groups, f"预热没覆盖全部分组：{groups}"

    text = "快照失效校验专用语料 栀子花失效标记 未来函数 回测"
    _remember(pg, text)

    left = {k[0] for k in _SNAPSHOTS.get(pg._target_key(), {})}     # noqa: SLF001
    assert left == set(), f"store() 之后还残留快照分组 {left} ⇒ 写路径漏了 _snap_drop"


def test_new_fragment_is_searchable_immediately(pg):
    """存完立刻查得到 —— 端到端证明没有「陈旧到影响结果」的窗口。

    检索词用固定的生僻词（不掺数字/字母）：jieba 会把 `zqvis1757...` 这类串
    切碎，`_sanitize_terms` 再滤掉碎片 ⇒ 断言会假红，与快照无关。
    """
    text = "端到端可见性校验 栀子花标记 未来函数 回测 bar"
    key = _remember(pg, text)
    keys = [f["_key"] for f in pg.search_bm25("栀子花标记")]
    assert key in keys, "刚写入的碎片在紧接着的 BM25 里查不到（快照陈旧或未生效）"


def test_supersede_and_correct_drop_corpus_snapshot(pg):
    """封边 / 纠正改的是「活记忆」口径 ⇒ 语料统计快照必须作废。"""
    text = f"封边快照校验 zqs{int(time.time())} 回测 bar"
    key = _remember(pg, text)
    pg.search_bm25("回测 未来函数")          # 预热 n_avgdl/dfs
    pg.supersede_fragment(key, "__void__")
    assert not [k for k in _SNAPSHOTS.get(pg._target_key(), {})   # noqa: SLF001
                if k[0] in ("n_avgdl", "dfs")], "supersede 之后语料统计快照还在"


class _Counting:
    """只数「有没有真的开过游标」的最小代理。"""

    def __init__(self, ctx, counter):
        self._ctx = ctx
        self._counter = counter

    def __enter__(self):
        self._counter["n"] += 1
        return self._ctx.__enter__()

    def __exit__(self, *a):
        return self._ctx.__exit__(*a)
