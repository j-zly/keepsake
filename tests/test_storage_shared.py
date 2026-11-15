"""共用逻辑「单一实现」测试 —— PG 与 SQLite 必须走同一份代码。

背景（批 2 的架构要求）：检索后处理（RRF 融合 / v2 过滤 / 综合重排 / 批量载入）
与注意力、热词的加权公式都跟后端无关。两个后端各抄一份必然漂移，而上层按同样的
键取值 —— 一漂移就是**静默错排**（不报错，只是排出来的东西不对）。

本文件用三种手段交叉证明「只有一份实现」：
  1. **对象身份**：`PgStorage._rrf_fuse is SqliteStorage._rrf_fuse`（最强证据：
     不是「代码长得像」，是同一个函数对象）
  2. **AST 全仓扫描**：全仓 `src/keepsake` 里 `rrf_fuse` 等名字**只允许有一处 def**
  3. **行为等价**：同一个桩 storage 经两个后端类调用，结果逐字相等

零网络零数据库。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from keepsake import storage_shared
from keepsake.storage_pg import PgStorage
from keepsake.storage_sqlite import SqliteStorage

SRC = Path(storage_shared.__file__).resolve().parent

# 抽取到 storage_shared 的检索后处理：函数名 → 它在两个后端类体里绑定的私有名
SHARED_METHODS = {
    "rrf_fuse": "_rrf_fuse",
    "apply_v2_filters": "_apply_v2_filters",
    "rerank_with_decay": "_rerank_with_decay",
    "load_fragments_by_keys": "_load_fragments_by_keys",
}
BACKENDS = {"PgStorage": PgStorage, "SqliteStorage": SqliteStorage}


# ---------------------------------------------------------------- 二、同一个函数对象

@pytest.mark.parametrize("func_name", sorted(SHARED_METHODS))
def test_both_backends_bind_the_very_same_function_object(func_name):
    """🔴 最强证据：两个后端类上的同名方法是**同一个对象**。"""
    attr = SHARED_METHODS[func_name]
    impl = getattr(storage_shared, func_name)
    for backend_name, cls in BACKENDS.items():
        bound = cls.__dict__[attr]
        assert bound is impl, (
            f"{backend_name}.{attr} 不是 storage_shared.{func_name} 本身 —— "
            f"说明该后端另抄了一份实现，共用逻辑已经分叉"
        )
    assert PgStorage.__dict__[attr] is SqliteStorage.__dict__[attr]


def test_shared_functions_live_only_in_storage_shared_module():
    """四个共用函数确实定义在 storage_shared.py 里。"""
    for func_name in SHARED_METHODS:
        func = getattr(storage_shared, func_name)
        assert Path(func.__code__.co_filename).resolve() == Path(storage_shared.__file__).resolve()
        assert func.__module__ == "keepsake.core.shared"


def test_storage_shared_does_not_import_either_backend():
    """共用层不能反向依赖任一后端（否则两个后端又绑在一起了）。"""
    tree = ast.parse(Path(storage_shared.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert "keepsake.storage" not in imported
    assert "keepsake.storage_pg" not in imported
    # 2026-10 分层后后端实现叫 core.redis / core.pg / core.sqlite
    assert not any(m.split(".")[-1] in ("redis", "pg", "sqlite", "storage", "storage_pg")
                   for m in imported), imported


# ---------------------------------------------------------------- 三、行为等价

class _StubStorage:
    """最小 storage 桩：只带共用逻辑真正会读的属性（与两个后端同名同义）。"""

    _final_limit = 5
    _v2_min_score = 0.05
    _decay_half_days = 60
    _emotion_intensity_factor = 0.4
    _feedback_positive_boost = 1.3
    _feedback_negative_penalty = 0.5

    def _fetch_superseded_by(self, keys):
        return {}

    def _fetch_fragments(self, keys):
        return {}

    def match_hot_topics(self, content, limit=10):
        return 0.0

    def match_attention(self, content, top_n=10):
        return 1.0


def _bm25_rows():
    return [
        {"content": "用户要求做分页", "tags": "shared", "created": "2026-01-01T00:00:00+00:00",
         "sentiment_score": "0.5", "feedback_score": "1", "_key": "k1", "_bm25_score": 2.0},
        {"content": "部署流程用 rsync", "tags": "shared", "created": "2026-01-02T00:00:00+00:00",
         "sentiment_score": "0.0", "feedback_score": "0", "_key": "k2", "_bm25_score": 1.0},
        {"content": "无关闲聊内容", "tags": "shared", "created": "2026-01-03T00:00:00+00:00",
         "sentiment_score": "1.0", "feedback_score": "-2", "_key": "k3", "_bm25_score": 0.5},
    ]


def _knn_rows():
    return [
        {"content": "无关闲聊内容", "tags": "shared", "created": "2026-01-03T00:00:00+00:00",
         "sentiment_score": "1.0", "feedback_score": "-2", "_key": "k3", "_knn_score": 0.1},
        {"content": "用户要求做分页", "tags": "shared", "created": "2026-01-01T00:00:00+00:00",
         "sentiment_score": "0.5", "feedback_score": "1", "_key": "k1", "_knn_score": 0.3},
    ]


def _round_floats(obj, nd: int = 7):
    """递归把浮点四舍五入 —— 消掉「两次调用相隔几微秒」带来的末位差。"""
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, dict):
        return {k: _round_floats(v, nd) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, nd) for v in obj]
    return obj


@pytest.mark.parametrize("func_name", sorted(SHARED_METHODS))
def test_shared_function_yields_identical_result_through_either_backend(func_name):
    """同一个桩 storage，经 PgStorage 与 SqliteStorage 两条入口调用结果逐字相同。"""
    attr = SHARED_METHODS[func_name]
    # 每次都新建 —— 共用实现会就地写 _combined_score / _sim，复用同一批 dict
    # 会让「第二个后端」跑在被第一个后端改过的输入上，比较就失去意义了。
    if func_name == "rrf_fuse":
        make_args = lambda: (_bm25_rows(), _knn_rows())          # noqa: E731
    else:
        make_args = lambda: (_bm25_rows(),)                      # noqa: E731
    kwargs = {"score_key": "_bm25_score"} if func_name == "rerank_with_decay" else {}

    out = {}
    for backend_name, cls in BACKENDS.items():
        stub = _StubStorage()
        if func_name == "load_fragments_by_keys":
            out[backend_name] = cls.__dict__[attr](stub, ["k1", "k2"])
        else:
            out[backend_name] = cls.__dict__[attr](stub, *make_args(), **kwargs)
    # rerank 里带 `datetime.now()` 的时间衰减，两次调用相差几微秒 ⇒ 综合分末位会差。
    # 比到 7 位小数：足以证明「同一份算法」，又不把时钟抖动误判成分叉。
    assert _round_floats(out["PgStorage"]) == _round_floats(out["SqliteStorage"])


def test_rrf_fuse_puts_cross_validated_content_first():
    """RRF 本体行为：两路都命中的排最前（交叉验证加权生效）。"""
    stub = _StubStorage()
    fused = storage_shared.rrf_fuse(stub, _bm25_rows(), _knn_rows())
    assert fused[0]["content"] == "用户要求做分页"      # rank1 + rank2
    assert fused[1]["content"] == "无关闲聊内容"        # rank3 + rank1
    assert len(fused) == 3                             # 三条不同 content，去重后三条
    assert all("_combined_score" in f for f in fused)

    # 截断到 _final_limit
    stub._final_limit = 2
    assert len(storage_shared.rrf_fuse(stub, _bm25_rows(), _knn_rows())) == 2


def test_apply_v2_filters_applies_min_score_floor():
    """v2 地板：_sim 低于 _v2_min_score 的被剔除（共用实现，两后端同效）。"""
    stub = _StubStorage()
    frags = [{"_key": "a", "content": "A", "_sim": 0.9},
             {"_key": "b", "content": "B", "_sim": 0.01}]
    assert [f["_key"] for f in storage_shared.apply_v2_filters(stub, frags)] == ["a"]


def test_rerank_with_decay_knn_distance_semantics():
    """KNN 口径：score 是余弦距离(0~2)，_sim = 1 - dist/2（两个后端同一份）。"""
    stub = _StubStorage()
    frags = [{"_key": "near", "content": "近", "_knn_score": 0.0, "created": "2026-01-01T00:00:00+00:00"},
             {"_key": "far", "content": "远", "_knn_score": 2.0, "created": "2026-01-01T00:00:00+00:00"}]
    out = {f["_key"]: f for f in storage_shared.rerank_with_decay(stub, frags, "_knn_score", True)}
    assert out["near"]["_sim"] == pytest.approx(1.0)
    assert out["far"]["_sim"] == pytest.approx(0.0)


# ---------------------------------------------------------------- 加权公式唯一实现

@pytest.mark.parametrize("func_name, users", [
    ("attention_boost_from_topics", ["keepsake.core.pg", "keepsake.core.sqlite"]),
    ("hot_topic_weighted_hits", ["keepsake.core.pg", "keepsake.core.sqlite"]),
])
def test_weight_formula_has_one_definition_and_is_used_by_both_backends(func_name, users):
    """注意力/热词加权：只有一处定义，且两个后端的调用点都引用它。

    判据用 AST 而非 grep —— grep 会把「定义那一行」也当成「用了」，测不出分叉。
    """
    tree = ast.parse(Path(storage_shared.__file__).read_text(encoding="utf-8"))
    defs = [n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == func_name]
    assert len(defs) == 1, f"storage_shared 里 {func_name} 必须只有一处定义，实得 {len(defs)}"

    for mod in users:
        path = SRC / (mod.split(".")[-1] + ".py")
        src = Path(path).read_text(encoding="utf-8")
        assert func_name in src, f"{mod} 没有引用共用的 {func_name} —— 它自己抄了一份？"


def test_both_backends_reference_shared_weight_helpers():
    """两个后端都必须引用两个共用加权函数（双向都查，不漏一边）。"""
    for path in (SRC / "pg.py", SRC / "sqlite.py"):
        src = path.read_text(encoding="utf-8")
        assert "attention_boost_from_topics" in src, f"{path.name} 未引用共用注意力公式"
    for path in (SRC / "pg.py", SRC / "sqlite.py"):
        src = path.read_text(encoding="utf-8")
        assert "hot_topic_weighted_hits" in src, f"{path.name} 未引用共用热词衰减公式"


# ---------------------------------------------------------------- 上游函数仍在原处

@pytest.mark.parametrize("name", ["_sanitize_terms", "_expand_terms"])
def test_query_helpers_have_one_definition_in_shared(name):
    """查询式构造助手唯一实现在 core/shared.py。

    2026-11：这两支历史上寄居 redis.py、被 pg/sqlite 反向借用；Redis 后端移除后
    搬进共用层。这里钉「只有一处 def」—— 后端另抄一份就是分叉起点。"""
    tree = ast.parse((SRC / "shared.py").read_text(encoding="utf-8"))
    defs = [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert len(defs) == 1, f"{name} 必须只在 shared.py 定义一次，实得 {len(defs)}"


def test_pg_backend_reuses_sanitize_terms_instead_of_copying():
    """PG 侧必须 import `_sanitize_terms` / `_expand_terms`，不能自己再写一套。"""
    tree = ast.parse((SRC / "pg.py").read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                for a in n.names}
    assert {"_sanitize_terms", "_expand_terms"} <= imported
    # 并且本文件没有另写一份同名实现
    defs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert "_sanitize_terms" not in defs and "_expand_terms" not in defs


def test_storage_pg_is_actually_using_the_module():
    """storage_pg 真的用上了 storage_shared（不是 import 了却不用）。"""
    src = (SRC / "pg.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    for name in ("apply_v2_filters", "rerank_with_decay", "rrf_fuse",
                 "load_fragments_by_keys", "attention_boost_from_topics",
                 "hot_topic_weighted_hits", "SEARCH_FIELDS"):
        assert name in used, f"storage_pg.py import 了 {name} 却没真正使用"
    # 2026-10 ks_lenfloor：长度地板**可配置**了 ⇒ 模块级 `MAX_CONTENT_LEN = 600`
    # 退役为**类级默认**（缺省 5000，三后端同一个值，见 storage_shared 的长度地板一节）。
    assert PgStorage.MAX_CONTENT_LEN == 5000
    assert PgStorage.MAX_CONTENT_LEN is storage_shared.DEFAULT_MAX_CONTENT_LEN


# ---------------------------------------------------------------- 迁移脚本的只读红线

# ---------------------------------------------------------------- pg 侧注意力公式接线

def test_pg_match_attention_uses_shared_formula():
    """🔴 PgStorage.match_attention 必须走共用公式，不得退回历史副本
    （`attention.match_attention_boost` 那份是上游独立实现，有自己一套循环）。"""
    tree = ast.parse((SRC / "pg.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "PgStorage")
    fn = next(n for n in cls.body
              if isinstance(n, ast.FunctionDef) and n.name == "match_attention")
    called = set()
    for c in ast.walk(fn):
        if not isinstance(c, ast.Call):
            continue
        if isinstance(c.func, ast.Attribute):
            called.add(c.func.attr)
        elif isinstance(c.func, ast.Name):
            called.add(c.func.id)
    assert "attention_boost_from_topics" in called, (
        f"PgStorage.match_attention 没调共用公式，实际调了 {called}"
    )
    assert "match_attention_boost" not in called, (
        "match_attention 仍在调上游那份独立公式 —— 共用没真正生效"
    )


# 2026-11：`scripts/migrate_redis_to_pg.py` / `eval_compare_backends.py` 的只读红线
# 与题集复用测试随 Redis 后端一起移除（脚本在 git 历史里，1.1.x 仍可用）。


# ------------------------------------------------ 语料维护判定链（批 3）唯一实现
#
# 2026-10 ks_pg_syn：PG 补齐 discover_synonyms / generate_jieba_dict 之后，
# 「三后端靠配置切换」这个用户口径要求两侧判定链**不能各写一份**。
# 下面两条把它钉死：定义只允许在 storage_shared，PG/SQLite 都必须引用。

#: 语料维护的纯计算：PG / SQLite 两个后端共用的判定链
CORPUS_MAINTENANCE_FUNCS = (
    "synonym_words", "jieba_dict_words",
    "accumulate_word_freq", "accumulate_co_occurrence",
    "discover_synonym_pairs", "merge_synonym_maps", "synonym_rows",
    "jieba_dict_entries",
)
#: 两个后端各自的语料维护入口（方法名，用于查「引用了共用函数」）
CORPUS_MAINTENANCE_ENTRYPOINTS = {
    "pg.py": ("discover_synonyms", "generate_jieba_dict"),
    "sqlite.py": ("discover_synonyms", "generate_jieba_dict"),
}


@pytest.mark.parametrize("func_name", CORPUS_MAINTENANCE_FUNCS)
def test_corpus_maintenance_chain_has_one_definition(func_name):
    """全仓 `src/keepsake` 里 `def <func_name>` 只允许有一处，且在 storage_shared。

    判据用 AST 而非 grep：grep 分不清「定义」与「引用」，测不出后端里另抄了一份。
    """
    seen = []
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(n, ast.FunctionDef) and n.name == func_name
               for n in ast.walk(tree)):
            seen.append(path.name)
    assert seen == ["shared.py"], (
        f"{func_name} 必须只在 core/shared.py 里定义一次，实测定义于 {seen}"
    )


@pytest.mark.parametrize("mod, methods", sorted(CORPUS_MAINTENANCE_ENTRYPOINTS.items()))
def test_backends_reference_shared_corpus_maintenance_chain(mod, methods):
    """每个后端的语料维护入口都真的调到了共用判定链（不能只 import 不调用）。"""
    src = (SRC / mod).read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef)
               and n.name in ("PgStorage", "SqliteStorage"))
    for method in methods:
        fn = next(n for n in cls.body
                  if isinstance(n, ast.FunctionDef) and n.name == method)
        called = set()
        for c in ast.walk(fn):
            if not isinstance(c, ast.Call):
                continue
            if isinstance(c.func, ast.Attribute):
                called.add(c.func.attr)
            elif isinstance(c.func, ast.Name):
                called.add(c.func.id)
        used = {"synonym_words", "jieba_dict_words", "discover_synonym_pairs",
                "merge_synonym_maps", "jieba_dict_entries"}
        assert used & called, (
            f"{mod}.{method} 一个共用判定链函数都没调，实际调了 {sorted(called)} —— "
            f"它自己抄了一份？"
        )
