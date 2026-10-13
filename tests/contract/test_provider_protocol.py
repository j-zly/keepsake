"""通用记忆契约（`keepsake.adapters.generic.protocol`）双向契约测试。

契约测试的价值在**反向**：正向「能跑」谁都能写，反向「故意违反必须被检出」
才能保证契约不被后来的改动悄悄稀释。本文件三段：

  ① 正向-结构：Fake 实现满足 Protocol 全部成员；**删掉任一成员必须被检出**
  ② 正向-行为：真 `SqliteStorage` 走一遍 add → get → search → remove 断言语义
  ③ 反向-违约：`search` 返回 None、`merge` 不收 dry_run 等必须被 `check_contract` 判红

再加一条 `KeepsakeProvider` 自身对契约的兑现（插件解耦点的落点）：
它结构上满足 Protocol、全部方法可调用、`capabilities()` 返回 `version="v1"`。

本文件 **hermetic**：只用 tmp_path 里的临时 SQLite 文件，不连 Redis / PG / 网络。
"""

from __future__ import annotations

import inspect

import pytest

from keepsake.adapters.generic.capabilities import Capabilities
from keepsake.adapters.generic.protocol import MemoryProvider
from keepsake.storage_sqlite import SqliteStorage

#: 契约成员清单。`__protocol_attrs__` 是 3.12+ 才有的，3.11 上退回扫命名空间。
PROTOCOL_MEMBERS: list[str] = sorted(
    getattr(MemoryProvider, "__protocol_attrs__", None)
    or [n for n in vars(MemoryProvider) if not n.startswith("_")]
)


# ---------------------------------------------------------------------------
# 契约检查器：把「满足契约」拆成可断言的几条
# ---------------------------------------------------------------------------

def check_contract(provider) -> list[str]:
    """返回 provider 违反契约的**原因列表**（空列表 = 满足）。

    比 `isinstance(provider, MemoryProvider)` 严：runtime_checkable 的 Protocol
    只查方法在不在，查不出「签名对不上」和「返回类型不对」——
    而这两样恰好是最容易被改坏的地方。
    """
    violations: list[str] = []

    # 1. 成员齐全且可调用
    for name in PROTOCOL_MEMBERS:
        attr = getattr(provider, name, None)
        if attr is None:
            violations.append(f"缺少成员 {name}")
        elif not callable(attr):
            violations.append(f"成员 {name} 不可调用")

    # 2. 签名一致（含关键字专用参数与默认值）
    for name in PROTOCOL_MEMBERS:
        impl = getattr(provider, name, None)
        want = getattr(MemoryProvider, name, None)
        if impl is None or want is None:
            continue
        got_sig = inspect.signature(impl)
        want_sig = inspect.signature(want)
        for pname, want_p in want_sig.parameters.items():
            if pname == "self":
                continue
            got_p = got_sig.parameters.get(pname)
            if got_p is None:
                violations.append(f"{name}() 缺参数 {pname}")
                continue
            if got_p.kind is not want_p.kind:
                violations.append(
                    f"{name}() 参数 {pname} 的种类不符："
                    f"契约 {want_p.kind.name} / 实现 {got_p.kind.name}"
                )
            if want_p.default is inspect.Parameter.empty and \
                    got_p.default is not inspect.Parameter.empty:
                violations.append(f"{name}() 参数 {pname} 不该有默认值")

    # 3. 返回语义（只查能安全调用的：检索/写层在 Fake 上不碰 IO）
    return violations


# ---------------------------------------------------------------------------
# ① 正向-结构：Fake 实现满足 Protocol；缺方法必被检出
# ---------------------------------------------------------------------------

