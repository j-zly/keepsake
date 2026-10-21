"""未读私信提醒（**插件**侧）—— `prefetch` 注入文本末尾的 📩 提醒行。

2026-10 ks_dm_hint：提醒寄生在每轮必然发生的检索上（零定时器方案）。
本文件锁死四件事：

1. 有未读 ⇒ 注入文本**含** 📩 提醒行（条数 + 前 3 个发件人，不带正文）；
2. 没有未读 ⇒ **一行都不加**（静默是默认态）；
3. **同一水位 60s 内不重复提醒**（防刷屏）；
4. 水位变了（读完又收到 / 又来一封）⇒ **立刻**再提醒。

口径与服务端**同一份**（`Messaging.unread_summary`）—— 两处各写一条 SQL
必然漂（漂了就是「面板说 3 条、注入说 1 条」）。

全程 hermetic：`tmp_path` 里的临时 SQLite，不连 Redis / PG / 网络 / 生产库。
"""

from __future__ import annotations

import pytest

from keepsake.adapters.hermes import provider as prov_mod
from keepsake.adapters.hermes.provider import KeepsakeProvider
from keepsake.core.sqlite import SqliteStorage
from keepsake.govern.messaging import Messaging

#: 检索词（要与入库的正文对得上，命中才有「相关记忆」段落）
QUERY = "回滚 流程 校验"


@pytest.fixture(autouse=True)
def _reset_hint_cache():
    """提醒的去重缓存是**进程级**的 ⇒ 用例之间必须清干净。"""
    prov_mod._DM_HINT_CACHE.update({"ts": 0.0, "key": None})      # noqa: SLF001
    yield
    prov_mod._DM_HINT_CACHE.update({"ts": 0.0, "key": None})      # noqa: SLF001


@pytest.fixture()
def st(tmp_path):
    """真 SqliteStorage（临时文件）。"""
    storage = SqliteStorage(path=str(tmp_path / "dmhint.db"), is_primary=True)
    assert storage.ensure_index() is True
    storage.store(text=f"{QUERY} 的正式条目", tags="shared",
                  category="probe", source="probe")
    yield storage
    storage.close()


def _provider(storage, agent_id: str = "agent-b") -> KeepsakeProvider:
    """构造一个只挂了 storage 的 provider（跳过 initialize，不连任何后端）。"""
    p = object.__new__(KeepsakeProvider)
    p._config, p._storage, p._tag_filter = {}, storage, ""
    p._is_primary = True
    p._exclude_tags = ()
    p._agent_id = agent_id
    p._keepsake_messaging = Messaging(storage)
    p._skip_min_length = 2
    p._skip_patterns = set()
    p._role_cache = None
    return p


# --------------------------------------------------------------------------
# ① 有未读 ⇒ 有提醒行；没有 ⇒ 一行都不加
# --------------------------------------------------------------------------

def test_有未读时注入提醒行(st):
    """🔴 未读 > 0 ⇒ 注入文本末尾有一行 📩（条数 + 发件人，不带正文）。"""
    box = Messaging(st)
    assert box.ensure_schema() is True
    p = _provider(st)

    assert "📩" not in p.prefetch(QUERY), "没有未读就不该有提醒行"

    box.send("agent-a", "agent-b", "在吗")
    out = p.prefetch(QUERY)
    assert "📩 你有 1 条未读私信（来自: agent-a）。请调用 message_inbox 处理。" in out, out
    assert "在吗" not in out, "提醒行不该带私信正文（省 token，正文等 inbox 拉）"


def test_读完就没提醒行了(st):
    """🔴 读掉 ⇒ 未读数 0 ⇒ 静默（提醒行的意义就是「还有没读的」）。"""
    box = Messaging(st)
    assert box.ensure_schema() is True
    p = _provider(st)
    box.send("agent-a", "agent-b", "在吗")
    assert "📩" in p.prefetch(QUERY)

    box.fetch_inbox("agent-b")            # 取走即算已读
    assert "📩" not in p.prefetch(QUERY)


