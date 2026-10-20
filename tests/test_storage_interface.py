"""存储接口一致性测试 —— RedisStorage / PgStorage 共用 StorageBase。

覆盖：
  1. 两个后端都实现了接口的**全部**方法（缺一个就能检出）
  2. 两个后端的方法签名与接口逐参数对齐（名字/默认参数/顺序漂移都能检出）
  3. 探针：故意从后端删掉一个方法 → 检出逻辑必须报 missing（证明不是空转）
  4. 检索三方法在 PG 侧必须抛 NotImplementedError（绝不静默空返回）

零网络零数据库：全部纯本地静态/反射检查。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from keepsake.storage import RedisStorage
from keepsake.storage_base import StorageBase
from keepsake.storage_pg import PgStorage
from keepsake.storage_sqlite import SqliteStorage

# 接口要求的方法全集（= 任务书列出的「实际被调用点」+ health_check）
REQUIRED_METHODS = (
    "health_check",
    "ensure_index",
    "close",
    "store",
    "supersede_fragment",
    "correct_fragments",
    "record_feedback",
    "get_fragment",
    "get_fragments_batch",
    # 2026-10 ks_pcli：`_get_client()` 依赖链后端无关化的三个落点
    "fragment_exists",
    "touch_fragment",
    "set_supersedes",
    "search",
    "search_bm25",
    "search_knn",
    # 2026-10 ks_pmn：合并/遗忘的维护原语（分页扫描 / 批量写 / 局部更新 / 批量删）
    "scan_fragment_keys",
    "write_fragments_batch",
    "update_fragment_fields",
    "delete_fragments_batch",
    "match_attention",
    "match_hot_topics",
    "get_hot_topics",
    "entity_timeline",
    "discover_synonyms",
    "generate_jieba_dict",
)

BACKENDS = {"RedisStorage": RedisStorage, "PgStorage": PgStorage,
            "SqliteStorage": SqliteStorage}   # 2026-10 ks_sqlite_p1：第三后端纳入同一套接口检查


def _missing_methods(cls) -> list:
    """该类缺哪些接口方法。"""
    return [m for m in REQUIRED_METHODS if not callable(getattr(cls, m, None))]


def _signature_mismatches(cls) -> list:
    """该类的方法签名与接口声明不一致的项。

    契约规则（2026-10 keepsake_reconcile_v2 起恢复严格）：
      - 接口声明的**每个**参数都得在实现里出现，且名字 + 默认值一致；
      - 实现里**任何**接口外的额外参数都算漂移 —— 无论它是带默认值的可选入参
        还是别的。契约 v2 引入的 `applicability` / `reason` 已在 base.py 补齐，
        当初那条「三后端共同 allowlist」的过渡口径（让三后端签名比接口多出
        这几个可选入参而不报漂移）随之**删除**：接口已经把这几个入参收进去，
        再留一条「多几个也算合法」的口子，下一个单子里多出来的参数就会
        顺着这条口子滑进去 —— 而漂移的表现不是报错，是「只有一个后端认这个
        参数」，排查成本极高（2026-06 DriftedPg.store 就是这么溜进去的）。
    """
    bad = []
    for name in REQUIRED_METHODS:
        impl = getattr(cls, name, None)
        declared = getattr(StorageBase, name, None)
        if impl is None or declared is None:
            continue
        impl_params = list(inspect.signature(impl).parameters.values())[1:]
        base_params = list(inspect.signature(declared).parameters.values())[1:]
        impl_by_name = {p.name: p for p in impl_params}
        base_names = {b.name for b in base_params}
        # 1) 接口声明的每个参数都得在实现里有同名 + 同默认值
        mismatch = False
        for bp in base_params:
            ip = impl_by_name.get(bp.name)
            if ip is None or ip.default != bp.default:
                mismatch = True
                break
        if not mismatch:
            # 2) 实现里**不在接口**里的额外参数 = 漂移（严格口径，无 allowlist）
            for ep in impl_params:
                if ep.name in base_names:
                    continue
                mismatch = True
                break
        if mismatch:
            bad.append(name)
    return bad


def test_required_method_list_matches_abstract_base():
    """接口的抽象方法集合 == 任务书要求的方法集（防止漏写/多写）。"""
    assert set(StorageBase.__abstractmethods__) == set(REQUIRED_METHODS)


@pytest.mark.parametrize("backend_name", sorted(BACKENDS))
def test_backend_implements_every_interface_method(backend_name):
    """两个后端都实现了接口全部方法。"""
    missing = _missing_methods(BACKENDS[backend_name])
    assert not missing, f"{backend_name} 缺接口方法: {missing}"


@pytest.mark.parametrize("backend_name", sorted(BACKENDS))
def test_backend_signature_matches_interface(backend_name):
    """两个后端的方法签名与接口逐参数对齐。"""
    bad = _signature_mismatches(BACKENDS[backend_name])
    assert not bad, f"{backend_name} 签名与接口不符: {bad}"


@pytest.mark.parametrize("backend_name", sorted(BACKENDS))
def test_backend_is_storage_base_subclass(backend_name):
    """两个后端都注册在接口下。"""
    assert issubclass(BACKENDS[backend_name], StorageBase)


def test_missing_method_probe_is_not_vacuous():
    """探针：故意删掉一个方法 → missing 必须真的报出来。

    没有这条，上面几条可能因为「检查逻辑写错而恒真」变成空转。
    """
    class BrokenPg(PgStorage):
        entity_timeline = None      # 模拟「实现漏了/被误删」

    assert _missing_methods(PgStorage) == [], "对照组：完好后端不该报缺"
    assert _missing_methods(BrokenPg) == ["entity_timeline"], "缺方法必须被检出"

    class DuckTyped:               # 完全不继承接口的裸类也要被抓出来
        pass

    assert len(_missing_methods(DuckTyped)) == len(REQUIRED_METHODS)


def test_signature_drift_probe_is_not_vacuous():
    """探针：把 store() 的默认参数改掉 → mismatch 必须被检出。"""
    class DriftedPg(PgStorage):
        def store(self, text, tags="", category="", source="", fragment_type="",
                  sentiment_score=None, sentiment_label=None, unexpected=1):
            return True

    assert _signature_mismatches(PgStorage) == [], "对照组：完好后端不该报漂移"
    assert _signature_mismatches(DriftedPg) == ["store"], "签名漂移必须被检出"


class _FakeEmbedder:
    """最小 embedder 桩：有它 search_knn 才会真去连库（无 embedder 返回 [] 是
    与 Redis 侧一致的合法行为，不是故障）。"""

    _registered = True
    _model = "fake-4d"
    dimension = 4

    def get_embedding(self, text: str):
        return [float(len(text) % 7), 0.5, 0.25, 1.0]


_BAD_DSN = "postgresql://nobody:nopass@127.0.0.1:1/nonexistent_db"


def test_pg_search_methods_are_implemented():
    """2026-10 ks_pg_b2：PG 侧检索三方法**已实现**，不再抛 NotImplementedError。

    DSN 是故意写错的：证明「不再抛 NotImplementedError」发生在建连之前，
    且真去连库时抛的是连接类异常 —— 既不是静默空列表，也不是「以为实现了」。
    """
    pg = PgStorage(dsn=_BAD_DSN, embedder=_FakeEmbedder())
    for name in ("search", "search_bm25", "search_knn"):
        with pytest.raises(Exception) as exc:
            getattr(pg, name)("测试查询")
        assert not isinstance(exc.value, NotImplementedError), \
            f"{name} 仍抛 NotImplementedError —— 批 2 已交付检索"


def test_pg_corpus_maintenance_is_implemented_and_never_silently_zero():
    """2026-10 ks_pg_syn：语料维护两法**已实现**，不再抛 NotImplementedError。

    与检索三方法同一条契约：DSN 故意写错 ⇒ 抛的是**连接类异常**，
    既不是 `NotImplementedError`（= 没实现），也不是静默返回零统计
    （= 「什么都没扫到」被读成「扫过了，没有同义词」—— 三后端对照直接失真）。
    """
    pg = PgStorage(dsn=_BAD_DSN)
    for name, args in (("discover_synonyms", (True,)),
                       ("discover_synonyms", ()),
                       ("generate_jieba_dict", ("/tmp/ks_pg_syn_unittest_dict.txt",))):
        with pytest.raises(Exception) as exc:
            getattr(pg, name)(*args)
        assert not isinstance(exc.value, NotImplementedError), \
            f"{name}{args} 仍抛 NotImplementedError —— 批 3 已交付语料维护"


def test_pg_search_methods_never_silently_return_empty_on_backend_failure():
    """反向断言：PG 检索绝不能因为「连不上」而返回 []。

    静默空 = 记忆搜不到的静默故障（切了 backend=postgres 却什么都不报）。
    """
    pg = PgStorage(dsn=_BAD_DSN, embedder=_FakeEmbedder())
    for name in ("search_bm25", "search_knn"):
        try:
            got = getattr(pg, name)("测试查询")
        except NotImplementedError:
            pytest.fail(f"{name}() 仍抛 NotImplementedError —— 批 2 已交付检索")
        except Exception:
            continue        # 连接类异常 = 正确行为（明确报错）
        pytest.fail(f"{name}() 在后端不可达时返回了 {got!r}，必须抛错而不是静默空")


def test_pg_search_knn_without_embedder_returns_empty_like_redis():
    """无 embedder 时 search_knn 返回 [] —— 这是与 Redis 侧一致的合法契约
    （向量路不可用），且会打 WARNING，不是静默。"""
    pg = PgStorage(dsn=_BAD_DSN)
    assert pg.search_knn("测试查询") == []


def test_psycopg_is_not_a_module_level_import():
    """没装 psycopg 也不能影响 import storage_pg —— 只允许在函数体内 import。"""
    tree = ast.parse(Path(inspect.getfile(PgStorage)).read_text(encoding="utf-8"))
    top_level = {
        alias.name.split(".")[0]
        for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in getattr(node, "names", [])
    }
    assert "psycopg" not in top_level, "psycopg 必须在 _connect() 内部延迟 import"


# ---------------------------------------------------------------------------
# 改 agent_id 的归属级联（2026-10 keepsake_agent_setid）
# ---------------------------------------------------------------------------
#
# 🔴 `reassign_agent` **不进 REQUIRED_METHODS**：那份清单与
# `StorageBase.__abstractmethods__` 逐字相等（见上面那条断言），而本原语在
# 基类里没有抽象声明 —— 它是**三后端各自实现**的扩展原语。
# 漏一个后端 = 「换个后端，代理改 id 之后记忆归属不跟着搬」的静默分叉，
# 所以这里单独钉一条：三后端都得**自己**实现（继承来的空壳不算）。

@pytest.mark.parametrize("backend_name", sorted(BACKENDS))
def test_backend_implements_reassign_agent(backend_name):
    """三后端都得自己实现 `reassign_agent`（改 id 时归属跟着搬）。"""
    cls = BACKENDS[backend_name]
    impl = getattr(cls, "reassign_agent", None)
    assert callable(impl), f"{backend_name} 缺 reassign_agent"
    assert "reassign_agent" in vars(cls), \
        f"{backend_name} 的 reassign_agent 是继承来的（没自己实现）"
    import inspect
    params = list(inspect.signature(impl).parameters)
    assert params == ["self", "old_id", "new_id"], f"{backend_name} 签名漂移：{params}"