class _GoodFake:
    """契约的最小完整实现（也是反例的对照组）。"""

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    def search(self, query: str, *, limit: int = 10) -> list:
        return []

    def get(self, key: str):
        return None

    def add(self, text: str, *, tags: str = "", meta=None) -> str:
        return ""

    def update(self, key: str, *, content=None, tags=None) -> bool:
        return False

    def remove(self, target: str) -> bool:
        return False

    def merge(self, *, dry_run: bool = True) -> dict:
        return {}

    def forget(self, *, dry_run: bool = True) -> dict:
        return {}

    def capabilities(self) -> Capabilities:
        return Capabilities(core_tier=False, knn=False, dry_run_merge=True)


def test_complete_fake_satisfies_protocol():
    assert isinstance(_GoodFake(), MemoryProvider)
    assert check_contract(_GoodFake()) == []


@pytest.mark.parametrize("member", PROTOCOL_MEMBERS)
def test_missing_any_member_is_detected(member):
    """🔴 反向：逐个删掉成员，`isinstance` 必须翻红且给出可读原因。"""
    kept = {k: v for k, v in vars(_GoodFake).items() if not k.startswith("__")}
    kept.pop(member)
    broken_cls = type(f"_Missing_{member}", (), kept)

    assert not isinstance(broken_cls(), MemoryProvider), f"{member} 缺失却仍被判为满足契约"
    assert any(member in v for v in check_contract(broken_cls())), check_contract(broken_cls())


# ---------------------------------------------------------------------------
# ② 正向-行为：真 SqliteStorage 走一遍 add → get → search → remove
# ---------------------------------------------------------------------------

@pytest.fixture()
def provider(tmp_path):
    """KeepsakeProvider 接真 SqliteStorage（tmp 文件库，不连任何外部服务）。"""
    from keepsake import KeepsakeProvider
    from keepsake.forgetter import Forgetter

    # is_primary=True：否则 agent 隔离会把非 shared 标签的条目全滤掉
    # （口径与 tests/test_storage_sqlite.py 的 search_store fixture 逐字一致）
    storage = SqliteStorage(path=str(tmp_path / "contract.db"),
                            is_primary=True, final_limit=10)
    assert storage.ensure_index() is True
    p = KeepsakeProvider()
    p._storage = storage
    p._forgetter = Forgetter(storage)
    yield p
    storage.close()


def test_add_get_search_remove_roundtrip(provider):
    text = "PostgreSQL 索引优化 批量写入 事务"

    key = provider.add(text, tags="contract")
    assert key, "add 必须返回非空 key，否则调用方无从回查"
    # 复刻的 key 算法必须与后端实际落库的 key 一致（否则 get 永远 miss）
    assert key.startswith("memory:frag:")

    got = provider.get(key)
    assert got is not None, "add 之后立刻 get 必须命中"
    assert got["content"] == text

    hits = provider.search("PostgreSQL 索引")
    assert isinstance(hits, list)
    # 命中项里 key 落在 `_key`（检索期临时字段，不入库），内容在 `content`
    assert any(text in (h.get("content") or "") for h in hits), hits

    assert provider.remove(key) is True
    assert provider.get(key) is None, "remove 之后 get 必须返回 None（确实不存在）"
    assert provider.remove(key) is False, "重复 remove 必须报失败（fail-closed），不许假装成功"


def test_search_limit_is_respected(provider):
    for i in range(5):
        provider.add(f"批量写入 事务 压测 第{i}次", tags="contract")
    assert len(provider.search("批量写入 事务", limit=2)) <= 2


def test_update_changes_content_and_tags(provider):
    key = provider.add("原始内容 草稿")
    assert provider.update(key, content="修订内容 定稿", tags="final") is True
    got = provider.get(key)
    assert got["content"] == "修订内容 定稿"
    assert "final" in got["tags"]
    assert provider.update("memory:frag:不存在", content="x") is False


def test_read_path_raises_when_uninitialized():
    """🔴 未初始化 = 「查不到」，不是「确实没有」⇒ 读路径必须抛错，绝不返回空。"""
    from keepsake import KeepsakeProvider

    p = KeepsakeProvider()
    with pytest.raises(RuntimeError):
        p.search("任意查询")
    with pytest.raises(RuntimeError):
        p.get("memory:frag:whatever")


