"""Hermes 桥接写入路径的常驻层写闸 —— 代理写入**最高只到 core_candidate**。

🔴 **本文件锁死的是「代理写不进常驻层」**（2026-11 keepsake_core_gate）：

  1. `_core_write("add", …)` 落的碎片带 `core_candidate` + 提名日戳，**不带** `core`
  2. 常驻注入面（`_core_fragments` / `system_prompt_block`）**看不见**候选
  3. 提名即带基数：落库后按 `tiering.nominate_backfill_hits` 回填召回基数
  4. 匹配面（幂等查重 / 删除定位）= core ∪ core_candidate ⇒ replace 不留双份、
     remove 删得到（候选落库后旧的常驻面匹配会静默失效）
  5. 存量裸 `core` 条目（人工批准过的）仍删得到 —— 并集匹配回归

除 SQLite 真库用例外全部 hermetic（内存 fake storage，零网络零数据库）。
🔴 **禁止 `RedisStorage()` 无参直调**：本机残留 redis 在应答，会真写数据。
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List

import pytest

from keepsake import KeepsakeProvider, _CORE_CACHE, _has_tag
from keepsake.adapters.hermes import provider as provider_mod
from keepsake.storage_sqlite import SqliteStorage


# ---------------------------------------------------------------------------
# fake storage：只实现本文件用到的原语
# ---------------------------------------------------------------------------

class FakeStorage:
    """内存 fake —— `list_by_tag` / `store` / `delete_fragments_batch` /
    `fragment_exists` / `record_hits_repeat`。

    key 算法与三个真后端同款（`memory:frag:{sha256(text)[:12]}`）——
    这样「回填打到的 key」与「真落库的 key」是同一个 ⇒ 断言才有意义。
    """

    def __init__(self, rows: List[Dict[str, Any]] | None = None):
        self.rows: List[Dict[str, Any]] = [dict(r) for r in (rows or [])]
        self.store_calls: List[Dict[str, Any]] = []
        self.deleted: List[str] = []
        self.hits_calls: List[tuple] = []

    def list_by_tag(self, tag: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.rows if _has_tag(r.get("tags", ""), tag)]

    def fragment_exists(self, key: str) -> bool:
        return any(r.get("key") == key for r in self.rows)

    def store(self, text: str, tags: str = "", category: str = "", source: str = "",
              fragment_type: str = "", **_kw) -> bool:
        key = f"memory:frag:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        self.store_calls.append({"key": key, "text": text, "tags": tags,
                                 "category": category, "source": source,
                                 "fragment_type": fragment_type})
        self.rows.append({"key": key, "content": text, "tags": tags, "updated_at": ""})
        return True

    def delete_fragments_batch(self, keys: List[str]) -> int:
        self.deleted.extend(keys)
        self.rows = [r for r in self.rows if r.get("key") not in keys]
        return len(keys)

    def record_hits_repeat(self, key: str, times: int) -> int:
        """提名基数回填的原语 —— 只**计数**，不真写 3000 条命中事件。"""
        self.hits_calls.append((key, int(times)))
        return int(times)


def _provider(storage) -> KeepsakeProvider:
    """构造一个只挂了 storage 的 provider（跳过 initialize，不连任何后端）。"""
    p = object.__new__(KeepsakeProvider)
    p._config = {}
    p._storage = storage
    p._tag_filter = ""
    p._skip_min_length = 2
    p._skip_patterns = set()
    return p


@pytest.fixture(autouse=True)
def _clear_core_cache():
    """核心层缓存是进程级的 —— 每个用例前后清干净，否则跨用例串味。"""
    _CORE_CACHE["ts"] = 0.0
    _CORE_CACHE["frags"] = None
    yield
    _CORE_CACHE["ts"] = 0.0
    _CORE_CACHE["frags"] = None


@pytest.fixture
def backfill_count(monkeypatch) -> int:
    """把提名基数条数钉成 7（真实缺省 3000 —— 不真写 3000 条，只断言计数）。"""
    monkeypatch.setattr(provider_mod, "_nominate_backfill_hits", lambda: 7)
    return 7


PROBE = "【探针】端口 8091，路径 /opt/net/x"


def _key_of(text: str) -> str:
    return f"memory:frag:{hashlib.sha256(text.encode()).hexdigest()[:12]}"


# ---------------------------------------------------------------------------
# 一、写入落候选：带候选标 + 提名日戳，不带 core
# ---------------------------------------------------------------------------

def test_add_lands_as_candidate_not_core(backfill_count):
    """🔴 代理写入的常驻条目最高只到 `core_candidate`（带提名日戳）——**不带** `core`。

    不带 `core` 是硬红线：带了就等于一写就进常驻层（这正是本单要治的病）。
    """
    st = FakeStorage()
    _provider(st)._core_write("add", "memory", PROBE, {})
    assert len(st.store_calls) == 1
    tags = st.store_calls[0]["tags"]
    assert _has_tag(tags, "core_candidate"), "必须落候选标"
    assert not _has_tag(tags, "core"), "代理写入不许直接落常驻层"
    assert "tier:auto-hot:" in tags, "候选必须带提名日戳（否则班车永远不审它）"
    assert st.store_calls[0]["fragment_type"] == "core"


def test_candidate_is_invisible_to_resident_injection(backfill_count):
    """🔴 常驻注入面看不见候选 —— 未经人工批准的条目绝不许每轮进 system prompt。"""
    st = FakeStorage()
    p = _provider(st)
    p._core_write("add", "memory", PROBE, {})
    assert p._core_fragments() == [], "候选不该出现在常驻面"
    assert PROBE not in p.system_prompt_block()


def test_nomination_backfills_hits_on_the_new_key(backfill_count):
    """提名即带基数：落库后按配置条数回填召回基数，打在**这条新碎片自己的 key** 上。"""
    st = FakeStorage()
    _provider(st)._core_write("add", "memory", PROBE, {})
    assert st.hits_calls == [(_key_of(PROBE), 7)], "基数必须补在新候选的 key 上"


def test_backfill_is_skipped_when_count_is_zero(monkeypatch):
    """配置 `nominate_backfill_hits = 0` ⇒ 不回填（回填是附属品，不是提名本身）。"""
    monkeypatch.setattr(provider_mod, "_nominate_backfill_hits", lambda: 0)
    st = FakeStorage()
    _provider(st)._core_write("add", "memory", PROBE, {})
    assert len(st.store_calls) == 1
    assert st.hits_calls == []


def test_add_twice_is_idempotent(backfill_count):
    """同一条写两次 ⇒ 库里仍只有一条（重复注入是用户能看出来的故障）。"""
    st = FakeStorage()
    p = _provider(st)
    p._core_write("add", "memory", PROBE, {})
    st.store_calls.clear()
    p._core_write("add", "memory", PROBE, {})
    assert st.store_calls == []


# ---------------------------------------------------------------------------
# 二、匹配面扩到候选：replace 不留双份、remove 删得到
# ---------------------------------------------------------------------------

def test_replace_on_candidate_surface_leaves_no_duplicate(backfill_count):
    """🔴 候选面也要能 replace：旧候选删掉、新候选写入 —— 绝不留下同一事实两份。

    匹配面若只看 `core`，旧候选删不掉 ⇒ 库里两份（这正是收紧后最容易出的故障）。
    """
    st = FakeStorage()
    p = _provider(st)
    p._core_write("add", "memory", "端口是 8091", {})
    assert len(st.rows) == 1
    p._core_write("replace", "memory", "端口是 9091", {"old_text": "端口是 8091"})
    assert [r["content"] for r in st.rows] == ["端口是 9091"], "replace 必须删旧留新"
    assert st.deleted == [_key_of("端口是 8091")]


def test_remove_on_candidate_surface_deletes_the_entry(backfill_count):
    """候选面也要能 remove（老_text 是短子串时同样删得到）。"""
    st = FakeStorage()
    p = _provider(st)
    p._core_write("add", "memory", "端口是 8091", {})
    p._core_write("remove", "memory", "", {"old_text": "端口是 8091"})
    assert st.rows == [], "remove 必须删得到候选"
    assert st.deleted == [_key_of("端口是 8091")]


def test_legacy_core_entry_is_still_removable(backfill_count):
    """并集匹配的回归：存量裸 `core` 条目（人工批准过的）照样删得到。"""
    st = FakeStorage([{"key": "memory:frag:legacy", "content": "人工批准过的常驻条目",
                       "tags": "core,memory", "updated_at": ""}])
    p = _provider(st)
    p._core_write("remove", "memory", "", {"old_text": "人工批准过的常驻条目"})
    assert st.deleted == ["memory:frag:legacy"]
    assert st.rows == []


def test_candidate_tags_are_not_matched_as_core(backfill_count):
    """🔴 判标签是**逗号边界的完整标签相等**：`core_candidate` 不是 `core`。

    拿子串 grep 判标签（`"core" in tags`）会把候选误认成常驻 ⇒ 候选直接进注入面。
    """
    st = FakeStorage()
    p = _provider(st)
    p._core_write("add", "memory", PROBE, {})
    row = st.rows[-1]
    assert _has_tag(row.get("tags", ""), "core") is False
    assert p._core_fragments() == []


# ---------------------------------------------------------------------------
# 三、SQLite 真库：候选真的落成候选，且不进常驻面（hermetic，tmp_path 临时库）
# ---------------------------------------------------------------------------

def test_sqlite_candidate_is_stored_as_candidate_and_hidden_from_core(tmp_path):
    """真库串一遍：落库的 tags 带候选标、`list_by_tag("core")` 看不见它。"""
    s = SqliteStorage(path=str(tmp_path / "candidate.db"))
    assert s.ensure_index() is True
    try:
        p = _provider(s)
        p._core_write("add", "memory", PROBE, {})
        rows = s.list_by_tag("core_candidate")
        assert [r["content"] for r in rows] == [PROBE]
        assert s.list_by_tag("core") == []
        assert p._core_fragments() == []
    finally:
        s.close()
