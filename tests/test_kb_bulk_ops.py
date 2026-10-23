"""知识库**勾选批量**（批准 / 驳回 / 真删）这一层的测试（keepsake_kb_bulk_ops）。

**口径与 `ops/bulk` 同款**（两边必须一致，否则用户按同一个按钮却拿到不同的集合）：
统一体 `{"ids": [...], "confirm": "<ids 去重后的条数>"}`，逐条**复用单条端点的
那份实现**（全部校验 / 备份快照 / audit.record / 归属闸），返回
`{"succeeded": N, "failed": [{"id","reason"}], "total": M}`，**单条失败不回滚其他条**。

本文件钉死六件最容易悄悄坏掉的事：

  1. `confirm` 缺失 / 不等 ⇒ 400（`confirm_required` / `confirm_mismatch`）——
     勾了 3 篇却手输 4 都不放行；
  2. ids 去重后 > 200 ⇒ 400（防误选全库；**超限不截断**）；
  3. bulk-approve **部分失败不拖累其他条**（已生效的那条进 failed，其余照常批准）；
  4. bulk-delete **归属闸不放松**：非 primary 删别人名下的 ⇒ 那条 failed 且
     原因可辨识（`kb_not_owner`），自己名下的照常删；
  5. bulk-reject 的 `reason` **必填**（驳回不删正文，理由是日后唯一的线索）；
  6. 每条都有**备份**（真删的回退路径）+ 审计留痕（逐条 + 批次汇总各一行）。

**不连生产库**：真库用例走既有测试基建的隔离 schema（`tests/api/test_kb_crud.py`
的 `_kb_with`：独立 schema + 用完 DROP CASCADE），读不到 91 测试库凭据就 skip；
纯接线用例（confirm / 上限 / 理由必填）**不碰库**，任何环境都跑。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
from fastapi import HTTPException

from keepsake.api.routers import kb as kb_router

# 共用 fixture 与建表 DDL（测试库形状与生产同款）—— 与 tests/api/ 下那批 kb 用例同源。
sys.path.insert(0, str(Path(__file__).resolve().parent / "api"))
from test_kb_crud import (                                                    # noqa: E402
    AUTH, PG_ONLY, raw, _kb_with,
)

#: 与生产同款 DDL，**外加 `agent_id text NULL`**（归属列）—— 归属闸要用它。
#: 🔴 归属列可空 = 无主（存量语义不变）；有主的行才谈得上「谁的名下」。
DDL_WITH_AGENT = (
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
    'CREATE INDEX idx_documents_tags_gin ON "{s}".documents USING gin(tags)',
    'CREATE TABLE "{s}".chunks ('
    ' id bigserial PRIMARY KEY, document_id bigint NOT NULL'
    ' REFERENCES "{s}".documents(id), ord int NOT NULL, text text, source_path text)',
)

BULK_APPROVE = "/api/v1/kb/documents/bulk-approve"
BULK_REJECT = "/api/v1/kb/documents/bulk-reject"
BULK_DELETE = "/api/v1/kb/documents/bulk-delete"


@pytest.fixture()
def agent_kb(monkeypatch, tmp_path) -> Iterator[Any]:
    """隔离 schema：**带归属列**的真库（归属闸要在它上面验）。"""
    yield from _kb_with(monkeypatch, tmp_path, DDL_WITH_AGENT)


@pytest.fixture(autouse=True)
def _audit_tmp(monkeypatch, tmp_path) -> Iterator[None]:
    """纯接线用例也会走 `_reject` ⇒ `audit.record` 写文件；别让护栏测试往真审计目录里写。"""
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))


def _seed_doc(kb: Any, title: str, *, source_type: str = "manual",
              status: str = "effective", body: str = "", agent_id: str = "") -> int:
    """手工塞一篇文档（模拟各种域/状态的存量数据）。"""
    import psycopg

    meta = json.dumps({"body": body}, ensure_ascii=False) if body else "{}"
    with psycopg.connect(kb.dsn, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{kb.schema}".documents'
                    "(source_type, source_key, title, meta, content_hash, status, review_meta, agent_id)"
                    " VALUES (%s, %s, %s, '{}'::jsonb, %s, %s, %s::jsonb, %s) RETURNING id",
                    [source_type, f"{source_type}:{title}", title, f"sha:{title}", status,
                     meta, agent_id or None])
        doc_id = cur.fetchone()[0]
        cur.execute(f'INSERT INTO "{kb.schema}".chunks(document_id, ord, text, source_path)'
                    ' VALUES (%s, 0, %s, %s)', [doc_id, body or f"{title} 正文", f"{source_type}:{title}"])
    return doc_id


def _fail(detail: Any) -> Dict[str, Any]:
    """把 HTTPException 的 detail 掏出来（断言错误码与文案）。"""
    return detail


# ---------------------------------------------------------------- 纯接线（不碰库）

def test_批量确认串缺失或不等_400():
    """🔴 confirm 缺失 / 不等一律 400（与 ops/bulk 同一口径文案）：
    勾了 2 篇却手输 3（或压根没输）都不放行 —— 「我要动的这一批」与
    「我点确认的那一批」必须是同一个数。"""
    for confirm, error in (("", "confirm_required"), ("3", "confirm_mismatch"),
                           ("1", "confirm_mismatch")):
        with pytest.raises(HTTPException) as e:
            kb_router.kb_bulk_approve(
                kb_router.KbBulkBody(ids=["1", "2"], confirm=confirm),
                {"username": "console"})
        assert e.value.status_code == 400, confirm
        assert e.value.detail["error"] == error, confirm


def test_批量上限200_超了400不截断():
    """🔴 防误选全库：去重后 > 200 ⇒ 400。**不截断成前 200** ——
    被截断的批量写 = 用户以为都做了、实际没做。"""
    ok = kb_router._bulk_ids([str(i) for i in range(200)], "test")
    assert len(ok) == 200
    with pytest.raises(HTTPException) as e:
        kb_router._bulk_ids([str(i) for i in range(201)], "test")
    assert e.value.status_code == 400
    assert e.value.detail["error"] == "bulk_too_large"


def test_批量去重保序_空ids_400():
    """ids 去重（保序：与勾选顺序一致，回包好对账）；空 ⇒ 400（别静默跑零条）。"""
    assert kb_router._bulk_ids(["3", "1", "3", " 2 "], "test") == ["3", "1", "2"]
    with pytest.raises(HTTPException) as e:
        kb_router._bulk_ids([], "test")
    assert e.value.status_code == 400 and e.value.detail["error"] == "bulk_no_ids"


def test_批量驳回缺理由_400():
    """🔴 reason 必填：驳回不删正文（只标 rejected），理由是日后重新审核时
    唯一的线索 —— 空理由的驳回等于把一篇文档永久卡在队列里还不说缘由。"""
    with pytest.raises(HTTPException) as e:
        kb_router.kb_bulk_reject(
            kb_router.KbBulkRejectBody(ids=["1"], confirm="1", reason="   "),
            {"username": "console"})
    assert e.value.status_code == 400
    assert e.value.detail["error"] == "reason_required"


def test_批量端点都挂了会话与写闸():
    """🔴 静态判据：三个批量端点都必须挂**认会话的依赖**与 `write_guard`
    （漏了 write_guard = 审计落不下也照写，纪律就废了）。"""
    from keepsake.api.app import create_app
    from conftest import flat_routes

    app = create_app()
    want = {BULK_APPROVE, BULK_REJECT, BULK_DELETE}
    seen = set()
    for r in flat_routes(app):
        path = getattr(r, "path", "")
        if path not in want:
            continue
        names = {getattr(d.call, "__name__", "") for d in r.dependant.dependencies}
        assert any("require_session" in n or "writer_identity" in n for n in names), path
        assert any("write_guard" in n for n in names), path
        seen.add(path)
    assert seen == want, f"有端点没注册：{want - seen}"


# ---------------------------------------------------------------- 真库：逐条行为

@PG_ONLY
def test_批量批准_部分失败不拖累其他条(agent_kb):
    """🔴 批量里一条失败（已生效 ⇒ 409）不许把整批拖成 0 成功：
    另一条照样批准（切块进检索）。"""
    pending = _seed_doc(agent_kb, "待审一篇", source_type="agent",
                        status="pending", body="待审正文")
    done = _seed_doc(agent_kb, "已生效一篇", status="effective")
    r = agent_kb.client.post(BULK_APPROVE,
                             json={"ids": [str(pending), str(done)], "confirm": "2"},
                             headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["succeeded"] == 1 and body["total"] == 2
    assert [(f["id"], f["reason"]) for f in body["failed"]] == [(str(done), "kb_doc_not_pending")]
    # 批准那篇：切成块、落进检索
    assert raw(agent_kb, 'SELECT status FROM "{s}".documents WHERE id=%s',
               [pending]) == [("effective",)]
    assert raw(agent_kb, 'SELECT COUNT(*) FROM "{s}".chunks WHERE document_id=%s',
               [pending]) == [(1,)]


@PG_ONLY
def test_批量删除_归属闸_非primary删别人名下_那条failed(agent_kb):
    """🔴 **归属闸不放松**：非 primary 只能删自己名下的；删别人名下 ⇒ 那一条
    进 failed（原因可辨识 = `kb_not_owner`），自己名下的照常删（且有备份）。"""
    mine = _seed_doc(agent_kb, "我名下的", source_type="agent", agent_id="worker-b")
    theirs = _seed_doc(agent_kb, "别人名下的", source_type="agent", agent_id="worker-a")
    writer = {"kind": "agent", "agent_id": "worker-b", "role": "sub", "status": "pending"}
    res = kb_router.kb_bulk_delete(
        kb_router.KbBulkBody(ids=[str(mine), str(theirs)], confirm="2"), writer)
    assert res["succeeded"] == 1 and res["total"] == 2
    assert [(f["id"], f["reason"]) for f in res["failed"]] == [(str(theirs), "kb_not_owner")]
    # 自己名下那篇真删了（chunks 一起没），别人那篇还在
    assert raw(agent_kb, 'SELECT title FROM "{s}".documents WHERE id=%s',
               [theirs]) == [("别人名下的",)]
    assert raw(agent_kb, 'SELECT title FROM "{s}".documents WHERE id=%s', [mine]) == []
    assert raw(agent_kb, 'SELECT COUNT(*) FROM "{s}".chunks WHERE document_id=%s',
               [mine]) == [(0,)]


@PG_ONLY
def test_批量删除_站内会话primary_全量可删且逐篇备份(agent_kb, tmp_path):
    """站内会话 = 主脑本人（primary）⇒ 全量可删（含代理域）；删前**逐篇**备份
    （备份文件是唯一的回退路径）。"""
    a = _seed_doc(agent_kb, "手动一篇", body="手动正文")
    b = _seed_doc(agent_kb, "代理一篇", source_type="agent", agent_id="worker-a",
                  body="代理正文")
    r = agent_kb.client.post(BULK_DELETE,
                             json={"ids": [str(a), str(b)], "confirm": "2"}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["succeeded"] == 2 and body["failed"] == []
    assert raw(agent_kb, 'SELECT COUNT(*) FROM "{s}".documents')[0][0] == 0
    # 逐篇备份：两个备份文件都落盘，且正文找得到
    backups = sorted(Path(tmp_path / "ops").glob("**/document.*.json"))
    assert len(backups) == 2, [p.name for p in backups]
    texts = "".join(p.read_text(encoding="utf-8") for p in backups)
    assert "手动正文" in texts and "代理正文" in texts


@PG_ONLY
def test_批量删除_灌入器域仍409(agent_kb):
    """skill/skill_hk 是夜间灌入器的地盘：改了当晚被覆盖、删了被死文档清理扫掉
    —— 批量也不许碰（那一条进 failed，原因码 `kb_doc_immutable`）。"""
    doc_id = _seed_doc(agent_kb, "灌入器技能", source_type="skill")
    res = kb_router.kb_bulk_delete(kb_router.KbBulkBody(ids=[str(doc_id)], confirm="1"),
                                   {"kind": "console", "agent_id": "", "role": "primary"})
    assert res["succeeded"] == 0
    assert res["failed"] == [{"id": str(doc_id), "reason": "kb_doc_immutable"}]
    assert raw(agent_kb, 'SELECT title FROM "{s}".documents WHERE id=%s', [doc_id])


@PG_ONLY
def test_批量驳回_统一理由落每篇(agent_kb):
    """整批一个理由写进每篇的 review_meta；理由缺失 ⇒ 整批 400（一条都不改）。"""
    a = _seed_doc(agent_kb, "待审甲", source_type="agent", status="pending", body="正文甲")
    b = _seed_doc(agent_kb, "待审乙", source_type="agent", status="pending", body="正文乙")
    r = agent_kb.client.post(BULK_REJECT,
                             json={"ids": [str(a), str(b)], "confirm": "2",
                                   "reason": "来源不可信"},
                             headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["succeeded"] == 2
    for doc_id in (a, b):
        got = raw(agent_kb, 'SELECT status, review_meta FROM "{s}".documents WHERE id=%s',
                  [doc_id])[0]
        assert got[0] == "rejected"
        assert got[1]["reject_reason"] == "来源不可信"


@PG_ONLY
def test_批量动作各留审计_逐条加批次汇总(agent_kb, tmp_path):
    """逐条那几行由单条端点自己记（与单条操作同一动作名），另记一条批次汇总
    —— 事后对账能一眼看出「这批动了哪几篇」。"""
    a = _seed_doc(agent_kb, "待审一篇", source_type="agent", status="pending", body="正文")
    agent_kb.client.post(BULK_APPROVE, json={"ids": [str(a)], "confirm": "1"}, headers=AUTH)
    files = sorted(Path(tmp_path / "audit").glob("audit-*.jsonl"))
    lines = [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]
    assert any(l["action"] == "kb.document.approve" and l["target"] == str(a) for l in lines)
    summary = [l for l in lines if l["action"] == "kb.bulk.approve"]
    assert summary and "succeeded=1" in summary[0]["reason"]
