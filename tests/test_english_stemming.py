"""英文检索：Porter2 词干化（**对称**）+ 子串兜底档 —— 红绿闭环。

覆盖任务书「判据」里的 6 条（SQLite 后端；PG 后端同款见
`tests/test_english_stemming_pg.py`）：

  ① running/runs、cities/city **互查**命中（对称铁律：索引侧与查询侧同一个函数）
  ② 标签 / 系统保留字**不被词干化**（写闸与可见性判据不许破）
  ③ 标识符类查询经**子串兜底档**命中（字面路 0 命中时才走）
  ④ 兜底档**不越权**：非 primary 搜不到未共享代理的正文
  ⑤ **零命中才走兜底**、有命中**不多查**（断言兜底调用次数）
  ⑥ 同义词**英文词条联动**（discover → generate → 命中 "generating"）

本文件 **hermetic**：全部用 `tmp_path` 里的临时 SQLite 库，不连任何外部服务。
"""

from __future__ import annotations

import json
from typing import List

import pytest

from keepsake.splitter import stem_english
from keepsake.storage_sqlite import SqliteStorage


@pytest.fixture()
def store(tmp_path) -> SqliteStorage:
    # is_primary=True：默认身份**看得见全部**（非 primary 走可见性收窄，
    # 那是 `test_fallback_respects_visibility` 专门测的那条）。
    s = SqliteStorage(path=str(tmp_path / "ks.db"), is_primary=True)
    assert s.ensure_index() is True
    yield s
    s.close()


def _store_at(tmp_path, name: str, agent_id: str = "", is_primary: bool = False):
    """同一个临时库上再开一个存储实例（换身份用）。"""
    s = SqliteStorage(path=str(tmp_path / "ks.db"), agent_id=agent_id,
                       is_primary=is_primary)
    assert s.ensure_index() is True
    return s


# ---------------------------------------------------------------------------
# 纯函数：词干化口径本身
# ---------------------------------------------------------------------------

def test_stem_english_basics():
    """词干化只作用于「纯 ASCII 字母词」，其余原样。"""
    assert stem_english("running") == "run"
    assert stem_english("runs") == "run"
    assert stem_english("cities") == "citi"
    assert stem_english("city") == "citi"
    # 标识符（含数字/符号）原样 —— 它们是精确名，词干化会把名字改掉
    assert stem_english("StrategyCompare01") == "StrategyCompare01"
    assert stem_english("v2") == "v2"
    # 中文原样
    assert stem_english("回测") == "回测"
    # 标签 / 保留字豁免（词干化会破坏精确逗号边界判据）
    assert stem_english("shared") == "shared"
    assert stem_english("core") == "core"
    assert stem_english("agent") == "agent"


def test_stem_english_is_idempotent():
    """🔴 幂等是硬要求：同义词表按词干存、查询侧再词干化一次，两次必须一致。"""
    for w in ("running", "runs", "cities", "city", "generate", "generating",
              "discover", "national", "relational", "achilles"):
        once = stem_english(w)
        assert stem_english(once) == once, f"{w} → {once} 再词干化又变了"


# ---------------------------------------------------------------------------
# ① 对称铁律：索引侧与查询侧命中
# ---------------------------------------------------------------------------

def test_running_runs_mutual_hit_sqlite(store):
    """索引 "running" 查 "runs" 命中；索引 "runs" 查 "running" 也命中。"""
    assert store.store("we are running the marathon every morning") is True
    hits = store.search_bm25("runs")
    assert hits, "索引 running、查询 runs 应当命中（对称铁律）"
    assert "running" in hits[0]["content"]

    assert store.store("he runs the daily batch job") is True
    hits = store.search_bm25("running")
    assert hits, "索引 runs、查询 running 应当命中"
    assert any("runs" in h["content"] for h in hits)


