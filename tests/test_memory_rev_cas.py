"""乐观并发（rev CAS）定向测试 —— 2026-10 ks_memory_rev_cas。

被测的语义（任务书 B/��� 段）：

  1. `add` → `get` 返回 `rev='1'`；
  2. `update(expected_rev='1')` 成功 → 新行 `rev='2'`、老行 `is_latest='0'`、
     新行 `supersedes` 指向老 key；
  3. **并发冲突**：两个线程同时改同一条、同 `expected_rev='1'` ⇒ **恰一个 ok、
     一个 conflict**，且 conflict 载荷带**对方最新 content/rev**
     （断言**无内容分叉、无静默覆盖**）；
  4. 改写不存在的 key ⇒ `not_found`；
  5. 删除：带错 `expected_rev` ⇒ conflict 且**行仍在**；带对 ⇒ 删成功；
     **provider / storage 层不带 ⇒ 现行为逐字不变**（**MCP 对外层不带 ⇒ 一律拒**，
     见 ⑧ —— ks_cas_hard 的 fail-closed 收紧）；
  6. 归属：caller A 不能改 caller B 名下的碎片（primary 全量）；
  7. Redis 后端改写 ⇒ 抛 `backend_unsupported`（**显式降级，不许静默**）。

本文件 **hermetic**：只用 tmp_path 里的临时 SQLite 文件 + 内存里的 provider，
不连 PG / Redis / 网络。Redis 那条只调「构造 + 抛错」路径（不建连）。
"""

from __future__ import annotations

import threading

import pytest

from keepsake.adapters.hermes.provider import KeepsakeProvider
from keepsake.core.base import BackendUnsupportedError
from keepsake.core.redis import RedisStorage
from keepsake.core.sqlite import SqliteStorage

#: 两条并发分支各自要写的内容（**故意不同** ⇒ 两条哈希不同 ⇒ 真能分叉）
RACE_TEXT_A = "并发改写测试 甲代理 改写后的正文"
RACE_TEXT_B = "并发改写测试 乙代理 改写后的正文"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _make_provider(path: str, *, agent_id: str = "", is_primary: bool = True
                   ) -> KeepsakeProvider:
    """provider + 真 SqliteStorage（tmp 文件库，不连任何外部服务）。"""
    storage = SqliteStorage(path=path, agent_id=agent_id,
                            is_primary=is_primary, final_limit=10)
    assert storage.ensure_index() is True
    provider = KeepsakeProvider()
    provider._storage = storage
    return provider


@pytest.fixture()
def provider(tmp_path):
    """单份存储的 provider（多数用例用这个）。"""
    p = _make_provider(str(tmp_path / "cas.db"))
    yield p
    p._storage.close()


@pytest.fixture()
def db_path(tmp_path) -> str:
    return str(tmp_path / "cas.db")


# ---------------------------------------------------------------------------
# ① add → get 带 rev
# ---------------------------------------------------------------------------

def test_写入后读到rev为1(provider):
    """新写的条目 `rev='1'`（存量行 ALTER 补列后也是 '1' ⇒ 迁移零感知）。"""
    key = provider.add("第一条记忆 内容甲")
    frag = provider.get(key)
    assert frag is not None
    assert frag["rev"] == "1", frag


def test_批量读也带rev(provider):
    """`get_fragments_batch` 同样带 rev（代理常走批量读拿版本号）。"""
    key = provider.add("批量读用的记忆 内容乙")
    docs = provider._storage.get_fragments_batch([key])
    assert docs[key]["rev"] == "1", docs


# ---------------------------------------------------------------------------
# ② CAS 改写成功：新版本 rev+1、老行转链身、supersedes 指向正确
# ---------------------------------------------------------------------------

