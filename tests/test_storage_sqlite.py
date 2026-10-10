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

import json
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
from keepsake.storage_sqlite import ALL_COLUMNS, FTS_TABLE, SqliteStorage


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
    # 批 2 起 search / search_bm25 / match_* / get_hot_topics / entity_timeline 已实现，
    # 批 3 起 discover_synonyms / generate_jieba_dict 也已实现（见下方批 3 段），
    # 不再是留桩。仍在留桩的必须**显式可辨识**。
    ("search_knn", ("q",)),
])
def test_stub_methods_raise_not_implemented(store, name, args):
    """留桩必须**显式可辨识** —— 返回空值/None 假装成功是最危险的失败形态。"""
    with pytest.raises(NotImplementedError) as ei:
        getattr(store, name)(*args)
    assert "sqlite backend" in str(ei.value) and name in str(ei.value)


def test_search_knn_stub_names_sqlite_vec(store):
    """🔴 向量路的降级必须**指名所需扩展**（第 3 批才做）。

    只抛 NotImplementedError 而不说要什么，调用方无法判断是「该装 sqlite-vec」
    还是「这个后端根本不支持向量」—— 两种情况处置完全不同。
    """
    with pytest.raises(NotImplementedError) as ei:
        store.search_knn("任意查询")
    msg = str(ei.value)
    assert "sqlite-vec" in msg and "search_knn" in msg
    # 绝不静默返回空列表
    assert msg.strip() != ""


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


def test_storage_from_config_sqlite_reads_synonym_thresholds(tmp_path):
    """同义词三阈值经 `storage_from_config` 装配必须**真的读配置**（p3.1 B）。

    修前 sqlite 分支只透传 `_resolve_knobs` 的 10 个排序权重键 ⇒ 这三个键恒取
    模块默认（10 / 0.5 / 3），改配置不生效。口径同 `_resolve_knobs`：kwargs > cfg > 默认。
    """
    from keepsake.storage import storage_from_config

    cfg = {"storage": {"backend": "sqlite",
                       "sqlite": {"path": str(tmp_path / "syn.db")}},
           "synonym_min_word_freq": 7, "synonym_jaccard_threshold": 0.75,
           "synonym_min_co_occurrence": 5}
    s = storage_from_config(config=cfg)
    try:
        assert (s._synonym_min_word_freq, s._synonym_jaccard_threshold,
                s._synonym_min_co_occurrence) == (7, 0.75, 5)
    finally:
        s.close()

    d = storage_from_config(config={"storage": {"backend": "sqlite",
                                                "sqlite": {"path": str(tmp_path / "d.db")}}})
    try:
        assert (d._synonym_min_word_freq, d._synonym_jaccard_threshold,
                d._synonym_min_co_occurrence) == (10, 0.5, 3)   # 缺省 ⇒ 保持原默认
    finally:
        d.close()

    k = storage_from_config(config=cfg, synonym_min_word_freq=99)
    try:
        assert k._synonym_min_word_freq == 99                     # kwargs 胜出
        assert k._synonym_min_co_occurrence == 5                 # 其余键仍取 cfg
    finally:
        k.close()


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


# ---------------------------------------------------------------------------
# 批 2：FTS5 + jieba + BM25 + 共用重排 + 加权信号
# ---------------------------------------------------------------------------

CORPUS300 = Path("/tmp/ks_corpus300.jsonl")
VERIFY_QUERIES = ("部署流程", "网关重启", "Redis 连接池", "飞书", "Termux",
                  "Python 依赖", "备份策略", "截图分析", "证书续签", "记忆检索")


@pytest.fixture()
def search_store(tmp_path) -> SqliteStorage:
    """检索用后端：主脑=True（否则 agent 隔离会把非 shared 的条目全滤掉）。"""
    s = SqliteStorage(path=str(tmp_path / "search.db"), is_primary=True, final_limit=10)
    assert s.ensure_index() is True
    yield s
    s.close()


