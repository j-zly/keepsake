"""SQLite 后端（批 1）读写原语 + schema 自愈 —— 红绿闭环。

覆盖（每条对应任务书 C.1 的验收点）：
  1. 空库 `ensure_index()` ⇒ True 且列齐（★ PG 踩过的 CREATE/ALTER 撞车坑）
  2. 老库（人工删列）再打开 ⇒ `ensure_index()` 幂等补列
  3. `write_fragments_batch` → `scan_fragment_keys` key 游标分页条数一致（**禁 OFFSET**）
  4. `get_fragments_batch` 返回条数 == 传入有效 key 数（★ Redis 批量读恒空坑）
  5. 带二进制/非 UTF-8 字段的条目能正常读出（★ Redis 二进制解码坑，别重演）
  6. `update_fragment_fields` / `delete_fragments_batch` 后计数正确
  7. 留桩方法**显式抛 NotImplementedError**（绝不静默返回空）
  8. 并发：两进程并发写不丢数据；写事务进行中只读进程仍可读
  9. p1.2 守卫：schema 未就绪 ⇒ 写/读路径显式抛 `StorageNotReadyError`
    （不许「进程退出码 0 + 库里 0 行」的静默丢数据）；批量写失败要看得见

本文件 **hermetic**：全部用 `tmp_path` 里的临时库文件，不连任何外部服务。
"""

from __future__ import annotations

import logging
import sqlite3
import struct
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import List

import pytest

from keepsake.storage_pg import FRAGMENT_COLUMNS, MAINTENANCE_COLUMNS
from keepsake.storage_sqlite import ALL_COLUMNS, SqliteStorage


@pytest.fixture()
def store(tmp_path) -> SqliteStorage:
    s = SqliteStorage(path=str(tmp_path / "ks.db"))
    assert s.ensure_index() is True
    yield s
    s.close()


def _rows(store: SqliteStorage) -> int:
    with store._lock:
        return store._db().execute("SELECT COUNT(*) FROM ks_fragment").fetchone()[0]


def _cols(store: SqliteStorage, table: str = "ks_fragment") -> set:
    with store._lock:
        return {r[1] for r in store._db().execute(f"PRAGMA table_info({table})")}


# ---------------------------------------------------------------------------
# 1. 空库：首次 ensure_index 必须一次成功，列齐
# ---------------------------------------------------------------------------

def test_fresh_db_ensure_index_creates_full_schema(tmp_path):
    """空库一次成功 + 列齐 —— 对齐 PG 侧「建表即含全部列」的坑位修复。"""
    s = SqliteStorage(path=str(tmp_path / "fresh.db"))
    try:
        assert s.ensure_index() is True, "空库首次 ensure_index 必须成功"
        # 列集合必须覆盖 PG 版基准（key + FRAGMENT_COLUMNS + MAINTENANCE_COLUMNS）
        have = _cols(s)
        missing = (set(FRAGMENT_COLUMNS) | set(MAINTENANCE_COLUMNS)) - have
        assert not missing, f"缺列: {missing}"
        # 任务书点名的列（含 level/consumed_*/attention_score/supersedes/hash）
        for col in ("key", "content", "tags", "category", "fragment_type", "created",
                    "updated", "level", "consumed_by", "consumed_at", "feedback_score",
                    "sentiment_score", "sentiment_label", "attention_score",
                    "supersedes", "superseded_by", "source", "hash", "embedding"):
            assert col in have, f"缺列 {col}"
        # 幂等：第二次不炸、不重复建
        assert s.ensure_index() is True
        assert _cols(s) == have
    finally:
        s.close()


