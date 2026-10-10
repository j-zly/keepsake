"""PG 语料维护（discover_synonyms / generate_jieba_dict）测试 —— 2026-10 ks_pg_syn。

## 为什么要有这个文件

PG 侧这两个方法此前是 `NotImplementedError` 留桩，是**三后端等价性里唯一的缺口**
（Redis / SQLite 两侧都已实现）。本文件锁死「补齐之后」的三件事：

  1. **行为**：写进 `ks_synonym`、rebuild 洗表 / 增量保留手工项、降噪判据、
     词典格式与确定性（逐条对齐 `tests/test_storage_sqlite.py` 的批 3 段）。
  2. **等价性**：同一份语料灌 PG 与 SQLite，两侧 `total_terms` 与 term 集合
     **逐个相等** —— 这才是「三个后端靠配置切换」这个用户口径的机械证据。
  3. **单一实现**：`PgStorage` 与 `SqliteStorage` 绑的是 `storage_shared` 里
     **同一批函数对象**（对象身份断言），不是各写一份。

## 隔离与凭据

* DSN 只从 `~/.keepsake_pg_test.env` 读；host/dbname 必须是 8.140.192.91 /
  keepsake_test，否则整模块 skip（防手滑连生产）。
* 每个用例在**独立 schema**（`ks_syn_*`）里建表，跑完 `DROP SCHEMA ... CASCADE`
  并复查残留 = 0 —— 与 `test_pg_legacy_maintenance.py` 同一套办法，
  **public 的 3701 行真语料一行都不碰**。
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from keepsake import storage_shared
from keepsake.storage_pg import PgStorage
from keepsake.storage_sqlite import SqliteStorage

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST = "8.140.192.91"
ALLOWED_DBNAME = "keepsake_test"

psycopg = pytest.importorskip("psycopg", reason="未装 psycopg（pip install 'keepsake-memory[postgres]'）")


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


DSN = _read_dsn()
if not (DSN and ALLOWED_HOST in DSN and ALLOWED_DBNAME in DSN):
    pytest.skip(f"无测试库凭据或指向非 {ALLOWED_HOST}/{ALLOWED_DBNAME}，跳过 PG 语料维护真库测试",
                allow_module_level=True)

_PROBE_SCHEMAS: list[str] = []

# 语料：每条都含同一批词 ⇒ 共现数 4 ≥ min_co_occurrence(3)，成对必然成立。
# 阈值**不用默认**（默认 min_word_freq=10 会把 4 条语料全筛掉 ⇒ 空转）。
_CORPUS = "备份 策略 轮换 保留 天数 校验 恢复 演练"


def _rows(n: int = 4, content: str = _CORPUS) -> list[dict]:
    return [{"key": f"memory:frag:{i:04d}", "content": content} for i in range(n)]


def _dsn_for(schema: str) -> str:
    """把 `search_path` 钉到本用例的隔离 schema（public 保留：pgvector 装在那）。"""
    return DSN + f"?options=-csearch_path%3D{schema},public"


def _terms(storage: PgStorage) -> dict[str, list[str]]:
    with storage._ro() as cur:                                    # noqa: SLF001 — 测试读自己写的表
        cur.execute("SELECT term, synonyms FROM ks_synonym")
        return {t: sorted(s) for t, s in cur.fetchall()}


@pytest.fixture
def pg_store():
    """独立 schema 的空 PG 库；用例结束 DROP 掉并复查残留。"""
    schema = f"ks_syn_{uuid.uuid4().hex[:12]}"
    _PROBE_SCHEMAS.append(schema)
    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    storage = PgStorage(dsn=_dsn_for(schema), connect_timeout=30, embed_dim=4,
                        synonym_min_word_freq=2, synonym_min_co_occurrence=2)
    assert storage.ensure_index() is True
    try:
        yield storage
    finally:
        storage.close()
        with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture(autouse=True)
def _no_schema_residue():
    """teardown 后复查：本轮建过的 schema 一个都不许留在库里。"""
    yield
    with psycopg.connect(DSN, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute("SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)",
                    (_PROBE_SCHEMAS,))
        assert not cur.fetchall(), f"隔离 schema 残留: {cur.fetchall()}"


# ------------------------------------------------------------------ 一、行为

def test_discover_synonyms_writes_ks_synonym(pg_store):
    """真写 `ks_synonym`，返回字段与 SQLite 侧逐字同形。"""
    assert pg_store.write_fragments_batch(_rows()) == 4

    stats = pg_store.discover_synonyms(rebuild=True)
    assert stats["rebuild"] is True
    assert stats["scanned_fragments"] == 4
    assert stats["discovered_groups"] > 0, stats
    assert stats["degraded"] == [], f"健康路径不该有降级: {stats['degraded']}"

    table = _terms(pg_store)
    assert stats["total_terms"] == len(table), stats
    assert {"备份", "策略"} <= set(table), sorted(table)
    # JSON 是数组不是字符序列（修过一次的坑：逐字符迭代 ⇒ 词表全是单字碎片）
    assert all(isinstance(v, list) for v in table.values()), table
    for junk in ("[", "]", '"'):
        assert junk not in table, f"JSON 串被当成字符序列迭代了：出现了 {junk!r}"
    # 反向映射也在（_expand_terms 靠它扩召回）
    assert "备份" in table["策略"], table["策略"]


def test_discover_synonyms_empty_corpus_returns_zeros_not_crash(pg_store):
    """空库：返回零统计 + 空表，**不崩也不报错**（这是合法的「没扫到东西」）。"""
    stats = pg_store.discover_synonyms(rebuild=True)
    assert stats == {"discovered_groups": 0, "total_terms": 0, "scanned_fragments": 0,
                     "rebuild": True, "degraded": []}, stats
    assert _terms(pg_store) == {}


def test_rebuild_clears_and_incremental_keeps_manual_term(pg_store):
    """rebuild=True 洗表；默认增量**保留**手工项（不被自动发现覆盖）。"""
    pg_store.write_fragments_batch(_rows())
    with pg_store._tx() as cur:
        cur.execute("INSERT INTO ks_synonym (term, synonyms) VALUES (%s, %s::jsonb)",
                    ("手动词", json.dumps(["手工伙伴"], ensure_ascii=False)))

    incremental = pg_store.discover_synonyms(rebuild=False)
    assert incremental["rebuild"] is False
    assert _terms(pg_store)["手动词"] == ["手工伙伴"], "增量模式必须保留手工添加项"

    pg_store.discover_synonyms(rebuild=True)
    assert "手动词" not in _terms(pg_store), "rebuild=True 必须洗掉历史项"


def test_rebuild_drops_stale_synonym_snapshot(pg_store):
    """🔴 rebuild 之后检索侧的同义词快照**必须作废**。

    快照 TTL 60s，不作废的话：洗表重建 → 下一轮增量拿到的仍是删表前的词表 ⇒
    手工项「复活」、新词进不了查询扩展，而且**不报错**（对照评测直接失真）。
    """
    pg_store.write_fragments_batch(_rows())
    with pg_store._tx() as cur:
        cur.execute("INSERT INTO ks_synonym (term, synonyms) VALUES (%s, %s::jsonb)",
                    ("手动词", json.dumps(["手工伙伴"], ensure_ascii=False)))
    assert "手动词" in pg_store._load_synonym_map()

    pg_store.discover_synonyms(rebuild=True)          # 洗掉了「手动词」
    assert "手动词" not in pg_store._load_synonym_map(), \
        "rebuild 后同义词快照没作废 —— 检索会继续用删表前的词表"


def test_denoises_short_ascii_and_stopwords(pg_store):
    """降噪口径与 Redis/SQLite 同源：**对分词器字面输出零假设**。

    本仓库的老坑：断言 `"embedding" in terms` 依赖 jieba 把 `embedding` 切成一个
    整词；有用户词典的机器上它会被切成 `em be dd in g` ⇒ 同代码两台机两种结论。
    这里改成用**同一条降噪判据 + 同一个分词入口**算出「实际 token 集合」再断言。
    """
    import jieba
    from keepsake.splitter import _STOP_WORDS

    content = "no no to in of embedding model 备份 策略 embedding model 备份 策略"
    rows = _rows(4, content)
    assert pg_store.write_fragments_batch(rows) == 4
    stats = pg_store.discover_synonyms(rebuild=True)

    raw = set(jieba.lcut(content))
    survivors = {w for w in raw
                 if len(w) >= 2 and w not in _STOP_WORDS
                 and w not in storage_shared.DENOISE_STOPWORDS
                 and not w.isdigit() and not storage_shared.is_pure_ascii_short(w)}
    noise = raw - survivors
    assert len(survivors) >= 2, f"样例语料至少留下 2 个候选词，实测 {sorted(raw)}"
    assert noise, f"样例语料应当能造出碎渣词，实测 {sorted(raw)}"

    table = _terms(pg_store)
    assert not (noise & set(table)), f"碎渣词混进了同义词表: {sorted(noise & set(table))}"
    assert set(table) <= survivors, f"表里有没走降噪判据的词: {sorted(set(table) - survivors)}"
    assert survivors <= set(table), f"该进的候选词没进表: {sorted(survivors - set(table))}"
    assert stats["degraded"] == [], stats["degraded"]
    assert stats["total_terms"] == len(table), stats


def test_scan_coverage_shortfall_is_reported_as_degraded(pg_store, monkeypatch):
    """🔴 铁律「不许静默跳过」：分页原语 fail-open ⇒ 覆盖不全必须进 `degraded`。

    制造「只扫到 2 条、库里有 4 条」：让 `get_fragments_batch` 对一半 key 返回空
    （这正是它 fail-open 的形态 —— 只打 WARNING，不抛错）。断言 degraded 点名覆盖度。
    """
    pg_store.write_fragments_batch(_rows())
    orig = pg_store.get_fragments_batch
    monkeypatch.setattr(
        pg_store, "get_fragments_batch",
        lambda keys: {k: v for k, v in orig(keys).items() if k.endswith(("0", "1"))})

    stats = pg_store.discover_synonyms(rebuild=True)
    assert stats["scanned_fragments"] == 2, stats
    assert any("扫描覆盖不全" in r for r in stats["degraded"]), stats
    assert any("扫到 2 条" in r for r in stats["degraded"]), stats


def test_generate_jieba_dict_format_and_determinism(pg_store, tmp_path):
    """词典格式 = `词 词频 nz`，末尾带换行，同语料重跑**逐字节相同**。"""
    pg_store.write_fragments_batch(_rows())

    out = tmp_path / "dicts" / "jieba_dict.txt"
    stats = pg_store.generate_jieba_dict(str(out))
    assert stats["degraded"] == [], stats["degraded"]
    assert stats["written_terms"] > 0
    text = out.read_text(encoding="utf-8")
    assert text.endswith("\n")
    for line in text.splitlines():
        parts = line.split()
        assert len(parts) == 3 and parts[1].isdigit() and parts[2] == "nz", repr(line)
    assert {"备份", "策略"} <= {l.split()[0] for l in text.splitlines()}

    again = tmp_path / "again.txt"
    pg_store.generate_jieba_dict(str(again))
    assert again.read_text(encoding="utf-8") == text, "同语料重跑必须逐字节相同"


def test_generate_jieba_dict_includes_synonym_terms(pg_store, tmp_path):
    """同义词表里的 term 至少算 3 次：手工加的词不该被词频筛掉。"""
    pg_store.write_fragments_batch(_rows(2))
    with pg_store._tx() as cur:
        cur.execute("INSERT INTO ks_synonym (term, synonyms) VALUES (%s, %s::jsonb)",
                    ("罕见手工词", json.dumps(["伙伴"], ensure_ascii=False)))

    out = tmp_path / "d.txt"
    pg_store.generate_jieba_dict(str(out))
    words = {l.split()[0] for l in out.read_text(encoding="utf-8").splitlines()}
    assert "罕见手工词" in words, sorted(words)


# ------------------------------------------------------------- 二、PG ≡ SQLite

def test_pg_and_sqlite_produce_identical_synonym_tables_on_same_corpus(pg_store, tmp_path):
    """🔴 同一份语料 → 两侧 `total_terms`、term 集合、同义词列表**逐个相等**。

    这是「三后端功能完全等价、靠配置切换」的机械证据；共享判定链（storage_shared）
    是它成立的原因，但本测试断言的是**结果**，不依赖实现细节。
    """
    corpus = " ".join([
        _CORPUS,
        "网关 重启 连接池 重建 证书 续签",
        "worker 并发 灰度 上线 回滚 限流",
        "指标 回撤 过滤 链路 计算 模块",
    ])
    rows = _rows(6, corpus)

    pg_store.write_fragments_batch(rows)
    pg_stats = pg_store.discover_synonyms(rebuild=True)
    pg_table = _terms(pg_store)

    sq = SqliteStorage(path=str(tmp_path / "cmp.db"),
                       synonym_min_word_freq=pg_store._synonym_min_word_freq,   # noqa: SLF001
                       synonym_min_co_occurrence=pg_store._synonym_min_co_occurrence)  # noqa: SLF001
    try:
        assert sq.ensure_index() is True
        sq.write_fragments_batch(rows)
        sq_stats = sq.discover_synonyms(rebuild=True)
        with sq._lock:                                                   # noqa: SLF001
            raw = sq._db().execute("SELECT term, synonyms FROM ks_synonym").fetchall()  # noqa: SLF001
    finally:
        sq.close()
    sq_table = {t: sorted(json.loads(v)) for t, v in raw}

    assert pg_stats["total_terms"] == sq_stats["total_terms"], (pg_stats, sq_stats)
    assert set(pg_table) == set(sq_table), (
        f"term 集合不一致：PG 独有 {sorted(set(pg_table) - set(sq_table))[:20]}，"
        f"SQLite 独有 {sorted(set(sq_table) - set(pg_table))[:20]}")
    assert pg_table == sq_table, "term 集合相同但同义词列表不同 —— 合并/截断逻辑漂移"
    assert pg_stats["discovered_groups"] == sq_stats["discovered_groups"]
    assert pg_stats["scanned_fragments"] == sq_stats["scanned_fragments"]


def test_pg_and_sqlite_bind_the_same_shared_corpus_maintenance_functions():
    """🔴 最强证据：两个后端用的**是同一批函数对象**（不是「代码长得像」）。"""
    for func_name in ("synonym_words", "jieba_dict_words", "discover_synonym_pairs",
                      "merge_synonym_maps", "synonym_rows", "jieba_dict_entries"):
        impl = getattr(storage_shared, func_name)
        assert impl.__module__ == "keepsake.storage_shared", func_name
        assert Path(impl.__code__.co_filename).resolve() == Path(storage_shared.__file__).resolve()
    # 阈值默认值三处同源（PG / SQLite / Redis 各自 __init__ 的形参）
    import inspect
    for cls in (PgStorage, SqliteStorage):
        sig = inspect.signature(cls.__init__)
        assert sig.parameters["synonym_min_word_freq"].default == 10, cls
        assert sig.parameters["synonym_jaccard_threshold"].default == 0.5, cls
        assert sig.parameters["synonym_min_co_occurrence"].default == 3, cls


# --------------------------------------------------------------- 三、配置接线

def test_storage_from_config_pg_reads_synonym_thresholds():
    """PG 分支的同义词三阈值必须**真的读配置**（与 redis/sqlite 分支同键同默认）。"""
    from keepsake.storage import storage_from_config

    cfg = {"storage": {"backend": "postgres"},
           "synonym_min_word_freq": 7, "synonym_jaccard_threshold": 0.75,
           "synonym_min_co_occurrence": 5}
    s = storage_from_config(config=cfg)
    assert (s._synonym_min_word_freq, s._synonym_jaccard_threshold,      # noqa: SLF001
            s._synonym_min_co_occurrence) == (7, 0.75, 5)

    d = storage_from_config(config={"storage": {"backend": "postgres"}})
    assert (d._synonym_min_word_freq, d._synonym_jaccard_threshold,      # noqa: SLF001
            d._synonym_min_co_occurrence) == (10, 0.5, 3)   # 缺省 ⇒ 与另两侧同一个默认

    k = storage_from_config(config=cfg, synonym_min_word_freq=99)       # kwargs 胜出
    assert k._synonym_min_word_freq == 99                               # noqa: SLF001
    assert k._synonym_min_co_occurrence == 5                            # noqa: SLF001
