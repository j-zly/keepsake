"""知识库审核队列（批2：非可信来源写入的知识文档先审后用）。

**判据只有一条 —— 谁在写**（服务端判定，不信调用方自称）：站内会话与主代理是可信
来源，直进 `effective`（现行为零变化）；**子代理**写进来的落 `pending`，进控制台待审
队列；未批准/已吊销/凭据无效的代理 ⇒ 403，连队列都不进。

本文件钉死八件最容易悄悄坏掉的事：

  1. 子代理提交 ⇒ `status='pending'`、**一行切片都没有**、列表/检索/导出都看不见；
  2. 批准 ⇒ 同一个事务里 改 effective + **此刻才切块** + 记批准痕迹；切完立刻可检索；
  3. 驳回 ⇒ `status='rejected'`、检索不可见、`review_meta.reject_reason` 落库；
     且**不删正文**（可再审）；
  4. 站内手建 ⇒ 直接 effective（有切片、检索得到）—— **零回归**；
  5. 被拒的代理（pending/revoked/坏凭据）⇒ 403，且库里一篇都没多；
  6. 待审/已驳回的文档不许从改/删端点绕过审核动作（409）；
  7. 老库未迁移 status 列 ⇒ 读路径一律当全 effective（降级不炸），写审核路径人话 500；
  8. 提交查重（同标题/近重复）只记 `review_meta.dup_of` 提示，**不拒收**；
     批准/驳回各留一行审计。

真库用例与 test_kb_crud 共用同一套 fixture（同一个隔离 schema 的建表 DDL ——
那里已同步加上 `status` / `review_meta` 两列，与生产迁移**同形状**）。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, Iterator, List

import pytest
from fastapi import HTTPException

from keepsake.api.routers import kb as kb_router

# 共用 fixture 与建表 DDL（测试库形状与生产同款，含 status / review_meta）
from test_kb_crud import (                                                    # noqa: F401
    AUTH, CREATE, EXPORT, IMPORT, PG_ONLY, create, docs, kb, legacy_kb,
    raw, search, seed_skill, snippets, _kb_with, DDL,
)

#: 「批1 已迁移、批2 还没迁」的老表形状：有 content_type 与 tags、**没有 status /
#: review_meta** —— 这是真实上线顺序下的窗口期，缺列防御必须在这张表上验。
DDL_NO_STATUS = (
    'CREATE SCHEMA "{s}"',
    'CREATE TABLE "{s}".documents ('
    ' id bigserial PRIMARY KEY, source_type text NOT NULL, source_key text NOT NULL,'
    ' content_type text NOT NULL DEFAULT \'note\','
    ' tags text[] NOT NULL DEFAULT \'{{}}\','
    ' title text, meta jsonb, content_hash text,'
    ' updated_at timestamptz NOT NULL DEFAULT now(),'
    ' UNIQUE (source_type, source_key))',
    'CREATE TABLE "{s}".chunks ('
    ' id bigserial PRIMARY KEY, document_id bigint NOT NULL'
    ' REFERENCES "{s}".documents(id), ord int NOT NULL, text text, source_path text)',
)

#: 生产迁移 SQL（与主脑手执行的形状逐字一致；测试跑它来保证幂等 + 真能迁）
MIGRATE_STATUS_SQL = (
    'ALTER TABLE "{s}".documents ADD COLUMN IF NOT EXISTS '
    "status text NOT NULL DEFAULT 'effective'",
    'ALTER TABLE "{s}".documents ADD COLUMN IF NOT EXISTS '
    "review_meta jsonb NOT NULL DEFAULT '{{}}'",   # ⚠️ 双花括号：下面要 .format(s=schema)
    'CREATE INDEX IF NOT EXISTS idx_documents_status ON "{s}".documents(status)',
)

#: 子代理凭据形态（`ksk_` + 32 hex）。这里只是一串**假**串：真凭据只在注册表
#: approve 的响应里出现一次，库里只存 sha256。
SUB_CRED = "ksk_" + "ab" * 16
PRIMARY_CRED = "ksk_" + "cd" * 16
SUB_AUTH = {"Authorization": f"Bearer {SUB_CRED}"}
PRIMARY_AUTH = {"Authorization": f"Bearer {PRIMARY_CRED}"}


class _Reg:
    """注册表替身：只回答 `verify_any`（身份链的**唯一**入口，与 AgentGate 同一个）。

    不替 `verify` / `approve` …：控制台侧的知识库写入只用 `verify_any`（头里只有
    Bearer，身份由凭据反查），替多了就等于在测试里另发明一条身份路径。
    `table` 按凭据分别给结论，**没登记的凭据一律按 default 拒**（fail-closed）。
    """

    def __init__(self, table: Dict[str, Dict[str, Any]] = None,
                 default: Dict[str, Any] = None) -> None:
        self.table = dict(table or {})
        self.default = dict(default or _denied("unknown_credential"))
        self.seen: List[str] = []

    def verify_any(self, credential: str) -> Dict[str, Any]:
        self.seen.append(credential)
        return dict(self.table.get(credential, self.default))


def _ok(agent_id: str, role: str) -> Dict[str, Any]:
    return {"ok": True, "role": role, "status": "approved", "reason": "",
            "agent_id": agent_id}


def _denied(reason: str = "not_approved") -> Dict[str, Any]:
    return {"ok": False, "role": "sub", "status": "revoked", "reason": reason,
            "agent_id": "sub-bot"}


def _sub_reg() -> _Reg:
    """只有子代理那条凭据能用（主代理凭据也是拒的，别在用例里混用两个身份）。"""
    return _Reg({SUB_CRED: _ok("sub-bot", "sub")})


@pytest.fixture()
def sub_agent(monkeypatch) -> Iterator[_Reg]:
    """把 `_agent_registry` 换成「凭据 SUB ⇒ 已批准的子代理」；凭据本身照常原样传下去。"""
    reg = _sub_reg()

    def fake(storage: Any) -> _Reg:
        assert storage is None or storage is not False, "不该有存储参数被硬塞进来"
        return reg

    monkeypatch.setattr(kb_router, "_agent_registry", fake)
    yield reg


@pytest.fixture()
def primary_agent(monkeypatch) -> Iterator[_Reg]:
    """两条凭据都在：主代理可信（直进 effective）、子代理照旧进待审队列。"""
    reg = _Reg({SUB_CRED: _ok("sub-bot", "sub"), PRIMARY_CRED: _ok("main-brain", "primary")})
    monkeypatch.setattr(kb_router, "_agent_registry", lambda storage: reg)
    yield reg


@pytest.fixture()
def revoked_agent(monkeypatch) -> Iterator[_Reg]:
    reg = _Reg({SUB_CRED: _denied()}, default=_denied("unknown_credential"))
    monkeypatch.setattr(kb_router, "_agent_registry", lambda storage: reg)
    yield reg


@pytest.fixture()
def no_status_kb(monkeypatch, tmp_path) -> Iterator[Any]:
    """隔离 schema：批1 已迁移、**status / review_meta 还没迁**（缺列窗口期）。"""
    yield from _kb_with(monkeypatch, tmp_path, DDL_NO_STATUS)


#: 生产同款形状：chunks 带 **tsvector** 列（180 库有 `tsv`，走 tsquery 检索路）。
#: 🔴 91 测试库的默认 DDL 没这根列 ⇒ 测试全走 ILIKE 兜底路，**永远逮不到 tsquery
#: 参数错位**（批2 首收口时生产实锤过：`[*params, *rank_params]` 把 status 值喂给
#: tsquery ⇒ 迁移后检索恒 0）。这条 fixture 就是把这个盲区钉进回归网。
DDL_TSV = DDL + (
    'ALTER TABLE "{s}".chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS '
    "(to_tsvector('simple', coalesce(text, ''))) STORED",
)


@pytest.fixture()
def tsv_kb(monkeypatch, tmp_path) -> Iterator[Any]:
    """隔离 schema + chunks.tsv（生产同款检索路径）。"""
    yield from _kb_with(monkeypatch, tmp_path, DDL_TSV)


def pending(client, **params) -> Dict[str, Any]:
    r = client.get("/api/v1/kb/pending", params=params or None, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def doc_row(kb, doc_id: Any, column: str = "status"):
    return raw(kb, 'SELECT {c} FROM "{{s}}".documents WHERE id=%s'.format(c=column),
               [doc_id])


def rmeta(kb, doc_id: Any) -> Dict[str, Any]:
    got = raw(kb, 'SELECT review_meta FROM "{s}".documents WHERE id=%s', [doc_id])
    val = got[0][0]
    return val if isinstance(val, dict) else json.loads(val or "{}")


def audit_lines(tmp_path) -> List[Dict[str, Any]]:
    files = sorted(pathlib.Path(tmp_path / "audit").glob("audit-*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- 身份判定（不碰库）

def test_身份_站内会话不碰注册表_直进effective(monkeypatch, tmp_path):
    """🔴 站内人工路径零变化：走 require_session，压根不构造注册表、不进审核。"""
    monkeypatch.setenv("CONSOLE_TOKEN", AUTH["Authorization"][len("Bearer "):])
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    ident = kb_router.writer_identity(authorization=AUTH["Authorization"], storage=None)
    assert ident == {"kind": "console", "agent_id": "", "role": "",
                     "status": "effective"}
    # 没给凭据 ⇒ 原样 401（判据一字不改）
    with pytest.raises(HTTPException) as e:
        kb_router.writer_identity(authorization=None, storage=None)
    assert e.value.status_code == 401


def test_身份_子代理落pending_主代理直进effective(monkeypatch, tmp_path):
    for role, want in (("sub", "pending"), ("primary", "effective")):
        monkeypatch.setattr(kb_router, "_agent_registry",
                            lambda storage, r=role: _Reg({SUB_CRED: _ok("a1", r)}))
        ident = kb_router.writer_identity(authorization=SUB_AUTH["Authorization"],
                                          storage=None)
        assert ident["status"] == want, role
        assert ident["agent_id"] == "a1"


def test_身份_凭据无效或已吊销_403且不泄露凭据(monkeypatch, tmp_path):
    """🔴 agent_gate 同一 fail-closed 方向：核不过 ⇒ 403（不是 401，更不是放行）。"""
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    for reason in ("not_approved", "bad_credential", "unknown_agent"):
        monkeypatch.setattr(kb_router, "_agent_registry",
                            lambda storage, rs=reason: _Reg({SUB_CRED: _denied(rs)}))
        with pytest.raises(HTTPException) as e:
            kb_router.writer_identity(authorization=SUB_AUTH["Authorization"], storage=None)
        detail = e.value.detail
        assert e.value.status_code == 403 and detail["error"] == "agent_denied"
        assert SUB_CRED not in json.dumps(detail, ensure_ascii=False)
    # 注册表装不出来（存储不可达）也按拒绝处理 —— 核不了身份就放行等于闸门关着
    def boom(storage: Any):
        raise RuntimeError("存储不可达")
    monkeypatch.setattr(kb_router, "_agent_registry", boom)
    with pytest.raises(HTTPException) as e:
        kb_router.writer_identity(authorization=SUB_AUTH["Authorization"], storage=None)
    assert e.value.status_code == 403


# ---------------------------------------------------------------- 代理写入 → pending

@PG_ONLY
def test_子代理提交落pending_不进列表检索导出且零切片(kb, sub_agent):
    """🔴 「先审后用」的三条：状态是 pending、切了一片都没切、三个读口都看不见。"""
    r = kb.client.post(CREATE, json={"confirm": True, "title": "子代理笔记",
                                     "content": "秘密配方是动量因子"}, headers=SUB_AUTH)
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert r.json()["review"] == "pending" and r.json()["chunks_count"] == 0
    assert doc_row(kb, doc_id) == [("pending",)]
    assert raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s',
               [doc_id]) == []               # 🔴 一行切片都没有（未审正文不进检索索引）
    assert sub_agent.seen == [SUB_CRED]     # 身份由凭据反查，不采信调用方自称
    assert docs(kb.client)["items"] == []
    assert search(kb.client, "动量因子")["items"] == []
    exported = [json.loads(line) for line in
                kb.client.get(EXPORT, headers=AUTH).text.splitlines() if line.strip()]
    assert exported == []


@PG_ONLY
def test_待审面板给得出队列_计数与正文预览(kb, sub_agent):
    """面板一屏要够审核的人判：标题/类型/来源代理/时间/tags + 正文预览。"""
    kb.client.post(CREATE, json={"confirm": True, "title": "子代理笔记",
                                 "content": "秘密配方是动量因子", "tags": ["量化"]},
                   headers=SUB_AUTH)
    got = pending(kb.client)
    assert got["pending_count"] == 1 and got["count"] == 1 and len(got["items"]) == 1
    item = got["items"][0]
    assert item["title"] == "子代理笔记" and item["status"] == "pending"
    assert item["submitted_by"] == "sub-bot" and item["submitted_at"]
    assert item["content_type"] == "note" and item["tags"] == ["量化"]
    assert item["preview"] == "秘密配方是动量因子"
    # 🔴 计数不写死：站内再建两篇已生效的，pending_count 仍是 1（全量 pending 口径）
    create(kb.client, "站内一篇", "正文一")
    create(kb.client, "站内二篇", "正文二")
    assert pending(kb.client)["pending_count"] == 1


@PG_ONLY
def test_主代理提交直进effective_子代理更新正文打回待审(kb, primary_agent):
    """🔴 可信来源不劳审：主代理写的是 effective；子代理改正文才重新进审核。"""
    body = kb.client.post(CREATE, json={"confirm": True, "title": "主代理笔记",
                                       "content": "第一版正文"}, headers=PRIMARY_AUTH)
    assert body.json()["review"] == "effective"
    doc_id = body.json()["id"]
    assert doc_row(kb, doc_id) == [("effective",)]
    assert raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s',
               [doc_id]) == [("第一版正文",)]

    r = kb.client.post(f"/api/v1/kb/documents/{doc_id}/update",
                       json={"confirm": True, "content": "第二版正文"}, headers=SUB_AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["review"] == "pending"
    assert doc_row(kb, doc_id) == [("pending",)]
    # 🔴 不重切：旧块还在但被 status 谓词挡住（批准时才 replace_chunks）
    assert raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s',
               [doc_id]) == [("第一版正文",)]
    assert search(kb.client, "第一版正文")["items"] == []      # pending 不可检索
    assert search(kb.client, "第二版正文")["items"] == []
    assert [i["title"] for i in docs(kb.client)["items"]] == []
    # 新正文在 review_meta.body 里等着被批准时切块
    assert rmeta(kb, doc_id)["body"] == "第二版正文"


@PG_ONLY
def test_被拒代理403_库里一篇都没多(kb, revoked_agent):
    """🔴 吊销的代理连队列都不进：403，且 documents 表零新增（不是「进了队再拒」）。"""
    before = raw(kb, 'SELECT COUNT(*) FROM "{s}".documents')[0][0]
    r = kb.client.post(CREATE, json={"confirm": True, "title": "偷偷写",
                                     "content": "偷偷写"}, headers=SUB_AUTH)
    assert r.status_code == 403 and r.json()["detail"]["error"] == "agent_denied"
    assert raw(kb, 'SELECT COUNT(*) FROM "{s}".documents')[0][0] == before
    assert pending(kb.client)["pending_count"] == 0


@PG_ONLY
def test_查重_同标题记dup_of提示但不拒收(kb, sub_agent):
    """🔴 查重是**提示**不是闸门：子代理批量交同名文档是常态，拒收只会逼它改标题绕过。"""
    create(kb.client, "值班手册", "第一份正文")
    r = kb.client.post(CREATE, json={"confirm": True, "title": "值班手册",
                                     "content": "第二份正文（同名不同内容）"},
                       headers=SUB_AUTH)
    assert r.status_code == 200, r.text
    got = rmeta(kb, r.json()["id"])
    assert got["dup_of"] and got["dup_exact"] is True
    assert got["dup_of_title"] == "值班手册"
    # 审核页看得见这条警示
    assert pending(kb.client)["items"][0]["dup_of_title"] == "值班手册"
    # 重复提交同一篇：不许把**自己**当成重复（按 source_key 排除自己那行）
    again = kb.client.post(CREATE, json={"confirm": True, "title": "值班手册",
                                         "content": "第二份正文（同名不同内容）"},
                           headers=SUB_AUTH)
    assert again.json()["status"] == "unchanged"
    assert rmeta(kb, again.json()["id"])["dup_of"] != again.json()["id"]


# ---------------------------------------------------------------- 批准 / 驳回

@PG_ONLY
def test_批准_此刻才切块且立刻可检索(kb, sub_agent):
    """🔴 批准事务内：改 effective + 按 content_type 切块 + 记批准痕迹。"""
    r = kb.client.post(CREATE, json={"confirm": True, "title": "子代理长文",
                                     "content": "# 甲\n" + "内容" * 2000},
                       headers=SUB_AUTH)
    doc_id = r.json()["id"]
    assert raw(kb, 'SELECT COUNT(*) FROM "{s}".chunks WHERE document_id=%s',
               [doc_id]) == [(0,)]

    a = kb.client.post(f"/api/v1/kb/documents/{doc_id}/approve",
                       json={"confirm": True}, headers=AUTH)
    assert a.status_code == 200, a.text
    assert a.json()["status"] == "effective" and a.json()["chunks_count"] >= 2
    assert doc_row(kb, doc_id) == [("effective",)]
    assert raw(kb, 'SELECT COUNT(*) FROM "{s}".chunks WHERE document_id=%s',
               [doc_id]) == [(a.json()["chunks_count"],)]
    # 批准后才进检索与列表
    assert [i["title"] for i in docs(kb.client)["items"]] == ["子代理长文"]
    assert search(kb.client, "内容内容")["items"]
    meta = rmeta(kb, doc_id)
    assert meta["approved_by"] and meta["approved_at"] and meta["submitted_by"] == "sub-bot"
    assert pending(kb.client)["pending_count"] == 0


@PG_ONLY
def test_批准_link仍按单块入库(kb, sub_agent):
    """🔴 批准走的是同一个 `chunk_document` 注册表：link 批准后仍是一块。"""
    r = kb.client.post(CREATE, json={"confirm": True, "title": "子代理链接",
                                     "content": "摘要一句", "content_type": "link",
                                     "url": "https://example.com/a"}, headers=SUB_AUTH)
    doc_id = r.json()["id"]
    a = kb.client.post(f"/api/v1/kb/documents/{doc_id}/approve",
                       json={"confirm": True}, headers=AUTH)
    assert a.status_code == 200, a.text
    assert a.json()["chunks_count"] == 1
    assert raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s',
               [doc_id]) == [("子代理链接\nhttps://example.com/a\n摘要一句",)]


@PG_ONLY
def test_驳回_标rejected检索不可见且理由落meta不删正文(kb, sub_agent):
    """🔴 驳回是**标记不是删除**：正文还在 review_meta.body，改主意能再批。"""
    r = kb.client.post(CREATE, json={"confirm": True, "title": "子代理笔记",
                                     "content": "动量因子配方"}, headers=SUB_AUTH)
    doc_id = r.json()["id"]
    j = kb.client.post(f"/api/v1/kb/documents/{doc_id}/reject",
                       json={"confirm": True, "reason": "与已有手册重复，且没有出处"},
                       headers=AUTH)
    assert j.status_code == 200, j.text
    assert j.json()["status"] == "rejected"
    assert doc_row(kb, doc_id) == [("rejected",)]
    assert search(kb.client, "动量因子")["items"] == []
    assert docs(kb.client)["items"] == []            # 「全部」Tab 不出现
    meta = rmeta(kb, doc_id)
    assert meta["reject_reason"] == "与已有手册重复，且没有出处"
    assert meta["rejected_by"] and meta["rejected_at"]
    assert meta["body"] == "动量因子配方"             # 正文没删
    # 面板留一行状态（已驳回仍看得见，chip 计数不含它）
    got = pending(kb.client)
    assert got["pending_count"] == 0 and got["count"] == 1
    assert got["items"][0]["status"] == "rejected"
    assert got["items"][0]["reject_reason"] == "与已有手册重复，且没有出处"
    # 可再审：驳回过的还能批（批准后照常切块进检索）
    a = kb.client.post(f"/api/v1/kb/documents/{doc_id}/approve",
                       json={"confirm": True}, headers=AUTH)
    assert a.status_code == 200, a.text
    assert search(kb.client, "动量因子")["items"]


@PG_ONLY
def test_驳回_reason必填(kb, sub_agent):
    """🔴 空理由的驳回会把文档永久卡在队列里还不说缘由 ⇒ 400。"""
    r = kb.client.post(CREATE, json={"confirm": True, "title": "子代理笔记",
                                     "content": "正文"}, headers=SUB_AUTH)
    for body in ({"confirm": True}, {"confirm": True, "reason": "   "},
                 {"reason": "有理由但没确认"}):
        j = kb.client.post(f"/api/v1/kb/documents/{r.json()['id']}/reject",
                           json=body, headers=AUTH)
        assert j.status_code == 400, body
        assert j.json()["detail"]["error"] in ("reason_required", "confirm_required")
    assert doc_row(kb, r.json()["id"]) == [("pending",)]


@PG_ONLY
def test_批准已生效的文档409_驳回也不许(kb):
    """🔴 驳回一篇在用的文档 = 让它从检索里消失而没人批准过；那是真删的活。"""
    doc = create(kb.client, "站内一篇", "正文")
    path = f"/api/v1/kb/documents/{doc['id']}"
    a = kb.client.post(f"{path}/approve", json={"confirm": True}, headers=AUTH)
    assert a.status_code == 409 and a.json()["detail"]["error"] == "kb_doc_not_pending"
    j = kb.client.post(f"{path}/reject", json={"confirm": True, "reason": "不想要了"},
                       headers=AUTH)
    assert j.status_code == 409 and j.json()["detail"]["error"] == "kb_doc_not_pending"
    assert doc_row(kb, doc["id"]) == [("effective",)]


@PG_ONLY
def test_审核端点不接受代理身份(kb, sub_agent):
    """🔴 审核动作只能由控制台的人做：子代理自己批自己的稿 ⇒ 401（会话不过）。"""
    r = kb.client.post(CREATE, json={"confirm": True, "title": "子代理笔记",
                                     "content": "正文"}, headers=SUB_AUTH)
    doc_id = r.json()["id"]
    a = kb.client.post(f"/api/v1/kb/documents/{doc_id}/approve",
                       json={"confirm": True}, headers=SUB_AUTH)
    assert a.status_code == 401
    assert doc_row(kb, doc_id) == [("pending",)]


@PG_ONLY
def test_待审文档不许从改删端点绕过审核(kb, sub_agent):
    """🔴 改/删只对已生效的开放：绕过去就等于在审核队列底下偷改或偷删。"""
    r = kb.client.post(CREATE, json={"confirm": True, "title": "子代理笔记",
                                     "content": "正文"}, headers=SUB_AUTH)
    doc_id = r.json()["id"]
    u = kb.client.post(f"/api/v1/kb/documents/{doc_id}/update",
                       json={"confirm": True, "content": "偷改"}, headers=AUTH)
    assert u.status_code == 409 and u.json()["detail"]["error"] == "kb_doc_not_effective"
    d = kb.client.post(f"/api/v1/kb/documents/{doc_id}/delete",
                       json={"confirm": "子代理笔记"}, headers=AUTH)
    assert d.status_code == 409 and d.json()["detail"]["error"] == "kb_doc_not_effective"
    assert doc_row(kb, doc_id) == [("pending",)]
    assert rmeta(kb, doc_id)["body"] == "正文"


@PG_ONLY
def test_批准驳回各留一行审计(kb, sub_agent, tmp_path):
    r = kb.client.post(CREATE, json={"confirm": True, "title": "子代理笔记",
                                     "content": "正文"}, headers=SUB_AUTH)
    keep = r.json()["id"]
    drop = kb.client.post(CREATE, json={"confirm": True, "title": "子代理另一篇",
                                        "content": "正文二"}, headers=SUB_AUTH).json()["id"]
    kb.client.post(f"/api/v1/kb/documents/{keep}/approve", json={"confirm": True},
                   headers=AUTH)
    kb.client.post(f"/api/v1/kb/documents/{drop}/reject",
                   json={"confirm": True, "reason": "没出处"}, headers=AUTH)
    # 被拒的写也留痕（先拒的那次：没给 reason）
    kb.client.post(f"/api/v1/kb/documents/{drop}/reject", json={"confirm": True},
                   headers=AUTH)
    lines = audit_lines(tmp_path)
    ok_lines = {(l["action"], l["target"]): l for l in lines if l["ok"]}
    assert ("kb.document.approve", keep) in ok_lines
    assert ("kb.document.reject", drop) in ok_lines
    assert "chunks=" in ok_lines[("kb.document.approve", keep)]["reason"]
    # 被拒的写也留痕（reason 必填那条）
    bad = [l for l in lines if not l["ok"] and l["action"] == "kb.document.reject"]
    assert bad and bad[0]["reason"] == "reason_required"


@PG_ONLY
def test_重复提交同一篇不进队列_也不搅已生效那篇(kb, primary_agent):
    """🔴 `unchanged`（同键同 hash）时这次提交没进队列：已生效那篇的审核台账不许被搅。

    键是内容派生的 ⇒ 同键必同内容，而那篇已经审过了；真把它打回 pending 等于让
    一篇在用的文档凭空从检索里消失。
    """
    doc = create(kb.client, "值班手册", "联系人是张三")
    again = kb.client.post(CREATE, json={"confirm": True, "title": "值班手册",
                                         "content": "联系人是张三"}, headers=SUB_AUTH)
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "unchanged" and again.json()["review"] == "unchanged"
    assert again.json()["id"] == doc["id"]
    assert doc_row(kb, doc["id"]) == [("effective",)]        # 没被打回待审
    assert rmeta(kb, doc["id"]) == {}                       # 台账一个字没多
    assert pending(kb.client)["pending_count"] == 0
    assert search(kb.client, "张三")["items"]                # 照常可检索


# ---------------------------------------------------------------- tsvector 检索回归（生产实锤过的错位）

@PG_ONLY
def test_tsvector路_批准后可检索_参数不错位(tsv_kb, sub_agent):
    """🔴 生产盲区回归：chunks.tsv 在（tsquery 路径）时，status 谓词的值**不许**被
    喂给 plainto_tsquery —— 批2 首收口在生产实锤：`[*params, *rank_params]` 顺序
    与占位符出现顺序（ts_rank 在 SELECT、先于 WHERE）不匹配 ⇒ 迁移后检索恒 0。
    91 测试库默认无 tsv 列全走 ILIKE，此用例把 tsquery 路径钉进回归网。"""
    r = tsv_kb.client.post(CREATE, json={"confirm": True, "title": "tsv路探针",
                                         "content": "ZZPROBEWORD9001 动量因子",
                                         }, headers=SUB_AUTH)
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    # pending：tsquery 路径也看不见（status 谓词生效）
    assert search(tsv_kb.client, "ZZPROBEWORD9001")["mode"] == "tsvector"
    assert search(tsv_kb.client, "ZZPROBEWORD9001")["items"] == []
    a = tsv_kb.client.post(f"/api/v1/kb/documents/{doc_id}/approve",
                           json={"confirm": True}, headers=AUTH)
    assert a.status_code == 200, a.text
    hits = search(tsv_kb.client, "ZZPROBEWORD9001")
    assert hits["mode"] == "tsvector"
    assert [i["doc_id"] for i in hits["items"]] == [doc_id]   # 🔴 批准即检索得到（错位=恒 0）
    # pending 的另一篇不许漏进来
    tsv_kb.client.post(CREATE, json={"confirm": True, "title": "tsv路待审",
                                     "content": "ZZPROBEWORD9002 未审稿"}, headers=SUB_AUTH)
    assert [i["doc_id"] for i in search(tsv_kb.client, "ZZPROBEWORD9002")["items"]] == []


# ---------------------------------------------------------------- 站内手建零回归

@PG_ONLY
def test_站内手建直进effective且有切片(kb):
    """🔴 红线：站内人工路径**零变化** —— 手建不审、当场切块、立刻能检索。"""
    doc = create(kb.client, "值班手册", "联系人是张三")
    assert doc_row(kb, doc["id"]) == [("effective",)]
    assert doc["chunks_count"] == 1
    assert [i["title"] for i in docs(kb.client)["items"]] == ["值班手册"]
    assert snippets(kb.client, "张三")
    assert rmeta(kb, doc["id"]) == {}
    assert pending(kb.client)["pending_count"] == 0


@PG_ONLY
def test_导出导入往返不含待审(kb, sub_agent):
    """🔴 导出只导已生效的：待审那篇不许混进备份/迁移文件里。"""
    kb.client.post(CREATE, json={"confirm": True, "title": "待审一篇",
                                 "content": "别导我"}, headers=SUB_AUTH)
    create(kb.client, "已生效一篇", "导我")
    rows = [json.loads(line) for line in
            kb.client.get(EXPORT, headers=AUTH).text.splitlines() if line.strip()]
    assert [r["title"] for r in rows] == ["已生效一篇"]


# ---------------------------------------------------------------- 缺列防御（老库）

@PG_ONLY
def test_缺列_读路径一律当全effective(no_status_kb):
    """🔴 与 content_type / tags 同款降级：老库上队列恒空、列表/检索照常。"""
    import psycopg

    with psycopg.connect(no_status_kb.dsn, autocommit=True, connect_timeout=30) as conn, \
            conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{no_status_kb.schema}".documents'
                    "(source_type, source_key, title, meta, content_hash) VALUES"
                    " ('manual','manual:old','老库手记','{}'::jsonb,'sha:x')")
        cur.execute(f'INSERT INTO "{no_status_kb.schema}".chunks(document_id, ord, text)'
                    " VALUES (1, 0, '老库正文')")
    assert [i["title"] for i in docs(no_status_kb.client)["items"]] == ["老库手记"]
    assert search(no_status_kb.client, "老库正文")["items"]
    got = pending(no_status_kb.client)
    assert got == {"items": [], "count": 0, "pending_count": 0,
                   "page": 1, "page_size": 20}       # 不编一个假队列出来


@PG_ONLY
def test_缺列_站内写照常_审核动作人话500带迁移SQL(no_status_kb, sub_agent):
    """🔴 防「主脑忘了先迁移」：站内手建不受影响，审核相关的写要拿到人话 500。"""
    create(no_status_kb.client, "站内一篇", "正文")
    assert [i["title"] for i in docs(no_status_kb.client)["items"]] == ["站内一篇"]
    r = no_status_kb.client.post(CREATE, json={"confirm": True, "title": "子代理笔记",
                                              "content": "正文"}, headers=SUB_AUTH)
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "kb_schema_unknown"
    assert "ALTER TABLE" in detail["message"] and "status" in detail["message"]
    assert [i["title"] for i in docs(no_status_kb.client)["items"]] == ["站内一篇"]


@PG_ONLY
def test_迁移SQL_幂等且真能把老表迁起来(legacy_kb):
    """探针：跑两遍生产迁移 SQL ⇒ 列出现、存量行拿到 DEFAULT 'effective'、不炸。"""
    import psycopg

    with psycopg.connect(legacy_kb.dsn, autocommit=True, connect_timeout=30) as conn, \
            conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{legacy_kb.schema}".documents'
                    "(source_type, source_key, title) VALUES"
                    " ('manual','manual:old','老库手记')")
        for stmt in MIGRATE_STATUS_SQL:
            cur.execute(stmt.format(s=legacy_kb.schema))
            cur.execute(stmt.format(s=legacy_kb.schema))
    assert doc_row(legacy_kb, 1) == [("effective",)]
    assert rmeta(legacy_kb, 1) == {}
    # 迁完之后队列端点照常返回（空）
    assert pending(legacy_kb.client)["count"] == 0
