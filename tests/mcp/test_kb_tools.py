"""MCP 面上的知识库工具（kb_store / kb_search / kb_get / kb_remove）。

知识库原来只对控制台开放，现在对**所有代理**开放，治理口径与记忆**同一把尺子**
（`govern.visibility`）。本文件把任务书的判据一条条钉死：

  ① `kb_store` 打对 owner（**凭据反查的调用方**）；控制台路径（不传 caller）
     保持 NULL（= 无主，历史文档全是）；
  ② 非 primary `kb_search` 只见**自己名下 + 无主老文档**（别人的看不见）；
  ③ 开了 `share_memory` 后互相可见（关回去立刻看不见）；
  ④ `kb_get` 不可见 = **not-found**（存在性本身也不许泄露）；
  ⑤ `kb_remove`：删**别人**的拒 + **零内容回显**；删自己 / 无主的过；primary 全删过；
     `confirm` 缺省即拒；
  ⑥ **未批准**的凭据四工具全拒（fail-closed，与记忆工具同一道闸）。

**用真 PG**（知识库是 PG 专有，documents/chunks 无 SQLite 对应物）：连
8.140.192.91/keepsake_test，每个用例建一个独立 schema（表结构与生产同款 DDL，
**含 `agent_id` 归属列**），用完 `DROP SCHEMA ... CASCADE`；读不到凭据就 skip ——
与 `tests/api/test_kb_crud.py` 同一套接法。代理注册表走**临时 SQLite**
（不连 Redis / 生产库）。
"""

from __future__ import annotations

import functools
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List

import anyio
import pytest

from keepsake.mcp_server.server import build_server
from keepsake.mcp_server.tools import ToolError

#: 子代理 A / B（各一篇有主文档）+ 一篇无主的老文档
SUB = "worker-sub"
OTHER = "worker-other"

#: 检索词（三篇文档的正文都含它）
WORD = "回滚手册"

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST, ALLOWED_DBNAME = "8.140.192.91", "keepsake_test"
psycopg = pytest.importorskip("psycopg", reason="未装 psycopg")


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


DSN = _read_dsn()
#: 只认白名单里的 host/dbname —— 防止有人把生产 DSN 顺手写进这个文件就被测试打上
_pg_ready = bool(DSN and ALLOWED_HOST in DSN and ALLOWED_DBNAME in DSN)

PG_ONLY = pytest.mark.skipif(not _pg_ready,
                             reason="无 8.140.192.91/keepsake_test 凭据，跳过 PG 真库")

#: 与生产同款的 documents/chunks DDL（**含 `agent_id text NULL`** —— 归属列）。
#: `agent_id` 是可空的：历史行全无主（NULL），加了列也不改存量语义。
DDL = (
    'CREATE SCHEMA "{s}"',
    'CREATE TABLE "{s}".documents ('
    ' id bigserial PRIMARY KEY, source_type text NOT NULL, source_key text NOT NULL,'
    ' content_type text NOT NULL DEFAULT \'note\','
    ' tags text[] NOT NULL DEFAULT \'{{}}\','
    ' status text NOT NULL DEFAULT \'effective\','
    ' review_meta jsonb NOT NULL DEFAULT \'{{}}\','
    ' agent_id text,'
    ' title text, meta jsonb, content_hash text,'
    ' updated_at timestamptz NOT NULL DEFAULT now(),'
    ' UNIQUE (source_type, source_key))',
    'CREATE TABLE "{s}".chunks ('
    ' id bigserial PRIMARY KEY, document_id bigint NOT NULL'
    ' REFERENCES "{s}".documents(id), ord int NOT NULL, text text, source_path text)',
)


def _build(tmp_path, monkeypatch):
    """按临时配置装配一个 server（审计/备份落 tmp；`KEEPSAKE_CONFIG` 指 tmp sqlite）。"""
    cfg = {
        "storage": {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "kb.db")}},
        "is_primary": True,
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.delenv("KEEPSAKE_AGENT_ID", raising=False)
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL", raising=False)
    return build_server(govern=True)


