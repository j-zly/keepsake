"""**orphan 第四档**定向测试 —— 2026-11 ks_err_tiers。

被测语义（任务书「①错误文案分档：orphan 第四档」）：

  条目 key = 内容哈希；`update` = 新建版本行 + 旧行转链身（`is_latest='0'` +
  `superseded_by`）。写/删路径经 `_resolve_chain_head` 回溯**当前链头** ——
  **若目标行的链头后来被删除，回溯断链 ⇒ 返回 None**。

  现状（修前）：PG 与 SQLite 的 `cas_update` / `cas_remove` 都把 `head is None`
  塌成 `("not_found", None)` ⇒ 客户端对「历史版本 + 链头已删」收到「条目不存在」
  ⇒ **误导**（这条明明还在，`memory_get` 还取得到）。

  修后：断链单独成档 **`orphan`** —— 行还在，但演化链断了 ⇒ 不可改写 / 不可删除。

本文件 **hermetic**：只用 tmp_path 里的临时 SQLite 文件 + 内存里的 provider，
不连 PG / Redis / 网络。PG 侧同逻辑用**静态源码断言**覆盖（不建 PG 连接）。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from keepsake.adapters.hermes.provider import KeepsakeProvider
from keepsake.core.sqlite import SqliteStorage

#: 三条正文（**故意不同** ⇒ 哈希不同 ⇒ 能建出演化链）
BODY_A = "孤儿档测试 原始正文 甲"
BODY_B = "孤儿档测试 改写后的正文 乙"


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
    """单份存储的 provider。"""
    p = _make_provider(str(tmp_path / "orphan.db"))
    yield p
    p._storage.close()


def _make_orphan_chain(p: KeepsakeProvider) -> tuple:
    """造出 orphan 形态：**A 成链身 → 删掉链头 B**。

    返回 `(key_a, key_b)`。此时 A 行**还在**（`get` 仍取得到），
    但它的 `superseded_by` 指向的 B **已被删** ⇒ 演化链断裂。
    """
    key_a = p.add(BODY_A)
    assert p.get(key_a)["rev"] == "1"
    out = p.update(key_a, BODY_B, "1")
    assert out.get("ok") is True, out
    key_b = out["key"]
    # A 已转链身（is_latest='0'，superseded_by → B）
    frag_a = p.get(key_a)
    assert frag_a["is_latest"] == "0", frag_a
    assert frag_a["superseded_by"] == key_b, frag_a
    # 删掉链头 B ⇒ A 的链断了（**行还在**）
    removed = p.remove(key_b, expected_rev="2")
    assert removed.get("ok") is True, removed
    assert p.get(key_b) is None
    assert p.get(key_a) is not None, "orphan 行**还在**（这正是与 not_found 的分别）"
    return key_a, key_b


# ---------------------------------------------------------------------------
# ① 存储层：断链 ⇒ orphan（**不是** not_found）
# ---------------------------------------------------------------------------

def test_存储层_cas_update断链返回orphan(provider):
    """链身 + 链头已删 ⇒ `cas_update` 回 `("orphan", None)`。"""
    key_a, _ = _make_orphan_chain(provider)
    status, payload = provider._storage.cas_update(
        key_a, {"content": "试图改写孤儿行"}, "1")
    assert status == "orphan", (status, payload)
    assert payload is None, "orphan 载荷**恒为 None**（不回显任何正文）"


def test_存储层_cas_remove断链返回orphan(provider):
    """链身 + 链头已删 ⇒ `cas_remove` 回 `("orphan", None)`。"""
    key_a, _ = _make_orphan_chain(provider)
    status, payload = provider._storage.cas_remove(key_a, "1")
    assert status == "orphan", (status, payload)
    assert payload is None, "orphan 载荷**恒为 None**（不回显任何正文）"


# ---------------------------------------------------------------------------
# ② provider 层：orphan 单独成档
# ---------------------------------------------------------------------------

def test_provider_update对孤儿回orphan(provider):
    """provider.update 对 orphan 行回 `{"error": "orphan"}`（**不是** not_found）。"""
    key_a, _ = _make_orphan_chain(provider)
    out = provider.update(key_a, "试图改写孤儿行", "1")
    assert out.get("error") == "orphan", out
    assert "not_found" not in out.get("error", ""), out


def test_provider_remove对孤儿回orphan(provider):
    """provider.remove 对 orphan 行回 `{"error": "orphan"}`（**不是** not_found）。"""
    key_a, _ = _make_orphan_chain(provider)
    out = provider.remove(key_a, expected_rev="1")
    assert out.get("error") == "orphan", out


# ---------------------------------------------------------------------------
# ③ MCP 工具面：orphan 的 ToolError 文案
# ---------------------------------------------------------------------------

def test_mcp_memory_update对孤儿报错(provider):
    """MCP `memory_update` 对 orphan ⇒ ToolError，文案点明「历史版本 + 链头已删」。"""
    from keepsake.mcp_server.tools import ToolError, memory_update

    key_a, _ = _make_orphan_chain(provider)
    with pytest.raises(ToolError) as exc:
        memory_update(provider, key_a, "试图改写孤儿行", "1")
    msg = str(exc.value)
    assert "历史版本" in msg, msg
    assert "链头已被删除" in msg, msg
    # not_found 档的文案是「记忆条目不存在：{key}」—— orphan **不是**它
    assert "记忆条目不存在" not in msg, f"orphan **不是**「不存在」：{msg}"


def test_mcp_memory_remove对孤儿报错(provider):
    """MCP `memory_remove` 对 orphan ⇒ ToolError（**不是**「条目不存在」）。

    🔴 存在性预探测（`provider.get(key)`）对 orphan 行**过得去**（行还在）
       ⇒ 正常落到 CAS 档 ⇒ 这里才分出 orphan。
    """
    from keepsake.mcp_server.tools import ToolError, memory_remove

    key_a, _ = _make_orphan_chain(provider)
    with pytest.raises(ToolError) as exc:
        memory_remove(provider, key_a, True, expected_rev="1")
    msg = str(exc.value)
    assert "历史版本" in msg, msg
    assert "链头已被删除" in msg, msg
    # not_found 档的文案是「条目不存在：{key}」；兜底档是「删除失败（…）」
    # —— orphan **都不是**它们
    assert "条目不存在" not in msg, f"orphan **不是**「不存在」：{msg}"
    assert "删除失败" not in msg, f"orphan 不该落进兜底档：{msg}"
    # 行**仍在**（orphan 只是「不可操作」，不是「已消失」）
    assert provider.get(key_a) is not None


# ---------------------------------------------------------------------------
# ④ 逐字回归：另两档**不受影响**
# ---------------------------------------------------------------------------

def test_从未存在的key删除仍回条目不存在(provider):
    """**逐字回归**：从未存在过的 key ⇒ 仍是「条目不存在：{key}」。"""
    from keepsake.mcp_server.tools import ToolError, memory_remove

    missing = "memory:frag:从来就不存在"
    with pytest.raises(ToolError) as exc:
        memory_remove(provider, missing, True, expected_rev="1")
    assert str(exc.value) == f"条目不存在：{missing}", str(exc.value)


def test_活链头错版本号仍回conflict(provider):
    """**逐字回归**：活着的链头 + 错 rev ⇒ 仍是 conflict（结构化回，不抛错）。"""
    from keepsake.mcp_server.tools import memory_remove, memory_update

    key = provider.add("活链头冲突回归 正文")
    out = memory_update(provider, key, "活链头冲突回归 改写", "999")
    assert out.get("error") == "conflict", out
    assert out.get("current_rev") == "1", out

    removed = memory_remove(provider, key, True, expected_rev="999")
    assert removed.get("error") == "conflict", removed
    assert provider.get(key) is not None, "版本对不上**不许删**"


# ---------------------------------------------------------------------------
# ⑤ PG 侧：静态源码断言（**不建 PG 连接** —— 本仓测试基建不连生产 PG）
# ---------------------------------------------------------------------------

def test_pg侧两处断链分支都回orphan():
    """PG 侧同款两分支（`cas_update` / `cas_remove`）源码里都回 `("orphan", None)`。

    🔴 用**静态断言**而非真连 PG：① 任务书允许（测试基建不便连 PG）；
       ② 更重要的是**它钉住的是「源码里写的是什么」** —— 真连 PG 的用例
       在没 PG 的机器上会被 skip，钉不住任何东西。
    """
    src = Path(__file__).resolve().parents[1] / "src" / "keepsake" / "core" / "pg.py"
    text = src.read_text(encoding="utf-8")

    # 两处 `head is None` 分支：都要回 orphan，**不许**回 not_found
    branches = text.count("head is None:")
    assert branches >= 2, f"pg.py 里的断链分支数不对：{branches}"

    for name in ("cas_update", "cas_remove"):
        fn = getattr(
            __import__("keepsake.core.pg", fromlist=["PgStorage"]).PgStorage, name)
        fn_src = inspect.getsource(fn)
        assert '("orphan", None)' in fn_src, f"pg.{name} 的断链分支没有回 orphan"
        # 断链分支**不许**再回 not_found
        orphan_idx = fn_src.index('("orphan", None)')
        assert '("not_found", None)' in fn_src[:orphan_idx], \
            f"pg.{name} 里 not_found 的行数分支应仍在（行压根不在）"


# ---------------------------------------------------------------------------
# ⑥ 三后端状态枚举对齐（provider 层不漏）
# ---------------------------------------------------------------------------

def test_三后端cas方法签名与状态枚举一致():
    """PG / SQLite 真实现、Redis 显式抛「不支持」—— 方法与签名必须齐。"""
    from keepsake.core.pg import PgStorage
    from keepsake.core.redis import RedisStorage

    for cls in (PgStorage, SqliteStorage, RedisStorage):
        for name in ("cas_update", "cas_remove"):
            fn = getattr(cls, name, None)
            assert callable(fn), f"{cls.__name__}.{name} 缺失"
            params = list(inspect.signature(fn).parameters.values())[1:]
            assert params[0].name == "key", (cls.__name__, name)
            assert params[-1].name == "expected_rev", (cls.__name__, name)

    for cls in (PgStorage, SqliteStorage):
        for name in ("cas_update", "cas_remove"):
            fn_src = inspect.getsource(getattr(cls, name))
            assert '("orphan", None)' in fn_src, f"{cls.__name__}.{name} 缺 orphan 档"
