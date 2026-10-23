"""知识库**标签 chip 行**的机器标签过滤 + Top-N 截断（keepsake_kb_tags_filter）。

**为什么要有这一层**：库里实测 `documents.tags` 里混着 `local-kb-id-1..67`、
`entry-fact/entry-note/entry-rule/entry-decision` 这类**机器标签**（子代理同步与
蒸馏管线带进来的），`/kb/tags` 一律平铺 ⇒ 界面一屏 140+ 个 chip，根本没法用。
那些不是人打的筛选维度，剔掉即可（**前缀口径只有一处**，见
`keepsake.api.routers.kb.MACHINE_TAG_PREFIXES`）。

本文件钉死四件事：

  1. 机器标签（`local-kb-id-` / `entry-` 前缀）**不出现在** `/kb/tags` 里；
  2. 人工标签**照常在**（过滤不许误伤），计数照旧来自库；
  3. 默认只回 **Top-20**（按文档数降序），`total_distinct` = **过滤后**的真实
     去重数（前端据此显示「+N 更多」，不靠前端自己数）；
  4. `?all=1` 一次给回全部过滤后的标签（展开「更多」时用；分页在前端切）。

**不连生产库**：真库用例走既有测试基建的隔离 schema（读不到 91 测试库凭据就
skip）；接线用例（过滤 / 截断 / total_distinct）用假连接跑，**任何环境都跑**。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from keepsake.api.routers import kb as kb_router

# 共用 fixture 与建表 DDL（测试库形状与生产同款）
sys.path.insert(0, str(Path(__file__).resolve().parent / "api"))
from test_kb_crud import (                                                    # noqa: E402
    AUTH, PG_ONLY, create, raw, _kb_with, DDL,
)


@pytest.fixture()
def kb(monkeypatch, tmp_path) -> Iterator[Any]:
    """隔离 schema 的真库（标签列与生产同款：text[] + GIN）。"""
    yield from _kb_with(monkeypatch, tmp_path, DDL)


def _fake_tags_conn(rows: List[Dict[str, Any]]) -> Any:
    """假连接：`/kb/tags` 只发一条 GROUP BY 查询，回给这几行。"""
    calls: List[tuple] = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            calls.append((" ".join(sql.split()), params))
        def fetchall(self):
            return rows

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    return _Conn(), calls


# ---------------------------------------------------------------- 口径（不碰库）

def test_机器标签前缀判据只此一处():
    """🔴 前缀口径只有这个 tuple：别处再写一份必然漂（两份口径各筛各的，
    迟早出现「界面上有、后端查不到」的标签）。"""
    assert kb_router.MACHINE_TAG_PREFIXES == ("local-kb-id-", "entry-")
    for tag in ("local-kb-id-1", "local-kb-id-67", "entry-fact", "entry-note",
                "entry-rule", "entry-decision"):
        assert kb_router._is_machine_tag(tag) is True, tag
    for tag in ("keepsake", "量化", "entrypoint", "local", "我的 entry 笔记"):
        assert kb_router._is_machine_tag(tag) is False, tag


def test_标签接口_机器标签不出现在结果里_计数照旧(monkeypatch):
    """🔴 机器标签不出现在 chip 行；人工标签与它们的计数**一字不动**
    （过滤不许误伤：人工标签是唯一的筛选维度）。"""
    rows = [
        {"tag": "keepsake", "n": 12},
        {"tag": "local-kb-id-1", "n": 67},
        {"tag": "entry-fact", "n": 40},
        {"tag": "量化", "n": 3},
    ]
    conn, _calls = _fake_tags_conn(rows)
    saved_cols, saved_conn = kb_router._table_columns, kb_router._connect
    kb_router._table_columns = lambda schema, tables: {"documents": {"id", "tags"}}  # type: ignore[assignment]
    kb_router._connect = lambda: conn                                            # type: ignore[assignment]
    try:
        body = kb_router.kb_tags()
    finally:
        kb_router._table_columns = saved_cols                                    # type: ignore[assignment]
        kb_router._connect = saved_conn                                          # type: ignore[assignment]
    assert [i.tag for i in body.items] == ["keepsake", "量化"]
    assert body.items[0].count == 12
    assert body.total_distinct == 2


def test_标签接口_默认Top20且total_distinct是真实去重数(monkeypatch):
    """🔴 默认只回 Top-20（chip 行一屏放不下），但 `total_distinct` 给的是
    **过滤后的真实去重数**（前端据此显示「+N 更多」）—— 不截断就悄悄丢掉
    标签，比截断更坏（用户按那个标筛会发现「标签坏了」）。"""
    rows = [{"tag": f"tag{i:02d}", "n": 100 - i} for i in range(30)]
    rows += [{"tag": "entry-fact", "n": 999}]
    conn, _calls = _fake_tags_conn(rows)
    saved_cols, saved_conn = kb_router._table_columns, kb_router._connect
    kb_router._table_columns = lambda schema, tables: {"documents": {"id", "tags"}}  # type: ignore[assignment]
    kb_router._connect = lambda: conn                                            # type: ignore[assignment]
    try:
        body = kb_router.kb_tags()
        every = kb_router.kb_tags(all=True)
    finally:
        kb_router._table_columns = saved_cols                                    # type: ignore[assignment]
        kb_router._connect = saved_conn                                          # type: ignore[assignment]
    assert len(body.items) == 20
    assert body.total_distinct == 30            # 30 个人工标签（机器标签已剔除）
    assert [i.tag for i in body.items][:2] == ["tag00", "tag01"]   # 按文档数降序
    assert len(every.items) == 30              # ?all=1 一次给回全部（前端自己切页）
    assert every.total_distinct == 30


def test_标签接口_老库没有tags列_给空列表不编chip(monkeypatch):
    """🔴 老库未迁移 tags 列 ⇒ 读路径降级成空列表（前端整行隐藏，不编假 chip）。"""
    saved_cols = kb_router._table_columns
    kb_router._table_columns = lambda schema, tables: {"documents": {"id"}}  # type: ignore[assignment]
    try:
        body = kb_router.kb_tags()
    finally:
        kb_router._table_columns = saved_cols                                    # type: ignore[assignment]
    assert body.items == [] and body.total_distinct == 0


# ---------------------------------------------------------------- 真库

@PG_ONLY
def test_真库_机器标签不出现在接口结果里(kb):
    """端到端：库里塞一篇带 `local-kb-id-*` / `entry-*` 的文档 ⇒
    `/kb/tags` 里不许出现这两个前缀的标签，人工标签照常在。"""
    import psycopg

    create(kb.client, "带机器标签的一篇", "正文", tags=["keepsake", "entry-fact"])
    with psycopg.connect(kb.dsn, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'UPDATE "{kb.schema}".documents'
                    ' SET tags = ARRAY[\'keepsake\',\'local-kb-id-7\',\'entry-note\',\'量化\']'
                    " WHERE title = %s", ["带机器标签的一篇"])
    body = kb.client.get("/api/v1/kb/tags", headers=AUTH).json()
    assert {i["tag"] for i in body["items"]} == {"keepsake", "量化"}
    assert body["total_distinct"] == 2
    # 库里那三个标签**一个都没少**（过滤只影响 chip 行，不动数据）
    assert raw(kb, 'SELECT tags FROM "{s}".documents WHERE title=%s',
               ["带机器标签的一篇"]) == [(["keepsake", "local-kb-id-7", "entry-note", "量化"],)]