def test_cities_city_mutual_hit_sqlite(store):
    """索引 "cities" 查 "city" 命中；反向亦然。"""
    assert store.store("many cities were visited during the trip") is True
    hits = store.search_bm25("city")
    assert hits, "索引 cities、查询 city 应当命中"

    assert store.store("this city has a huge library") is True
    hits = store.search_bm25("cities")
    assert hits, "索引 city、查询 cities 应当命中"


# ---------------------------------------------------------------------------
# ② 标签 / 保留字不被词干化
# ---------------------------------------------------------------------------

def test_tags_are_not_stemmed(store):
    """🔴 标签流不进词干化：`shared` 不能被砍成 `share`（否则标签判据破口）。"""
    assert store.store("a shared note about deployment", tags="shared,core") is True
    with store._lock:
        tsv = store._db().execute(
            "SELECT content_tsv FROM ks_fragment WHERE content LIKE ?",
            ("%shared note%",)).fetchone()[0]
    assert "share" not in tsv.split(), f"标签被词干化了: {tsv}"
    assert "shared" in tsv.split(), f"标签没进索引: {tsv}"
    # 标签的精确逗号边界判据照旧
    assert len(store.list_by_tag("shared")) == 1
    assert store.list_by_tag("share") == [], "被词干化的标签串不该命中"


def test_reserved_words_keep_visibility_semantics(store):
    """保留字 `core` 的可见性判据不因词干化而变（core 条目仍可按标签取到）。"""
    assert store.store("常驻记忆：网关配置基线", tags="core") is True
    assert len(store.list_by_tag("core")) == 1
    assert store.list_by_tag("cor") == []


# ---------------------------------------------------------------------------
# ③④⑤ 子串兜底档
# ---------------------------------------------------------------------------

def test_identifier_hit_goes_through_substring_fallback(store, monkeypatch):
    """③ 标识符类查询：字面路 0 命中 ⇒ 走子串兜底档命中。

    🔴 这条要**真红转绿**：先把兜底档掐掉（返回空）⇒ 必须 0 命中；
    放开兜底档 ⇒ 命中。中间那一步证明「命中确实是兜底档带来的」。
    """
    assert store.store("StrategyCompare01(2026 策略对比记录") is True
    query = "ategyCompare01(202"

    calls: List[str] = []
    real = store._like_fallback_rows

    def _spy(q, where_sql, params):
        calls.append(q)
        return real(q, where_sql, params)

    # 🔴 先把兜底档掐掉 ⇒ 字面路必须 0 命中（证明「命中确实是兜底档带来的」）
    monkeypatch.setattr(store, "_like_fallback_rows", lambda q, w, p: [])
    assert store.search_bm25(query) == [], "字面路不该命中这个标识符查询"

    monkeypatch.setattr(store, "_like_fallback_rows", _spy)
    hits = store.search_bm25(query)
    assert hits, "标识符子串应当被兜底档命中"
    assert "StrategyCompare01" in hits[0]["content"]
    assert calls, "兜底档没被调用 ⇒ 命中不是它带来的"

    # 融合入口 `search()`（API / MCP 走这条）也要能命中 ⇒ 兜底档的行过得了 v2 地板
    fused = store.search(query)
    assert fused, "融合入口 search() 也应当命中"
    assert any("StrategyCompare01" in h.get("content", "") for h in fused)


def test_no_fallback_when_bm25_hits(store, monkeypatch):
    """⑤ 有命中**不多查**：兜底档一次都不许被调用。"""
    assert store.store("we are running the marathon") is True
    calls: List[str] = []
    monkeypatch.setattr(store, "_like_fallback_rows",
                        lambda q, w, p: calls.append(q) or [])
    hits = store.search_bm25("runs")
    assert hits, "字面路应当直接命中"
    assert not calls, f"有命中还去查兜底档了（白扫一遍表）: {calls}"


