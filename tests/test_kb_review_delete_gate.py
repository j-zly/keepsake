"""审核页「删除 / 批量删除」的后端闸门（2026-10 kb_del_stats_r2）。

审核页补了行操作「删除」与批量条「批量删除」，打的是**既有**端点
（`POST /kb/documents/{id}/delete` 与 `POST /kb/documents/bulk-delete`）。
本文件钉死那条状态闸门（`_need_deletable`）：

  * **primary（站内会话 / 主代理凭据）** ⇒ 待审 / 已驳回的行**可以真删**
    （它从未切过块、从未进过检索 —— 删掉它 = 「这条我不收」）；
  * **非 primary（子代理凭据）** ⇒ 仍旧 **409 `kb_doc_not_effective`**
    （c24a014 的「待审行不许改/删绕过」对代理面一个字不动）；
  * 已生效的行 ⇒ 谁都能删（归属闸 `_writable_doc` 另管，不在本文件）。

纯函数级（不连库）：闸门本身是纯判断，钉在这里就能跑。
真库那一层（备份 / 审计 / 逐条成败）由 `tests/api/test_kb_review.py` 与
`tests/api/test_kb_bulk_ops.py` 覆盖（需要 PG 测试库，本地会 skip）。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from keepsake.api.routers import kb as kb_router


@pytest.fixture(autouse=True)
def _audit_dir(tmp_path, monkeypatch):
    """审计落盘到临时目录（拒绝路径也会写一行审计 —— 别往 /root 里写）。"""
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))


#: 站内会话（console）与主代理凭据（primary）—— 两者都算 primary
CONSOLE_WRITER = {"kind": "console", "agent_id": "", "role": "primary"}
PRIMARY_WRITER = {"kind": "agent", "agent_id": "main-brain", "role": "primary"}
#: 子代理凭据（sub）—— 非 primary
SUB_WRITER = {"kind": "agent", "agent_id": "zly-hermes", "role": "sub"}


def _doc(status: str):
    return {"doc_id": 1, "title": "某篇文档", "status": status}


@pytest.mark.parametrize("writer", [CONSOLE_WRITER, PRIMARY_WRITER])
@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_primary能删待审与已驳回(writer, status):
    """审核面板能删待审 / 已驳回的行（删掉 = 「这条我不收」，不是偷偷放行）。"""
    assert kb_router._need_deletable(_doc(status), "1", "kb.document.delete", writer) == status


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_非primary删待审仍旧409(status):
    """🔴 子代理那侧的老纪律一个字不动：待审 / 已驳回的行不许绕过审核动作删掉。"""
    with pytest.raises(HTTPException) as ei:
        kb_router._need_deletable(_doc(status), "1", "kb.document.delete", SUB_WRITER)
    assert ei.value.status_code == 409
    assert ei.value.detail.get("error") == "kb_doc_not_effective"


def test_已生效的行谁都能过这关():
    """已生效的行：闸门放行（能不能删是**归属闸** `_writable_doc` 的事）。"""
    for writer in (CONSOLE_WRITER, PRIMARY_WRITER, SUB_WRITER):
        assert kb_router._need_deletable(_doc("effective"), "1", "kb.document.delete", writer) \
            == "effective"


def test_老库没有status列时按已生效处理():
    """老库缺 status 列 ⇒ 读出来是空串 ⇒ 按 effective 放行（缺列降级不炸）。"""
    doc = {"doc_id": 1, "title": "老库那篇"}
    assert kb_router._need_deletable(doc, "1", "kb.document.delete", SUB_WRITER) == "effective"