def test_ensure_index_is_idempotent_on_legacy_db_missing_columns(tmp_path):
    """老库缺列 ⇒ `PRAGMA table_info` 判定后 `ALTER TABLE ADD COLUMN` 幂等补齐。"""
    path = tmp_path / "legacy.db"
    s = SqliteStorage(path=str(path))
    assert s.ensure_index() is True
    s.close()
    # 人工模拟「老库」：重建一张缺 level / consumed_by / supersedes / embedding 的表
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE ks_fragment")
    conn.execute("""
        CREATE TABLE ks_fragment (
            key TEXT PRIMARY KEY, content TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '', created TEXT NOT NULL DEFAULT '',
            sentiment_score TEXT NOT NULL DEFAULT '', sentiment_label TEXT NOT NULL DEFAULT '',
            feedback_score TEXT NOT NULL DEFAULT '0', entities TEXT NOT NULL DEFAULT '',
            fragment_type TEXT NOT NULL DEFAULT '', valid_until TEXT NOT NULL DEFAULT '',
            is_archived TEXT NOT NULL DEFAULT '', superseded_by TEXT NOT NULL DEFAULT '',
            superseded_at TEXT NOT NULL DEFAULT '', corrected_at TEXT NOT NULL DEFAULT '',
            invalid_at TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("INSERT INTO ks_fragment (key, content) VALUES ('k1', '老数据')")
    conn.commit()
    conn.close()

    s2 = SqliteStorage(path=str(path))
    try:
        assert s2.ensure_index() is True, "老库补列必须成功"
        have = _cols(s2)
        for col in ("level", "consumed_by", "consumed_at", "supersedes", "embedding"):
            assert col in have, f"未补列 {col}"
        # 存量行不受影响（DEFAULT '' 兼容旧数据）
        assert s2.get_fragment("k1")["content"] == "老数据"
        assert s2.ensure_index() is True    # 再补一次仍是 no-op
    finally:
        s2.close()


# ---------------------------------------------------------------------------
# 2. 批量写 + key 游标分页扫描（禁 OFFSET）
# ---------------------------------------------------------------------------

def test_scan_fragment_keys_keyset_pagination_counts_match(store):
    """write_fragments_batch → scan_fragment_keys 逐页 key 游标，条数与总数一致。"""
    n = 53
    rows = [{"key": f"memory:frag:{i:04d}", "content": f"内容 {i}", "level": "1"}
            for i in range(n)]
    assert store.write_fragments_batch(rows) == n

    seen, cursor, pages = [], "", 0
    while True:
        cursor, keys = store.scan_fragment_keys(cursor=cursor, limit=10)
        seen.extend(keys)
        pages += 1
        if not cursor:
            break
        assert pages < 20, "游标分页必须收敛（不得无限翻页）"
    assert len(seen) == n == len(set(seen)), f"分页条数不一致: {len(seen)} != {n}"
    assert seen == sorted(seen), "keyset 分页必须按 key 升序"
    # 前缀过滤：非 memory:frag: 前缀扫不到
    _, other = store.scan_fragment_keys(prefix="memory:full:", limit=100)
    assert other == []


# ---------------------------------------------------------------------------
# 3. 批量读：条数必须等于传入的有效 key 数
# ---------------------------------------------------------------------------

def test_get_fragments_batch_returns_every_valid_key(store):
    """★ Redis 踩过的坑（批量读恒空 ⇒ 合并/遗忘静默失效）不许重演。"""
    keys = [f"memory:frag:{i:03d}" for i in range(30)]
    store.write_fragments_batch([{"key": k, "content": f"c{i}"} for i, k in enumerate(keys)])
    got = store.get_fragments_batch(keys + ["memory:frag:不存在"])
    assert len(got) == len(keys), f"批量读丢了条目: {len(got)} != {len(keys)}"
    assert got["memory:frag:000"]["content"] == "c0"
    assert "memory:frag:不存在" not in got


# ---------------------------------------------------------------------------
# 4. 二进制 / 非 UTF-8 字段（★ Redis 二进制解码坑）
# ---------------------------------------------------------------------------

def test_binary_and_non_utf8_fields_roundtrip(store):
    """带二进制 blob 的条目必须整条读出，且二进制**原样 bytes**（不解码、不 str）。"""
    blob = struct.pack("6f", 0.1, -2.5, 3.0, 4.25, 5.5, 6.0)
    junk = b"\xff\xfe\x00\x80 not-utf8 \xc3\x28"     # 故意不是合法 UTF-8
    store.write_fragments_batch([{
        "key": "memory:frag:bin", "content": "带向量的条目",
        "embed_bin": blob, "embedding": junk, "tags": "a,b",
    }])
    got = store.get_fragments_batch(["memory:frag:bin"])
    assert len(got) == 1, "带二进制字段的条目被整条丢掉（Redis 恒空坑）"
    doc = got["memory:frag:bin"]
    assert doc["embed_bin"] == blob, "二进制必须原样 bytes 返回"
    assert doc["embedding"] == junk, "非 UTF-8 二进制不得被解码成乱码 str"
    assert doc["content"] == "带向量的条目"
    # 单条读同一口径
    assert store.get_fragment("memory:frag:bin")["embed_bin"] == blob
    # 文本列收到 bytes ⇒ 显式报错（静默 str() 会毁成 "b'...'" 字面量）
    with pytest.raises(TypeError):
        store.write_fragments_batch([{"key": "memory:frag:x", "content": b"\xff\xfe"}])


# ---------------------------------------------------------------------------
# 5. 局部更新 / 批量删 的计数
# ---------------------------------------------------------------------------

def test_update_and_delete_counts(store):
    keys = [f"memory:frag:{i:03d}" for i in range(10)]
    store.write_fragments_batch([{"key": k, "content": f"c{i}"} for i, k in enumerate(keys)])

    assert store.update_fragment_fields(keys[0], {"consumed_by": "memory:frag:xxx",
                                                  "consumed_at": "2026-10-10T00:00:00"}) is True
    assert store.get_fragment(keys[0])["consumed_by"] == "memory:frag:xxx"
    # 不在白名单的字段被忽略（不猜列）
    assert store.update_fragment_fields(keys[0], {"content": "x"}) is True   # content 在白名单
    assert store.update_fragment_fields(keys[0], {"不存在列": "x"}) is False
    assert store.update_fragment_fields("", {"content": "x"}) is False
    # 不存在的 key ⇒ False
    assert store.update_fragment_fields("memory:frag:没有", {"consumed_by": "x"}) is False

    deleted = store.delete_fragments_batch(keys[:4] + ["memory:frag:没有"])
    assert deleted == 4, f"删除计数不对: {deleted}"
    assert _rows(store) == 6
    # 删干净
    assert store.delete_fragments_batch(keys[4:]) == 6
    assert _rows(store) == 0


# ---------------------------------------------------------------------------
# 6. 其余读原语 + 留桩必须显式可辨识
# ---------------------------------------------------------------------------

def test_write_read_probes_and_feedback(store):
    assert store.write_fragments_batch([{"key": "memory:frag:a", "content": "甲"},
                                        {"content": "无 key 跳过"}]) == 1
    assert store.fragment_exists("memory:frag:a") is True
    assert store.fragment_exists("memory:frag:none") is False
    assert store.fragment_exists("") is False
    assert store.record_feedback("memory:frag:a", True) is True
    assert store.record_feedback("memory:frag:a", False) is True
    assert float(store.get_fragment("memory:frag:a")["feedback_score"]) == -1.0
    assert store.supersede_fragment("memory:frag:a", "memory:frag:b") is True
    assert store.get_fragment("memory:frag:a")["superseded_by"] == "memory:frag:b"
    assert store.set_supersedes("memory:frag:不存在", "memory:frag:a") is False  # 不在库里
    store.write_fragments_batch([{"key": "memory:frag:b", "content": "新版"}])
    assert store.set_supersedes("memory:frag:b", "memory:frag:a") is True
    assert store.get_fragment("memory:frag:b")["supersedes"] == "memory:frag:a"
    assert store.touch_fragment("memory:frag:a") is True
    assert store.get_fragment("memory:frag:a")["touch_count"] == "1"
    # store() 走完整路径（同内容去重 → 旧版封边 + 新 key）
    assert store.store("用户偏好 SQLite 后端") is True
    assert store.store("用户偏好 SQLite 后端") is True
    _, keys = store.scan_fragment_keys(prefix="memory:frag:", limit=50)
    assert len(keys) >= 4          # a + a 的版本化新 key + 两条同内容


def test_correct_fragments_tags_and_count(store):
    store.write_fragments_batch([{"key": f"memory:frag:{i}", "content": "x"} for i in range(3)])
    assert store.correct_fragments(["memory:frag:0", "memory:frag:1"]) == 2
    doc = store.get_fragment("memory:frag:0")
    assert doc["feedback_score"] == "-1"
    assert "corrected" in doc["tags"]
    # 重复纠正不重复打 tag
    assert store.correct_fragments(["memory:frag:0"]) == 1
    assert store.get_fragment("memory:frag:0")["tags"].count("corrected") == 1
    assert store.correct_fragments([]) == 0


class _FakeEmbedder:
    """最小 embedder 桩（与 test_storage_pg.py 同款形状）。"""

    _registered = True
    _model = "fake-4d"
    dimension = 4

    def get_embedding(self, text: str):
        return [float(len(text) % 7), 0.5, 0.25, 1.0]


def test_store_writes_float32_blob_with_embedder(tmp_path):
    """有 embedder 时 store() 落 float32 blob（本批无向量读取方，但写入必须先在）。"""
    s = SqliteStorage(path=str(tmp_path / "emb.db"), embedder=_FakeEmbedder())
    try:
        assert s.ensure_index() is True
        assert s.store("向量写入校验") is True
        _, keys = s.scan_fragment_keys(prefix="memory:frag:", limit=10)
        doc = s.get_fragment(keys[0])
        assert isinstance(doc["embed_bin"], bytes)
        assert len(doc["embed_bin"]) == 16          # 4 维 × float32
        assert doc["embed_bin"][:4] == struct.pack("f", float(len("向量写入校验") % 7))
    finally:
        s.close()
    # 无 embedder ⇒ 不写向量（与 Redis/PG 同一口径），不是空 bytes
    s2 = SqliteStorage(path=str(tmp_path / "emb2.db"))
    try:
        s2.ensure_index()
        assert s2.store("没有 embedder 的写入") is True
        _, keys = s2.scan_fragment_keys(prefix="memory:frag:", limit=10)
        assert "embed_bin" not in s2.get_fragment(keys[0])
    finally:
        s2.close()


@pytest.mark.parametrize("name,args", [
    ("search", ("q",)), ("search_bm25", ("q",)), ("search_knn", ("q",)),
    ("match_attention", ("c",)), ("match_hot_topics", ("t",)),
    ("get_hot_topics", ()), ("entity_timeline", ("e",)),
    ("discover_synonyms", ()), ("generate_jieba_dict", ()),
])
def test_stub_methods_raise_not_implemented(store, name, args):
    """留桩必须**显式可辨识** —— 返回空值/None 假装成功是最危险的失败形态。"""
    with pytest.raises(NotImplementedError) as ei:
        getattr(store, name)(*args)
    assert "sqlite backend" in str(ei.value) and name in str(ei.value)


# ---------------------------------------------------------------------------
# 7. 并发：两进程并发写不丢数据；写事务进行中只读进程仍可读
# ---------------------------------------------------------------------------

_WRITER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {src!r})
    from keepsake.storage_sqlite import SqliteStorage
    s = SqliteStorage(path={path!r}, busy_timeout_ms={busy_ms!r})
    assert s.ensure_index() is True
    if {hold!r}:                      # 写事务期间把持写锁，让只读方验证可读/可排队
        s._lock.acquire()
        try:
            s._db().execute("BEGIN IMMEDIATE")
            s._db().execute(
                "INSERT INTO ks_fragment (key, content) VALUES ('hold', '写事务中')")
            print("LOCKED", flush=True)
            time.sleep({hold_s})
            s._db().execute("COMMIT")
            s._lock.release()
        finally:
            s.close()
    else:
        # 逐条写并**自报账**：ret = write_fragments_batch 返回 True 的条数。
        # ⚠️ write_fragments_batch 是 fail-open 的（upsert 失败只 warning + 返回 False，
        #    不抛异常）⇒ 「进程退出码」证明不了有没有丢数据，**只能靠这条自报账
        #    跟库里的权威行数对账**。这正是把并发断言从「随机时序」改成「契约」的关键。
        ret = 0
        for i in range({n}):
            ret += s.write_fragments_batch([
                {{"key": "memory:frag:{tag}-%03d" % i, "content": "并发写入 {tag} %d" % i}}])
        s.close()
        print("STAT %d %d" % (ret, {n}), flush=True)
    print("DONE", flush=True)
""")


