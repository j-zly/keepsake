"""静默降级面的护栏（2026-11-18 ks_degradation_robustness_r1）。

四处审查定罪的「静默降级」面，各钉一条**可定向跑**的用例
（定向跑：``pytest tests/test_degradation_failclosed.py -k failclosed``）：

1. **forgetter 注意力 fail-closed**（``attention-failclosed``）：``match_attention``
   抛异常 ⇒ **保留**该条（查询失败 ≠ 保护确认，必须 fail-closed），
   绝不把高关注记忆划进可遗忘表。
2. **shared 加权失败必须吭声**：``match_hot_topics`` / ``match_attention`` 抛异常
   ⇒ 至少留一条 ``加权查询失败`` 的 warning（判据本身不动）。
3. **维护班车 errors>0 报红**：合并有失败 ⇒ ``run()`` 返回 ``ok=False``。
4. **bus._pg_conf 单源**：配置路径走 ``KEEPSAKE_CONFIG``（与主存储同一份），
   缺 ``storage.postgres`` 节 ⇒ 抛 ``ValueError``（人话），绝不静默回退默认路径。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import pytest

from keepsake.core.ops.forgetter import Forgetter
from keepsake.core.shared import rerank_with_decay


# --------------------------------------------------------------------------
# 假存储（只回答遗忘 / 重排两条路径用到的方法）
# --------------------------------------------------------------------------

class _FakeStore:
    """内存假后端：加权查询可配置成「炸」（测 fail-closed / 吭声）。"""

    def __init__(self, rows: Dict[str, Dict[str, str]], *, weight_query_raises: bool = False):
        self.rows = dict(rows)
        self.weight_query_raises = weight_query_raises
        self.deletes: List[str] = []
        self._decay_half_days = 30      # noqa: SLF001 —— 重排路径要读的实例配置

    def scan_fragment_keys(self, cursor: str = "", limit: int = 200,
                           prefix: str = "memory:frag:") -> Tuple[str, List[str]]:
        keys = sorted(k for k in self.rows if k.startswith(prefix) and k > cursor)
        page = keys[:limit]
        if not page:
            return "", []
        return (page[-1], page) if len(page) >= limit else ("", page)

    def get_fragments_batch(self, keys):
        return {k: dict(self.rows[k]) for k in keys if k in self.rows}

    def delete_fragments_batch(self, keys) -> int:
        n = 0
        for k in keys:
            self.deletes.append(k)
            if self.rows.pop(k, None) is not None:
                n += 1
        return n

    def match_attention(self, content, top_n: int = 10) -> float:
        if self.weight_query_raises:
            raise RuntimeError("注意力查询炸了（测试剧本）")
        return 1.0

    def match_hot_topics(self, text, limit: int = 10) -> float:
        if self.weight_query_raises:
            raise RuntimeError("热词查询炸了（测试剧本）")
        return 0.0


def _old(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _rows(*texts: str) -> Dict[str, Dict[str, str]]:
    return {
        f"memory:frag:{i:04d}": {
            "content": t, "tags": "", "category": "", "source": "test",
            "created": _old(200), "sentiment_score": "0", "sentiment_label": "flat",
            "feedback_score": "0", "fragment_type": "",
        }
        for i, t in enumerate(texts)
    }


# --------------------------------------------------------------------------
# 1. forgetter 注意力 fail-closed
# --------------------------------------------------------------------------

def test_forget_attention_failclosed_keeps_entry(caplog):
    """🔴 ``match_attention`` 抛异常 ⇒ 该条**不进**可遗忘表（fail-closed）。

    改前是 ``except Exception: pass`` ⇒ 异常时不 continue ⇒ 高关注记忆被划进
    可遗忘表（保护机制静默失效）。改后：查询失败 = 无法确认保护 ⇒ 保留 + 吭声。
    """
    rows = _rows("很旧 的 低价值 内容 A")
    s = _FakeStore(rows, weight_query_raises=True)

    with caplog.at_level(logging.WARNING, logger="keepsake.forgetter"):
        stats = Forgetter(s, dry_run=True).forget()

    assert stats["candidates"] == 0, "注意力查询失败时不得把条目划进可遗忘表"
    assert s.deletes == [], "不得删任何条目"
    assert any("注意力查询失败" in r.message for r in caplog.records), \
        "fail-closed 必须吭声（warning）"


def test_forget_attention_failclosed_baseline_still_forgets():
    """对照组：注意力查询正常返回低权重 ⇒ 仍按原判据可遗忘（没把口径改死）。"""
    rows = _rows("很旧 的 低价值 内容 A")
    s = _FakeStore(rows)

    stats = Forgetter(s, dry_run=True).forget()

    assert stats["candidates"] == 1


# --------------------------------------------------------------------------
# 2. shared 加权失败必须吭声
# --------------------------------------------------------------------------

def test_rerank_weight_query_failclosed_warns(caplog):
    """🔴 加权查询炸了 ⇒ 至少一条 ``加权查询失败`` warning（排序权重回落 1.0）。"""
    s = _FakeStore(_rows("内容"), weight_query_raises=True)
    frags = [{"content": "内容", "created": _old(1), "tags": "",
              "sentiment_score": "0", "feedback_score": "0", "_bm25_score": 1.0}]

    with caplog.at_level(logging.WARNING, logger="keepsake.storage_shared"):
        out = rerank_with_decay(s, frags)

    assert len(out) == 1
    assert sum("加权查询失败" in r.message for r in caplog.records) >= 2, \
        "热门话题 / 注意力两处加权失败都必须吭声"
    assert out[0]["_weights"]["hot_topic"] == 1.0
    assert out[0]["_weights"]["attention"] == 1.0


# --------------------------------------------------------------------------
# 3. 维护班车 errors>0 报红
# --------------------------------------------------------------------------

def test_maintenance_failclosed_errors_reports_red(monkeypatch):
    """🔴 合并有失败（errors>0）⇒ ``run()`` 返回 ``ok=False``（班车记红）。"""
    from keepsake.api import audit
    from keepsake.buses import maintenance as maint_mod
    from keepsake.core import factory
    from keepsake.core.ops import consolidator as cons_mod
    from keepsake.core.ops import forgetter as forg_mod

    class _Store:
        def close(self): pass

    class _Cons:
        def consolidate(self, dry_run: bool = False) -> Dict[str, Any]:
            return {"scanned": 3, "groups_found": 1, "merged": 0, "skipped": 0,
                    "errors": 2, "dry_run": dry_run, "would_merge": 0}

    class _Forg:
        def forget(self, force: bool = False) -> Dict[str, Any]:
            return {"scanned": 3, "candidates": 0, "deleted": 0,
                    "skipped_protected": 0, "dry_run": not force}

    monkeypatch.setattr(factory, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(factory, "storage_from_config", lambda *a, **k: _Store())
    monkeypatch.setattr(cons_mod, "Consolidator", lambda *a, **k: _Cons())
    monkeypatch.setattr(forg_mod, "Forgetter", lambda *a, **k: _Forg())
    monkeypatch.setattr(maint_mod, "_affected_keys", lambda *a, **k: [])
    monkeypatch.setattr(maint_mod, "_export_before", lambda *a, **k: "/tmp/x.jsonl")
    monkeypatch.setattr(audit, "backup_json", lambda *a, **k: "/tmp/x.json")

    ok, summary = maint_mod.run(dry_run=False)
    assert ok is False, "合并有失败必须报红（ok=False）"
    assert "合并失败 2 条" in summary


# --------------------------------------------------------------------------
# 4. bus._pg_conf 单源
# --------------------------------------------------------------------------

def test_pg_conf_failclosed_reads_env_config(monkeypatch, tmp_path):
    """``KEEPSAKE_CONFIG`` 指向的文件 ⇒ 读出**该文件**的 storage.postgres 节。"""
    from keepsake.core import bus

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "storage": {"backend": "postgres",
                    "postgres": {"host": "pg.example", "port": 5433,
                                 "dbname": "ks_test", "user": "u", "password": "p"}},
    }), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_path))

    pg = bus._pg_conf()  # noqa: SLF001

    assert pg["host"] == "pg.example"
    assert pg["dbname"] == "ks_test"


def test_pg_conf_failclosed_missing_postgres_section_raises(monkeypatch, tmp_path):
    """缺 ``storage.postgres`` 节 ⇒ 抛 ``ValueError``（人话），不静默回退默认路径。"""
    from keepsake.core import bus

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"storage": {"backend": "sqlite"}}), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_path))

    with pytest.raises(ValueError) as ei:
        bus._pg_conf()  # noqa: SLF001
    assert "storage.postgres 未配置" in str(ei.value)


def test_pg_conf_failclosed_missing_file_raises(monkeypatch, tmp_path):
    """配置文件整个不在 ⇒ 抛 ``ValueError``（绝不猜默认路径）。"""
    from keepsake.core import bus

    monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "nope.json"))

    with pytest.raises(ValueError) as ei:
        bus._pg_conf()  # noqa: SLF001
    assert "班车需要 PG jobstore" in str(ei.value)