def test_改写成功_新行rev加一_老行转链身(provider):
    key = provider.add("原始内容 版本一")
    rev = provider.get(key)["rev"]
    assert rev == "1"

    out = provider.update(key, "改写后的内容 版本二", rev)
    assert out.get("ok") is True, out
    new_key = out["key"]
    assert new_key and new_key != key, "改写换 key（key = 内容哈希）"
    assert out["rev"] == "2"

    new_frag = provider.get(new_key)
    assert new_frag["content"] == "改写后的内容 版本二"
    assert new_frag["rev"] == "2"
    assert new_frag["is_latest"] == "1"
    # 稀疏语义：空值不进 dict ⇒ 链头的 superseded_by 取不到 = 没有后继
    assert new_frag.get("superseded_by", "") == "", "链头不该有后继"

    old_frag = provider.get(key)
    assert old_frag["is_latest"] == "0", "老行必须转链身（否则检索会同时召回两版）"
    assert old_frag["superseded_by"] == new_key
    assert old_frag["superseded_at"], "封边要留时间戳"
    assert new_frag["supersedes"] and key in new_frag["supersedes"], \
        f"新行的 supersedes 必须指向老 key：{new_frag.get('supersedes')}"


def test_改写不带版本号时也能改_但链仍完整(provider):
    """`expected_rev` 空 = 不做版本校验（老调用方兼容路径）；链维护照样做。"""
    key = provider.add("不带版本号的改写 原文")
    out = provider.update(key, "不带版本号的改写 新文", "")
    assert out.get("ok") is True, out
    assert provider.get(key)["is_latest"] == "0"
    assert provider.get(out["key"])["is_latest"] == "1"


# ---------------------------------------------------------------------------
# ③ 并发冲突：恰一个 ok、一个 conflict；载荷带对方最新内容；**无内容分叉**
# ---------------------------------------------------------------------------

def test_并发改写同一key_恰一个成功一个冲突(db_path):
    """两个线程基于**同一版**各改各的 ⇒ 恰一个成功、一个冲突。

    🔴 断言的重点不是「谁赢」，而是：
      * **没有内容分叉**（输的那一版**根本没落库**）；
      * 输的一方拿到的 conflict 载荷里是**赢的那一版的最新内容 + rev**
        （据此就能合并重提，不必再盲改一次）。
    """
    seed = _make_provider(db_path)
    original = "并发改写测试 原始正文 版本一"
    key = seed.add(original)
    rev = seed.get(key)["rev"]
    seed._storage.close()

    barrier = threading.Barrier(2)
    results: dict = {}
    texts = {"A": RACE_TEXT_A, "B": RACE_TEXT_B}
    errors: dict = {}

    def worker(tag: str) -> None:
        p = _make_provider(db_path)
        try:
            barrier.wait(timeout=20)          # 尽量让两边同时起跑
            results[tag] = p.update(key, texts[tag], rev)
        except Exception as e:      # noqa: BLE001 —— 失败也要带出去，别吞
            errors[tag] = repr(e)
        finally:
            p._storage.close()

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"并发分支抛异常：{errors}"
    assert len(results) == 2, results
    winners = [tag for tag, r in results.items() if r.get("ok")]
    losers = [tag for tag, r in results.items() if r.get("error") == "conflict"]
    assert len(winners) == 1, f"必须恰一个成功：{results}"
    assert len(losers) == 1, f"必须恰一个冲突：{results}"

    win, lose = winners[0], losers[0]
    win_out, lose_out = results[win], results[lose]

    # 冲突载荷 = **赢的那一版**的最新内容 + rev
    assert lose_out["current_rev"] == win_out["rev"] == "2", lose_out
    assert lose_out["current_content"] == texts[win], lose_out
    assert lose_out["current_key"] == win_out["key"], lose_out

    # 🔴 **无内容分叉**：输的那一版**根本没落库**
    check = _make_provider(db_path)
    try:
        loser_key = check._storage._fragment_key_for(texts[lose])
        assert not check._storage.get_fragments_batch([loser_key]), \
            "输的那一版不该落库（落了就叫内容分叉）"
        # 链上只有两行：原版 + 赢的那一版
        assert check.get(key)["is_latest"] == "0"
        assert check.get(key)["superseded_by"] == win_out["key"]
        head = check.get(win_out["key"])
        assert head["rev"] == "2" and head["is_latest"] == "1"
    finally:
        check._storage.close()