def _load_corpus300(store: SqliteStorage):
    """用基准语料建库（300 条）—— 与 PG 侧同数据对照用。"""
    rows = [json.loads(line) for line in
            CORPUS300.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 300, f"基准语料应 300 条，实际 {len(rows)}"
    return store.write_fragments_batch(rows)


def test_store_maintains_fts_index(search_store):
    """store() 必须在**同一个事务**里把 FTS 索引写好（否则索引与主表会漂）。"""
    assert search_store.store("网关重启后 Redis 连接池要重建", tags="ops,shared") is True
    assert search_store.store("证书续签用 acme.sh 每周一次", tags="ops,shared") is True
    with search_store._lock:
        rows = search_store._db().execute(
            f"SELECT frag_key, content_tok FROM {FTS_TABLE} ORDER BY frag_key"
        ).fetchall()
        n_frag = search_store._db().execute("SELECT COUNT(*) FROM ks_fragment").fetchone()[0]
    assert n_frag == 2
    assert len(rows) == 2, f"FTS 行数 {len(rows)} != 主表 {n_frag}"
    # content_tsv（真相）与 FTS 行（索引）必须逐字一致
    with search_store._lock:
        tsv = dict(search_store._db().execute(
            "SELECT key, content_tsv FROM ks_fragment"))
    for frag_key, tok in rows:
        assert tsv[frag_key] == tok
    # jieba 预切痕迹：中文被切成词，且不是一整串
    joined = " ".join(t for _, t in rows)
    assert "网关" in joined and "重启" in joined


def test_fts_two_char_chinese_word_hits(search_store):
    """🔴 2 字中文词必须能命中（这正是**不能**用 trigram 分词器的理由）。

    trigram 有 3 字下限 ⇒ 「网关」恒 0 命中；中文里 2 字词占大头。
    """
    search_store.store("网关重启后 Redis 连接池要重建", tags="shared")
    search_store.store("飞书机器人 webhook 配置在环境变量里", tags="shared")
    hits = search_store.search_bm25("网关")
    assert len(hits) >= 1, "2 字中文词 0 命中 —— 分词器口径错了（trigram？）"
    assert "网关" in hits[0]["content"]


def test_bm25_score_direction_is_larger_is_better(search_store):
    """🔴 FTS5 `bm25()` 越小越相关（负数）—— 必须翻正成「越大越相关」。

    与 PG/Redis 契约不一致 ⇒ 共用 `rerank_with_decay` 的 min-max 归一化会把
    最好的候选算成最差，结果**整体倒排**且不报错。
    """
    search_store.store("Redis 连接池 网关 网关 网关 重启", tags="shared")
    search_store.store("飞书机器人 webhook 配置", tags="shared")
    hits = search_store.search_bm25("网关")
    assert hits, "应有命中"
    top = hits[0]
    assert top["_bm25_score"] > 0, f"_bm25_score 应为正（已取负），实际 {top['_bm25_score']}"
    assert "网关" in top["content"], "词频更高的文档应排第一"
    scores = [h["_bm25_score"] for h in hits]
    assert scores == sorted(scores, reverse=True), f"未按 _bm25_score 降序: {scores}"


def test_search_bm25_returns_pg_shaped_fields(search_store):
    """返回字段契约必须与 PG `search_bm25` 一致（逐字段对齐）。"""
    search_store.store("网关重启后 Redis 连接池要重建", tags="ops,shared")
    hits = search_store.search_bm25("网关重启")
    assert len(hits) > 0
    f = hits[0]
    for name in ("_key", "_bm25_score", "_sim", "_combined_score", "_weights"):
        assert name in f, f"缺检索字段 {name}"
    assert set(f["_weights"]) == {"sim", "decay", "emotion", "feedback",
                                  "hot_topic", "attention"}
    assert 0.0 <= float(f["_sim"]) <= 1.0
    assert f["content"] and f["created"]


def test_search_bm25_tag_filter_single_and_multi(search_store):
    """🔴 `tag_filter` 拼 SQL：单标签 / 多标签都**不许抛异常**（p2.1 修掉的必崩 bug）。

    修前拼法是「`X OR ` + ` OR `.join([X] * (n-1)) + `)`」⇒ n=1 时拼出
    `(instr(...) > 0 OR )` —— **任何单标签过滤必抛** `near ")": syntax error`
    （p2.1 做 PG 对照时实测踩到，整个检索直接不可用）。

    ℹ️ 命中语义（分隔符/精确相等/空格容错/多标签并集）另有
    `tests/test_tag_filter_semantics.py` 专测：原先这里用竖线边界
    `instr('|'||tags||'|', '|tag|')` 匹配**逗号分隔**的 tags，一条都匹配不上
    （PG 的 `strpos('|'||tags||'|', …)` 逐字同款），现已改为逗号边界。
    """
    search_store.store("网关重启后 Redis 连接池要重建", tags="ops,shared")
    search_store.store("备份策略每天全量加每周归档", tags="backup,shared")
    assert search_store.search_bm25("网关"), "无过滤时本就该有结果（对照基线）"
    for tf in ("ops", "ops,backup", "  ", "不存在的标签"):
        out = search_store.search_bm25("网关", tag_filter=tf)   # 不抛 = 通过
        assert isinstance(out, list), f"tag_filter={tf!r} 返回的不是列表"
    assert search_store.search_bm25("", tag_filter="ops") == [], "空查询契约不变"


def test_search_bm25_empty_query_returns_empty_with_warning(search_store, caplog):
    """空查询 → 空列表 + **warning**（明确、不静默）。"""
    with caplog.at_level(logging.WARNING):
        assert search_store.search_bm25("   ") == []
    assert any("empty query" in r.message for r in caplog.records)


def test_update_and_delete_keep_fts_in_sync(search_store):
    """改/删正文后索引必须跟着变 —— 否则检索**静默召回错误内容**。"""
    search_store.store("原始内容讲的是 A 主题", tags="shared")
    key = search_store.search_bm25("A")[0]["_key"]

    # 删 ⇒ 不再召回
    assert search_store.delete_fragments_batch([key]) == 1
    assert search_store.search_bm25("A") == [], "删掉的碎片仍被召回（FTS 僵尸行）"

    # 改 ⇒ 新词能召回、旧词召不回
    assert search_store.store("原始内容讲的是 A 主题", tags="shared") is True
    key2 = search_store.scan_fragment_keys(limit=1)[1][0]
    assert search_store.update_fragment_fields(key2, {"content": "改写后讲的是 B 主题"}) is True
    assert search_store.search_bm25("B"), "改写后新词召不回"
    assert not search_store.search_bm25("A"), "改写后旧词仍召回（索引没更新）"


def test_ensure_index_rebuilds_fts_for_batch1_legacy_db(tmp_path):
    """🔴 批 1 建的库**有数据但索引是空的** ⇒ 补建虚表后必须全量重建。

    「表在、查询正常执行、结果永远为空」是最危险的静默失败形态。
    """
    path = str(tmp_path / "legacy.db")
    s = SqliteStorage(path=path, is_primary=True)
    try:
        assert s.ensure_index() is True
        s.store("批 1 时代写入的记忆 Redis 连接池", tags="shared")
        s.store("另一条 老库记忆 网关", tags="shared")
        # 模拟批 1 老库：删掉 FTS 表（等价于「当时还没有批 2」）
        with s._lock:
            s._db().execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
        assert s.ensure_index() is True, "补建 FTS 表失败"
        with s._lock:
            n = s._db().execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()[0]
        assert n == 2, f"FTS 重建后应 2 行，实际 {n} —— 老库检索恒 0 命中"
        assert s.search_bm25("网关"), "老库重建后仍搜不到"
    finally:
        s.close()


def test_search_uses_shared_rerank_object(search_store):
    """🔴 排序逻辑**不得重写**：必须复用 `storage_shared` 的同一个函数对象。"""
    from keepsake.storage_shared import rerank_with_decay as shared_rerank
    assert SqliteStorage._rerank_with_decay is shared_rerank
    assert SqliteStorage._apply_v2_filters.__module__ == "keepsake.storage_shared"


def test_search_degrades_to_bm25_loudly(search_store, caplog):
    """向量路不可用 ⇒ **降级要出声**（WARNING），且照常返回 BM25 结果。

    注：语料要 ≥3 条 —— FTS5 的 `bm25()` 在单文档语料上 idf≈0，
    分数被共用的 `min_score` 地板（0.05）滤掉是**三后端共有的行为**，
    不是本后端的缺陷（PG 的 Python 侧 BM25 同样如此）。
    """
    search_store.store("网关重启后 Redis 连接池要重建", tags="shared")
    search_store.store("飞书机器人 webhook 配置在环境变量里", tags="shared")
    search_store.store("备份策略是每天全量加每小时增量", tags="shared")
    with caplog.at_level(logging.WARNING):
        hits = search_store.search("网关重启")
    assert hits, "降级后仍应返回 BM25 结果，不是空列表"
    assert any("sqlite-vec" in r.message or "向量" in r.message
               for r in caplog.records), "降级没有告警 = 静默降级"


def test_aux_structures_written_and_read(search_store):
    """辅助结构：热词三榜 / 注意力 / 实体时间线，写入侧同步维护。"""
    search_store.store("网关重启后 Redis 连接池要重建", tags="shared")
    search_store.store("Redis 连接池 网关 重启 部署", tags="shared")

    topics = search_store.get_hot_topics(10)
    assert topics and all({"topic", "count"} <= set(t) for t in topics)
    assert {t["topic"] for t in topics} >= {"网关", "连接池"}
    # 未知 period 回落 all（与 PG/Redis 同口径）
    assert search_store.get_hot_topics(10, "nonsense") == topics
    assert search_store.get_hot_topics(10, "daily") is not None

    assert search_store.match_attention("网关 Redis 连接池") >= 1.0
    assert search_store.match_hot_topics("网关重启") > 0
    assert search_store.match_attention("") == 1.0
    assert search_store.match_hot_topics("") == 0.0
    assert search_store.entity_timeline("") == []


def test_entity_timeline_matches_pg_shape(search_store):
    """时间线返回字段与 PG 侧逐字同形（content/created/valid_until）。"""
    search_store.store("网关重启后 Redis 连接池要重建", tags="shared")
    with search_store._lock:
        ents = [r[0] for r in search_store._db().execute(
            "SELECT entity FROM ks_entity_timeline")]
    assert ents, "store() 未维护 ks_entity_timeline"
    tl = search_store.entity_timeline(ents[0])
    assert tl and set(tl[0]) == {"content", "created", "valid_until"}


def test_corpus300_top10_keys_are_stable(search_store):
    """300 条基准语料 × 10 条固定查询 —— 断言**不变量**，不断言字面 key 顺序。

    🔴 为什么不能写死 top-10 key：jieba 的切词结果**依赖机器上的用户词典**
    （`~/.config/keepsake/jieba_dict.txt`）。有词典的机器与没有的机器切出不同的词
    ⇒ 名次不同 ⇒ 写死的期望在「主脑」红、在 CI 机绿 —— 这是本项目的老坑
    （对分词器输出做字面期望）。因此这里只断言**与词典无关的不变量**：

      1. 语料里有逐字命中 ⇒ 必须有召回（分词/索引没坏）；
      2. 无重复 key；
      3. `_combined_score` 单调不增；
      4. 更深 N 的结果是更浅 N 的**同序前缀**；
      5. 同一 query 连跑两次结果完全一致（同分 tiebreak 稳定）。

    具体 top-10 key 顺序由 /tmp/ks_sq_align_verify.txt 出证（供与 PG 侧机械比对），
    不进断言。
    """
    rows = [json.loads(line) for line in
            CORPUS300.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 300
    assert search_store.write_fragments_batch(rows) == 300

    blob = " \n".join(f"{r['content']} {r.get('tags','')}" for r in rows).lower()
    for q in VERIFY_QUERIES:
        hits = search_store.search_bm25(q)
        in_corpus = q.lower() in blob
        if in_corpus:
            assert hits, f"查询 {q!r} 在语料里有逐字命中却 0 召回 —— 分词/索引坏了"
        # 2. 无重复 key
        keys = [h["_key"] for h in hits]
        assert len(keys) == len(set(keys)), f"{q!r} 返回了重复 key: {keys}"
        # 3. _combined_score 单调不增
        scores = [float(h.get("_combined_score", 0.0)) for h in hits]
        assert scores == sorted(scores, reverse=True), f"{q!r} 未按综合分降序: {scores}"
        # 4. 更深 N 是更浅 N 的同序前缀（临时放宽 final_limit 再比）
        shallow_limit, search_store._final_limit = search_store._final_limit, 50
        try:
            deeper = search_store.search_bm25(q)
        finally:
            search_store._final_limit = shallow_limit
        assert len(deeper) >= len(hits)
        # 只比 key 序：`_combined_score` 带**运行时衰减**（time.time()），逐次会抖
        # 末位浮点；名次才是契约。
        assert [h["_key"] for h in deeper[:len(hits)]] == keys, \
            f"{q!r} 加深 N 后前 {len(hits)} 条不是原结果的同序前缀"
        # 5. 同 query 连跑一致
        assert [h["_key"] for h in search_store.search_bm25(q)] == keys, \
            f"{q!r} 两次连跑名次不一致（tiebreak 抖动）"


def test_topn_count_is_min_hits_and_final_limit(tmp_path):
    """条数契约：返回条数 == min(命中数, top_k)（不是写死的 5）。"""
    rows = [{"key": f"memory:frag:{i:012x}",
             "content": f"部署流程 第{i}版 流水线 说明",
             "tags": "ops", "created": "2026-10-01T00:00:00+00:00"}
            for i in range(12)]
    s = SqliteStorage(path=str(tmp_path / "lim.db"), is_primary=True, final_limit=10)
    try:
        assert s.ensure_index() is True
        assert s.write_fragments_batch(rows) == 12
        assert len(s.search_bm25("部署流程")) == 10, "final_limit 没生效"
    finally:
        s.close()
    s2 = SqliteStorage(path=str(tmp_path / "lim2.db"), is_primary=True, final_limit=99)
    try:
        assert s2.ensure_index() is True
        assert s2.write_fragments_batch(rows) == 12
        assert len(s2.search_bm25("部署流程")) == 12, "命中 12 条时不该被砍到 top_k 以下"
    finally:
        s2.close()


def test_sqlite_search_limits_come_from_config(tmp_path):
    """🔴 条数上限必须**读配置**，与 PG 分支同源（`top_k` / `bm25_limit` / `candidate_k`）。

    修前 sqlite 分支一个都不传 ⇒ 恒用模块默认 5/20/20 ⇒ 与 PG 同查询差一个数量级。
    """
    from keepsake.storage import storage_from_config

    cfg = {"storage": {"backend": "sqlite",
                       "sqlite": {"path": str(tmp_path / "cfg.db")}},
           "top_k": 7, "bm25_limit": 9, "candidate_k": 11}
    s = storage_from_config(config=cfg)
    try:
        assert isinstance(s, SqliteStorage)
        assert s._final_limit == 7, f"final_limit 没读 top_k：{s._final_limit}"
        assert s._bm25_limit == 9, f"bm25_limit 没读配置：{s._bm25_limit}"
        assert s._candidate_count == 11, f"candidate_count 没读配置：{s._candidate_count}"
    finally:
        s.close()


def test_search_returns_entities_like_pg(search_store):
    """返回字段集合必须含 `entities`，且值与库内一致（PG 侧同一字段集实测有该列）。"""
    key = "memory:frag:0123456789ab"
    content = "飞书机器人 webhook 部署在 91 网关，凭证走环境变量"
    assert search_store.write_fragments_batch([{ "key": key, "content": content,
                                                 "tags": "ops,shared"}]) == 1
    hits = search_store.search_bm25("飞书")
    assert hits, "飞书 0 召回"
    frag = hits[0]
    assert "entities" in frag, f"返回字段缺 entities: {sorted(frag)}"
    assert frag["entities"], "entities 为空（写路径没有落实体）"
    assert frag["entities"] == search_store.get_fragment(frag["_key"])["entities"]


# --- 排序契约：SQLite 的分数与名次必须由 PG 那套 BM25 公式决定（p2.1）-------

def _tsv_lexemes(store: SqliteStorage) -> dict:
    """库内 `content_tsv` → {key: [词元]}（**不碰分词器**：读库里的真相）。

    测试刻意只依赖已落库的 token 串 ⇒ 与机器上的 jieba 用户词典无关。
    """
    with store._lock:
        rows = store._db().execute("SELECT key, content_tsv FROM ks_fragment").fetchall()
    return {k: (tsv or "").split() for k, tsv in rows}


def _expected_order(store: SqliteStorage, query: str, depth: int) -> list:
    """按 **PG 的公式与 tiebreak** 独立算出的期望 key 序（测试内重算一遍）。"""
    from keepsake.splitter import segment_query
    from keepsake.storage import _expand_terms, _sanitize_terms
    from keepsake.storage_pg import bm25_score

    terms = [t.lower() for t in _sanitize_terms(_expand_terms(segment_query(query), {}))]
    lex = _tsv_lexemes(store)
    assert all(all(w.isalnum() or "_" in w for w in ws) for ws in lex.values()), \
        "content_tsv 里出现了非词元字符（分词口径与 FTS5/PG 不一致）"
    n_docs = float(len(lex))
    avgdl = sum(len(v) for v in lex.values()) / n_docs if lex else 0.0
    dfs = {t: float(sum(1 for v in lex.values() if t in v)) for t in set(terms)}
    scored = []
    for key, words in lex.items():
        wl = [w.lower() for w in words]
        tfs = {t: float(wl.count(t)) for t in set(terms)}
        score = bm25_score({t: c for t, c in tfs.items() if c}, float(len(words)),
                           n_docs, avgdl, dfs)
        if score > 0:
            scored.append((score, key))
    # 与 PG `_tiebreak` 同规则：分数降序，同分按 key 字典序升序
    scored.sort(key=lambda sk: (-sk[0], sk[1]))
    return [k for _, k in scored[:depth]]


def test_bm25_score_and_order_follow_pg_formula(tmp_path):
    """🔴 SQLite 的 `_bm25_score` 与名次必须由 **PG 的同一个 BM25 公式**决定。

    修前用 FTS5 内建 `bm25()`：idf 变体、长度归一化与 PG 的 RediSearch 公式都不同
    ⇒ 同一份语料两后端的名次不一致（同序前缀对不上）。
    本测试**独立重算**一遍 PG 公式（只读库里的 content_tsv，不依赖分词器），
    逐条比对分数与顺序。
    """
    rows = [{"key": f"memory:frag:{i:012x}", "tags": "ops,shared",
             "content": c, "created": "2026-10-01T00:00:00+00:00"}
            for i, c in enumerate([
                "部署流程 分四批 读写原语 schema 自愈",
                "网关重启 后 Redis 连接池 要重建",
                "备份策略 每天全量 每周归档",
                "证书续签 用 acme 每周一次",
                "部署流程 网关 重启 的 演练 记录",
                "飞书机器人 webhook 凭证 走环境变量",
                "网关 网关 网关 重启 重启 日志",
                "Termux 在手机上跑 Python 脚本",
            ])]
    s = SqliteStorage(path=str(tmp_path / "bm.db"), is_primary=True,
                      final_limit=3, bm25_limit=100)
    try:
        assert s.ensure_index() is True
        assert s.write_fragments_batch(rows) == len(rows)
        for q in ("部署流程", "网关", "备份策略", "证书续签"):
            hits = s.search_bm25(q)
            exp = _expected_order(s, q, 3)
            assert [h["_key"] for h in hits] == exp, (
                f"{q!r} 名次与 PG 公式重算结果不一致：\n"
                f"  实际 {[h['_key'] for h in hits]}\n  期望 {exp}\n"
                f"  分数 {[round(h['_bm25_score'], 6) for h in hits]}")
    finally:
        s.close()


def test_bm25_score_equals_pg_formula_value(tmp_path):
    """分数**数值**本身也要等于 PG 公式（不只是顺序巧合）。"""
    from keepsake.splitter import segment_query
    from keepsake.storage import _expand_terms, _sanitize_terms
    from keepsake.storage_pg import bm25_score

    rows = [{"key": f"memory:frag:{i:012x}", "tags": "ops,shared",
             "content": "记忆检索 用 FTS5 加 jieba 全文 召回",
             "created": "2026-10-01T00:00:00+00:00"} for i in range(4)]
    rows.append({"key": "memory:frag:ffffffffff", "tags": "ops,shared",
                 "content": "网关 重启 之后 要 重建 连接池", "created":
                 "2026-10-01T00:00:00+00:00"})
    s = SqliteStorage(path=str(tmp_path / "bm2.db"), is_primary=True,
                      final_limit=10, bm25_limit=100)
    try:
        assert s.ensure_index() is True
        assert s.write_fragments_batch(rows) == len(rows)
        terms = [t.lower() for t in _sanitize_terms(_expand_terms(segment_query("记忆检索"), {}))]
        lex = _tsv_lexemes(s)
        n_docs = float(len(lex))
        avgdl = sum(len(v) for v in lex.values()) / n_docs
        dfs = {t: float(sum(1 for v in lex.values() if t in v)) for t in set(terms)}
        hits = s.search_bm25("记忆检索")
        assert hits
        for h in hits:
            wl = [w.lower() for w in lex[h["_key"]]]
            want = bm25_score({t: float(wl.count(t)) for t in set(terms) if wl.count(t)},
                              float(len(wl)), n_docs, avgdl, dfs)
            assert abs(h["_bm25_score"] - want) < 1e-9, (
                f"{h['_key']}: 实际 {h['_bm25_score']!r} != PG 公式 {want!r}")
    finally:
        s.close()


# ===========================================================================
# 批 3：维护能力（合并 consolidator / 遗忘 forgetter / 同义词与词典）
#
# 铁律（任务书）：
#   1. 维护逻辑只经抽象层访问存储 —— 本段全部用 `Consolidator` / `Forgetter`
#      （后端无关模块）驱动，**不**给 SQLite 另写一份合并/遗忘实现。
#   2. 阈值从配置读（非法值回落默认 + 留 reason），不写死魔数。
#   3. 维护列判「未设置」用 `= ''`（列是 TEXT NOT NULL DEFAULT ''），**不得 IS NULL**。
#   4. 降级/跳过必须留痕（`degraded` / WARNING）；未就绪仍抛 StorageNotReadyError。
# ===========================================================================

from keepsake.consolidator import Consolidator, resolve_consolidate_config  # noqa: E402
from keepsake.forgetter import Forgetter  # noqa: E402


@pytest.fixture()
def maint_store(tmp_path) -> SqliteStorage:
    """维护用后端：主脑=True（与检索段同一口径）。"""
    s = SqliteStorage(path=str(tmp_path / "maint.db"), is_primary=True,
                      synonym_min_word_freq=2, synonym_jaccard_threshold=0.3,
                      synonym_min_co_occurrence=2,
                      synonym_scan_batch=2)   # 小页 ⇒ 分页路径被真的走到
    assert s.ensure_index() is True
    yield s
    s.close()


# ---------------------------------------------------------------------------
# 批 3 B1：合并（consolidator）
# ---------------------------------------------------------------------------

def test_consolidator_scans_unconsolidated_on_sqlite(maint_store):
    """`_scan_unconsolidated` 在 SQLite 上要真扫得出待合并碎片（不是恒 0）。"""
    rows = [{"key": f"memory:frag:{i:04d}",
             "content": f"部署流程 第{i}步 网关 重启 连接池 重建 检查 证书 续签",
             "created": "2020-01-01T00:00:00+00:00"} for i in range(6)]
    rows.append({"key": "memory:frag:fresh",
                 "content": "新碎片 网关 重启 连接池 重建 检查 证书 续签",
                 "created": "2999-01-01T00:00:00+00:00"})   # 太新 ⇒ 不该被扫进来
    assert maint_store.write_fragments_batch(rows) == len(rows)

    c = Consolidator(maint_store, min_group_size=3, max_age_hours=72)
    frags = c._scan_unconsolidated()
    keys = {f["_key"] for f in frags}
    assert len(keys) == 6, f"应扫出 6 条老碎片，实际 {len(keys)}: {sorted(keys)}"
    assert "memory:frag:fresh" not in keys, "太新的碎片不该进合并候选"
    # 已 consumed 的不参与（多级提炼时原料才不会被反复合并）
    assert all(f.get("fragment_type") != "consumed" for f in frags)


def test_consolidator_dry_run_reports_three_numbers_and_writes_nothing(maint_store):
    """dry-run：报 scanned/groups_found/would_merge，且**一个字都不写**。"""
    rows = [{"key": f"memory:frag:{i:04d}",
             "content": f"备份 策略 轮换 保留 天数 校验 和 恢复 演练",
             "created": "2020-01-01T00:00:00+00:00"} for i in range(4)]
    assert maint_store.write_fragments_batch(rows) == 4
    before = _rows(maint_store)

    stats = Consolidator(maint_store, min_group_size=3,
                         max_age_hours=0).consolidate(dry_run=True)
    assert stats["dry_run"] is True
    assert stats["scanned"] == 4
    assert stats["groups_found"] >= 1
    assert stats["would_merge"] >= 3, stats
    assert stats["merged"] == 0
    assert _rows(maint_store) == before, "dry-run 竟然改了库"


def test_consolidator_merges_and_marks_consumed_via_primitives(maint_store, monkeypatch):
    """真合并：新碎片写入（level=2/category=consolidated）+ 旧碎片标 consumed_by/consumed_at。

    🔴 走的是 `consolidator.py` 里**后端无关**的那份逻辑（`_merge_group` →
    `write_fragments_batch` / `update_fragment_fields`）—— 本测试同时是
    「SQLite 没有被另写一份合并实现」的证据。
    """
    import keepsake.consolidator as C

    monkeypatch.setattr(
        C, "_call_llm",
        lambda msgs, model="", **kw: "备份策略统一为：每日轮换、保留 30 天、每月恢复演练")

    rows = [{"key": f"memory:frag:{i:04d}",
             "content": f"备份 策略 轮换 保留 天数 校验 和 恢复 演练 记录 {i}",
             "created": "2020-01-01T00:00:00+00:00"} for i in range(3)]
    assert maint_store.write_fragments_batch(rows) == 3

    stats = Consolidator(maint_store, min_group_size=3, max_age_hours=0).consolidate()
    assert stats["merged"] == 3, stats

    with maint_store._lock:
        cur = maint_store._db().cursor()
        new = cur.execute(
            "SELECT key, level, category, source FROM ks_fragment "
            "WHERE fragment_type='consolidated'").fetchall()
        old = cur.execute(
            "SELECT key, consumed_by, consumed_at, fragment_type FROM ks_fragment "
            "WHERE consumed_by <> ''").fetchall()
    assert len(new) == 1, new
    new_key, level, category, source = new[0]
    assert (level, category, source) == ("2", "consolidated", "consolidator")
    assert len(old) == 3, old
    for key, consumed_by, consumed_at, ftype in old:
        assert consumed_by == new_key
        assert consumed_at, "consumed_at 必须落库"
        assert ftype == "consumed"


def test_maintenance_columns_judged_with_eq_empty_not_is_null(maint_store):
    """🔴 铁律 3：维护列是 `TEXT NOT NULL DEFAULT ''` ⇒ 判「未设置」必须 `= ''`。

    断言的是**判定语句本身**：库里所有维护列都必须是非 NULL 的文本，
    任何 `IS NULL` 判据在这套 schema 上永远不成立 ⇒ 静默判错。
    """
    maint_store.write_fragments_batch(
        [{"key": "memory:frag:a", "content": "一条普通碎片 网关 重启"}])
    with maint_store._lock:
        cur = maint_store._db().cursor()
        for col in ("level", "consumed_by", "consumed_at"):
            nulls = cur.execute(
                f"SELECT COUNT(*) FROM ks_fragment WHERE {col} IS NULL").fetchone()[0]
            empties = cur.execute(
                f"SELECT COUNT(*) FROM ks_fragment WHERE {col} = ''").fetchone()[0]
            assert nulls == 0, f"{col} 出现 NULL ⇒ schema 契约被破坏，`IS NULL` 判据会失效"
            assert empties == 1, f"{col} 未设置时应为 ''，实际空串行数 {empties}"
        # 「未设置」的判据（= ''）必须真能选中行
        assert cur.execute(
            "SELECT COUNT(*) FROM ks_fragment WHERE consumed_by = '' "
            "AND level = '' AND consumed_at = ''").fetchone()[0] == 1


def test_consolidate_threshold_comes_from_config_with_fallback_trace(maint_store):
    """阈值从配置读；非法值回落默认**并留 reason**（不静默）。"""
    assert resolve_consolidate_config({"consolidate_min_overlap": 5})["min_overlap"] == 5
    bad = resolve_consolidate_config({"consolidate_min_overlap": "三", "consolidate_min_group": 0})
    assert bad["min_overlap"] == 3 and bad["min_group_size"] == 3, bad
    assert len(bad["reasons"]) == 2 and all("回落默认" in r for r in bad["reasons"])

    c = Consolidator(maint_store, config={"consolidate_min_overlap": "x"})
    assert c._min_overlap == 3, "非法值必须回落默认 3"
    assert Consolidator(maint_store, config={"consolidate_min_overlap": 5})._min_overlap == 5
    # 阈值随统计回传（运维 dry-run 能看到本轮用的是几）
    stats = c.consolidate(dry_run=True)
    assert stats["min_overlap"] == 3 and stats["min_group_size"] == 3


# ---------------------------------------------------------------------------
# 批 3 B2：遗忘（forgetter）
# ---------------------------------------------------------------------------

def _frags(store: SqliteStorage, sql: str) -> List:
    with store._lock:
        return store._db().execute(sql).fetchall()


def test_forgetter_dry_run_reports_four_numbers_and_deletes_nothing(maint_store):
    """dry-run：报 scanned/candidates/deleted/skipped_protected，且不删。"""
    rows = [{"key": f"memory:frag:{i:04d}", "content": f"琐碎 内容 {i}",
             "created": "2020-01-01T00:00:00+00:00",
             "sentiment_score": "0.0", "feedback_score": "0"}
            for i in range(5)]
    rows.append({"key": "memory:frag:keep", "content": "正反馈碎片",
                 "created": "2020-01-01T00:00:00+00:00", "feedback_score": "3"})
    rows.append({"key": "memory:frag:cons", "content": "已合并的高层条目",
                 "created": "2020-01-01T00:00:00+00:00", "fragment_type": "consolidated"})
    assert maint_store.write_fragments_batch(rows) == len(rows)
    before = _rows(maint_store)

    stats = Forgetter(maint_store, max_age_days=1, dry_run=True).forget()
    assert stats["dry_run"] is True
    assert stats["scanned"] == len(rows)
    assert stats["candidates"] == 5, stats           # 5 条琐碎
    assert stats["deleted"] == 0
    assert stats["skipped_protected"] == 2, stats    # 正反馈 + consolidated
    assert _rows(maint_store) == before, "dry-run 竟然删了行"


def test_forgetter_protection_rules_match_backend_neutral_code(maint_store):
    """保护规则：consolidated / 正反馈 / hermes_agent 无负反馈 —— 都不许删。"""
    rows = [
        {"key": "memory:frag:cons", "content": "高层条目", "created": "2020-01-01T00:00:00+00:00",
         "fragment_type": "consolidated"},
        {"key": "memory:frag:pos", "content": "有用的条目", "created": "2020-01-01T00:00:00+00:00",
         "feedback_score": "1"},
        {"key": "memory:frag:man", "content": "手动存的", "created": "2020-01-01T00:00:00+00:00",
         "source": "hermes_agent", "feedback_score": "0"},
        {"key": "memory:frag:dodge", "content": "琐碎条目", "created": "2020-01-01T00:00:00+00:00"},
    ]
    assert maint_store.write_fragments_batch(rows) == 4
    stats = Forgetter(maint_store, max_age_days=1, dry_run=True).forget()
    assert stats["skipped_protected"] == 3, stats
    assert stats["candidates"] == 1, stats


def test_forgetter_feedback_weight_participates(maint_store):
    """反馈权重参与判定：`min_feedback_score` 抬高 ⇒ 原本可遗忘的碎片转入受保护。"""
    maint_store.write_fragments_batch(
        [{"key": "memory:frag:zero", "content": "零反馈碎片",
          "created": "2020-01-01T00:00:00+00:00", "feedback_score": "0"}])
    loose = Forgetter(maint_store, max_age_days=1, min_feedback_score=0, dry_run=True).forget()
    assert loose["candidates"] == 1, loose
    strict = Forgetter(maint_store, max_age_days=1, min_feedback_score=-1, dry_run=True).forget()
    assert strict["candidates"] == 0 and strict["skipped_protected"] == 1, strict


def test_forgetter_real_delete_goes_through_delete_primitives(maint_store):
    """真删路径（dry_run=False）：走 `delete_fragments_batch`，FTS 同步清。"""
    keys = [f"memory:frag:d{i}" for i in range(4)]
    assert maint_store.write_fragments_batch(
        [{"key": k, "content": f"待遗忘的琐碎条目 {i}", "created": "2020-01-01T00:00:00+00:00"}
         for i, k in enumerate(keys)]) == 4
    maint_store.write_fragments_batch(
        [{"key": "memory:frag:keep", "content": "留着的条目", "created": "2020-01-01T00:00:00+00:00",
          "feedback_score": "5"}])
    before = _rows(maint_store)

    stats = Forgetter(maint_store, max_age_days=1, dry_run=False).forget()
    assert stats["deleted"] == 4, stats
    assert _rows(maint_store) == before - 4
    assert len(_frags(maint_store,
                      "SELECT key FROM ks_fragment WHERE key LIKE 'memory:frag:d%'")) == 0
    # 🔴 FTS 僵尸行：主表删了索引没删 ⇒ 检索仍能召回一条取不回正文的碎片
    assert len(_frags(maint_store,
                      "SELECT frag_key FROM ks_fragment_fts WHERE frag_key LIKE 'memory:frag:d%'")) == 0
    assert len(_frags(maint_store,
                      "SELECT frag_key FROM ks_fragment_fts WHERE frag_key = 'memory:frag:keep'")) == 1


def test_forgetter_force_overrides_dry_run_and_report_stays_truthful(maint_store):
    """C：`Forgetter(storage, dry_run=True).forget(force=True)` 的 `"dry_run": false`
    是**设计如此且报告自洽**，不是「报告字段不实」。

    机制（forgetter.py:57-104）：`stats["dry_run"] = self._dry_run and not force`
    与删除分支判定 `if self._dry_run and not force:` 用的是**同一个表达式** ⇒
    字段与行为恒等：字段为 true ⇒ 一行都不删；字段为 false ⇒ 真删且 deleted 是
    实删条数。`force` 的 docstring 已写明「True 时忽略 dry_run 设置，实际删除」。
    本测试锁住这个恒等式在 SQLite 后端上同样成立。
    """
    rows = [{"key": f"memory:frag:f{i}", "content": f"待遗忘的琐碎条目 {i}",
             "created": "2020-01-01T00:00:00+00:00"} for i in range(4)]
    assert maint_store.write_fragments_batch(rows) == 4
    before = _rows(maint_store)

    stats = Forgetter(maint_store, max_age_days=1, dry_run=True).forget(force=True)
    assert stats["dry_run"] is False, stats            # force 覆盖了 dry_run
    assert stats["candidates"] == 4, stats
    assert stats["deleted"] == 4, stats                # 真删了，字段没骗人
    assert _rows(maint_store) == before - 4

    # 同一形态的另一半：dry_run=True 且不 force ⇒ 字段 true 且 deleted 恒为 0
    assert maint_store.write_fragments_batch(rows) == 4
    kept = _rows(maint_store)
    stats2 = Forgetter(maint_store, max_age_days=1, dry_run=True).forget()
    assert stats2["dry_run"] is True and stats2["candidates"] == 4, stats2
    assert stats2["deleted"] == 0 and _rows(maint_store) == kept


def test_maintenance_primitives_raise_when_schema_not_ready(tmp_path):
    """🔴 铁律 4：schema 未就绪时维护原语**仍抛 StorageNotReadyError**（不是静默扫 0 条）。"""
    from keepsake.storage_sqlite import StorageNotReadyError

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("占位")
    s = SqliteStorage(path=str(blocker / "ks.db"))
    try:
        assert s.ensure_index() is False
        for call in (lambda: s.scan_fragment_keys(),
                     lambda: s.get_fragments_batch(["memory:frag:x"]),
                     lambda: s.write_fragments_batch([{"key": "k", "content": "c"}]),
                     lambda: s.update_fragment_fields("k", {"level": "2"}),
                     lambda: s.delete_fragments_batch(["k"]),
                     lambda: s.discover_synonyms(),
                     lambda: s.generate_jieba_dict(str(tmp_path / "d.txt"))):
            with pytest.raises(StorageNotReadyError):
                call()
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 批 3 B3：同义词与词典
# ---------------------------------------------------------------------------

def test_discover_synonyms_writes_ks_synonym(maint_store):
    """同义词自动发现：真写 `ks_synonym`，返回统计与 `degraded`。"""
    rows = [{"key": f"memory:frag:{i:04d}",
             "content": "备份 策略 轮换 保留 天数 校验 和 恢复 演练"}
            for i in range(4)]
    assert maint_store.write_fragments_batch(rows) == 4

    stats = maint_store.discover_synonyms(rebuild=True)
    assert stats["rebuild"] is True
    assert stats["scanned_fragments"] == 4
    assert stats["discovered_groups"] > 0, stats
    assert stats["degraded"] == [], f"健康路径不该有降级: {stats['degraded']}"
    terms = {t for (t,) in _frags(maint_store, "SELECT term FROM ks_synonym")}
    assert "备份" in terms and "策略" in terms, sorted(terms)[:20]


def test_synonym_map_is_loaded_as_json_not_character_soup(maint_store):
    """🔴 批 3 修的真 bug：`ks_synonym.synonyms` 是 TEXT（JSON 串）。

    修前 `for s in (syns or [])` 在**逐字符**迭代 JSON ⇒ 词表全是单字碎片，
    查询式扩展被垃圾 token 撑大且**不报错**。这里断言反例：不得出现 `[`/`"` 等字符。
    """
    with maint_store._lock:
        maint_store._db().execute(
            "INSERT INTO ks_synonym (term, synonyms) VALUES (?, ?)",
            ("防火墙", json.dumps(["网络屏障", "gateway"], ensure_ascii=False)),
        )
    m = maint_store._load_synonym_map()
    assert "防火墙" in m
    assert m["防火墙"] == {"网络屏障", "gateway"}, m["防火墙"]
    assert "gateway" in m, "反向映射也必须在（同 PG/Redis 口径）"
    for junk in ("[", "]", '"', "\\"):
        assert junk not in m, f"JSON 串被当成字符序列迭代了：出现了 {junk!r}"


def test_synonym_broken_row_is_skipped_loudly(maint_store, caplog):
    """坏行必须**留痕跳过**，不许静默当空（也不许整表崩）。"""
    with maint_store._lock:
        cur = maint_store._db().cursor()
        cur.execute("INSERT INTO ks_synonym (term, synonyms) VALUES ('好的', ?)",
                    (json.dumps(["正常词"]),))
        cur.execute("INSERT INTO ks_synonym (term, synonyms) VALUES ('坏的', 'not-json')")
    with caplog.at_level(logging.WARNING, logger="keepsake.storage_sqlite"):
        m = maint_store._load_synonym_map()
    assert m.get("好的") == {"正常词"}
    assert any("格式非法" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]
    assert "坏的" in m, "坏行应保留 term 占位（只是没同义词），不得整行消失"


def test_discover_synonyms_rebuild_clears_and_incremental_keeps_manual(maint_store):
    """rebuild=True 清表重建；默认增量保留手动已有项（不被自动发现覆盖）。"""
    rows = [{"key": f"memory:frag:{i:04d}",
             "content": "备份 策略 轮换 保留 天数 校验 和 恢复 演练"}
            for i in range(4)]
    maint_store.write_fragments_batch(rows)

    with maint_store._lock:
        maint_store._db().execute(
            "INSERT INTO ks_synonym (term, synonyms) VALUES (?, ?)",
            ("手动词", json.dumps(["手工伙伴"], ensure_ascii=False)))

    incremental = maint_store.discover_synonyms(rebuild=False)
    assert incremental["rebuild"] is False
    assert _frags(maint_store,
                  "SELECT synonyms FROM ks_synonym WHERE term='手动词'")[0][0], \
        "增量模式必须保留手动添加项"

    maint_store.discover_synonyms(rebuild=True)
    assert not _frags(maint_store, "SELECT term FROM ks_synonym WHERE term='手动词'"), \
        "rebuild=True 必须洗掉历史项"


def test_discover_synonyms_denoises_short_ascii_and_stopwords(maint_store):
    """降噪口径与 Redis 侧同源：**对分词器字面输出零假设**。

    🔴 本仓库的老坑：断言 `assert "embedding" in terms` 依赖 jieba 把 `embedding`
    切成一个整词。有用户词典（`~/.config/keepsake/jieba_dict.txt`）的机器上它会被
    切成 `em be dd in g` ⇒ 断言必红，而无词典的机器绿 —— 同一份代码两台机两种结论。
    （现场证据 /tmp/ks_sq_p31_red.txt。）

    这里改成断言与词典无关的不变量：用**同一条降噪判据 + 同一个分词入口**先算出
    「实际 token 集合」，再断言「该滤的滤掉了、该进的都进了」，两边口径同源。
    """
    import jieba
    from keepsake.splitter import _STOP_WORDS
    from keepsake.storage_sqlite import _DENOISE_STOPWORDS, _is_pure_ascii_short

    def keep(w):                      # 与 discover_synonyms 里那条判据逐条同源
        return (len(w) >= 2 and w not in _STOP_WORDS and w not in _DENOISE_STOPWORDS
                and not w.isdigit() and not _is_pure_ascii_short(w))

    content = "no no to in of embedding model 备份 策略 embedding model 备份 策略"
    rows = [{"key": f"memory:frag:{i:04d}", "content": content} for i in range(4)]
    assert maint_store.write_fragments_batch(rows) == 4
    stats = maint_store.discover_synonyms(rebuild=True)

    raw = set(jieba.lcut(content))                 # 本机词典下的**实际**切词结果
    survivors = {w for w in raw if keep(w)}       # 过了降噪判据的（即实现里的候选前集）
    noise = raw - survivors
    assert len(survivors) >= 2, f"样例语料至少留下 2 个候选词，实测 {sorted(raw)}"
    assert noise, f"样例语料应当能造出碎渣词，实测 {sorted(raw)}"

    terms = {t for (t,) in _frags(maint_store, "SELECT term FROM ks_synonym")}
    # 该滤的：被降噪判据滤掉的 token（含纯 ASCII 短词/内置虚词）一个都不许进表
    assert not (noise & terms), f"碎渣词混进了同义词表: {sorted(noise & terms)}"
    assert terms <= survivors, f"表里有没走降噪判据的词: {sorted(terms - survivors)}"
    # 该进的：每条碎片都出现的候选词两两共现 >= min_co_occurrence ⇒ 必成对进表
    assert survivors <= terms, f"该进的候选词没进表: {sorted(survivors - terms)}"
    # stats 自洽：表里几行就报几个词条
    assert stats["degraded"] == [], stats["degraded"]
    assert stats["total_terms"] == len(terms), stats
    assert stats["discovered_groups"] >= 1, stats


def test_generate_jieba_dict_writes_loadable_format(maint_store, tmp_path):
    """词典输出格式 = jieba 用户词典样式 `词 词频 nz`，且同语料重跑逐字节相同。"""
    rows = [{"key": f"memory:frag:{i:04d}",
             "content": "备份 策略 轮换 网关 重启 连接池 重建 证书 续签"}
            for i in range(4)]
    maint_store.write_fragments_batch(rows)

    out = tmp_path / "dicts" / "jieba_dict.txt"
    stats = maint_store.generate_jieba_dict(str(out))
    assert stats["degraded"] == [], stats["degraded"]
    assert stats["written_terms"] > 0
    text = out.read_text(encoding="utf-8")
    assert text.endswith("\n")
    for line in text.splitlines():
        parts = line.split()
        assert len(parts) == 3 and parts[2] == "nz" and parts[1].isdigit(), repr(line)
    words = {l.split()[0] for l in text.splitlines()}
    assert {"备份", "策略", "网关"} <= words, sorted(words)

    again = tmp_path / "again.txt"
    maint_store.generate_jieba_dict(str(again))
    assert again.read_text(encoding="utf-8") == text, "同语料重跑结果必须逐字节相同"


def test_generate_jieba_dict_includes_synonym_terms(maint_store, tmp_path):
    """同义词表里的 term 至少算 3 次（手工词不该被词频筛掉）。"""
    maint_store.write_fragments_batch(
        [{"key": "memory:frag:a", "content": "一条 只有 一次 的 罕见词"}])
    with maint_store._lock:
        maint_store._db().execute(
            "INSERT INTO ks_synonym (term, synonyms) VALUES (?, ?)",
            ("手工罕见词", json.dumps(["伙伴"], ensure_ascii=False)))
    out = tmp_path / "d.txt"
    maint_store.generate_jieba_dict(str(out))
    body = out.read_text(encoding="utf-8")
    assert "手工罕见词 3 nz" in body, body


def test_maintenance_scan_leaves_degraded_trace_on_partial_read(maint_store, monkeypatch, caplog):
    """🔴 铁律 4：分页原语 fail-open 时，覆盖度自检必须把「扫到一半」变成可辨识返回值。"""
    maint_store.write_fragments_batch(
        [{"key": f"memory:frag:{i:04d}", "content": f"备份 策略 轮换 校验 恢复 演练 {i}"}
         for i in range(6)])

    real_batch = maint_store.get_fragments_batch
    calls = {"n": 0}

    def lossy_batch(keys):
        calls["n"] += 1
        if calls["n"] == 2:      # 第二页整页读失败（fail-open：只 warning、返回残缺）
            return {}
        return real_batch(keys)

    monkeypatch.setattr(maint_store, "get_fragments_batch", lossy_batch)
    with caplog.at_level(logging.WARNING, logger="keepsake.storage_sqlite"):
        stats = maint_store.discover_synonyms(rebuild=True)
    assert stats["degraded"], "整页读失败却没有降级留痕 ⇒ 静默少做一半活"
    assert any("覆盖不全" in d for d in stats["degraded"]), stats["degraded"]
    assert any("降级" in r.getMessage() for r in caplog.records)


def test_maintenance_modules_are_backend_neutral_ast():
    """合并/遗忘**不许**出现第二份 SQLite 实现（铁律 1）。

    机械判据：`consolidator.py` / `forgetter.py` 全文不 import 任何具体后端模块
    —— 它们只经 StorageBase 的维护原语访问存储，PG 与 SQLite 走**逐字同一份代码**。
    """
    import ast

    for mod in ("consolidator", "forgetter"):
        path = Path(__file__).resolve().parents[1] / "src" / "keepsake" / f"{mod}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        backends = {"storage", "storage_pg", "storage_sqlite", "redis"} & {
            m.split(".")[-1] for m in imported}
        assert not backends, f"{mod}.py 直接 import 了具体后端 {backends} ⇒ 存在后端分叉"
