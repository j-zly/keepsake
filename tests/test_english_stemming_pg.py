"""PG 后端：英文检索 —— Porter2 词干化（**对称**）+ pg_trgm 子串兜底档。

与 `tests/test_english_stemming.py`（SQLite，hermetic）同款判据，PG 侧再来一遍
（两个后端的口径必须逐字同形，PG 的索引形态是 tsvector 而 SQLite 是 FTS5 文本串）。

🔴 凭据纪律（抄 `tests/test_storage_pg.py`）：
  * DSN 只从 `~/.keepsake_pg_test.env`（KEEPSAKE_PG_DSN）读；
  * 读不到 / 没装 psycopg ⇒ **skip，不 fail**；
  * 只连 8.140.192.91 / keepsake_test，否则 skip（防手滑连到生产）；
  * 凭据绝不进代码、日志、断言输出。

测试结束删除本次写入的全部行（不留残渣）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List

import pytest

from keepsake.splitter import extract_entities
from keepsake.storage_pg import PgStorage

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST = "8.140.192.91"
ALLOWED_DBNAME = "keepsake_test"

psycopg = pytest.importorskip("psycopg", reason="未装 psycopg（pip install 'psycopg[binary]'）")


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


def _dsn_is_safe(dsn: str) -> bool:
    return bool(dsn) and ALLOWED_HOST in dsn and ALLOWED_DBNAME in dsn


DSN = _read_dsn()
if not _dsn_is_safe(DSN):
    pytest.skip(f"无测试库凭据或指向非 {ALLOWED_HOST}/{ALLOWED_DBNAME}，跳过 PG 真库测试",
                allow_module_level=True)

#: 本次运行写入的 key（teardown 按 key 清；整表不 TRUNCATE —— 那是别人的库）
_WROTE_KEYS: List[str] = []


@pytest.fixture(scope="module")
def pg():
    """建表 + 建实例；整个模块共用一条连接（远端库建连要 2~4s）。"""
    storage = PgStorage(dsn=DSN, connect_timeout=30, is_primary=True)
    storage.ensure_index()
    yield storage
    storage.close()


@pytest.fixture(autouse=True)
def _cleanup(pg):
    """每个用例后清掉本次写入的行（只删自己的 key，不 TRUNCATE 整表）。"""
    yield
    keys = list(_WROTE_KEYS)
    if not keys:
        return
    with pg._tx() as cur:      # noqa: SLF001 — 测试内清理自有数据
        # 先取回正文：要靠它反推实体（删完就取不到了）
        cur.execute("SELECT content FROM ks_fragment WHERE key = ANY(%s)", (keys,))
        ents = sorted({e.lower().strip()
                       for (content,) in cur.fetchall()
                       for e in extract_entities(content or "")})
        cur.execute("DELETE FROM ks_fragment WHERE key = ANY(%s)", (keys,))
        cur.execute("DELETE FROM ks_entity_timeline WHERE frag_key = ANY(%s)", (keys,))
        # 共现：只删本轮实体两两组成的 pair（别人的 pair 不许动）
        pairs = [f"{ents[i]}||{ents[j]}"
                 for i in range(len(ents)) for j in range(i + 1, len(ents))]
        if pairs:
            cur.execute("DELETE FROM ks_entity_cooc WHERE pair = ANY(%s)", (pairs,))
    _WROTE_KEYS.clear()


def _store(pg, text: str, tags: str = "") -> str:
    """写一条并登记 key（teardown 按 key 清）。"""
    assert pg.store(text, tags=tags) is True
    key = f"memory:frag:{__import__('hashlib').sha256(text.encode()).hexdigest()[:12]}"
    _WROTE_KEYS.append(key)
    return key


def _pg(pg, agent_id: str = "", is_primary: bool = True) -> PgStorage:
    """同一个库上再开一个实例（换身份用）。"""
    s = PgStorage(dsn=DSN, connect_timeout=30, agent_id=agent_id, is_primary=is_primary)
    return s


# ---------------------------------------------------------------------------
# ① 对称铁律
# ---------------------------------------------------------------------------

def test_running_runs_mutual_hit_pg(pg):
    """索引 "running" 查 "runs" 命中；索引 "runs" 查 "running" 也命中。"""
    _store(pg, "we are running the marathon every morning")
    hits = pg.search_bm25("runs")
    assert hits, "索引 running、查询 runs 应当命中（对称铁律）"

    _store(pg, "he runs the daily batch job")
    hits = pg.search_bm25("running")
    assert hits, "索引 runs、查询 running 应当命中"


def test_cities_city_mutual_hit_pg(pg):
    """索引 "cities" 查 "city" 命中；反向亦然。"""
    _store(pg, "many cities were visited during the trip")
    hits = pg.search_bm25("city")
    assert hits, "索引 cities、查询 city 应当命中"

    _store(pg, "this city has a huge library")
    hits = pg.search_bm25("cities")
    assert hits, "索引 city、查询 cities 应当命中"


# ---------------------------------------------------------------------------
# ② 标签 / 保留字不被词干化
# ---------------------------------------------------------------------------

def test_tags_are_not_stemmed(pg):
    """🔴 标签流不进词干化：`shared` 不能被砍成 `share`。"""
    key = _store(pg, "a shared note about deployment", tags="shared")
    with pg._ro() as cur:      # noqa: SLF001 — 测试内读
        # tsvector 里的词元用 unnest 取（文本形态是 'lex':pos, 不好直接切）
        cur.execute("SELECT t.lexeme FROM ks_fragment f, unnest(f.content_tsv) t "
                    "WHERE f.key = %s", (key,))
        lexemes = [r[0] for r in cur.fetchall()]
    assert "share" not in lexemes, f"标签被词干化了: {lexemes}"
    assert "shared" in lexemes, f"标签没进索引: {lexemes}"
    assert len(pg.list_by_tag("shared")) >= 1
    assert pg.list_by_tag("share") == [], "被词干化的标签串不该命中"


# ---------------------------------------------------------------------------
# ③④⑤ 子串兜底档（pg_trgm）
# ---------------------------------------------------------------------------

def test_identifier_hit_goes_through_trgm_fallback(pg, monkeypatch):
    """③ 标识符类查询：字面路 0 命中 ⇒ pg_trgm 子串兜底档命中。

    🔴 真红转绿：先把兜底档掐掉 ⇒ 必须 0 命中；放开 ⇒ 命中。
    """
    _store(pg, "StrategyCompare01(2026 策略对比记录")
    query = "ategyCompare01("

    calls: List[str] = []
    real = pg._trgm_fallback_rows

    def _spy(cur, q, where_sql, params, t, w, k):
        calls.append(q)
        return real(cur, q, where_sql, params, t, w, k)

    monkeypatch.setattr(pg, "_trgm_fallback_rows",
                        lambda *a, **kw: [])
    assert pg.search_bm25(query) == [], "字面路不该命中这个标识符查询"

    monkeypatch.setattr(pg, "_trgm_fallback_rows", _spy)
    hits = pg.search_bm25(query)
    assert hits, "标识符子串应当被兜底档命中"
    assert "StrategyCompare01" in hits[0]["content"]
    assert calls, "兜底档没被调用 ⇒ 命中不是它带来的"

    # API / MCP 走的是**融合入口** `search()`（BM25 + KNN + v2 过滤）⇒ 兜底档
    # 的行必须能活着穿过 v2 的相似度地板（0.05）。
    fused = pg.search(query)
    assert fused, "融合入口 search() 也应当命中（兜底档的行要过得了 v2 地板）"
    assert any("StrategyCompare01" in h.get("content", "") for h in fused)


def test_no_fallback_when_bm25_hits(pg, monkeypatch):
    """⑤ 有命中**不多查**：兜底档一次都不许被调用。"""
    _store(pg, "we are running the marathon")
    calls: List[str] = []
    monkeypatch.setattr(pg, "_trgm_fallback_rows",
                        lambda *a: calls.append("x") or [])
    hits = pg.search_bm25("runs")
    assert hits, "字面路应当直接命中"
    assert not calls, f"有命中还去查兜底档了（白扫一遍表）: {calls}"


def test_trgm_fallback_respects_visibility(pg):
    """🔴 ④ 兜底档**不越权**：非 primary 搜不到未共享代理的正文。

    🔴 真红转绿的关键：**primary 搜得到**（兜底档确实在干活），
    而**非 primary 搜不到**（可见性判据没被破口）。
    """
    _store(pg, "StrategyCompare01(2026 私有策略对比")
    query = "ategyCompare01("

    # 🔴 真红转绿的前半段：primary 搜得到（兜底档确实在干活）
    assert pg.search_bm25(query), "primary 应当能搜到（兜底档在工作）"

    other = _pg(pg, agent_id="beta", is_primary=False)
    try:
        hits = other.search_bm25(query)
        assert hits == [], f"非 primary 搜到了别人的正文（可见性破口）: {hits}"
    finally:
        other.close()


# ---------------------------------------------------------------------------
# ⑥ 同义词英文词条联动
# ---------------------------------------------------------------------------

def test_english_synonym_links_to_stemmed_index(pg):
    """⑥ 同义词（英文）按词干联动：查 "discover" 命中索引里的 "generat"。"""
    _store(pg, "we are generating the weekly report")
    with pg._tx() as cur:      # noqa: SLF001 — 测试内写
        cur.execute("INSERT INTO ks_synonym (term, synonyms) VALUES (%s, %s::jsonb) "
                    "ON CONFLICT (term) DO UPDATE SET synonyms = excluded.synonyms",
                    ("discover", json.dumps(["generate"], ensure_ascii=False)))
    try:
        hits = pg.search_bm25("discover")
        assert hits, "同义词扩展 + 词干化后应当命中 generating 那条"
        assert "generating" in hits[0]["content"]
    finally:
        with pg._tx() as cur:  # noqa: SLF001 — 测试内清理
            cur.execute("DELETE FROM ks_synonym WHERE term = %s", ("discover",))