def test_冲突后再读能拿到最新版本(provider):
    """冲突 → 重新读 → 带上新 rev 再提交 ⇒ 这次成功（代理的标准重试路径）。"""
    key = provider.add("重试路径 原文")
    rev = provider.get(key)["rev"]
    first = provider.update(key, "重试路径 第一次改写", rev)
    assert first.get("ok") is True

    second = provider.update(key, "重试路径 第二次改写", rev)   # 旧 rev ⇒ 冲突
    assert second.get("error") == "conflict", second
    latest_key = second["current_key"]
    latest_rev = second["current_rev"]

    third = provider.update(latest_key, "重试路径 合并后改写", latest_rev)
    assert third.get("ok") is True, third
    assert provider.get(third["key"])["content"] == "重试路径 合并后改写"


# ---------------------------------------------------------------------------
# ④ 不存在的 key
# ---------------------------------------------------------------------------

def test_改写不存在的key返回not_found(provider):
    out = provider.update("memory:frag:根本不存在", "随便写点什么", "1")
    assert out.get("error") == "not_found", out


# ---------------------------------------------------------------------------
# ⑤ 删除：CAS / 不 CAS
# ---------------------------------------------------------------------------

def test_删除带错版本号_冲突且行仍在(provider):
    key = provider.add("删除测试 条目甲")
    out = provider.remove(key, expected_rev="999")
    assert out.get("error") == "conflict", out
    assert out["current_rev"] == "1"
    assert provider.get(key) is not None, "版本对不上**不许删**"


def test_删除带对版本号_删除成功(provider):
    key = provider.add("删除测试 条目乙")
    out = provider.remove(key, expected_rev="1")
    assert out.get("ok") is True, out
    assert provider.get(key) is None


def test_删除不带版本号_保持现行为(provider):
    """**不带 expected_rev ⇒ 逐字保持现行为**（向后兼容已接入的老代理）。"""
    key = provider.add("删除测试 条目丙")
    assert provider.remove(key) is True
    assert provider.get(key) is None
    assert provider.remove("memory:frag:不存在") is False


# ---------------------------------------------------------------------------
# ⑥ 归属：非 owner 不能改别人的碎片；primary 全量
# ---------------------------------------------------------------------------

def test_非归属代理不能改别人的碎片(db_path):
    """caller A 写的碎片，caller B 改不了（primary 除外）。"""
    p = _make_provider(db_path)
    try:
        key = p.add("归属测试 A代理写的记忆", caller_agent_id="agent-a")
        rev = p.get(key)["rev"]
        assert "agent:agent-a" in p.get(key)["tags"]

        # B 改 A 的 ⇒ 拒（不是「改不动」，是「不归我管」）
        out = p.update(key, "B 试图改写 A 的记忆", rev,
                       caller_agent_id="agent-b", caller_role="sub")
        assert out.get("error") == "not_owner", out

        # A 自己改 ⇒ 成功
        out = p.update(key, "A 改写自己的记忆", rev,
                       caller_agent_id="agent-a", caller_role="sub")
        assert out.get("ok") is True, out

        # primary 全量（= 主脑现状逐字不变）
        key2 = p.add("归属测试 第二条", caller_agent_id="agent-a")
        out = p.update(key2, "primary 改写别人的记忆",
                       p.get(key2)["rev"],
                       caller_agent_id="agent-primary", caller_role="primary")
        assert out.get("ok") is True, out
    finally:
        p._storage.close()


# ---------------------------------------------------------------------------
# ⑦ Redis 后端：显式不支持（**禁止静默降级**）
# ---------------------------------------------------------------------------

