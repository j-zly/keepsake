"""知识库代理面的**可见范围**（`kb/service.py::visible_scope`）—— 知识库旗专用。

知识库面与记忆面**解耦**（2026-11 ks_share_kb）：知识库面读 `share_kb` 那一列，
记忆面读 `share_memory` 那一列；结构同尺、口径对称，但**一个旗不许串台**。

本文件钉死三件事：

  1. `visible_scope(caller, registry, is_primary=False)` = {自己} ∪
     {`share_kb=1` 的已批准代理} —— **只有开了知识库旗的**才进可见集；
  2. **primary ⇒ `None`**（全量，不过滤）—— 现状逐字不变；
  3. **无主文档谓词不变**：`agent_id IS NULL OR agent_id = '' OR
     agent_id = ANY(scope)`（无主老文档人人可见，这条不许被收窄）。

**不连生产库**：全程 hermetic —— 注册表用 `tmp_path` 里的临时 SQLite，
检索路径用**假连接**（只验 SQL 谓词，不碰任何真库）。
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Tuple

import pytest

from keepsake.govern.registry import Registry
from keepsake.kb import service as kb_service


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture()
def reg(tmp_path) -> Iterator[Registry]:
    """一份真注册表（临时 SQLite）：primary + 两个子代理，都已批准。"""
    from keepsake.core.sqlite import SqliteStorage

    st = SqliteStorage(path=str(tmp_path / "kb_share.db"))
    assert st.ensure_index() is True
    r = Registry(st)
    assert r.ensure_schema() is True
    for aid in ("primary", "agent-k", "agent-m", "agent-b"):
        r.register(aid, aid)
        r.approve(aid)
    r.set_role("primary", "primary")
    yield r
    st.close()


class _FakeCur:
    """假游标：记下 SQL 与参数，回空行集（不连库）。"""

    def __init__(self, calls: List[Tuple[str, Any]]) -> None:
        self._calls = calls

    def __enter__(self) -> "_FakeCur":
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._calls.append((" ".join(sql.split()), params))

    def fetchall(self) -> List[Dict[str, Any]]:
        return []


class _FakeConn:
    def __init__(self, calls: List[Tuple[str, Any]]) -> None:
        self._calls = calls

    def cursor(self) -> _FakeCur:
        return _FakeCur(self._calls)

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *a: Any) -> bool:
        return False


@pytest.fixture()
def fake_kb(monkeypatch) -> Iterator[List[Tuple[str, Any]]]:
    """把知识库层的探列 / 连线换成假的 —— 只验 SQL 谓词，不碰库。"""
    calls: List[Tuple[str, Any]] = []
    cols = {
        "documents": {"id", "title", "content_type", "tags", "status", "agent_id"},
        "chunks": {"id", "document_id", "content"},
    }
    monkeypatch.setattr(kb_service.config, "pg_schema", lambda: "ks_test")
    monkeypatch.setattr(kb_service, "table_columns",
                        lambda schema, tables: {t: set(cols.get(t, ())) for t in tables})
    monkeypatch.setattr(kb_service, "connect", lambda: _FakeConn(calls))
    yield calls


# ---------------------------------------------------------------------------
# ① 可见集 = 自己 + 开了知识库旗的已批准代理
# ---------------------------------------------------------------------------

def test_visible_scope只含自己与开了知识库旗的代理(reg):
    reg.set_share_kb("agent-k", True)
    scope = kb_service.visible_scope("agent-b", reg, is_primary=False)
    assert scope == ["agent-b", "agent-k"], scope


def test_visible_scope里记忆旗不串台(reg):
    """🔴 记忆面开了共享、知识库旗没开 ⇒ **知识库面**看不见它（两个旗解耦）。"""
    reg.set_share_memory("agent-m", True)
    reg.set_share_kb("agent-m", False)
    scope = kb_service.visible_scope("agent-b", reg, is_primary=False)
    assert scope == ["agent-b"], scope


def test_visible_scope里知识库旗不串到记忆面(reg):
    """🔴 反向：知识库旗开了、记忆旗没开 ⇒ **记忆面**看不见它。"""
    from keepsake.govern.visibility import visible_agent_ids

    reg.set_share_kb("agent-k", True)
    assert visible_agent_ids("agent-b", reg) == {"agent-b"}


def test_visible_scope_primary是全量(reg):
    """🔴 primary ⇒ `None`（不过滤，全量可见）—— 与记忆面同款硬回归线。"""
    reg.set_share_kb("agent-k", True)
    assert kb_service.visible_scope("primary", reg, is_primary=True) is None


def test_visible_scope只认已批准的代理(reg):
    """🔴 pending / revoked 的代理不进知识库共享集。"""
    reg.set_share_kb("agent-k", True)
    reg.revoke("agent-k")
    scope = kb_service.visible_scope("agent-b", reg, is_primary=False)
    assert scope == ["agent-b"], scope


# ---------------------------------------------------------------------------
# ② 无主文档谓词不变
# ---------------------------------------------------------------------------

def test_无主文档谓词不变(fake_kb):
    """🔴 无主文档（`agent_id IS NULL` 或空串）**人人可见** —— 谓词不许收窄。"""
    kb_service.search_documents(query="回滚", limit=5,
                                agent_scope=["agent-b", "agent-k"])
    sql, params = fake_kb[-1]
    assert ("d.agent_id IS NULL OR d.agent_id = '' OR "
            "d.agent_id = ANY(%s::text[])") in sql, sql
    assert ["agent-b", "agent-k"] in params, params


def test_无主文档在Python侧判读里也可见():
    """🔴 `_visible` 这一侧同样：无主（空归属）⇒ 可见（收紧前的公共数据）。"""
    assert kb_service._visible("", ["agent-b"]) is True          # noqa: SLF001 —— 判读口本体
    assert kb_service._visible("agent-b", ["agent-b"]) is True  # noqa: SLF001
    assert kb_service._visible("agent-k", ["agent-b"]) is False  # noqa: SLF001
    # agent_scope=None（primary / 控制台）⇒ 全量可见
    assert kb_service._visible("agent-k", None) is True         # noqa: SLF001