@pytest.fixture()
def kb(tmp_path, monkeypatch) -> Iterator[SimpleNamespace]:
    """隔离 schema 的真库 + 装配好的 MCP server；用完 DROP SCHEMA CASCADE。"""
    if not _pg_ready:
        pytest.skip("无 8.140.192.91/keepsake_test 凭据，跳过 PG 真库")
    schema = f"ks_kbtool_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        for stmt in DDL:
            cur.execute(stmt.format(s=schema))

    monkeypatch.setenv("KEEPSAKE_PG_DSN", DSN)
    monkeypatch.setenv("KEEPSAKE_PG_SCHEMA", schema)
    from keepsake.api.routers import kb as kb_router
    from keepsake.kb import service as kb_service

    kb_router._COLS_CACHE.clear()          # 列名缓存是进程级的，用例之间必须隔离
    kb_service._COLS_CACHE.clear()
    server = _build(tmp_path, monkeypatch)
    try:
        yield SimpleNamespace(server=server, schema=schema, dsn=DSN)
    finally:
        server.agent_gate.stop()
        kb_router._COLS_CACHE.clear()
        kb_service._COLS_CACHE.clear()
    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def call(server, name: str, **arguments: Any) -> Dict[str, Any]:
    _, structured = anyio.run(functools.partial(server.call_tool, name, arguments))
    return structured["result"]


def _as_caller(server, agent_id: str) -> None:
    """把 stdio 闸门切到另一个代理（标成「还没复核过」⇒ 下次调用必走一遍判定链）。"""
    gate = server.agent_gate
    gate.agent_id = agent_id
    gate._stdio_ok = None       # noqa: SLF001
    gate._stdio_at = 0.0        # noqa: SLF001


def _seed_agent(kb, agent_id: str, name: str = "") -> None:
    """把代理登记进注册表并**本机批准**（`bootstrap_local`：与记忆面同一套身份链）。"""
    kb.server.agent_gate.registry.bootstrap_local(agent_id, name or agent_id)


def _seed_doc(kb, title: str, agent_id: str, content: str) -> int:
    """手工塞一篇**已生效**的文档（模拟代理写进来的那一篇）+ 一块切片。"""
    with psycopg.connect(kb.dsn, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{kb.schema}".documents'
                    '(source_type, source_key, title, meta, content_hash, agent_id, status)'
                    " VALUES ('agent',%s,%s,'{}'::jsonb,%s,%s,'effective') RETURNING id",
                    [f"seed:{title}", title, f"sha:{title}", agent_id])
        doc_id = cur.fetchone()[0]
        cur.execute(f'INSERT INTO "{kb.schema}".chunks(document_id, ord, text, source_path)'
                    ' VALUES (%s, 0, %s, %s)', [doc_id, content, f"seed:{title}"])
    return doc_id