def test_fallback_respects_visibility(tmp_path):
    """🔴 ④ 兜底档**不越权**：非 primary 搜不到未共享代理的正文。

    🔴 真红转绿的关键：**primary 搜得到**（证明兜底档确实在干活），
    而**非 primary 搜不到**（可见性判据没被兜底档破口）。
    """
    owner = _store_at(tmp_path, "owner", agent_id="alpha", is_primary=False)
    assert owner.store("StrategyCompare01(2026 私有策略对比", tags="core") is True
    owner.close()

    query = "ategyCompare01(202"

    primary = _store_at(tmp_path, "primary", agent_id="", is_primary=True)
    try:
        hits = primary.search_bm25(query)
        assert hits, "primary 应当能搜到（兜底档在工作）"
    finally:
        primary.close()

    other = _store_at(tmp_path, "other", agent_id="beta", is_primary=False)
    try:
        hits = other.search_bm25(query)
        assert hits == [], f"非 primary 搜到了别人的正文（可见性破口）: {hits}"
    finally:
        other.close()


# ---------------------------------------------------------------------------
# ⑥ 同义词英文词条联动
# ---------------------------------------------------------------------------

def test_english_synonym_links_to_stemmed_index(store):
    """⑥ 同义词（英文）按词干联动：查 "discover" 命中索引里的 "generat"。"""
    assert store.store("we are generating the weekly report") is True
    with store._lock:
        store._db().execute(
            "INSERT INTO ks_synonym (term, synonyms) VALUES (?, ?)",
            ("discover", json.dumps(["generate"], ensure_ascii=False)),
        )
    hits = store.search_bm25("discover")
    assert hits, "同义词扩展 + 词干化后应当命中 generating 那条"
    assert "generating" in hits[0]["content"]


# ---------------------------------------------------------------------------
# 存量重建脚本（scripts/rebuild_content_tsv.py）
# ---------------------------------------------------------------------------

def _load_rebuild_script():
    """按路径加载 scripts/rebuild_content_tsv.py（scripts/ 不是包）。"""
    import importlib.util
    from pathlib import Path

    here = Path(__file__).resolve()
    path = here.parents[1] / "scripts" / "rebuild_content_tsv.py"
    assert path.exists(), f"重建脚本不在预期位置: {path}"
    spec = importlib.util.spec_from_file_location("ks_rebuild_content_tsv", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rebuild_script_fixes_old_style_row_sqlite(tmp_path):
    """🔴 重建脚本：老口径（未词干化）的行被认出来并改回新口径，且**可重跑幂等**。

    这是「存量重建」这条要求的最小可运行检查：脚本必须按**与写路径同一个
    函数**重算 content_tsv（这里 SQLite 侧还要同步 FTS5 虚表）。
    """
    db = str(tmp_path / "ks.db")
    s = SqliteStorage(path=db, is_primary=True)
    assert s.ensure_index() is True
    assert s.store("we are running the marathon in cities") is True
    with s._lock:
        s._db().execute("UPDATE ks_fragment SET content_tsv = ?",
                        ("we are running the marathon in cities",))
    s.close()

    mod = _load_rebuild_script()
    need, done = mod._sqlite_rebuild(db, dry=True, limit=0)
    assert need == 1, f"老口径的行应当被认出来: {need}"
    assert done == 0, "--dry 不许写库"

    need, done = mod._sqlite_rebuild(db, dry=False, limit=0)
    assert (need, done) == (1, 1), (need, done)

    # 重跑 ⇒ 0 行（幂等）
    need, done = mod._sqlite_rebuild(db, dry=True, limit=0)
    assert need == 0, f"重跑还判『变了』⇒ 不幂等: {need}"

    s = SqliteStorage(path=db, is_primary=True)
    try:
        with s._lock:
            tsv = s._db().execute("SELECT content_tsv FROM ks_fragment").fetchone()[0]
        assert "run" in tsv.split(), f"索引没重算成词干口径: {tsv}"
        assert "running" not in tsv.split(), f"索引里还留着未词干化的原形: {tsv}"
        hits = s.search_bm25("runs")
        assert hits, "重建后查 runs 应当命中"
    finally:
        s.close()