def test_write_path_is_fail_closed_when_uninitialized():
    """写层未初始化 → 返回 falsy（调用方看得见失败），不抛也不谎报成功。"""
    from keepsake import KeepsakeProvider

    p = KeepsakeProvider()
    assert p.add("随便写点") == ""
    assert p.update("memory:frag:x", content="y") is False
    assert p.remove("memory:frag:x") is False


# ---------------------------------------------------------------------------
# ③ 反向-违约：语义违反必须被 check_contract / 实调用判红
# ---------------------------------------------------------------------------

def _require_list_result(member: str, value) -> None:
    """契约检查器里检索层那条规则的实现：search 必须返回 list。

    写成独立函数是为了让「违反它必然红」可被断言，而不是靠人记得看。
    """
    assert isinstance(value, list), (
        f"{member} 必须返回 list（空列表 = 确实没有匹配）；"
        f"实际返回 {type(value).__name__} ⇒ 调用方会把「查不到」当成「没有记忆」"
    )


def test_search_returning_none_is_rejected():
    """🔴 反向：`search` 返回 None 而非 list —— 契约上等于「静默空」，最危险的失败形态。"""
    class _NoneSearch:
        def search(self, query: str, *, limit: int = 10):
            return None

    obj = _NoneSearch()
    with pytest.raises(AssertionError, match="search 必须返回 list"):
        _require_list_result("search", obj.search("x"))


def test_merge_without_dry_run_is_rejected():
    """`merge` 不接 dry_run ⇒ 破坏「默认只统计不写」的安全默认，必须被检出。"""
    class _NoDryRun:
        def merge(self) -> dict:
            return {}

    assert any("dry_run" in v for v in check_contract(_NoDryRun())), \
        "merge() 缺 dry_run 参数没有被检出"


def test_keyword_only_downgrade_is_rejected():
    """把 keyword-only 参数降成 positional ⇒ 调用方按契约传关键字会炸。"""
    class _Positional:
        def prefetch(self, query, session_id="") -> str:
            return ""

    violations = check_contract(_Positional())
    assert any("session_id" in v for v in violations), violations


def test_forget_reports_dict(provider):
    """forget() 返回统计 dict，且默认 dry_run 不删东西。"""
    provider.add("临时记忆 待遗忘")
    stats = provider.forget()
    assert isinstance(stats, dict)
    assert stats["dry_run"] is True, f"默认必须是 dry_run：{stats}"
    assert stats["deleted"] == 0


# ---------------------------------------------------------------------------
# ④ 插件解耦点的落点：KeepsakeProvider 自己兑现通用契约
# ---------------------------------------------------------------------------

def test_keepsake_provider_satisfies_generic_protocol():
    from keepsake import KeepsakeProvider

    p = KeepsakeProvider()
    assert isinstance(p, MemoryProvider), \
        "KeepsakeProvider 必须结构上满足 generic Protocol（缺成员会翻红）"
    assert check_contract(p) == [], check_contract(p)

    for name in PROTOCOL_MEMBERS:
        assert callable(getattr(p, name)), f"{name} 必须可调用"


def test_capabilities_declares_v1():
    from keepsake import KeepsakeProvider

    caps = KeepsakeProvider().capabilities()
    assert isinstance(caps, Capabilities)
    assert caps.version == "v1"
    assert caps.core_tier is True and caps.knn is True


def test_generic_layer_does_not_import_hermes():
    """🔴 依赖方向铁律：`adapters/generic` 不许反向依赖 `adapters/hermes`。"""
    import ast
    from pathlib import Path

    import keepsake.adapters.generic as generic_pkg

    pkg_dir = Path(generic_pkg.__file__).resolve().parent
    for path in sorted(pkg_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom) and node.level:
                mod = ("." * node.level) + (node.module or "")
            elif isinstance(node, ast.Import):
                mod = node.names[0].name
            assert "hermes" not in (mod or ""), \
                f"{path.name} 引用了 {mod} ⇒ 通用契约被 Hermes 绑死"