def _owner_of(kb, doc_id: int) -> Any:
    """直连隔离 schema 看这篇的归属列（不经过 API / 工具面）。"""
    with psycopg.connect(kb.dsn, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT agent_id FROM "{kb.schema}".documents WHERE id = %s', [doc_id])
        return cur.fetchone()[0]


def _titles(hits: Dict[str, Any]) -> set:
    return {str(h.get("title") or "") for h in hits.get("results") or []}


# ---------------------------------------------------------------------------
# ① kb_store 打对 owner
# ---------------------------------------------------------------------------

@PG_ONLY
def test_kb_store_打对owner_子代理写进自己名下(kb):
    """🔴 归属 = **凭据反查的调用方**（不是 provider 进程的身份，也不是自报的）。"""
    _seed_agent(kb, SUB, "子代理")
    _as_caller(kb.server, SUB)
    out = call(kb.server, "kb_store", title="值班手册", content=f"{WORD} 联系人是张三")
    assert out["id"], out
    assert out["agent_id"] == SUB, out
    assert _owner_of(kb, int(out["id"])) == SUB
    # 审核链不变：非 primary 写进来的落 pending（进控制台待审队列）
    assert out["review"] == "pending", out
    assert out["status"] in ("added", "updated"), out


@PG_ONLY
def test_kb_store_不传caller_保持无主NULL(kb):
    """控制台路径（不传调用方）⇒ 归属列**不写**（NULL = 无主，存量语义不变）。"""
    from keepsake.kb import service as kb_service

    out = kb_service.store_document(title="手写一篇", content=f"{WORD} 正文",
                                    agent_id="", is_primary=True)
    assert _owner_of(kb, int(out["id"])) is None


@PG_ONLY
def test_kb_store_不许写灌入器地盘(kb):
    """🔴 `skill` / `skill_hk` 是夜间灌入器的地盘：代理写进去的会被死文档清理
    扫掉（等于凭空丢数据）⇒ 明确拒，不静默写。"""
    _seed_agent(kb, SUB, "子代理")
    _as_caller(kb.server, SUB)
    with pytest.raises(ToolError) as exc:
        call(kb.server, "kb_store", title="偷灌技能", content="正文", source_type="skill")
    assert "source_type" in str(exc.value), str(exc.value)


@PG_ONLY
def test_kb_store_两个代理同题不撞行_归属各归各(kb):
    """🔴 键里带归属代理 id：两个代理存同一篇内容 ⇒ **两行**（各归各）。"""
    _seed_agent(kb, SUB, "子代理")
    _seed_agent(kb, OTHER, "另一个子代理")
    _as_caller(kb.server, SUB)
    a = call(kb.server, "kb_store", title="同一篇", content=f"{WORD} 同一段")
    _as_caller(kb.server, OTHER)
    b = call(kb.server, "kb_store", title="同一篇", content=f"{WORD} 同一段")
    assert a["id"] != b["id"], (a, b)
    assert _owner_of(kb, int(a["id"])) == SUB
    assert _owner_of(kb, int(b["id"])) == OTHER


# ---------------------------------------------------------------------------
# ② 可见性：非 primary 只见自己名下 + 无主老文档
# ---------------------------------------------------------------------------

@PG_ONLY
def test_非primary搜只看自己名下与无主(kb):
    """🔴 别人的（有主）文档**搜不到**；无主的老文档照旧可见。"""
    _seed_agent(kb, SUB, "子代理")
    _seed_agent(kb, OTHER, "另一个子代理")
    _seed_doc(kb, "A的", SUB, f"{WORD} A")
    _seed_doc(kb, "B的", OTHER, f"{WORD} B")
    _seed_doc(kb, "老文档", "", f"{WORD} 老")
    _as_caller(kb.server, SUB)
    hits = call(kb.server, "kb_search", query=WORD)
    who = _titles(hits)
    assert "A的" in who and "老文档" in who, who
    assert "B的" not in who, who


@PG_ONLY
def test_开了共享后看得见对方的(kb):
    """🔴 `share_memory` 开关吃同一份（与记忆面同款）。"""
    _seed_agent(kb, SUB, "子代理")
    _seed_agent(kb, OTHER, "另一个子代理")
    _seed_doc(kb, "A的", SUB, f"{WORD} A")
    _seed_doc(kb, "B的", OTHER, f"{WORD} B")
    kb.server.agent_gate.registry.set_share_memory(OTHER, True)
    _as_caller(kb.server, SUB)
    assert "B的" in _titles(call(kb.server, "kb_search", query=WORD))
    # 关回去立刻看不见（开关即时生效，不许有缓存窗口）
    kb.server.agent_gate.registry.set_share_memory(OTHER, False)
    assert "B的" not in _titles(call(kb.server, "kb_search", query=WORD))


@PG_ONLY
def test_primary搜全量可见(kb):
    """primary 永远全量可见（= 主脑现状逐字不变）。"""
    _seed_agent(kb, SUB, "子代理")
    _seed_agent(kb, OTHER, "另一个子代理")
    _seed_doc(kb, "A的", SUB, f"{WORD} A")
    _seed_doc(kb, "B的", OTHER, f"{WORD} B")
    hits = call(kb.server, "kb_search", query=WORD)
    assert {"A的", "B的"} <= _titles(hits), hits


# ---------------------------------------------------------------------------
# ③ kb_get：不可见 = not-found
# ---------------------------------------------------------------------------

@PG_ONLY
def test_kb_get_不可见按不存在回(kb):
    """🔴 存在性本身也不许泄露：别人的文档按「不存在」回（与 memory_get 同纪律）。"""
    _seed_agent(kb, SUB, "子代理")
    _seed_agent(kb, OTHER, "另一个子代理")
    other_id = _seed_doc(kb, "B的", OTHER, f"{WORD} B")
    _as_caller(kb.server, SUB)
    with pytest.raises(ToolError) as exc:
        call(kb.server, "kb_get", id=str(other_id))
    assert "不存在" in str(exc.value), str(exc.value)
    # 自己的看得到
    own_id = _seed_doc(kb, "A的", SUB, f"{WORD} A")
    out = call(kb.server, "kb_get", id=str(own_id))
    assert out["title"] == "A的" and WORD in out["content"]


# ---------------------------------------------------------------------------
# ④ kb_remove：删别人的拒、删自己/无主过、primary 全删过、confirm 缺省拒
# ---------------------------------------------------------------------------

@PG_ONLY
def test_kb_remove_删别人拒且零内容回显(kb):
    """🔴 拒时只说「不是你的」，**一个字的正文都不回显**。"""
    _seed_agent(kb, SUB, "子代理")
    _seed_agent(kb, OTHER, "另一个子代理")
    other_id = _seed_doc(kb, "B的密文", OTHER, f"{WORD} 绝密正文XYZ")
    _as_caller(kb.server, SUB)
    with pytest.raises(ToolError) as exc:
        call(kb.server, "kb_remove", id=str(other_id), confirm=True)
    msg = str(exc.value)
    assert "不是你的" in msg, msg
    assert "绝密正文XYZ" not in msg and "B的密文" not in msg, msg
    # 别人的文档还在（没被删掉）
    with psycopg.connect(kb.dsn, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT id FROM "{kb.schema}".documents WHERE id = %s', [other_id])
        assert cur.fetchone() is not None


@PG_ONLY
def test_kb_remove_删自己与无主都过(kb):
    """子代理删自己名下的、删无主的老文档 ⇒ 都过（与记忆面同一把尺子）。"""
    _seed_agent(kb, SUB, "子代理")
    own_id = _seed_doc(kb, "A的", SUB, f"{WORD} A")
    old_id = _seed_doc(kb, "老文档", "", f"{WORD} 老")
    _as_caller(kb.server, SUB)
    assert call(kb.server, "kb_remove", id=str(own_id), confirm=True)["removed"] is True
    assert call(kb.server, "kb_remove", id=str(old_id), confirm=True)["removed"] is True
    with psycopg.connect(kb.dsn, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM "{kb.schema}".documents')
        assert cur.fetchone()[0] == 0
        cur.execute(f'SELECT COUNT(*) FROM "{kb.schema}".chunks')
        assert cur.fetchone()[0] == 0        # chunks 不许残留


@PG_ONLY
def test_kb_remove_primary全删过(kb):
    """primary 删任意文档 ⇒ 过（= 主脑现状逐字不变）。"""
    _seed_agent(kb, OTHER, "另一个子代理")
    other_id = _seed_doc(kb, "B的", OTHER, f"{WORD} B")
    assert call(kb.server, "kb_remove", id=str(other_id), confirm=True)["removed"] is True


@PG_ONLY
def test_kb_remove_confirm缺省拒(kb):
    """🔴 `confirm` 缺省即拒：绝不动数据（与 `memory_remove` 同一道闸）。"""
    _seed_agent(kb, SUB, "子代理")
    own_id = _seed_doc(kb, "A的", SUB, f"{WORD} A")
    _as_caller(kb.server, SUB)
    with pytest.raises(ToolError) as exc:
        call(kb.server, "kb_remove", id=str(own_id))
    assert "confirm" in str(exc.value), str(exc.value)
    with psycopg.connect(kb.dsn, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM "{kb.schema}".documents WHERE id = %s', [own_id])
        assert cur.fetchone()[0] == 1        # 一行都没删


# ---------------------------------------------------------------------------
# ⑤ 未批准凭据 ⇒ 四工具全拒（fail-closed）
# ---------------------------------------------------------------------------

@PG_ONLY
def test_未批准凭据四工具全拒(kb):
    """🔴 没批准 / 凭据无效 ⇒ 全部拒绝（与记忆工具同一道闸，fail-closed 一致）。"""
    kb.server.agent_gate.registry.register("worker-pending", "没批准的")
    _as_caller(kb.server, "worker-pending")
    for name, args in (("kb_store", {"title": "t", "content": "c"}),
                       ("kb_search", {"query": WORD}),
                       ("kb_get", {"id": "1"}),
                       ("kb_remove", {"id": "1", "confirm": True})):
        with pytest.raises(ToolError) as exc:
            call(kb.server, name, **args)
        assert "未批准" in str(exc.value) or "凭据" in str(exc.value), (name, str(exc.value))


# ---------------------------------------------------------------------------
# 判据本体（`govern.visibility` 那一把尺子）—— 知识库与记忆共用
# ---------------------------------------------------------------------------

def test_删除判据本体_与记忆共用():
    """知识库的删除判据 = `can_remove_fragment`（非 primary：自己的过、他人的拒、
    无主的过；primary 全量）。**不许另发明一套**。"""
    from keepsake.govern.visibility import can_remove_fragment

    assert can_remove_fragment("worker-sub", "worker-sub", is_primary=False) is True
    assert can_remove_fragment("worker-other", "worker-sub", is_primary=False) is False
    assert can_remove_fragment("", "worker-sub", is_primary=False) is True
    assert can_remove_fragment("worker-other", "worker-sub", is_primary=True) is True


def test_工具面清单():
    """四个知识库工具都在，且写口（存 / 删）登记成写（活动计数口径）。"""
    from keepsake.mcp_server.server import GATE_WRITE_TOOLS
    from keepsake.mcp_server.tools import KB_TOOLS

    assert set(KB_TOOLS) == {"kb_store", "kb_search", "kb_get", "kb_remove"}
    assert {"kb_store", "kb_remove"} <= set(GATE_WRITE_TOOLS)
    assert not ({"kb_search", "kb_get"} & set(GATE_WRITE_TOOLS))


def test_知识库层FastAPI_free():
    """🔴 MCP 服务面**不一定装了控制台的 fastapi**（`pyproject.toml` 里 fastapi
    压根不是依赖）—— 知识库数据层与工具面都不许 import 它，否则代理侧整个
    import 失败（代理的知识库工具用不了）。"""
    import pathlib
    import re

    import keepsake.kb.service as svc
    import keepsake.mcp_server.tools as tools

    svc_src = pathlib.Path(svc.__file__).read_text(encoding="utf-8")
    tools_src = pathlib.Path(tools.__file__).read_text(encoding="utf-8")
    # 只看**代码里的 import 语句**（docstring 里提到 fastapi 是解释，不算）
    assert not re.search(r"^\s*(import fastapi|from fastapi)", svc_src, re.M), \
        "知识库数据层不许 import fastapi（控制台的 fastapi 是可选依赖）"
    # 工具面也不许回头 import 控制台路由（那会把 fastapi 拖进来）
    assert not re.search(r"^\s*from \.\.api\.routers", tools_src, re.M), \
        "工具面不许 import 控制台路由（FastAPI-free 的纪律）"
