"""合并 / 遗忘的**后端无关**行为测试（2026-10 ks_pmn）。

覆盖点：
  1. 分页扫描：keyset 语义正确（不漏不重）、页大小可控、`next_cursor` 契约
  2. 合并 dry-run：**一个字段都不写**（fake 存储记录全部写调用并断言为空）
  3. 合并真跑：consumed_by / consumed_at / fragment_type 三个字段都落到原料上
  4. 遗忘 dry-run：不删；真跑：只删够格的（保护规则逐条对）
  5. `memory:full:` 前缀在两种后端都返回空（该 keyspace 无写方，见 A 门取证）

设计：用一个**内存 fake 后端**实现 StorageBase 的四个原语 —— 它不连任何真库，
但**逐字复用生产代码路径**（Consolidator / Forgetter 本体不改），所以
「原语契约」和「调用方怎么用原语」两端都被覆盖。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import pytest

from keepsake.consolidator import Consolidator
from keepsake.forgetter import Forgetter
from keepsake.storage_base import StorageBase


class FakeStorage(StorageBase):
    """内存后端：keyset 分页 + 四个维护原语，逐字对齐 PG 侧 SQL 语义。"""

    def __init__(self, rows: Dict[str, Dict[str, str]] | None = None):
        self.rows: Dict[str, Dict[str, str]] = dict(rows or {})
        self.writes: List[str] = []       # 被写的 key（write_fragments_batch）
        self.updates: List[Tuple[str, Dict[str, str]]] = []
        self.deletes: List[str] = []

    # ---- 维护原语 ----
    def scan_fragment_keys(self, cursor: str = "", limit: int = 200,
                           prefix: str = "memory:frag:") -> Tuple[str, List[str]]:
        keys = sorted(k for k in self.rows if k.startswith(prefix) and k > cursor)
        page = keys[:limit]
        if not page:
            return "", []
        if len(page) < limit:
            return "", page
        return page[-1], page

    def get_fragments_batch(self, keys):
        return {k: dict(self.rows[k]) for k in keys if k in self.rows}

    def write_fragments_batch(self, rows):
        for r in rows:
            self.writes.append(r["key"])
            self.rows[r["key"]] = {k: str(v) for k, v in r.items() if k != "key"}
        return len(rows)

    def update_fragment_fields(self, key, fields):
        if key not in self.rows:
            return False
        self.updates.append((key, dict(fields)))
        self.rows[key].update({k: str(v) for k, v in fields.items()})
        return True

    def delete_fragments_batch(self, keys):
        n = 0
        for k in keys:
            self.deletes.append(k)
            if self.rows.pop(k, None) is not None:
                n += 1
        return n

    def fragment_exists(self, key):
        return key in self.rows

    # ---- StorageBase 剩余抽象方法：合并/遗忘路径不碰，占位即可 ----
    def health_check(self): return True
    def ensure_index(self): return True
    def close(self): pass
    def store(self, *a, **k): return False
    def supersede_fragment(self, *a, **k): return False
    def correct_fragments(self, *a, **k): return 0
    def record_feedback(self, *a, **k): return False
    def get_fragment(self, key): return self.rows.get(key)
    def touch_fragment(self, *a, **k): return False
    def set_supersedes(self, *a, **k): return False
    def search(self, *a, **k): return []
    def search_bm25(self, *a, **k): return []
    def search_knn(self, *a, **k): return []
    def match_attention(self, content, top_n=10): return 1.0
    def match_hot_topics(self, *a, **k): return 0.0
    def get_hot_topics(self, *a, **k): return []
    def entity_timeline(self, *a, **k): return []
    def discover_synonyms(self, *a, **k): return {}
    def generate_jieba_dict(self, *a, **k): return {}


def _old(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _frag(text: str, **over) -> Dict[str, str]:
    d = {"content": text, "tags": "", "category": "", "source": "test",
         "created": _old(200), "sentiment_score": "0", "sentiment_label": "flat",
         "feedback_score": "0", "fragment_type": ""}
    d.update(over)
    return d


def _rows(*texts) -> Dict[str, Dict[str, str]]:
    return {f"memory:frag:{i:04d}": _frag(t) for i, t in enumerate(texts)}


# ===========================================================================
# 1. 分页扫描
# ===========================================================================

@pytest.mark.parametrize("limit", [1, 2, 3, 5, 100])
def test_scan_pagination_covers_all_exactly_once(limit):
    """任意页大小下：扫到的 key 集合 == 全集，且无重复、无遗漏。"""
    s = FakeStorage(_rows(*[f"内容{i}" for i in range(7)]))
    expected = set(s.rows)

    seen, cursor, pages = [], "", 0
    while True:
        cursor, keys = s.scan_fragment_keys(cursor=cursor, limit=limit)
        seen.extend(keys)
        pages += 1
        if not cursor:
            break
        assert pages < 50, "分页未收敛"

    assert len(seen) == len(expected) == len(set(seen))
    assert set(seen) == expected


def test_scan_respects_limit_and_returns_keys():
    s = FakeStorage(_rows(*[f"内容{i}" for i in range(10)]))
    _, keys = s.scan_fragment_keys(limit=3)
    assert len(keys) == 3


def test_scan_prefix_memory_full_is_empty_on_both_backends():
    """`memory:full:*` 无写方（A 门取证）⇒ 扫描恒空，Redis/PG 都一样。"""
    s = FakeStorage(_rows("碎片A", "碎片B"))
    cursor, keys = s.scan_fragment_keys(prefix="memory:full:")
    assert keys == [] and cursor == ""


# ===========================================================================
# 2/3. 合并
# ===========================================================================

def _fake_llm(monkeypatch, text: str = "合并后的高层知识条目"):
    monkeypatch.setattr("keepsake.consolidator._call_llm",
                        lambda *a, **k: text)


def test_consolidate_dry_run_writes_nothing(monkeypatch):
    """dry-run：报组数，**不写 consolidated、不标 consumed**。"""
    rows = _rows(
        "PostgreSQL 索引 优化 数据库",
        "PostgreSQL 索引 优化 数据库 性能",
        "PostgreSQL 索引 优化 数据库 调优",
        "完全不同 的话题 内容",
    )
    s = FakeStorage(rows)
    before = {k: dict(v) for k, v in s.rows.items()}
    _fake_llm(monkeypatch)

    stats = Consolidator(s, min_group_size=2, max_age_hours=1,
                         channel={"base_url": "x", "model": "m", "api_key": "k",
                                  "source": "configured"}).consolidate(dry_run=True)

    assert stats["dry_run"] is True
    assert stats["groups_found"] >= 1
    assert stats["merged"] == 0
    assert stats.get("would_merge", 0) > 0
    assert s.writes == [] and s.updates == [] and s.deletes == []
    assert s.rows == before          # 逐字未变


def test_consolidate_marks_sources_consumed(monkeypatch):
    """真跑：新碎片落库 + 原料三字段（consumed_by/at/fragment_type）都写。"""
    rows = _rows(
        "PostgreSQL 索引 优化 数据库",
        "PostgreSQL 索引 优化 数据库 性能",
    )
    s = FakeStorage(rows)
    _fake_llm(monkeypatch)

    stats = Consolidator(s, min_group_size=2, max_age_hours=1,
                         channel={"base_url": "x", "model": "m", "api_key": "k",
                                  "source": "configured"}).consolidate()

    assert stats["merged"] == 2
    assert len(s.writes) == 1
    new_key = s.writes[0]
    assert s.rows[new_key]["fragment_type"] == "consolidated"
    assert s.rows[new_key]["level"] == "2"

    for src in rows:
        assert s.rows[src]["fragment_type"] == "consumed"
        assert s.rows[src]["consumed_by"] == new_key
        assert s.rows[src]["consumed_at"]


def test_consolidate_skips_recent_fragments(monkeypatch):
    """太新的碎片不参与（本轮不动它们）。"""
    rows = _rows(
        "新 碎片 内容 关键词",
        "新 碎片 内容 关键词 第二个",
    )
    for r in rows.values():
        r["created"] = _old(0.001)      # 1 分钟前，远小于 max_age_hours
    s = FakeStorage(rows)
    _fake_llm(monkeypatch)

    stats = Consolidator(s, min_group_size=2, max_age_hours=72).consolidate()
    assert stats["scanned"] == 0
    assert s.writes == [] and s.updates == []


def test_consolidate_skips_already_consumed(monkeypatch):
    rows = _rows(
        "已经 被 吞掉 的 碎片 内容",
        "已经 被 吞掉 的 碎片 内容 两条",
    )
    for r in rows.values():
        r["fragment_type"] = "consumed"
    s = FakeStorage(rows)
    _fake_llm(monkeypatch)

    stats = Consolidator(s, min_group_size=2, max_age_hours=1).consolidate()
    assert stats["scanned"] == 0
    assert s.updates == []


# ===========================================================================
# 4. 遗忘
# ===========================================================================

def test_forget_dry_run_deletes_nothing():
    rows = _rows("很旧 的 低价值 内容 A", "很旧 的 低价值 内容 B")
    s = FakeStorage(rows)
    stats = Forgetter(s, dry_run=True, batch_size=1).forget()
    assert stats["dry_run"] is True
    assert stats["candidates"] == 2
    assert stats["deleted"] == 0
    assert s.deletes == []
    assert len(s.rows) == 2


def test_forget_force_deletes_only_eligible():
    """真跑：只删够格的；consolidated / 正反馈 / 新的 / 高烈度 都受保护。"""
    rows = _rows(
        "低价值 旧 内容 要被删",                       # 0 → 删
        "低价值 旧 内容 要被删 二",                    # 1 → 删
        "高价值 旧 内容 保护 汇总",                     # 2 → consolidated 保护
        "低价值 旧 内容 但有 好评",                    # 3 → feedback>0 保护
        "低价值 新内容 还 很新",                        # 4 → 年龄不够
        "低价值 旧 内容 情绪 激烈",                     # 5 → 烈度高
    )
    rows["memory:frag:0002"]["fragment_type"] = "consolidated"
    rows["memory:frag:0003"]["feedback_score"] = "1"
    rows["memory:frag:0004"]["created"] = _old(1)
    rows["memory:frag:0005"]["sentiment_score"] = "1.5"
    s = FakeStorage(rows)

    stats = Forgetter(s, dry_run=True, batch_size=2).forget(force=True)

    assert stats["deleted"] == 2
    assert set(s.deletes) == {"memory:frag:0000", "memory:frag:0001"}
    assert stats["skipped_protected"] == 2      # consolidated + 正反馈
    assert len(s.rows) == 4


def test_forget_protects_hermes_agent_nonnegative_feedback():
    rows = _rows("agent 存的 内容 手动记忆")
    rows["memory:frag:0000"]["source"] = "hermes_agent"
    s = FakeStorage(rows)
    stats = Forgetter(s, dry_run=False).forget()
    assert stats["candidates"] == 0
    assert stats["deleted"] == 0
    assert len(s.rows) == 1