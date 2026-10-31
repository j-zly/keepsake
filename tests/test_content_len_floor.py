"""检索面长度地板（2026-10 ks_lenfloor）—— 600 硬上限改「可配置地板」。

🔴 **为什么有这一单**：检索 WHERE 里的 `length(content) <= 600` 硬谓词把
103 条 600–2408 字符的**正经结构化长条目**整条藏了起来 —— 条目 `get` 取得到、
`search` 零召回。600 当年是防噪音经验值，用户拍板「不接受硬上限当产品边界」
⇒ 改成**可配置地板**（`retrieval.max_content_len`，缺省 5000）。

本文件钉住三件事（红绿双向）：

  1. **缺省地板下 700 字条目可召回**（改前是红：被 600 硬谓词挡掉）；
  2. **>5000 字条目仍隐身**（地板还在，只是抬高）；
  3. **改配置产生可观测差异**：显式配 `max_content_len=600` ⇒ 700 字条目回到隐身
     （证明配置真的接到了使用点，而不是只改了个常量）。

三后端：SQLite **真测**（真库、真 FTS5、真 BM25）；PG 打「真 SQL 谓词」
（发出的 SQL 里就是实例值）+ 有真库凭据时跑**真库端到端**（无凭据则 skip）；
Redis 按本仓库既有惯例用**假 client**（仓库没有真 Redis 测试库）。

本文件 **hermetic**：不连任何外部服务、不写任何真实记忆库。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from keepsake.core import shared as storage_shared
from keepsake.core.shared import (
    DEFAULT_MAX_CONTENT_LEN,
    content_len_clause,
    is_noise_length,
    max_content_len_from_config,
    resolve_max_content_len,
)
from keepsake.storage import RedisStorage, storage_from_config
from keepsake.storage_pg import PgStorage
from keepsake.storage_sqlite import SqliteStorage

CORE_DIR = Path(__file__).resolve().parent.parent / "src" / "keepsake" / "core"

#: 唯一探针词（ASCII）：保证「能不能被召回」只取决于长度谓词，不受分词/词频干扰。
PROBE = "lenfloorprobe"
#: 超长探针词（>5000 字的条目用它验「真超长仍隐身」）。
HUGE_PROBE = "lenfloorhuge"

#: 填充料（合成运维套话，无任何真实记忆内容）。
FILLER = ("交易系统按日切片加载行情快照并落盘归档；运维同学在变更窗口内滚动重启撮合进程；"
          "灰度期间新旧两套配置并行运行十五分钟，比对差异后择机切换。")

#: 夹具长度：700（>600 旧地板、<5000 新地板）与 5200（>5000 新地板）。
LEN_MID = 700
LEN_HUGE = 5200


def _make_content(token: str, length: int) -> str:
    """造一条指定长度的正文（开头就是唯一探针词，后面全是填充料）。"""
    text = (token + " " + FILLER + " ") * 120
    assert len(text) >= length, "填充料不够长"
    return text[:length]


# ---------------------------------------------------------------------------
# 1. 缺省值与「不再有第二处写死的 600」
# ---------------------------------------------------------------------------

def test_default_floor_is_5000_and_shared_by_three_backends():
    """缺省地板 5000，三后端**同一个类级默认**（单点常量，不各写一份）。"""
    assert DEFAULT_MAX_CONTENT_LEN == 5000
    assert PgStorage.MAX_CONTENT_LEN == 5000
    assert SqliteStorage.MAX_CONTENT_LEN == 5000
    assert RedisStorage.MAX_CONTENT_LEN == 5000
    assert PgStorage.MAX_CONTENT_LEN is DEFAULT_MAX_CONTENT_LEN
    assert SqliteStorage.MAX_CONTENT_LEN is DEFAULT_MAX_CONTENT_LEN
    assert RedisStorage.MAX_CONTENT_LEN is DEFAULT_MAX_CONTENT_LEN


def test_no_literal_600_left_in_retrieval_predicates():
    """三后端源码里**不许**再出现写死的长度阈值（`> 600` / `<= 600`）。

    长度谓词只允许存在于 `core/shared.py` 一处（`content_len_clause`）——
    三处各写一份必然漂移。
    """
    for name in ("pg.py", "sqlite.py", "redis.py"):
        src = (CORE_DIR / name).read_text(encoding="utf-8")
        assert "> 600" not in src, f"{name} 仍写死「> 600」"
        assert "<= 600" not in src, f"{name} 仍写死「<= 600」"
        assert "length(content)" not in src, f"{name} 又自己拼了一遍长度谓词"
    shared_src = (CORE_DIR / "shared.py").read_text(encoding="utf-8")
    assert shared_src.count("content <> '' AND length(content)") == 1, (
        "长度谓词的 SQL 片段必须只在 shared.py 一处")


def test_helper_is_single_point_for_three_backends():
    """判据单点：三个后端引用的是**同一个函数对象**（不是各抄一份）。"""
    from keepsake.core import pg as pg_mod
    from keepsake.core import redis as redis_mod
    from keepsake.core import sqlite as sqlite_mod

    assert pg_mod.content_len_clause is content_len_clause
    assert sqlite_mod.content_len_clause is content_len_clause
    assert redis_mod.is_noise_length is is_noise_length
    assert redis_mod.resolve_max_content_len is resolve_max_content_len


# ---------------------------------------------------------------------------
# 2. 判据本身：地板语义
# ---------------------------------------------------------------------------

def test_is_noise_length_semantics():
    """超地板 = 噪音（> 才是噪音；恰好等于地板仍然放行）。"""
    assert is_noise_length(701, 600) is True
    assert is_noise_length(600, 600) is False
    assert is_noise_length(5001, 5000) is True
    assert is_noise_length(5000, 5000) is False


def test_content_len_clause_shape():
    """SQL 片段逐字钉死（PG 与 SQLite 方言同形）。"""
    assert content_len_clause(5000) == "content <> '' AND length(content) <= 5000"
    assert content_len_clause(600) == "content <> '' AND length(content) <= 600"


@pytest.mark.parametrize("raw", [0, -1, True, False, 1.5, "abc", "", "  "])
def test_invalid_floor_falls_back_to_default(raw):
    """非法值 ⇒ 回落 5000（bool / float / 非整数串 一律拒）。"""
    assert resolve_max_content_len(raw) == DEFAULT_MAX_CONTENT_LEN


def test_valid_floor_accepted_in_int_and_digit_string_form():
    """只认 int 与「整数形态的字符串」（与 core_char_budget 同一把尺）。"""
    assert resolve_max_content_len(600) == 600
    assert resolve_max_content_len("600") == 600
    assert resolve_max_content_len(" 600 ") == 600


def test_config_reader_prefers_retrieval_section(caplog):
    """`retrieval.max_content_len` 优先于顶层同名键；缺省回落 5000。"""
    assert max_content_len_from_config({"retrieval": {"max_content_len": 700},
                                         "max_content_len": 600}) == 700
    assert max_content_len_from_config({"max_content_len": 600}) == 600
    assert max_content_len_from_config({}) == DEFAULT_MAX_CONTENT_LEN
    assert max_content_len_from_config(None) == DEFAULT_MAX_CONTENT_LEN


def test_invalid_config_value_warns_and_falls_back(caplog):
    """非法配置值 ⇒ 回落 5000 **并告警**（对齐「非法值回落+告警」纪律）。"""
    storage_shared._LENFLOOR_WARNED.clear()          # noqa: SLF001 — 告警去重表，测试里重置
    with caplog.at_level(logging.WARNING, logger="keepsake.storage_shared"):
        assert max_content_len_from_config({"retrieval": {"max_content_len": "六百"}}) == 5000
    assert any("max_content_len" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 3. SQLite 真测（真库 + 真 FTS5 + 真 BM25）
# ---------------------------------------------------------------------------

def _seed_sqlite(path: str) -> None:
    """落两条夹具：700 字（探针 PROBE）与 5200 字（探针 HUGE_PROBE）。"""
    st = SqliteStorage(path=path)
    assert st.ensure_index() is True
    assert st.store(_make_content(PROBE, LEN_MID), tags="lenfloor", category="fact") is True
    assert st.store(_make_content(HUGE_PROBE, LEN_HUGE), tags="lenfloor", category="fact") is True
    st.close()


def _open_sqlite(path: str, **kw: Any) -> SqliteStorage:
    st = SqliteStorage(path=path, final_limit=10, is_primary=True, **kw)
    assert st.ensure_index() is True
    return st


def test_sqlite_700_char_entry_is_recalled_under_default_floor(tmp_path):
    """绿：缺省地板（5000）下 700 字条目**能召回**（改前被 600 硬谓词挡掉）。"""
    path = str(tmp_path / "lenfloor.db")
    _seed_sqlite(path)
    st = _open_sqlite(path)
    try:
        hits = st.search_bm25(PROBE) or []
        assert any(PROBE in str(h.get("content", "")) for h in hits), (
            f"700 字条目应可召回，实际召回 {len(hits)} 条")
    finally:
        st.close()


def test_sqlite_700_char_entry_is_present_in_db(tmp_path):
    """条目**确实写进库里**（召回失败不是写入侧的问题）。"""
    path = str(tmp_path / "lenfloor.db")
    _seed_sqlite(path)
    st = _open_sqlite(path)
    try:
        with st._lock:                                        # noqa: SLF001 — 只读计数
            n = st._db().execute(                             # noqa: SLF001
                "SELECT COUNT(*) FROM ks_fragment WHERE content LIKE ?",
                (f"%{PROBE}%",)).fetchone()[0]
        assert n == 1, "700 字夹具应当写进库里"
    finally:
        st.close()


def test_sqlite_entry_over_5000_chars_stays_hidden(tmp_path):
    """>5000 字的条目仍然隐身（地板还在，只是抬高了）。"""
    path = str(tmp_path / "lenfloor.db")
    _seed_sqlite(path)
    st = _open_sqlite(path)
    try:
        hits = st.search_bm25(HUGE_PROBE) or []
        assert not any(HUGE_PROBE in str(h.get("content", "")) for h in hits), (
            "超地板条目不该进检索面")
    finally:
        st.close()


def test_sqlite_explicit_floor_600_hides_700_char_entry(tmp_path):
    """配置接线生效：显式配 600 ⇒ 700 字条目回到隐身（可观测差异）。"""
    path = str(tmp_path / "lenfloor.db")
    _seed_sqlite(path)
    st = _open_sqlite(path, max_content_len=600)
    try:
        assert st._max_content_len == 600
        hits = st.search_bm25(PROBE) or []
        assert not any(PROBE in str(h.get("content", "")) for h in hits), (
            "地板配回 600 后 700 字条目应当隐身")
    finally:
        st.close()


def test_sqlite_floor_700_boundary_recalls_701_char_entry(tmp_path):
    """边界自证：地板 700 时 701 字条目隐身、700 字条目放行（判据没写歪）。"""
    path = str(tmp_path / "lenfloor.db")
    st = SqliteStorage(path=path)
    assert st.ensure_index() is True
    st.store(_make_content(PROBE, 700), tags="lenfloor", category="fact")
    st.store(_make_content(HUGE_PROBE, 701), tags="lenfloor", category="fact")
    st.close()

    st = _open_sqlite(path, max_content_len=700)
    try:
        hits = st.search_bm25(HUGE_PROBE) or []
        assert not any(HUGE_PROBE in str(h.get("content", "")) for h in hits)
        hits = st.search_bm25(PROBE) or []
        assert any(PROBE in str(h.get("content", "")) for h in hits)
    finally:
        st.close()


# ---------------------------------------------------------------------------
# 4. PG：谓词生成（真 SQL）+ 有真库凭据时的端到端
# ---------------------------------------------------------------------------

def test_pg_predicate_uses_instance_floor(monkeypatch):
    """PG 侧发出的 SQL 里就是**实例值**（默认 5000 / 显式 600 / 显式 7000）。"""
    # `_contract_v2_missing()` 会探库（缺列探测）——这里没有真库，钉住「不是缺列」
    monkeypatch.setattr(PgStorage, "_contract_v2_missing", lambda self: set())

    def _clause(**kw: Any) -> str:
        st = PgStorage(**kw)
        sql, _params = st._search_filter_sql("", "", True)
        return sql

    assert "length(content) <= 5000" in _clause()
    assert "length(content) <= 600" in _clause(max_content_len=600)
    assert "length(content) <= 7000" in _clause(max_content_len=7000)


def test_pg_instance_value_overrides_class_default():
    """实例值覆盖类级默认（构造参数 → `self._max_content_len`）。"""
    assert PgStorage()._max_content_len == PgStorage.MAX_CONTENT_LEN      # noqa: SLF001
    assert PgStorage(max_content_len=600)._max_content_len == 600          # noqa: SLF001
    # 非法值 ⇒ 回落类级默认 + 告警
    storage_shared._LENFLOOR_WARNED.clear()          # noqa: SLF001
    assert PgStorage(max_content_len=0)._max_content_len == 5000           # noqa: SLF001
    assert PgStorage(max_content_len=True)._max_content_len == 5000        # noqa: SLF001


# --- PG 真库端到端（凭据规则与 tests/test_storage_pg.py 逐字同款）---

_PG_ENV_PATH = Path.home() / ".keepsake_pg_test.env"
_PG_ALLOWED_HOST = os.environ.get("KEEPSAKE_PG_TEST_HOST", "postgres.test.example")
_PG_ALLOWED_DBNAME = "keepsake_test"


def _pg_dsn() -> str:
    if not _PG_ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", _PG_ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


def _pg_dsn_is_safe(dsn: str) -> bool:
    return bool(dsn) and _PG_ALLOWED_HOST in dsn and _PG_ALLOWED_DBNAME in dsn


@pytest.fixture()
def pg_storage():
    """真库 PG 实例；无凭据 / 不装 psycopg ⇒ skip（本机没库时也要能跑全套）。"""
    pytest.importorskip("psycopg", reason="未装 psycopg")
    dsn = _pg_dsn()
    if not _pg_dsn_is_safe(dsn):
        pytest.skip(f"无测试库凭据或指向非 {_PG_ALLOWED_HOST}/{_PG_ALLOWED_DBNAME}，跳过 PG 真库测试")
    st = PgStorage(dsn=dsn, connect_timeout=30, final_limit=10, is_primary=True)
    assert st.ensure_index() is True
    yield st
    st.close()


def test_pg_real_db_700_char_entry_recall_and_floor_override(pg_storage):
    """PG 真库：700 字条目可召回；显式地板 600 ⇒ 回到隐身。"""
    st = pg_storage
    assert st.store(_make_content(PROBE, LEN_MID), tags="lenfloor", category="fact") is True
    try:
        hits = st.search_bm25(PROBE) or []
        assert any(PROBE in str(h.get("content", "")) for h in hits)

        st._max_content_len = 600                     # noqa: SLF001 — 显式地板
        hits = st.search_bm25(PROBE) or []
        assert not any(PROBE in str(h.get("content", "")) for h in hits)
    finally:
        st.delete_fragments_batch([st._key_for_content(_make_content(PROBE, LEN_MID))])  # noqa: SLF001


# ---------------------------------------------------------------------------
# 5. Redis：假 client（仓库没有真 Redis 测试库，见 tests/test_storage_redis.py 惯例）
# ---------------------------------------------------------------------------

class _FakeDoc:
    """RediSearch 文档的最小替身（属性名与 redis-py 返回的 doc 一致）。"""

    def __init__(self, key: str, content: str, score: float = 1.0) -> None:
        self.id = key
        self.content = content
        self.tags = "lenfloor"
        self.score = score
        self.created = "2026-10-01T00:00:00+00:00"
        self.invalid_at = ""
        self.valid_until = ""


class _FakeFt:
    def __init__(self, docs: List[_FakeDoc]) -> None:
        self._docs = docs

    def search(self, query: Any) -> Any:
        # 假 client **不解析查询式**：一律返回全部候选 —— 长度过滤因此可被单独观察
        class _Result:
            docs = self._docs
        return _Result()


class _FakeRedis:
    """只实现 search_bm25 走到的命令；其余一律抛（调用点自带 fail-open）。"""

    def __init__(self, docs: List[_FakeDoc]) -> None:
        self._docs = docs

    def ping(self) -> bool:
        return True

    def ft(self, _index: str) -> _FakeFt:
        return _FakeFt(self._docs)

    def zscan(self, *_a: Any, **_kw: Any):
        return 0, []

    def zscore(self, *_a: Any, **_kw: Any):
        return None


def _redis_storage(monkeypatch, docs: List[_FakeDoc], **kw: Any) -> RedisStorage:
    client = _FakeRedis(docs)
    monkeypatch.setattr(RedisStorage, "_get_client", lambda self: client)
    st = RedisStorage(query_expansion_enabled=False, final_limit=10,
                       is_primary=True, **kw)
    st._load_synonym_map = lambda: {}      # noqa: SLF001 — 不连真 Redis 拉同义词表
    st._fire_hits = lambda fragments: None  # noqa: SLF001 — 侧写打点不参与本用例
    return st


def test_redis_search_bm25_applies_instance_floor(monkeypatch):
    """Redis 侧判据同源：缺省地板放行 700 字条目、挡掉 5200 字条目。"""
    docs = [_FakeDoc("k-mid", _make_content(PROBE, LEN_MID)),
            _FakeDoc("k-huge", _make_content(HUGE_PROBE, LEN_HUGE))]
    st = _redis_storage(monkeypatch, docs)
    hits = st.search_bm25(PROBE) or []
    contents = [str(h.get("content", "")) for h in hits]
    assert any(PROBE in c for c in contents), "700 字条目应当放行"
    assert not any(HUGE_PROBE in c for c in contents), "超地板条目应当被挡掉"


def test_redis_explicit_floor_600_hides_700_char_entry(monkeypatch):
    """Redis 侧显式地板 600 ⇒ 700 字条目也隐身（配置真的到了使用点）。"""
    docs = [_FakeDoc("k-mid", _make_content(PROBE, LEN_MID))]
    st = _redis_storage(monkeypatch, docs, max_content_len=600)
    assert st._max_content_len == 600      # noqa: SLF001
    hits = st.search_bm25(PROBE) or []
    assert not any(PROBE in str(h.get("content", "")) for h in hits)


# ---------------------------------------------------------------------------
# 6. 配置 → 实例的接线（storage_from_config 是唯一配置装配入口）
# ---------------------------------------------------------------------------

def _sqlite_cfg(tmp_path: Path, **extra: Any) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "storage": {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "cfg.db")}},
    }
    cfg.update(extra)
    return cfg


def test_storage_from_config_reads_retrieval_max_content_len(tmp_path):
    """配置装配入口必须真的读 `retrieval.max_content_len`（改配置产生可观测差异）。"""
    st = storage_from_config(config=_sqlite_cfg(tmp_path,
                                                 retrieval={"max_content_len": 600}))
    try:
        assert st._max_content_len == 600      # noqa: SLF001
    finally:
        st.close()


def test_storage_from_config_falls_back_to_5000(tmp_path):
    """键缺失 ⇒ 缺省 5000（不静默取旧值 600）。"""
    st = storage_from_config(config=_sqlite_cfg(tmp_path))
    try:
        assert st._max_content_len == DEFAULT_MAX_CONTENT_LEN   # noqa: SLF001
    finally:
        st.close()


def test_storage_from_config_invalid_value_falls_back(tmp_path):
    """非法值 ⇒ 回落 5000（fail-closed 到缺省，不取 0/负数当地板）。"""
    st = storage_from_config(config=_sqlite_cfg(tmp_path,
                                                 retrieval={"max_content_len": -5}))
    try:
        assert st._max_content_len == DEFAULT_MAX_CONTENT_LEN   # noqa: SLF001
    finally:
        st.close()
