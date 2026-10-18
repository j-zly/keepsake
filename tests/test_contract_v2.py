"""契约 v2（演化链 + 适用条件）验收 —— 2026-10 keepsake_contract_v2。

对应上游 Hy-Memory 的四根列：is_latest（链头标记）/ supersedes（反向链）/
supersede_reason（作废原因）/ applicability（适用条件）。语义单点定义在
`keepsake.storage_shared.CONTRACT_V2_COLUMNS`，本文件只钉住**契约**。

覆盖（每条 = 任务书测试点）：
  1. 三后端各 1 条 supersede 链用例 —— 旧条 is_latest=0 + 新条 supersedes 回填
     + 检索不返链身 + 常驻层（core 列表）仍可见
  2. applicability 落库读回往返（store → get_fragment → 检索结果）
  3. 缺列降级：读省略键 / 写报人话 500（带迁移 SQL）
  4. 老行 is_latest 为 NULL/空 ⇒ 仍视为链头（迁移前写的行不许被整批抹掉）

后端可达性说明：PG / Redis 在本机**没有**真服务（无 psycopg、无 redis-server），
所以这两侧用假游标/假客户端断言「发出的 SQL / HSET 是什么」—— 契约本身
（写什么字段、检索过滤在哪一层、老数据怎么兜）才是这三后端真正要一致的东西，
而那正是假件能钉住的。SQLite 侧是**真库真跑**（tmp_path），红绿闭环。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, List

import pytest

from keepsake.storage import RedisStorage
from keepsake.storage_pg import FRAGMENT_COLUMNS, PgStorage
from keepsake.storage_shared import (
    CHAIN_HEAD_SQL,
    CONTRACT_V2_COLUMNS,
    CONTRACT_V2_MIGRATION_HINT,
    append_supersedes,
    tsv_lexemes,
)
from keepsake.storage_sqlite import SqliteStorage

TOKEN = "v2契约标记token"

#: 老库用例的正文与检索词。**用 ASCII 词元**而不是中文：查询侧 `query_lexemes`
#: 与索引侧 `tsv_lexemes` 的切法对某些中文组合并不对称（实测「老数据」查 →
#: 老/数/据，索引侧是 老/数据 ⇒ 恒 0 命中；那是既有分词口径的问题，与本单无关，
#: 但会把这个用例的红绿盖掉）。ASCII 词元两边切法一致，测的才是本单要测的东西。
LEGACY_TEXT = "遗留库兼容检查 存量链头"
LEGACY_QUERY = "检查"   # 跨环境稳健锚点：主脑与 88 的 jieba 词典不同（主脑有自定义词典），
                        # 英文混排串 'legacyv2marker' 两边切法不一致（88 绿、主脑红实证），
                        # 中文基础词在两套词典里都整词切出。


# =============================================================================
# 0. 单点拼法（三个后端都调它，拼歪了就成三份语义）
# =============================================================================

def test_反向链追加_去重且自愈老形态():
    """`append_supersedes`：追加不覆盖 + 老形态（裸 key 串）读时自愈。"""
    assert json.loads(append_supersedes("", "old")) == ["old"]
    # 再顶一条 ⇒ 两个都在（覆盖式会把第一条抹掉，链就断了）
    assert json.loads(append_supersedes(append_supersedes("", "old"), "old2")) == ["old", "old2"]
    # 同一个 key 顶两次不重复记
    assert json.loads(append_supersedes(append_supersedes("", "old"), "old")) == ["old"]
    # 🔴 老库那一列存的是**裸 key 串**（不是数组）：解不开就当单元素列表读进来
    assert json.loads(append_supersedes("memory:frag:老", "new")) == ["memory:frag:老", "new"]


def test_链身判据与老行口径():
    """`CHAIN_HEAD_SQL` 必须容忍 NULL —— 老行没这根列/值，全按链头。"""
    assert "COALESCE" in CHAIN_HEAD_SQL and "is_latest" in CHAIN_HEAD_SQL
    # 四根契约列必须齐（少一根就是「某个后端忘了补」）
    assert set(CONTRACT_V2_COLUMNS) == {"is_latest", "supersedes",
                                        "supersede_reason", "applicability"}
    # 出参形状：三后端检索结果共用 SEARCH_FIELDS，含 applicability
    from keepsake.storage_shared import SEARCH_FIELDS
    assert "applicability" in SEARCH_FIELDS


# =============================================================================
# 1a. SQLite：supersede 链（真库真跑）
# =============================================================================

@pytest.fixture()
def store(tmp_path) -> SqliteStorage:
    s = SqliteStorage(path=str(tmp_path / "ks.db"))
    assert s.ensure_index() is True
    yield s
    s.close()


def test_sqlite_supersede_旧条转链身_新条回填反向链_检索不返链身_core列表仍可见(store):
    """SQLite 侧 supersede 链全口径：双向链 + 检索排链身 + 常驻层不受影响。"""
    old = f"{TOKEN}旧"
    new = f"{TOKEN}新"
    # 两条都带 core 标签：一条测「检索排链身」，一条测「常驻层不该被这个过滤波及」
    store.store(text=f"{old} 数据库迁移用 Alembic", tags="core", category="convention")
    store.store(text=f"{new} 数据库迁移用 Flyway", tags="core", category="convention")
    old_key = store.scan_fragment_keys("", 50)[1]
    old_key = next(k for k in old_key if old in store.get_fragment(k)["content"])
    new_key = next(k for k in store.scan_fragment_keys("", 50)[1]
                   if new in store.get_fragment(k)["content"])

    # 反向链留痕（pipeline `_do_update` 的第一步）+ 封边（第二步，带 reason）
    assert store.set_supersedes(new_key, old_key) is True
    assert store.supersede_fragment(old_key, new_key, reason="换了迁移工具") is True

    old_doc, new_doc = store.get_fragment(old_key), store.get_fragment(new_key)
    # 旧条 = 链身
    assert old_doc["is_latest"] == "0", "旧条必须打成链身"
    assert old_doc["superseded_by"] == new_key
    # 新条 = 链头 + 反向链回填 + 作废原因
    assert new_doc["is_latest"] == "1"
    assert json.loads(new_doc["supersedes"]) == [old_key], new_doc.get("supersedes")
    assert new_doc["supersede_reason"] == "换了迁移工具"

    # 检索不返链身（只出新条）
    hits = store.search_bm25("数据库迁移")
    hit_keys = [h["_key"] for h in hits]
    assert new_key in hit_keys, "链头必须能被检索到"
    assert old_key not in hit_keys, "🔴 链身不许进检索面"

    # 🔴 常驻层（core 标签列表）**不加** is_latest 过滤 —— 历史 core 展示不受影响
    core = [r["key"] for r in store.list_by_tag("core")]
    assert old_key in core, "常驻层不该被 is_latest 过滤波及（否则历史 core 会凭空少条目）"


def test_sqlite_作废封边_无新条_只动旧条(store):
    """DELETE 路径（`__void__`）：没有新条 ⇒ 旧条转链身 + reason 记在旧条上。"""
    k = f"{TOKEN}待作废"
    store.store(text=f"{k} 这条要作废", tags="core")
    key = next(kk for kk in store.scan_fragment_keys("", 50)[1]
               if k in store.get_fragment(kk)["content"])
    assert store.supersede_fragment(key, "", reason="与新事实矛盾") is True
    doc = store.get_fragment(key)
    assert doc["is_latest"] == "0"
    assert doc["superseded_by"] == "__void__", "作废 sentinel 与 pipeline.VOID_KEY 必须一致"
    assert doc["supersede_reason"] == "与新事实矛盾"


# =============================================================================
# 1b. PG：supersede 链 + 检索过滤（假游标，看发出的 SQL）
# =============================================================================

class _FakeCur:
    """记录 SQL 与参数的假游标；每次 execute 顺次取结果队列里的下一份结果。

    与 `tests/test_pg_bm25.py` 同款写法 —— 关键在 `execute` 必须**重置** rows：
    `_contract_v2_missing` 走的是 `_fetch_col(cur, _LIST_...)`，由它自己发 SQL，
    不喂结果的话它 fetch 到空 ⇒ 四根列一律判缺。
    """

    def __init__(self, db, sink):
        self.db, self.sink = db, sink

    def execute(self, sql, params=None):
        stmt = (" ".join(sql.split()), list(params or []))
        self.db.statements.append(stmt)
        self.sink.append(stmt)
        self.db.rows = self.db.queue.pop(0) if self.db.queue else []

    def fetchall(self):
        return self.db.rows

    def fetchone(self):
        return self.db.rows[0] if self.db.rows else None

    @property
    def rowcount(self):
        return 1


class _FakeDb:
    """结果队列按「execute 的发生次序」消费 —— 一次上下文里的多条 SQL 各拿各的。"""

    def __init__(self, queue):
        self.statements: List[Any] = []
        self.queue = list(queue)
        self.rows: List[tuple] = []


class _Ctx:
    def __init__(self, db, sink):
        self.db, self.sink = db, sink

    def __enter__(self):
        self.cur = _FakeCur(self.db, self.sink)
        return self.cur

    def __exit__(self, *exc):
        return False


def _fake_pg(sink, columns=None, after=None) -> PgStorage:
    """不连库的 PgStorage：结果队列按 execute 次序消费。

    队列第一份是 `_contract_v2_missing` 的列清单探测结果，`after` 是之后各次
    fetch 依次拿到的结果集（默认给一份空集兜底）。
    """
    pg = PgStorage.__new__(PgStorage)
    pg._agent_id, pg._is_primary, pg._embed_dim = "", True, 4
    cols = list(FRAGMENT_COLUMNS) if columns is None else list(columns)
    db = _FakeDb([[(c,) for c in cols], *list(after or []), []])
    pg._ro = lambda: _Ctx(db, sink)
    pg._tx = lambda: _Ctx(db, sink)
    pg._snap_drop = lambda *a, **k: None
    return pg


def test_pg_supersede_旧条转链身_新条回填反向链():
    """PG 侧 supersede 链：旧条 is_latest='0' + 新条 supersedes/is_latest='1'。"""
    sink: List[Any] = []
    pg = _fake_pg(sink)
    assert pg.supersede_fragment("memory:frag:old", "memory:frag:new",
                                 reason="换了迁移工具") is True
    sqls = [s for s, _ in sink]
    # 旧条：打链身
    assert any("is_latest = '0'" in s and "superseded_by" in s for s in sqls), sqls
    # 新条：回填反向链 + 打链头 + 记原因
    assert any("supersedes = %s" in s and "is_latest = '1'" in s for s in sqls), sqls
    # 反向链的值是 JSON 数组（不是裸 key 串）
    params = [p for s, p in sink if "supersedes = %s" in s]
    assert json.loads(params[0][0]) == ["memory:frag:old"], params


def test_pg_检索侧排链身_常驻层不加():
    """`_search_filter_sql` 加链身判据；`list_by_tag`（常驻层）**不加**。

    常驻层那条用源码断言而不是假游标跑一遍：`list_by_tag` 的 SQL 是拼死的
    f-string，假游标能验「发了什么」，但更直接的护栏是**这根列压根不许出现在
    list_by_tag 里** —— 一旦有人顺手把过滤复制过去，历史 core 会凭空少条目，
    而那是不报错、少条目的静默故障。
    """
    sink: List[Any] = []
    pg = _fake_pg(sink)
    where, _ = pg._search_filter_sql("", "", True)
    assert CHAIN_HEAD_SQL in where, "检索侧必须排链身"

    import inspect
    src = inspect.getsource(PgStorage.list_by_tag)
    assert "is_latest" not in src, "常驻层查询不得含 is_latest（历史 core 展示不得受影响）"


def test_pg_旧调用方默认参数_落库值逐字不变():
    """不带 reason 的老调用方：superseded_by/superseded_at 与改造前一致。"""
    sink: List[Any] = []
    pg = _fake_pg(sink)
    assert pg.supersede_fragment("memory:frag:old", "memory:frag:new") is True
    old_stmt = [(s, p) for s, p in sink if "is_latest = '0'" in s][0]
    assert "superseded_by = %s" in old_stmt[0]
    assert old_stmt[1][0] == "memory:frag:new", "superseded_by 的值不能变"
    assert old_stmt[1][1], "superseded_at 必须仍然落一个非空 ISO 时刻"


# =============================================================================
# 1c. Redis：supersede 链（假客户端，看 HSET）
# =============================================================================

class _FakePipe:
    def __init__(self, sink):
        self.sink = sink

    def hset(self, key, field, value):
        self.sink.append(("hset", key, field, value))

    def execute(self):
        pass


class _FakeRedis:
    """够 supersede_fragment / set_supersedes 用的最小 Redis 替身。"""

    def __init__(self, sink, store=None):
        self.sink, self.store = sink, (store if store is not None else {})

    def hset(self, key, field, value):
        self.sink.append(("hset", key, field, value))
        self.store.setdefault(key, {})[field] = value

    def hget(self, key, field):
        return self.store.get(key, {}).get(field)

    def pipeline(self):
        return _FakePipe(self.sink)


def test_redis_supersede_旧条转链身_新条回填反向链():
    """Redis 侧 supersede 链：hash 侧同口径。"""
    sink: List[Any] = []
    redis = _FakeRedis(sink, {"memory:frag:new": {"supersedes": json.dumps(["old2"])}})
    st = RedisStorage.__new__(RedisStorage)
    st._get_client = lambda: redis

    assert st.supersede_fragment("memory:frag:old", "memory:frag:new",
                                 reason="换了迁移工具") is True
    sets = {(k, f): v for kind, k, f, v in sink if kind == "hset"}
    assert sets[("memory:frag:old", "is_latest")] == "0", "旧条必须打成链身"
    assert sets[("memory:frag:old", "superseded_by")] == "memory:frag:new"
    assert sets[("memory:frag:new", "is_latest")] == "1"
    assert sets[("memory:frag:new", "supersede_reason")] == "换了迁移工具"
    # 反向链是**追加**：库里已有的 old2 不能被抹掉
    assert json.loads(sets[("memory:frag:new", "supersedes")]) == ["old2", "memory:frag:old"]


def test_redis_链身判据_老数据当链头():
    """`_is_chain_body`：字段缺失/空/'1' 一律链头，只有 '0' 是链身。

    判错方向只有一侧有灾难：老数据当成链身 = 一次升级把存量记忆整批从检索里抹掉。
    """
    from keepsake.core.redis import _is_chain_body
    assert _is_chain_body(None) is False          # 字段压根没有
    assert _is_chain_body("") is False            # 空串
    assert _is_chain_body("1") is False           # 链头
    assert _is_chain_body(b"0") is True           # 链身（bytes 未解码的旧配置）
    assert _is_chain_body("0") is True
    assert _is_chain_body(" 0 ") is True


# =============================================================================
# 2. applicability 落库读回往返
# =============================================================================

def test_sqlite_applicability_落库读回往返(store):
    """适用条件：store 写进去 → get_fragment 读出来 → 检索结果里也带着。"""
    text = f"{TOKEN} 数据库迁移用 Alembic"
    app = "runtime: python 3.11\nsdk: alembic 1.13\nendpoint: 无\nenv: 不存值"
    assert store.store(text=text, tags="core", applicability=app) is True
    key = next(k for k in store.scan_fragment_keys("", 50)[1]
               if TOKEN in store.get_fragment(k)["content"])
    # 读回逐字一致（含换行）
    assert store.get_fragment(key)["applicability"] == app
    # 检索结果也带着（前端就靠这个渲染 `[适用条件] …`）
    hits = [h for h in store.search_bm25("数据库迁移") if h["_key"] == key]
    assert hits and hits[0]["applicability"] == app
    # 不传时是稀疏的：出参不带这个键（与 Redis hash 空值不进 dict 同口径）
    store.store(text=f"{TOKEN} 无适用条件", tags="core")
    plain = next(k for k in store.scan_fragment_keys("", 50)[1]
                 if "无适用条件" in store.get_fragment(k)["content"])
    assert "applicability" not in store.get_fragment(plain)


def test_三后端_store_都接_applicability_可选入参():
    """三后端 store 签名一致，且默认空串 ⇒ 老调用方逐字不受影响。"""
    import inspect
    for cls in (PgStorage, SqliteStorage, RedisStorage):
        sig = inspect.signature(cls.store)
        assert sig.parameters["applicability"].default == "", cls.__name__
        # 同样的可选入参在 supersede_fragment 上（reason）
        assert inspect.signature(cls.supersede_fragment).parameters["reason"].default == ""


# =============================================================================
# 3. 缺列降级（读省略键 / 写人话 500）
# =============================================================================

def test_pg_缺列降级_读省略键_写报人话错():
    """老库未迁移四列：读路径少 SELECT 这几列，写路径抛带迁移 SQL 的错。"""
    legacy = [c for c in FRAGMENT_COLUMNS if c not in CONTRACT_V2_COLUMNS]
    sink: List[Any] = []
    pg = _fake_pg(sink, columns=legacy)
    assert pg._contract_v2_missing() == frozenset(CONTRACT_V2_COLUMNS)

    # 读：SELECT 清单里根本没有这四根列（列不存在时 SELECT 它 = UndefinedColumn 500）
    sink.clear()
    cols = pg._read_columns(FRAGMENT_COLUMNS)
    assert not (set(CONTRACT_V2_COLUMNS) & set(cols)), "缺列时不得 SELECT 不存在的列"

    # 出参自然不带这些键（与 tags/content_type 降级一字同款）
    assert PgStorage._row_to_fragment(("k", "内容") + ("",) * (len(cols) - 2), cols) \
        == {"content": "内容"}

    # 检索：缺 is_latest 列时**不加**那条过滤（老数据照常可检索，而不是 500）
    sink.clear()
    where, _ = pg._search_filter_sql("", "", True)
    assert CHAIN_HEAD_SQL not in where, "缺列时不得引用不存在的列（会把整条检索打成 500）"
    assert "invalid_at = ''" in where, "既有过滤一字不动"

    # 写：点名要写缺列 ⇒ 抛人话错，带可执行的迁移 SQL
    with pytest.raises(Exception) as ei:
        pg.supersede_fragment("memory:frag:old", "memory:frag:new")
    msg = str(ei.value)
    assert "is_latest" in msg and CONTRACT_V2_MIGRATION_HINT in msg, msg
    assert "ADD COLUMN IF NOT EXISTS" in msg, "迁移 SQL 必须是可执行的，不能只说『请迁移』"


def test_sqlite_老库补列_存量行默认链头(tmp_path):
    """人工造一张「契约 v2 之前」的表 ⇒ ensure_index 补列 ⇒ 存量行落 '1'（链头）。"""
    path = tmp_path / "legacy.db"
    s = SqliteStorage(path=str(path))
    assert s.ensure_index() is True
    s.close()
    conn = sqlite3.connect(path)
    legacy_cols = ([c for c in FRAGMENT_COLUMNS if c not in CONTRACT_V2_COLUMNS]
                   + ["content_tsv"])   # content_tsv 是 FTS 自愈重建的输入
    conn.execute("DROP TABLE ks_fragment")
    conn.execute("CREATE TABLE ks_fragment ("
                 + ",".join(f"{c} TEXT PRIMARY KEY" if c == "key" else f"{c} TEXT DEFAULT ''"
                            for c in legacy_cols) + ")")
    conn.execute("INSERT INTO ks_fragment (key, content, content_tsv) VALUES (?,?,?)",
                 ("k1", LEGACY_TEXT, " ".join(tsv_lexemes(LEGACY_TEXT))))
    conn.commit()
    conn.close()

    s2 = SqliteStorage(path=str(path), is_primary=True)
    try:
        assert s2.ensure_index() is True, "老库补列必须成功"
        with s2._lock:      # noqa: SLF001
            have = {r[1] for r in s2._db().execute("PRAGMA table_info(ks_fragment)")}
        assert set(CONTRACT_V2_COLUMNS) <= have, "契约 v2 四列必须补齐"
        # 🔴 存量行落 '1'（链头）—— 迁移不能把老记忆整批变成链身
        assert s2.get_fragment("k1")["is_latest"] == "1"
        # 检索面一字不变：老数据照常能被检索到
        assert [h["_key"] for h in s2.search_bm25(LEGACY_QUERY)] == ["k1"]
    finally:
        s2.close()


# =============================================================================
# 4. 老行 is_latest 为 NULL / 空 ⇒ 仍视为链头
# =============================================================================

def test_sqlite_老行空值_is_latest_视为链头_仍可检索(store):
    """迁移窗口里可能落空值（列补上但从没写过）⇒ 仍当链头，仍能检索到。

    NULL 那侧由 `test_pg_老行_null_is_latest_视为链头` 用 SQL 谓词真跑一遍验；
    这里验 SQLite 侧现实可达的那一种 —— 列是 NOT NULL，写不进去 NULL，
    但**空串**写得进去（`ALTER ... DEFAULT` 没回填到的行、以及历史脏值）。
    """
    text = f"{TOKEN} 老行兼容"
    store.store(text=text, tags="core")
    key = next(k for k in store.scan_fragment_keys("", 50)[1]
               if TOKEN in store.get_fragment(k)["content"])
    with store._lock:      # noqa: SLF001
        store._db().execute("UPDATE ks_fragment SET is_latest = '' WHERE key = ?", (key,))
    assert "is_latest" not in store.get_fragment(key), "空串不该出现在出参里"
    assert [h["_key"] for h in store.search_bm25("老行兼容")] == [key], "空串行被误当链身过滤了"


def test_pg_老行_null_is_latest_视为链头():
    """PG 侧同一口径：谓词是 `COALESCE(is_latest,'1') <> '0'`。

    本机没有真 PG，但 `CHAIN_HEAD_SQL` 是**纯 SQL 片段**、PG 与 SQLite 共用
    同一份拼法 ⇒ 用真 SQLite 把这段谓词真跑一遍，验的是那条 SQL 本身
    （而不是我在 Python 里重写一遍等价逻辑 —— 那种测试恒绿，验不出任何东西）。
    三值逻辑是这条纪律的要害：裸 `is_latest <> '0'` 遇 NULL 得 NULL，
    WHERE 判 NULL 为假 ⇒ **整行被过滤**（老记忆凭空消失）。
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (k TEXT, is_latest TEXT)")
    conn.executemany("INSERT INTO t VALUES (?,?)",
                     [("null行", None), ("零行", "0"), ("一行", "1"), ("空串行", "")])
    kept = {r[0] for r in conn.execute(
        f"SELECT k FROM t WHERE {CHAIN_HEAD_SQL}")}
    conn.close()
    assert kept == {"null行", "一行", "空串行"}, f"老行被误当成链身过滤掉了: {kept}"
    assert "零行" not in kept, "链身必须被排掉"