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
    """该类的方法签名与接口声明不一致的项。"""
    bad = []
    for name in REQUIRED_METHODS:
        impl = getattr(cls, name, None)
        declared = getattr(StorageBase, name, None)
        if impl is None or declared is None:
            continue
        # 去掉 self 后比对剩余参数（名 + 默认值 + 顺序）
        impl_sig = list(inspect.signature(impl).parameters.values())[1:]
        base_sig = list(inspect.signature(declared).parameters.values())[1:]
        if ([p.name for p in impl_sig] != [p.name for p in base_sig]
                or [p.default for p in impl_sig] != [p.default for p in base_sig]):
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


def test_pg_corpus_maintenance_still_raises_not_implemented():
    """语料维护两法仍显式抛错（批 2 不在范围内），绝不静默返回零统计。"""
    pg = PgStorage(dsn=_BAD_DSN)
    for name in ("discover_synonyms", "generate_jieba_dict"):
        with pytest.raises(NotImplementedError) as exc:
            getattr(pg, name)()
        assert "batch 2" in str(exc.value), f"{name} 的报错必须写明 batch 2"


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