def test_提醒只列前三个发件人(st):
    """🔴 四个发件人 ⇒ 只列前 3 个（再多是噪音）。"""
    box = Messaging(st)
    assert box.ensure_schema() is True
    p = _provider(st)
    for who in ("agent-a", "agent-b", "agent-c", "agent-d"):
        box.send(who, "agent-b", "在吗")
    out = p.prefetch(QUERY)
    assert "你有 4 条未读私信" in out, out
    assert "agent-a" in out and "agent-d" not in out, out


# --------------------------------------------------------------------------
# ② 防刷屏：同一水位 60s 内只提醒一次；水位变了立刻再提醒
# --------------------------------------------------------------------------

def test_同一水位60s内不重复提醒(st):
    """🔴 同一未读水位在去重窗口内只提醒一次（否则每轮都刷一行）。"""
    box = Messaging(st)
    assert box.ensure_schema() is True
    p = _provider(st)
    box.send("agent-a", "agent-b", "在吗")

    assert "📩" in p.prefetch(QUERY)
    assert "📩" not in p.prefetch(QUERY), "同一水位不该重复提醒"


def test_水位变了立刻再提醒(st):
    """🔴 读掉一封又收到一封 ⇒ 条数没变也算新水位 ⇒ 立刻再提醒。"""
    box = Messaging(st)
    assert box.ensure_schema() is True
    p = _provider(st)
    box.send("agent-a", "agent-b", "在吗")
    assert "📩" in p.prefetch(QUERY)
    assert "📩" not in p.prefetch(QUERY)

    box.fetch_inbox("agent-b")            # 读完 ⇒ 水位归零
    assert "📩" not in p.prefetch(QUERY)
    box.send("agent-c", "agent-b", "在吗")  # 又来一封 ⇒ 水位回到 1
    out = p.prefetch(QUERY)
    assert "📩 你有 1 条未读私信（来自: agent-c）。请调用 message_inbox 处理。" in out, out


def test_窗口外同一水位会再提醒(st, monkeypatch):
    """🔴 60s 窗口只是**限频**，不是「一次都不许再说」—— 窗口过了仍会提醒。"""
    box = Messaging(st)
    assert box.ensure_schema() is True
    p = _provider(st)
    box.send("agent-a", "agent-b", "在吗")
    assert "📩" in p.prefetch(QUERY)
    assert "📩" not in p.prefetch(QUERY)

    # 把缓存的时刻往回拨 ⇒ 窗口已过
    prov_mod._DM_HINT_CACHE["ts"] -= 61.0                              # noqa: SLF001
    assert "📩" in p.prefetch(QUERY)


# --------------------------------------------------------------------------
# ④ 存储层口径（服务端与插件**共用**的那一份）
# --------------------------------------------------------------------------

def test_未读口径_计数与发件人(st):
    """🔴 唯一口径：计数 = `to_id=我 AND read_at IS NULL`；
    发件人 = distinct `from_id`（不取正文）。服务端与插件都走这一份。"""
    box = Messaging(st)
    assert box.ensure_schema() is True
    assert box.unread_summary("agent-b") == {"count": 0, "senders": []}

    box.send("agent-a", "agent-b", "一")
    box.send("agent-a", "agent-b", "二")
    box.send("agent-c", "agent-b", "三")
    assert box.unread_count("agent-b") == 3
    assert box.unread_summary("agent-b", sender_limit=3) == {
        "count": 3, "senders": ["agent-a", "agent-c"]}

    box.fetch_inbox("agent-b")            # 取走即算已读
    assert box.unread_summary("agent-b", sender_limit=3) == {"count": 0, "senders": []}


# --------------------------------------------------------------------------
# ③ 计数失败 ⇒ 不提醒、也不把对话带崩（fail-open）
# --------------------------------------------------------------------------

def test_信箱读失败时不提醒也不抛(st, monkeypatch):
    """🔴 提醒是附加值：信箱读炸了 ⇒ 本轮不提醒，绝不抛进对话关键路径。"""
    box = Messaging(st)
    assert box.ensure_schema() is True
    p = _provider(st)
    box.send("agent-a", "agent-b", "在吗")

    def _boom(*_a, **_kw):
        raise RuntimeError("信箱读不到")

    monkeypatch.setattr(box, "unread_summary", _boom)
    p._keepsake_messaging = box
    out = p.prefetch(QUERY)
    assert "📩" not in out, out
    assert "相关记忆" in out, out        # 检索本体照常
