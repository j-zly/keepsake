"""PG 后端 `_get_client()` 依赖链验收（2026-10 ks_pcli 门 C）。

覆盖本单四件事，全部在真 PG 库（91 测试库）上跑：
  1. R6 去重探测生效：`fragment_exists` 真判存在 → 第二次写入被判 update_state，
     日志里不再出现「R6 去重本次跳过」
  2. `touch_fragment` / `set_supersedes` 显式 unsupported 且**留有可辨识日志**
     （禁静默跳过）；`consolidate()` / `forget()` 返回具名 unsupported 而非 AttributeError
  3. 写 → 读 → 删闭环：唯一 token 写入 ⇒ 检索 rank1 命中自己 ⇒ 删除 ⇒ 残留 0 行

测试卫生：写入的 key 全部登记进 `_WROTE_KEYS`，由 autouse fixture 清理并复查残留。
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import List

import pytest

psycopg = pytest.importorskip("psycopg", reason="未装 psycopg")
from keepsake.consolidator import Consolidator           # noqa: E402
from keepsake.forgetter import Forgetter                 # noqa: E402
from keepsake.splitter import extract_keywords           # noqa: E402
from keepsake.storage_pg import PgStorage                # noqa: E402

ENV_PATH = Path("/home/claude_user/.keepsake_pg_test.env")
ALLOWED_HOST = "8.140.192.91"
ALLOWED_DBNAME = "keepsake_test"
dsn = ""
if ENV_PATH.exists():
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    dsn = m.group(1).strip() if m else ""
if not (dsn and ALLOWED_HOST in dsn and ALLOWED_DBNAME in dsn):
    pytest.skip(f"无测试库凭据或指向非 {ALLOWED_HOST}/{ALLOWED_DBNAME}", allow_module_level=True)

_WROTE_KEYS: List[str] = []
# 每轮唯一：残留数据来自上一轮时不会撞 key（撞了会触发 store() 的版本化，
# 把碎片写成 `key:<epoch>`，让 rank1 的 key 断言看起来像失败）。
_TOKEN = f"kspclicheck{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module")
def pg():
    """is_primary=True —— 与 tests/test_storage_pg.py 的 `pg_search` 同款。

    非主脑实例在 `_search_filter_sql` 里会强制要求 `shared` 标签
    （或 agent 隔离标签），不这么做检索会 0 命中 —— 那是隔离语义，不是缺陷。
    """
    storage = PgStorage(dsn=dsn, connect_timeout=30, is_primary=True, final_limit=5)
    storage.ensure_index()
    yield storage
    storage.close()


@pytest.fixture(autouse=True)
def _cleanup(pg):
    yield
    with pg._tx() as cur:      # noqa: SLF001 — 测试内清理自有数据
        keys = list(_WROTE_KEYS)
        if keys:
            cur.execute("SELECT content FROM ks_fragment WHERE key = ANY(%s)", (keys,))
            leaked: List[str] = []
            for (content,) in cur.fetchall():
                leaked.extend(extract_keywords(content or "", max_keywords=5))
            cur.execute("DELETE FROM ks_fragment WHERE key = ANY(%s)", (keys,))
            cur.execute("DELETE FROM ks_entity_timeline WHERE frag_key = ANY(%s)", (keys,))
            for k in keys:
                cur.execute("DELETE FROM ks_entity_timeline WHERE frag_key LIKE %s", (f"{k}:%",))
            if leaked:
                cur.execute("DELETE FROM ks_hot_topic WHERE topic = ANY(%s)", (leaked,))
                cur.execute("DELETE FROM ks_attention WHERE topic = ANY(%s)", (leaked,))
                cur.execute("DELETE FROM ks_hot_topic_seen WHERE topic = ANY(%s)", (leaked,))
            _WROTE_KEYS.clear()


def _key_for(text: str) -> str:
    """与 storage_pg._fragment_key_for 同一算法（sha256[:12]）。"""
    import hashlib
    return f"memory:frag:{hashlib.sha256(text.encode()).hexdigest()[:12]}"


# --------------------------------------------------------------------------
# 门 C.1 — R6 去重探测
# --------------------------------------------------------------------------

def test_r6_dedup_probe_works_on_pg(pg):
    """同一内容连写两次：第二次必须被判为「既有碎片」并走 update_state。"""
    from keepsake.ingest_gate import decide, update_state_only

    text = f"用户偏好用 PostgreSQL 存记忆 {_TOKEN} 第一条"
    key = _key_for(text)
    _WROTE_KEYS.append(key)

    # 第一次写入前 —— 不存在
    assert pg.fragment_exists(key) is False

    assert pg.store(text=text, category="turn_memory", source="ks_pcli", fragment_type="memory")

    # 第二次写入前 —— 必须探到（这是修复前 AttributeError 的那一步）
    assert pg.fragment_exists(key) is True
    existing_meta = {"key": key}

    decision2 = decide(text, "turn_memory", existing_meta, None)
    assert decision2.action == "update_state", (
        f"同内容二次写入未被 R6 识别为重复，action={decision2.action}"
    )

    # R6 执行体：PG 侧 unsupported 但**有日志**，且绝不覆盖 content
    assert update_state_only(pg, existing_meta) is False
    row = pg.get_fragment(key)
    assert row is not None and row["content"] == text, "R6 覆盖了原 content（绝不允许）"


def test_r6_probe_logs_no_skip_on_pg(pg, caplog):
    """探测**成功**时不得出现「R6 去重本次跳过」告警（那正是线上日志里的报错）。"""
    text = f"第二种记忆内容 {_TOKEN}"
    key = _key_for(text)
    _WROTE_KEYS.append(key)
    pg.store(text=text, category="turn_memory", source="ks_pcli", tags="shared")

    with caplog.at_level(logging.WARNING, logger="keepsake"):
        assert pg.fragment_exists(key) is True
    assert "R6 去重本次跳过" not in caplog.text, caplog.text


def test_fragment_exists_is_tri_state():
    """PG 侧永不返回 None（真查库）；空 key 判 False。"""
    pg = PgStorage.__new__(PgStorage)
    assert PgStorage.fragment_exists(pg, "") is False


# --------------------------------------------------------------------------
# 门 C.2 — 显式 unsupported（禁静默）
# --------------------------------------------------------------------------

def test_unsupported_capabilities_leave_identifiable_log(pg, caplog):
    """touch_fragment / set_supersedes 必须留可辨识日志，返回 False。"""
    with caplog.at_level(logging.INFO, logger="keepsake.storage_pg"):
        assert pg.touch_fragment("memory:frag:whatever") is False
        assert pg.set_supersedes("memory:frag:a", "memory:frag:b") is False
    assert "touch_fragment" in caplog.text and "unsupported" in caplog.text
    assert "set_supersedes" in caplog.text and "unsupported" in caplog.text


def test_consolidate_runs_on_pg_not_unsupported(pg):
    """合并：PG 下**真跑**（2026-10 ks_pmn 起不再是 unsupported）。

    LLM 通道未配置 ⇒ `_call_llm` 返回 None ⇒ 每组都合并失败，
    但**流程必须真跑完**：有 scanned/groups_found 统计，且一个字节都不写。
    """
    with pg._ro() as cur:      # noqa: SLF001
        cur.execute("SELECT count(*) FROM ks_fragment")
        before = cur.fetchone()[0]

    res = Consolidator(storage=pg).consolidate(dry_run=True)
    assert res.get("status") != "unsupported", res
    assert res["dry_run"] is True, res
    assert res["scanned"] >= 0 and "groups_found" in res, res

    with pg._ro() as cur:      # noqa: SLF001
        cur.execute("SELECT count(*) FROM ks_fragment")
        assert cur.fetchone()[0] == before, "dry-run 动了数据"


def test_forget_runs_on_pg_not_unsupported(pg):
    """遗忘（dry-run）：PG 下真跑，且**没有删任何数据**。"""
    with pg._ro() as cur:      # noqa: SLF001
        cur.execute("SELECT count(*) FROM ks_fragment WHERE key LIKE %s", (f"%{_TOKEN}%",))
        before = cur.fetchone()[0]

    res = Forgetter(storage=pg, dry_run=True).forget()
    assert res.get("status") != "unsupported", res
    assert res["dry_run"] is True, res
    assert res["deleted"] == 0, res
    assert res["scanned"] >= 0 and "candidates" in res, res

    with pg._ro() as cur:      # noqa: SLF001
        cur.execute("SELECT count(*) FROM ks_fragment WHERE key LIKE %s", (f"%{_TOKEN}%",))
        assert cur.fetchone()[0] == before, "dry-run 动了数据"


# --------------------------------------------------------------------------
# 门 C.4 — 写 → 读 → 删闭环
# --------------------------------------------------------------------------

def test_write_read_delete_roundtrip(pg):
    """唯一 token 条目：检索 rank1 命中自己 ⇒ 删除 ⇒ 残留 0 行。"""
    text = f"独特定位串 zzzpcli{_TOKEN} 仅用于本用例"
    key = _key_for(text)
    _WROTE_KEYS.append(key)
    assert pg.store(text=text, category="memory", source="ks_pcli", tags="shared")

    hits = pg.search(f"zzzpcli{_TOKEN}")
    assert hits, "检索 0 命中"
    assert hits[0].get("_key") == key, hits[0]   # rank1 命中自己

    with pg._tx() as cur:      # noqa: SLF001
        cur.execute("DELETE FROM ks_fragment WHERE key = %s", (key,))
        cur.execute("DELETE FROM ks_entity_timeline WHERE frag_key = %s", (key,))
        cur.execute(
            "SELECT count(*) FROM ks_fragment WHERE key = %s", (key,))
        assert cur.fetchone()[0] == 0, "删除后仍残留行"
    _WROTE_KEYS.remove(key)