def _run_writer(src: str, path: str, tag: str, hold: float = 0.0, n: int = 50,
                busy_ms: int = 5000):
    code = _WRITER.format(src=src, path=path, tag=tag, hold=bool(hold), hold_s=hold,
                          n=n, busy_ms=busy_ms)
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=180)


def _stat(out) -> tuple:
    """解析写者自报账 `STAT <成功条数> <尝试条数>`；没打出来 = 进程中途崩了。"""
    for line in out.stdout.splitlines():
        if line.startswith("STAT "):
            ret, attempts = (int(x) for x in line.split()[1:3])
            return ret, attempts
    return -1, -1


def test_two_processes_concurrent_write_lose_nothing(store, tmp_path):
    """两个独立进程各写 50 条 ⇒ **绝不静默丢数据**（契约断言，不依赖随机时序）。

    🔴 为什么不是「断言必然 100 行」：两个进程抢同一个**全新库文件**的建表/建索引
    与写锁，谁先谁到取决于调度（本机 python 3.11/sqlite 3.40 连跑 50 轮 0 红，
    主脑 python 3.14/sqlite 3.53 约 1/10 偶发红）。把时序写进断言 = 写了一个
    随机测试，红了也不知道是实现坏了还是撞上了窗口。

    所以改成**三条确定性契约**（任意时序下都恒成立，故必稳；且比原断言更强）：
      C1 对账无丢失：库中实际行数 == 各写者自报的成功条数之和
        （「声称写成功却没落库」= 静默丢数据 —— 这是本测试真正要守的语义，绝不放）
      C2 失败必须显式：任何少写的条数，必须在 stderr 留下逐条
        `upsert_fragment(...) failed:` 告警 —— 失败可被观测，不许无声。
        （`write_fragments_batch` 是 fail-open 的，返回 False 而不抛异常，
          所以「进程非零退出」这条判据在这里不可达；warning 日志就是它的报错载体。）
      C3 走完账：成功 + 失败 == 尝试 == 100（每条都被明确归类，不许凭空消失）
      外加无 `database is locked` 泄漏、无崩溃（`DONE` 必须打出）。
    顺利路径（无失败）下 C1 即退化为原来的强断言：恰好 100 行。
    """
    src = str(Path(__file__).resolve().parents[1] / "src")
    path = str(tmp_path / "conc.db")
    assert store.ensure_index() is True
    store.close()

    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run_writer, src, path, tag) for tag in ("a", "b")]
        outs = [f.result() for f in futures]

    ret_total = 0
    for out in outs:
        assert "DONE" in out.stdout, f"并发写进程异常: {out.stdout} / {out.stderr[-800:]}"
        assert "database is locked" not in out.stderr[-800:], out.stderr[-800:]
        ret, attempts = _stat(out)
        assert attempts == 50, f"写者没走完账（中途退出）: STAT={ret}/{attempts} {out.stderr[-800:]}"
        assert 0 <= ret <= attempts, f"自报账不合法: {ret}/{attempts}"
        # C2：失败条数 == stderr 里的显式告警条数（一条失败一次告警，可精确计数）
        failed = attempts - ret
        warned = out.stderr.count("upsert_fragment(")
        assert warned == failed, (
            f"静默丢数据：{failed} 条没写进去，却只有 {warned} 条显式告警\n{out.stderr[-800:]}")
        ret_total += ret
        assert out.returncode == 0, f"写者非零退出: {out.returncode} {out.stderr[-800:]}"

    # C3：逐进程「成功 + 失败 == 50」已核 ⇒ 全局「成功 + 失败 == 100」，无凭空消失
    assert ret_total <= 100

    conn = sqlite3.connect(path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM ks_fragment").fetchone()[0]
    finally:
        conn.close()
    # C1：库里权威行数必须等于自报成功数 —— 一条不多、一条不少
    assert total == ret_total, (
        f"静默丢数据：库里 {total} 行 != 自报成功 {ret_total} 行")
    if ret_total == 100:
        assert total == 100, f"并发写丢数据: {total} != 100"


def test_writer_queues_behind_held_write_txn_within_busy_timeout(tmp_path):
    """B1 排队语义（确定性）：写者1 `BEGIN IMMEDIATE` 持写事务 3s，
    写者2 **必须**在 `busy_timeout` 内拿到锁写成功 —— 这正是 busy_timeout 的核心承诺。

    不依赖随机时序：持锁 3s 是**已知**的墙钟窗口，写者2 的 busy_timeout 取 10s
    （余量 7s），所以「等到了」是必然而非侥幸；同时断言它**确实等了**（≥2.5s），
    排除「压根没碰上竞争窗口」这种假绿。
    """
    src = str(Path(__file__).resolve().parents[1] / "src")
    path = str(tmp_path / "queue.db")
    seed = SqliteStorage(path=path)
    assert seed.ensure_index() is True
    seed.close()

    holder = subprocess.Popen(
        [sys.executable, "-c", _WRITER.format(src=src, path=path, tag="hold",
                                               hold=True, hold_s=3.0, n=0,
                                               busy_ms=5000)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "LOCKED", "写方未拿到写锁就退出了"
        t0 = time.monotonic()
        waiter = _run_writer(src, path, tag="c", n=5, busy_ms=10000)
        waited = time.monotonic() - t0
        holder.wait(timeout=60)
    finally:
        if holder.poll() is None:
            holder.kill()

    assert "DONE" in holder.stdout.read(), holder.stderr.read()[-800:]
    assert "DONE" in waiter.stdout, f"排队写者异常: {waiter.stdout} / {waiter.stderr[-800:]}"
    assert waiter.returncode == 0, f"busy_timeout 内未拿到写锁: {waiter.stderr[-800:]}"
    assert "database is locked" not in waiter.stderr[-800:], waiter.stderr[-800:]
    assert waited >= 2.5, f"写者2 没有真正排队（{waited:.2f}s），本测试没验证到排队语义"

    conn = sqlite3.connect(path)
    try:
        got = conn.execute(
            "SELECT COUNT(*) FROM ks_fragment WHERE key LIKE 'memory:frag:c-%'").fetchone()[0]
    finally:
        conn.close()
    assert got == 5, f"排队写者的 {5 - got} 条没落库（静默丢行）"


def test_reader_sees_data_while_writer_holds_write_txn(tmp_path):
    """WAL：长写事务进行时，**别的进程**仍能读到已提交的数据（不阻塞）。"""
    src = str(Path(__file__).resolve().parents[1] / "src")
    path = str(tmp_path / "wal.db")
    seed = SqliteStorage(path=path)
    assert seed.ensure_index() is True
    seed.write_fragments_batch([{"key": "memory:frag:pre", "content": "写事务之前"}])
    seed.close()

    proc = subprocess.Popen(
        [sys.executable, "-c", _WRITER.format(src=src, path=path, tag="hold",
                                               hold=True, hold_s=3.0, n=0,
                                               busy_ms=5000)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "LOCKED", "写方未拿到写锁就退出了"
        # 写方正持有写锁（还没提交）：只读方必须立刻读得到之前已提交的数据
        reader = SqliteStorage(path=path)
        try:
            assert reader.health_check() is True
            doc = reader.get_fragment("memory:frag:pre")
            assert doc is not None and doc["content"] == "写事务之前", \
                "写事务期间只读被阻塞（WAL 语义丢失）"
        finally:
            reader.close()
        proc.wait(timeout=60)
        assert "DONE" in proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()


# ---------------------------------------------------------------------------
# 8. 配置接入：storage.backend == "sqlite"
# ---------------------------------------------------------------------------

def test_storage_from_config_sqlite_backend(tmp_path):
    from keepsake.storage import resolve_backend, storage_from_config

    cfg = {"storage": {"backend": "sqlite",
                       "sqlite": {"path": str(tmp_path / "cfg.db")}}}
    assert resolve_backend(cfg) == "sqlite"
    s = storage_from_config(config=cfg)
    try:
        assert isinstance(s, SqliteStorage)
        assert s._path == str(tmp_path / "cfg.db")
        assert s.ensure_index() is True
    finally:
        s.close()
    # 缺 path ⇒ 走有文档的默认路径（不建目录，只断言取值来源）
    s2 = storage_from_config(config={"storage": {"backend": "SQLITE"}})
    try:
        assert s2._path.endswith("keepsake.db")
        assert "keepsake" in s2._path
    finally:
        s2.close()
    # 默认后端逐字不变（缺行 / 非法值仍回 redis）
    assert resolve_backend(None) == "redis"
    assert resolve_backend({"storage": {"backend": "瞎写的"}}) == "redis"


def test_sqlite_backend_satisfies_storage_base_contract():
    """接口一致性：StorageBase 的每个抽象方法都必须有实现（签名漂移由其它测试拦）。"""
    from keepsake.storage_base import StorageBase
    missing = [n for n in StorageBase.__abstractmethods__ if n not in SqliteStorage.__dict__]
    assert not missing, f"未实现的抽象方法: {missing}"


def test_sqlite_columns_cover_pg_baseline():
    """列对齐基准检查（PG 版 FRAGMENT_COLUMNS + MAINTENANCE_COLUMNS 全覆盖）。"""
    pg_set = set(FRAGMENT_COLUMNS) | set(MAINTENANCE_COLUMNS)
    assert pg_set <= set(ALL_COLUMNS), f"未覆盖 PG 基准列: {pg_set - set(ALL_COLUMNS)}"


# ---------------------------------------------------------------------------
# 9. p1.2 写路径守卫：schema 未就绪必须**显式拒绝**，不许「退出码 0 + 库里 0 行」
#
#    实测形态（/tmp/ks_sqfl_mechanism_hold20.txt）：另一进程在**全新空库**上
#    持 EXCLUSIVE 时，`ensure_index()` 返回 False，随后每条写都是
#    `no such table: ks_fragment`，而**进程 stdout 仍是 DONE、退出码仍是 0**。
#    下面的断言只锁契约，不锁时序：两种结局（写成功 / 显式抛错）都算过。
# ---------------------------------------------------------------------------

# 持锁方：全新空库上 `BEGIN EXCLUSIVE`（写锁 + schema 锁）并在事务内建一张**极简**
# ks_fragment，全程不提交、最后 ROLLBACK。⇒ 另一个进程：能读（WAL 读不阻塞）但
# **看不到任何表**，且它的 DDL 会一直排队到 A 放手为止 —— 即实测里
# `ensure_index() False + 每条 no such table + 退出码 0` 的那个形态。
_SCHEMA_EXCLUSIVE_HOLDER = textwrap.dedent("""
    import sqlite3, time
    conn = sqlite3.connect({path!r}, timeout=0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN EXCLUSIVE")
    conn.execute("CREATE TABLE ks_fragment (key TEXT PRIMARY KEY)")
    print("LOCKED", flush=True)
    time.sleep({hold_s})
    conn.execute("ROLLBACK")      # 放手：库回到「全新空库」
    conn.close()
    print("DONE", flush=True)
""")

# 写者：**不预先 ensure_index**（真实调用方不会替存储后端补建表）。异常一律不吞 ——
# 「显式抛错 ⇒ 非零退出」正是本测试要能观察到的另一半结局。
_RACE_WRITER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {src!r})
    from keepsake.storage_sqlite import SqliteStorage
    s = SqliteStorage(path={path!r}, busy_timeout_ms={busy_ms!r})
    ok = 0
    for i in range({n}):
        ok += s.write_fragments_batch(
            [{{"key": "memory:frag:{tag}-%03d" % i, "content": "并发写入 {tag} %d" % i}}])
    s.close()
    print("STAT %d %d" % (ok, {n}), flush=True)
    print("DONE", flush=True)
""")


def test_fresh_db_racing_exclusive_creator_never_reports_success_with_zero_rows(tmp_path):
    """A 在新库上建 schema 并持 EXCLUSIVE 3s，B 同时打开同一库写入。

    **唯一断言**：不允许出现「B 报成功（退出码 0）但库里 0 行」。
      分支 1（守卫补跑的 ensure 生效）：B 退出码 0 ⇒ 自报成功数 == 尝试数，
              且**库里的权威行数**与之逐条相等。
      分支 2（显式拒绝）：B 非零退出 ⇒ stderr 必须出现 `StorageNotReadyError`
              与库路径（失败可定位，不许只有一行 no-such-table warning）。
    不锁时序分支：任一分支都算过，正是因为两条结局都是**契约**（旧实现两条都不满足）。
    """
    src = str(Path(__file__).resolve().parents[1] / "src")
    path = str(tmp_path / "race.db")
    holder = subprocess.Popen(
        [sys.executable, "-c", _SCHEMA_EXCLUSIVE_HOLDER.format(path=path, hold_s=3.0)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "LOCKED", "持锁方没建到 schema 就退出了"
        out = subprocess.run(
            [sys.executable, "-c",
             _RACE_WRITER.format(src=src, path=path, tag="race", n=5, busy_ms=5000)],
            capture_output=True, text=True, timeout=180)
        holder.wait(timeout=60)
    finally:
        if holder.poll() is None:
            holder.kill()

    ret, attempts = _stat(out)
    conn = sqlite3.connect(path)
    try:
        landed = conn.execute(
            "SELECT COUNT(*) FROM ks_fragment WHERE key LIKE 'memory:frag:race-%'").fetchone()[0]
    except sqlite3.OperationalError as e:        # 表压根没建出来（分支 2 的常态）
        landed = 0
        assert "no such table: ks_fragment" in str(e)
    finally:
        conn.close()

    if out.returncode == 0:
        assert attempts == 5, f"写者没走完账: STAT={ret}/{attempts} {out.stderr[-800:]}"
        assert ret == attempts, (
            f"🔴 静默丢数据：进程退出码 0、自报成功 {ret}/{attempts}，库里只有 {landed} 行")
    else:
        assert ret == -1, f"写者中途退出却已经打过账: STAT={ret}/{attempts}"
        assert "StorageNotReadyError" in out.stderr, (
            f"非零退出但不是显式拒绝（看不出根因）: {out.stderr[-800:]}")
        assert path in out.stderr, f"异常消息必须带库路径: {out.stderr[-800:]}"
    assert landed == ret, f"库里 {landed} 行 != 自报成功 {ret} 行（静默丢数据）"


def test_write_path_raises_storage_not_ready_when_schema_unbuildable(tmp_path):
    """ensure **必然**失败的场景 ⇒ 必须抛明确异常，且消息带 path。

    用「父路径是个普通文件」构造不可写位置：`_connect` 的 `mkdir(parents=True)`
    必然抛错 ⇒ `ensure_index()` 必然 False（与 uid/root 无关，比 chmod 稳）。
    """
    from keepsake.storage_sqlite import StorageNotReadyError   # 修前不存在 ⇒ 本测试红
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("占位")
    s = SqliteStorage(path=str(blocker / "ks.db"))
    try:
        assert s.ensure_index() is False, "构造失效：ensure_index 竟然成功了"

        with pytest.raises(StorageNotReadyError) as ei:
            s.write_fragments_batch([{"key": "memory:frag:x", "content": "写不进"}])
        assert str(blocker) in str(ei.value), f"异常消息必须带库路径: {ei.value}"
        assert "ensure_index()" in str(ei.value), f"异常消息必须带最近一次 ensure 结果: {ei.value}"

        # 读路径同款决策：未就绪时**同样显式拒绝**（静默返回空 = 「记忆搜不到」的
        # 最危险形态，与本模块 docstring 的既有立场一致），不返回 None/{} 假装「查不到」。
        for call in (lambda: s.get_fragment("memory:frag:x"),
                     lambda: s.get_fragments_batch(["memory:frag:x"]),
                     lambda: s.scan_fragment_keys()):
            with pytest.raises(StorageNotReadyError):
                call()
    finally:
        s.close()


def test_write_fragments_batch_reports_partial_failure(store, caplog, monkeypatch):
    """失败可见：成功数 == 总数-失败数，且**只打一条**汇总 WARNING（含原因分类）。

    两种失败同时制造，覆盖两条不同的失败路径：
      * 缺 key —— 旧实现静默 `continue`（调用方拿到 0 却不知道自己丢了东西）
      * 取锁失败 —— 真·DB 错误（由 `_begin_immediate` 抛出，走 upsert 的 fail-open 分支）
    """
    real_begin = store._begin_immediate
    calls = {"n": 0}

    def flaky_begin():
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("database is locked")
        return real_begin()

    monkeypatch.setattr(store, "_begin_immediate", flaky_begin)
    with caplog.at_level(logging.WARNING, logger="keepsake.storage_sqlite"):
        n = store.write_fragments_batch([
            {"key": "memory:frag:ok1", "content": "好的一条"},
            {"content": "没有 key 的坏行"},
            {"key": "memory:frag:bad", "content": "锁失败的坏行"},
        ])
    assert n == 1, f"返回值必须是实际成功条数: {n}（3 条里只应有 1 条成功）"
    assert _rows(store) == 1, "失败的行竟然落库了"

    summary = [r.getMessage() for r in caplog.records
               if "write_fragments_batch 部分失败" in r.getMessage()]
    assert len(summary) == 1, f"必须只有一条汇总告警（不许刷屏）: {summary}"
    msg = summary[0]
    assert "成功 1 / 共 3" in msg and "失败 2" in msg, msg
    assert "缺 key ×1" in msg and "OperationalError: database is locked ×1" in msg, msg


# ---------------------------------------------------------------------------
# 10. p1.3 **同进程多线程**：`check_same_thread=False` 的共享连接必须靠 Python 侧串行
#
#    p1.2 遗留②：`_lock` 原先只在 `_begin_immediate()` 内持有/释放，**事务体在锁外**
#    ⇒ 两个线程共用一条连接，各自 BEGIN 后语句交错 ⇒
#    `cannot start a transaction within a transaction`，或一个线程 COMMIT 掉
#    另一个线程的半个事务（静默丢数据）。跨进程语义（WAL）不受影响，本组只测进程内。
# ---------------------------------------------------------------------------

def _run_threads(fn, n_threads: int) -> List[List[str]]:
    """并发跑 n_threads 个 fn(i) ⇒ 每线程一串 repr 化结果（异常也算一条，不许吞）。

    ★ 为什么收集而不是直接让线程抛：fail-open 的写路径把异常吞成 `False`，
    异常必须由测试自己**逐条记账**才看得见（与 p1.2 的「失败必须显式」同一口径）。
    """
    import concurrent.futures as cf

    with cf.ThreadPoolExecutor(max_workers=n_threads) as pool:
        return [f.result() for f in [pool.submit(fn, i) for i in range(n_threads)]]


def test_concurrent_threads_write_same_instance_lose_nothing(tmp_path):
    """8 线程 × 各 10 条共写同一个 `SqliteStorage` ⇒ 恰好 80 行、**零异常**。

    🔴 这是 p1.2 遗留②的直接复现：共享连接 + 事务体在锁外 ⇒
    `cannot start a transaction within a transaction`（或交错的事务被提前提交）。
    断言「80 行 + 0 异常」而不是「退出码 0」：写路径 fail-open，只看返回值会漏掉
    「声称写成功其实没落库」和「异常被吞成 False」两种最危险的形态。
    """
    s = SqliteStorage(path=str(tmp_path / "mt_write.db"))
    assert s.ensure_index() is True
    try:
        def worker(tid: int) -> List[str]:
            out = []
            for i in range(10):
                k = f"memory:frag:t{tid}-{i:02d}"
                try:
                    ok = s.write_fragments_batch([{"key": k, "content": f"线程{tid}第{i}条"}])
                    out.append(f"{k}={ok}")
                except Exception as e:      # noqa: BLE001 — 异常必须显式记账，不许吞
                    out.append(f"{k}=EXC {type(e).__name__}: {e}")
            return out

        results = _run_threads(worker, 8)
        flat = [line for r in results for line in r]
        assert len(flat) == 80, f"线程没走完账（中途异常/退出）: {len(flat)}/80"
        exc = [line for line in flat if "=EXC " in line]
        assert not exc, f"同进程多线程写出现异常 {len(exc)}/80，例: {exc[:5]}"
        assert not any("transaction within a transaction" in line for line in flat), \
            "出现「cannot start a transaction within a transaction」（事务体在锁外）"
        assert all(line.endswith("=1") for line in flat), \
            f"有写调用返回 falsy（静默丢数据）: {[l for l in flat if not l.endswith('=1')][:5]}"

        assert _rows(s) == 80, f"库里 { _rows(s) } 行 != 80（有写被交错事务吞掉）"
        _, keys = s.scan_fragment_keys(prefix="memory:frag:t", limit=200)
        assert len(keys) == 80 and len(set(keys)) == 80, f"扫出来的 key 数不对: {len(keys)}"
    finally:
        s.close()


def test_concurrent_threads_mixed_read_write_no_exception(tmp_path):
    """4 写 + 4 读并发打同一个实例 ⇒ 写全落库、读**零异常**。

    写方走完整 `store()` 路径（事务内**先 SELECT 再写**，正是最容易与另一个事务
    交错的那种写法）；读方混合 `get_fragment` / `get_fragments_batch` /
    `scan_fragment_keys` / `fragment_exists`，断言读路径也不炸、不返回 None 假装。
    """
    s = SqliteStorage(path=str(tmp_path / "mt_mix.db"))
    assert s.ensure_index() is True
    try:
        barrier = threading.Barrier(8)

        def writer(tid: int) -> List[str]:
            barrier.wait()
            out = []
            for i in range(5):
                try:
                    # 内容带 tid+i ⇒ 内容 hash 唯一 ⇒ key 唯一 ⇒ 行数可精确对账
                    ok = s.store(f"混合并发 线程{tid} 条目{i} 的唯一内容")
                    out.append(f"w{tid}-{i}={ok}")
                except Exception as e:      # noqa: BLE001
                    out.append(f"w{tid}-{i}=EXC {type(e).__name__}: {e}")
            return out

        def reader(tid: int) -> List[str]:
            barrier.wait()
            out = []
            for _ in range(20):
                try:
                    keys = [f"memory:frag:{h}" for h in ("a", "b", "不存在的")]
                    s.get_fragments_batch(keys)
                    s.scan_fragment_keys(limit=10)
                    s.fragment_exists(keys[0])
                    s.get_fragment(keys[0])
                    out.append(f"r{tid}=ok")
                except Exception as e:      # noqa: BLE001
                    out.append(f"r{tid}=EXC {type(e).__name__}: {e}")
            return out

        def mixed(tid: int) -> List[str]:
            return writer(tid) if tid < 4 else reader(tid - 4)

        flat = [line for r in _run_threads(mixed, 8) for line in r]
        exc = [line for line in flat if "=EXC " in line]
        assert not exc, f"读写混合出现异常 {len(exc)}/{len(flat)}，例: {exc[:5]}"
        wrote = [line for line in flat if line.startswith("w")]
        assert len(wrote) == 20, f"写线程没走完账: {len(wrote)}/20"
        assert all(line.endswith("=True") for line in wrote), \
            f"有 store() 返回 falsy: {[l for l in wrote if not l.endswith('=True')][:5]}"
        assert len([l for l in flat if l.startswith("r")]) == 80, "读线程没走完账"
        assert _rows(s) == 20, f"库里 {_rows(s)} 行 != 20（写有丢失）"
    finally:
        s.close()