def test_redis后端改写抛backend_unsupported():
    """Redis 不做真 CAS ⇒ **明确抛错**，绝不静默忽略 expected_rev。

    🔴 静默降级（当成无条件覆盖）比报错坏得多：调用方会拿到「成功」回执，
    而并发写照样分叉 —— 报错时代理至少知道要重试。
    """
    storage = RedisStorage()
    with pytest.raises(RuntimeError) as excinfo:
        storage.cas_update("memory:frag:任意", {"content": "改写"}, "1")
    assert "backend_unsupported" in str(excinfo.value)

    with pytest.raises(RuntimeError) as excinfo:
        storage.cas_remove("memory:frag:任意", "1")
    assert "backend_unsupported" in str(excinfo.value)


def test_redis后端在provider层同样抛错():
    """provider 走 Redis 时**原样上抛**（不被吞成假成功/假冲突）。"""
    p = KeepsakeProvider()
    p._storage = RedisStorage()
    with pytest.raises(BackendUnsupportedError) as excinfo:
        p.update("memory:frag:任意", "改写内容", "1")
    assert "backend_unsupported" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ⑧ MCP 工具面：memory_update / memory_remove 的 expected_rev
# ---------------------------------------------------------------------------

def test_mcp_memory_update返回结构化冲突(db_path):
    """MCP 层：冲突**不是异常** ⇒ 结构化回给代理（带对方最新内容）。"""
    from keepsake.mcp_server.tools import memory_update

    p = _make_provider(db_path)
    try:
        key = p.add("MCP 测试 原文")
        rev = p.get(key)["rev"]
        out = memory_update(p, key, "MCP 测试 改写", rev)
        assert out.get("ok") is True, out

        conflict = memory_update(p, key, "MCP 测试 再改一次", rev)
        assert conflict.get("error") == "conflict", conflict
        assert conflict["current_content"] == "MCP 测试 改写"
        assert conflict["current_rev"] == "2"
    finally:
        p._storage.close()


def test_mcp_memory_remove带expected_rev(db_path):
    """MCP 层删除：版本对不上 ⇒ **不删**并回冲突；**不带版本号 ⇒ 一律拒**。

    🔴 2026-10 ks_cas_hard：MCP 对外 **fail-closed** —— 空串不再兼容成
    「无条件删除」。**provider / storage 内部**的空串路径仍合法（上面两条
    provider 层用例就是）。
    """
    from keepsake.mcp_server.tools import ToolError, memory_remove

    p = _make_provider(db_path)
    try:
        key = p.add("MCP 删除测试")
        conflict = memory_remove(p, key, True, expected_rev="999")
        assert conflict.get("error") == "conflict", conflict
        assert p.get(key) is not None, "版本对不上不许删"

        removed = memory_remove(p, key, True, expected_rev="1")
        assert removed.get("removed") is True, removed
        assert p.get(key) is None

        key2 = p.add("MCP 删除测试 不带版本号")
        with pytest.raises(ToolError) as exc:
            memory_remove(p, key2, True)
        assert "memory_get" in str(exc.value), str(exc.value)
        assert p.get(key2) is not None, "不带版本号不许删"
    finally:
        p._storage.close()


# ---------------------------------------------------------------------------
# ⑨ 接口一致性：三个后端都声明了 CAS 方法（签名逐字对齐）
# ---------------------------------------------------------------------------

def test_三后端都声明cas方法且签名一致():
    """PG / SQLite 真实现；Redis 显式抛「不支持」—— 但**方法必须在**。

    🔴 漏实现的后端会让调用方拿到 AttributeError（而不是一句人话）⇒
    这里钉住「方法存在 + 签名一致」。
    """
    import inspect

    from keepsake.core.pg import PgStorage

    for cls in (PgStorage, SqliteStorage, RedisStorage):
        for name in ("cas_update", "cas_remove"):
            fn = getattr(cls, name, None)
            assert callable(fn), f"{cls.__name__}.{name} 缺失"
            params = list(inspect.signature(fn).parameters.values())[1:]
            assert params[0].name == "key", (cls.__name__, name)
            assert params[-1].name == "expected_rev", (cls.__name__, name)
