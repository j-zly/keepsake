#!/usr/bin/env python3
"""把 Redis 里的记忆副本迁移到 PostgreSQL —— **对 Redis 严格只读**。

🔴 **只读红线（本脚本最硬的约束）**
   本文件只用 SCAN / HGETALL / PING 这几个读命令，**不存在任何写 Redis 的调用**。
   启动时 `assert_no_redis_writes()` 用 AST 静态自检：把源码里所有方法调用名收集起来，
   跟「写命令黑名单」对撞，出现任何一个就直接退出 —— 防止以后有人顺手加一行
   `client.set(...)` 而没人发现。这是自检，不依赖 pytest。

## 干什么
   SCAN `memory:frag:*` → pipeline HGETALL → 直写 PG `ks_fragment`。
   tsvector（jieba 分词）+ GIN 索引一律由 `PgStorage.upsert_fragment()` 负责算
   —— 脚本不重复实现一遍分词逻辑。

## bytes 解码（单点：`TEXT_FIELDS` / `BINARY_FIELDS`）
   `build_redis()` 刻意 `decode_responses=False`，于是 hash **值全是 bytes**。
   文本类必须解码（不解码 → embedder 的 `json.dumps` 抛
   `TypeError: Object of type bytes is not JSON serializable`）；
   二进制类（`embed_bin` 向量 blob）必须保持 bytes（硬解成乱码比报错更糟）。
   决策全部集中在 `_decode_field()` 一处，见该处的字段分类表。

## 向量：搬运而非重算
   Redis 里已有的 `embed_bin` 原样搬进 PG `embedding` 列，**不重算** ——
   对照评测要求两边**同源向量**，重算会引入与后端无关的差异。只有确实没有
   向量的碎片才调 embedder 现算。搬运后 `upsert_fragment()` 校验维度 ==
   `embed_dim`，不等即抛 `_SchemaDimMismatch`。

## 幂等
   主键就是 Redis 的 key，`ON CONFLICT (key) DO UPDATE` ⇒ 重复跑是覆盖更新，
   **不会**产生第二行。
   刻意**不走 `store()`**：store() 遇同内容会把旧版另起 `:<epoch>` 新键（在线写入的
   去重语义）。迁移要的是「Redis 里什么样，PG 里就什么样」。

## 辅助结构（话题榜 / 注意力榜 / 实体时间线 / 共现 / 同义词）
   碎片搬完了但这些没搬 ⇒ PG 侧重排加权走默认值 ⇒ 分数/名次不可比。所以一并搬：
   | Redis | → PG |
   |---|---|
   | `keepsake:hot_topics{,:daily,:weekly}` ZSET | `ks_hot_topic(scope,topic,score,expire_ts)` |
   | `keepsake:hot_topics:last_seen` HASH | `ks_hot_topic_seen(topic,last_seen)` |
   | `keepsake:attention{,:daily,:weekly}` ZSET | `ks_attention(scope,topic,score,expire_ts)` |
   | `keepsake:entity_timeline:<实体>` ZSET | `ks_entity_timeline(entity,frag_key,ts)` |
   | `keepsake:entity_cooc` ZSET | `ks_entity_cooc(pair,score,expire_ts)` |
   | `keepsake:synonyms` HASH | `ks_synonym(term,synonyms)` |

   写入一律走 `PgStorage.import_aux_rows()`：**覆盖**语义（`SET = EXCLUDED`），
   不走 `_record_topics()` 的累加路径 —— 累加路径跑第二遍分数就翻倍，幂等当场破。
   Redis 侧整集 TTL 换算成 PG 的逐行 `expire_ts`（同一件事的两种落法）。
   迁完逐项打印两侧计数，**对不上立即 FAIL**（并说明差在哪），不「迁了就算」。

## 用法
   python3 scripts/migrate_redis_to_pg.py --dry-run     # 只读、只打印计划
   python3 scripts/migrate_redis_to_pg.py --limit 100   # 先迁 100 条试水
   python3 scripts/migrate_redis_to_pg.py                # 全量
   python3 scripts/migrate_redis_to_pg.py --skip-aux     # 只搬碎片，不搬辅助结构
   python3 scripts/migrate_redis_to_pg.py --config /path/to/config.json

⚠️ 88 上没有到 Redis 的通路，本单**不连真 Redis** 跑；脚本按「主脑在能连通的环境
   执行」设计。凭据一律来自 keepsake 的 config.json / `KEEPSAKE_CONFIG`，
   不硬编码、不打印、不进日志。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# ---------------------------------------------------------------- 只读红线自检

#: 出现即视为写 Redis 的命令 —— 源码里有就直接拒绝启动。
#: 只收 **Redis 命令名**（都是 `client.<命令>()` 的形式）。
#: 刻意**不**收 `execute`（pipeline 的 flush 方法，本身不是命令；本脚本队列里
#: 只有 HGETALL）与 `close`/`ping` —— 否则自检会永远误报，红线也就没人看了。
FORBIDDEN_REDIS_COMMANDS = frozenset({
    "set", "setex", "setnx", "getset", "append", "incr", "incrby", "decr", "decrby",
    "hset", "hmset", "hdel", "hincrby", "hincrbyfloat", "hsetnx",
    # delete/unlink 是 DEL 的两个合法别名，两个都得禁（漏一个就等于没禁）
    "del", "delete", "unlink", "rename", "renamenx", "expire", "pexpire", "expireat",
    "persist",
    # execute_command：redis-py 的低层逃逸口，命令名是字符串参数，静态审不出来
    "execute_command",
    "zadd", "zrem", "zincrby", "zremrangebyscore", "zremrangebyrank",
    "sadd", "srem", "smove", "sunion", "sunionstore",
    "flushdb", "flushall", "eval", "evalsha", "script",
    "ft_create", "ft_drop", "ft_add", "ft_del", "ft_search", "ftalter", "ftalterindex",
})


def _redis_receiver_names(tree: ast.AST) -> set:
    """找出「可能持有 Redis 连接」的变量名。

    判据：被赋值为 `redis.Redis(...)` / `*.Redis(...)` / `*.pipeline()` 的名字，
    以及形参名里带 client/conn/pipe/redis 的。目的是让红线只盯着真正的连接对象，
    而不是把 `rows.append(...)` 也当成写 Redis。
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            val = node.value
            is_redis = (isinstance(val, ast.Call)
                        and isinstance(val.func, ast.Attribute)
                        and val.func.attr in ("Redis", "StrictRedis", "pipeline"))
            if not is_redis:
                continue
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.arg):
            low = node.arg.lower()
            if any(k in low for k in ("client", "conn", "pipe", "redis")):
                names.add(node.arg)
    return names


def assert_no_redis_writes(script_path: Optional[Path] = None) -> List[str]:
    """🔴 只读红线自检：源码里不得对 Redis 连接调用任何写命令。

    为什么不是「全文 grep 命令名」：那样 `rows.append(...)`、`set(...)` 全会误报，
    误报的红线等于没有红线。这里只审**「对 Redis 连接的」属性调用** ——
    先用 `_redis_receiver_names()` 把连接对象认出来，再看它们身上调了什么方法。

    另加一条低层逃逸口检查：redis-py 的 `execute_command("SET", ...)` 什么方法名都
    藏得住，所以裸函数调用里出现 `execute_command` 也直接拒绝。
    """
    path = script_path or Path(__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    receivers = _redis_receiver_names(tree)

    called: List[str] = []
    bad: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                and func.value.id in receivers:
            name = func.attr.lower()
            called.append(name)
            if name in FORBIDDEN_REDIS_COMMANDS:
                bad.append(name)
        elif isinstance(func, ast.Name) and func.id == "execute_command":
            # 低层逃逸口：命令名是字符串参数，静态审不出来 ⇒ 一律拒绝使用
            bad.append("execute_command(...)")

    if bad:
        raise SystemExit(
            f"🔴 只读红线被打破：脚本对 Redis 连接调用了 {sorted(set(bad))}。"
            "本脚本对 Redis 只能读。"
        )
    return sorted(set(called))


# ---------------------------------------------------------------- 配置 / 客户端


def load_config(path: str = "") -> Dict[str, Any]:
    """读 keepsake 配置（与 storage_from_config 同一份）。"""
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    default = os.path.expanduser("~/.config/keepsake/config.json")
    if not os.path.exists(default):
        raise SystemExit(
            f"找不到 keepsake 配置 {default}；用 --config 指定，或设 KEEPSAKE_CONFIG 环境变量"
        )
    return json.loads(Path(default).read_text(encoding="utf-8"))


def build_redis(cfg: Dict[str, Any]):
    """只读 Redis 客户端。

    🔴 刻意用裸 `redis.Redis` 而不是 `RedisStorage`：后者的 `store()` 是写路径，
    迁移时误用就违反只读红线了。
    """
    import redis  # 延迟 import：没装 redis 时仍可 import 本模块做语法检查

    return redis.Redis(
        host=cfg.get("redis_host", "127.0.0.1"),
        port=int(cfg.get("redis_port", 6379)),
        password=cfg.get("redis_password") or None,
        decode_responses=False,          # 保持 bytes，hash 字段值原样搬
        socket_connect_timeout=5,
        socket_timeout=30,
    )


def build_embedder(cfg: Dict[str, Any]):
    from keepsake.embedder import create_embedder

    ec = dict(cfg.get("embedder") or {})
    if not ec.get("model"):
        # 注意：**Redis 里已有的 embed_bin 照样原样搬运**（upsert_fragment 优先用
        # 它），所以没有 embedder ≠ PG 侧一定没有向量。只有 Redis 里也缺向量的
        # 那部分碎片会没有向量，KNN 对它们不可用（BM25 全程不受影响）。
        print("⚠️ 配置里没有 embedder 段 → Redis 里缺 embed_bin 的碎片不会补算向量"
              "（已有向量仍原样搬运；KNN 只对缺向量的那部分不可用，BM25 不受影响）。")
        return None
    return create_embedder(
        provider=ec.get("provider", "openai"),
        api_key=ec.get("api_key", ""),
        base_url=ec.get("base_url", ""),
        model=ec.get("model", ""),
    )


def build_pg(cfg: Dict[str, Any], embedder: Any, embed_dim: int):
    from keepsake.storage_pg import PgStorage

    pg_cfg = (cfg.get("storage") or {}).get("postgres") or {}
    if not isinstance(pg_cfg, dict):
        pg_cfg = {}
    storage = PgStorage(
        host=str(pg_cfg.get("host", "127.0.0.1")),
        port=int(pg_cfg.get("port", 5432)),
        dbname=str(pg_cfg.get("dbname", "keepsake")),
        user=str(pg_cfg.get("user", "")),
        password=str(pg_cfg.get("password", "")),
        sslmode=str(pg_cfg.get("sslmode", "")),
        agent_id=str(cfg.get("agent_id", "")),
        embedder=embedder,
        embed_dim=embed_dim,
    )
    storage.ensure_index()
    return storage


# ---------------------------------------------------------------- 读 Redis（只读）


def _s(v: Any) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v or "")


#: 🔴 **字段分类表 —— bytes 值的解码决策全部集中在这一个地方。**
#:
#: `build_redis()` 刻意 `decode_responses=False`（hash 字段值原样搬），于是
#: HGETALL 的值全是 bytes。文本类不解码会把 bytes 喂进 embedder 的
#: `json.dumps` → `TypeError: Object of type bytes is not JSON serializable`；
#: 二进制类按 UTF-8 硬解会把向量 blob 毁成乱码 —— 比报错更糟。
#: 两类都不能「顺手处理」，所以按字段名一次性分清，不在下游散写。
#:
#: 文本：全部 `ks_fragment` 的 text 列 + `key`。
TEXT_FIELDS = frozenset({
    "key", "content", "tags", "category", "source", "created",
    "sentiment_score", "sentiment_label", "feedback_score",
    "entities", "fragment_type", "valid_until", "is_archived",
    "superseded_by", "superseded_at", "corrected_at", "invalid_at",
})

#: 二进制：**保持 bytes 原样**，绝不按 UTF-8 解。
#: `embed_bin` 是 `struct.pack(f'{n}f', *vec)` 的 float32 向量 blob
#: （见 `storage.RedisStorage._text_to_blob`），由 `upsert_fragment()` 搬运。
BINARY_FIELDS = frozenset({"embed_bin"})

assert not (TEXT_FIELDS & BINARY_FIELDS), "字段分类表冲突：一个字段不能既文本又二进制"


def assert_field_table_covers_columns() -> None:
    """🔴 字段分类表的**漂移自检**：`ks_fragment` 每加一个文本列，本表必须跟上。

    为什么值得单独一个自检（且放在启动时）：漏跟的后果不是立刻报错，而是
    迁移跑到那一行才抛 `_BytesFieldError` —— 真实库里可能已经写进去几十万行，
    才在半夜的迁移里炸出来。这里把它变成「一启动就拒跑」。
    `storage_pg` 延迟 import：本脚本要能 import 本模块做语法检查（不装 PG 依赖）。
    """
    from keepsake.storage_pg import FRAGMENT_COLUMNS

    missing = [c for c in FRAGMENT_COLUMNS if c not in TEXT_FIELDS]
    if missing:
        raise SystemExit(
            f"🔴 字段分类表漏了列 {missing}：这些是 ks_fragment 的文本列，"
            f"却不在 TEXT_FIELDS 里 ⇒ 迁移时它们的 bytes 值不会被解码，"
            f"upsert_fragment 会抛 _BytesFieldError。把它们加进 TEXT_FIELDS。"
        )


def _decode_field(name: str, value: Any) -> Any:
    """按字段分类表决定解码与否 —— 解码决策的单点。"""
    if name in BINARY_FIELDS:
        return value                     # 原样搬运，绝不硬解
    if name in TEXT_FIELDS:
        return _s(value)
    # 🔴 未知字段：Redis 侧新增了列而本表没跟上。宁可原样透传让下游
    # upsert_fragment 报出字段名，也不要在这猜 —— 猜错会把二进制毁成乱码。
    return value


def iter_fragments(client, batch: int = 200, limit: int = 0) -> Iterator[List[Dict[str, Any]]]:
    """SCAN + pipeline HGETALL，逐批 yield 碎片 dict 列表（只读）。

    yield 的是 `{"key": <redis key>, <hash 字段>: 值, ...}`：
    **文本字段已解码成 str**（见 `TEXT_FIELDS`），**二进制字段仍是 bytes**
    （见 `BINARY_FIELDS`，供 `upsert_fragment` 搬运向量）。
    """
    scanned = 0
    cursor = 0
    while True:
        cursor, keys = client.scan(cursor=cursor, match="memory:frag:*", count=batch)
        if keys:
            pipe = client.pipeline()
            for k in keys:
                pipe.hgetall(k)
            rows = []
            for key, raw in zip(keys, pipe.execute()):
                scanned += 1
                if not raw:
                    continue
                rows.append({"key": _s(key),
                             **{name: _decode_field(name, fv)
                                for name, fv in ((_s(fk), fv) for fk, fv in raw.items())}})
            if rows:
                yield rows
        if limit and scanned >= limit:
            return
        if cursor == 0:
            return


# ---------------------------------------------------------------- 辅助结构（只读）

#: 🔴 Redis 侧辅助结构的 key 名**一律从 `keepsake.storage` / `keepsake.attention`
#: 导入，不在本脚本里重打一份**。理由与 `storage_pg.py` 的「不复制 Redis 物理 key 名」
#: 同源：重打一份就多一处必然漂移的地方，而漂移的后果是「静默迁 0 条、没人看得出来」。
#: 延迟 import 是为了本模块在没装 redis 时仍可 import 做语法检查。
def _aux_keys() -> Dict[str, Any]:
    from keepsake.attention import ATTENTION_DAILY, ATTENTION_SET, ATTENTION_WEEKLY
    from keepsake.storage import (
        ENTITY_COOC_KEY,
        ENTITY_COOC_TTL,
        ENTITY_TIMELINE_KEY,
        HOT_TOPIC_DAILY,
        HOT_TOPIC_LAST_SEEN,
        HOT_TOPIC_SET,
        HOT_TOPIC_WEEKLY,
        SYNONYM_HASH_KEY,
    )
    from keepsake.storage_pg import _ATTENTION_TTL, _ENTITY_COOC_TTL, _TOPIC_TTL

    # 无 TTL 时按 scope 落回标称值；值取 PG 侧同一张 TTL 表 ⇒ 两边过期语义一致。
    return {
        "hot_topic": ({"all": HOT_TOPIC_SET, "daily": HOT_TOPIC_DAILY,
                       "weekly": HOT_TOPIC_WEEKLY}, _TOPIC_TTL),
        "hot_topic_seen": HOT_TOPIC_LAST_SEEN,
        "attention": ({"all": ATTENTION_SET, "daily": ATTENTION_DAILY,
                       "weekly": ATTENTION_WEEKLY}, _ATTENTION_TTL),
        "entity_timeline": ENTITY_TIMELINE_KEY,   # 前缀本身；SCAN 模式见 `_timeline_prefix()`
        "entity_cooc": (ENTITY_COOC_KEY, _ENTITY_COOC_TTL),
        "synonym": SYNONYM_HASH_KEY,
    }


def _timeline_prefix() -> str:
    """实体时间线的 key 前缀（含尾冒号）——取自 `storage.ENTITY_TIMELINE_KEY`。

    单独一个函数而不是在 `_aux_keys()` 里 import：时间线是 4961 个键的 SCAN 模式，
    显式点出「前缀只有一个来源」比让它混在 dict 里更难看错。
    """
    from keepsake.storage import ENTITY_TIMELINE_KEY

    return f"{ENTITY_TIMELINE_KEY}:"


def _expire_ts(client, key: str, fallback_ttl: int) -> float:
    """Redis 键的**剩余** TTL → 绝对过期时刻（PG 的 `expire_ts` 列）。

    Redis 侧是「整集 TTL」，PG 侧是「逐行 expire_ts」——同一件事的两种落法，
    这里做的是这个换算。`ttl()` 返回 -1（无过期）时按该 scope 的标称 TTL 落回，
    **不能取 0**：0 等于「一落地就过期」，迁移完等于没迁。
    """
    ttl = client.ttl(key)
    ttl = ttl if isinstance(ttl, int) and ttl > 0 else int(fallback_ttl)
    return time.time() + ttl


def _read_scoped_zsets(client, key_by_scope, ttl_by_scope) -> List[tuple]:
    """三榜 ZSET → `(scope, topic, score, expire_ts)` 行（热词榜与注意力榜同款结构）。"""
    rows: List[tuple] = []
    for scope, key in key_by_scope.items():
        expire = _expire_ts(client, key, ttl_by_scope.get(scope, 86400))
        for member, score in client.zrange(key, 0, -1, withscores=True):
            rows.append((scope, _s(member), float(score), expire))
    return rows


def read_aux(client) -> Dict[str, Dict[str, Any]]:
    """读出全部辅助结构 → `{名称: {table, columns, on_conflict, key_cols, rows, found}}`。

    🔴 `found` 是「Redis 侧这个键到底存不存在」，**不是**「读到几行」：
    结构缺失与「结构在、但内容为空」在对照评测里后果完全不同（前者是能力缺口，
    后者是数据为空），必须能分开报。结构不存在时 `rows=[]`，调用方据此在
    计数报告里写「Redis 侧无此结构，未迁」——**绝不凭空造数据**。
    """
    # jsonb 列：Python list 直接当参数传会被 psycopg 序列化成 PG **数组**字面量
    # （`{a,b}`）被 jsonb 拒，必须显式包 Jsonb。这是实测踩出来的，不是猜的。
    from psycopg.types.json import Jsonb

    keys = _aux_keys()
    out: Dict[str, Dict[str, Any]] = {}

    ht_keys, ht_ttl = keys["hot_topic"]
    out["hot_topic"] = {
        "table": "ks_hot_topic", "columns": ("scope", "topic", "score", "expire_ts"),
        "key_cols": ("scope", "topic"),
        "on_conflict": "ON CONFLICT (scope, topic) DO UPDATE"
                       " SET score = EXCLUDED.score, expire_ts = EXCLUDED.expire_ts",
        "rows": _read_scoped_zsets(client, ht_keys, ht_ttl),
        "found": bool(client.exists(ht_keys["all"])
                      or client.exists(ht_keys["daily"])
                      or client.exists(ht_keys["weekly"])),
    }

    seen_key = keys["hot_topic_seen"]
    seen_raw = client.hgetall(seen_key) if client.exists(seen_key) else {}
    out["hot_topic_seen"] = {
        "table": "ks_hot_topic_seen", "columns": ("topic", "last_seen"),
        "key_cols": ("topic",),
        "on_conflict": "ON CONFLICT (topic) DO UPDATE SET last_seen = EXCLUDED.last_seen",
        "rows": [(_s(topic), float(_s(val))) for topic, val in seen_raw.items()],
        "found": bool(client.exists(seen_key)),
    }

    at_keys, at_ttl = keys["attention"]
    out["attention"] = {
        "table": "ks_attention", "columns": ("scope", "topic", "score", "expire_ts"),
        "key_cols": ("scope", "topic"),
        "on_conflict": "ON CONFLICT (scope, topic) DO UPDATE"
                       " SET score = EXCLUDED.score, expire_ts = EXCLUDED.expire_ts",
        "rows": _read_scoped_zsets(client, at_keys, at_ttl),
        "found": bool(client.exists(at_keys["all"])
                      or client.exists(at_keys["daily"])
                      or client.exists(at_keys["weekly"])),
    }

    prefix = _timeline_prefix()
    timeline: List[tuple] = []
    cursor = 0
    n_ents = 0
    while True:
        cursor, tkeys = client.scan(cursor=cursor, match=f"{prefix}*", count=500)
        for tkey in tkeys:
            # 前缀下理论上只有 ZSET（写路径唯一），但 type 判定很便宜：
            # 混进一个 hash 会让 zrange 直接抛，连「迁了几条」都报不出来。
            if _s(client.type(tkey)) != "zset":
                continue
            n_ents += 1
            entity = _s(tkey)[len(prefix):]
            for member, ts in client.zrange(tkey, 0, -1, withscores=True):
                timeline.append((entity, _s(member), float(ts)))
        if cursor == 0:
            break
    out["entity_timeline"] = {
        "table": "ks_entity_timeline", "columns": ("entity", "frag_key", "ts"),
        "key_cols": ("entity", "frag_key"),
        "on_conflict": "ON CONFLICT (entity, frag_key) DO UPDATE SET ts = EXCLUDED.ts",
        "rows": timeline, "found": n_ents > 0, "entities": n_ents,
    }

    cooc_key, cooc_ttl = keys["entity_cooc"]
    cooc_found = bool(client.exists(cooc_key))
    out["entity_cooc"] = {
        "table": "ks_entity_cooc", "columns": ("pair", "score", "expire_ts"),
        "key_cols": ("pair",),
        "on_conflict": "ON CONFLICT (pair) DO UPDATE"
                       " SET score = EXCLUDED.score, expire_ts = EXCLUDED.expire_ts",
        "rows": [(_s(m), float(s), _expire_ts(client, cooc_key, cooc_ttl))
                 for m, s in client.zrange(cooc_key, 0, -1, withscores=True)] if cooc_found else [],
        "found": cooc_found,
    }

    syn_key = keys["synonym"]
    syn_found = bool(client.exists(syn_key))
    syn_rows: List[tuple] = []
    for term, raw in (client.hgetall(syn_key) or {}).items():
        try:
            parsed = json.loads(_s(raw))
        except (ValueError, TypeError) as e:
            # 坏值不静默丢：报出来，让计数对齐去 FAIL（迁少了就该 FAIL，不是「跳过」）
            raise SystemExit(
                f"🔴 Redis 同义词 {term!r} 的值不是合法 JSON（{type(e).__name__}）——"
                "拒绝迁一个语义不明的值。PG 侧 ks_synonym.synonyms 是 jsonb，"
                "写进去会直接被 PG 拒。修 Redis 或修源数据后重跑。"
            )
        if not isinstance(parsed, list):
            raise SystemExit(
                f"🔴 Redis 同义词 {term!r} 的值是 {type(parsed).__name__}，不是数组 —— "
                "PG 侧读出来按 list 迭代，形状不对会让查询期静默丢词。"
            )
        syn_rows.append((_s(term), Jsonb(parsed)))
    out["synonym"] = {
        "table": "ks_synonym", "columns": ("term", "synonyms"), "key_cols": ("term",),
        "on_conflict": "ON CONFLICT (term) DO UPDATE SET synonyms = EXCLUDED.synonyms",
        "rows": syn_rows, "found": syn_found,
    }
    return out


#: 计数对齐时把复合主键拼成单列的分隔符。
#: 选它而不是 `|`：Redis 侧 cooc 的 member 本来就用 `a||b` 拼对，实体名里也可能有 `|`,
#: 用控制字符才撞不上业务字符。**必须与 SQL 里那个分隔符是同一个字节**，
#: 拼错的表现是「PG 一行都数不到」——不报错、只是恒为 0（自检首跑就撞到了）。
_SEP = "\x1f"


def aux_row_key(row: tuple, key_cols: tuple) -> str:
    """把一行的唯一键拼成**单列**可比较的字符串（计数对齐用）。"""
    return _SEP.join(str(row[key_cols.index(c)]) for c in key_cols)


def pg_count_for(pg, table: str, key_cols: tuple, rows: List[tuple]) -> int:
    """PG 侧「命中本次迁入键集合」的**行数**（只读）。

    🔴 为什么不是 `SELECT count(*) FROM <表>`：这些表可能同时有 `store()` 在线写进去的行，
    整表计数把「别人写的」也算进来 ⇒ 迁对了也会报不一致 ⇒ 这种 FAIL 报三次就没人看了。
    只数**键集合命中**的行，才能回答真正要问的那句：「Redis 有的，PG 是不是一条不缺」。
    """
    if not rows:
        return 0
    expr = f" || '{_SEP}' || ".join(key_cols)
    keys = [aux_row_key(r, key_cols) for r in rows]
    with pg._ro() as cur:      # noqa: SLF001 — 迁移脚本需要读回自己写没写进去
        cur.execute(f"SELECT count(*) FROM {table} WHERE {expr} = ANY(%s)", (keys,))
        return int(cur.fetchone()[0])


def migrate_aux(pg, client, dry_run: bool = False) -> Dict[str, Dict[str, Any]]:
    """把辅助结构搬进 PG，返回逐项计数对照（供调用方打印/判定）。

    写入一律走 `PgStorage.import_aux_rows()`（覆盖语义 + 复用 `_insert_many` 的
    排序防死锁），**不在脚本里另写一套 SQL**。
    """
    report: Dict[str, Dict[str, Any]] = {}
    for name, spec in read_aux(client).items():
        rows, table = spec["rows"], spec["table"]
        written = 0
        if rows and not dry_run:
            written = pg.import_aux_rows(table, spec["columns"], rows, spec["on_conflict"])
        pg_rows = 0 if (dry_run or not rows) else pg_count_for(pg, table, spec["key_cols"], rows)
        report[name] = {
            "table": table,
            "redis_rows": len(rows),
            "pg_rows": pg_rows,
            "written": written,
            "found": spec["found"],
            "match": pg_rows == len(rows),
            # 时间线是「每个实体一个 zset」，实体数与行数是两个量，都报出来
            "entities": spec.get("entities", 0),
        }
    return report


#: 🔴 **映射表漂移自检**：迁移脚本里每个 spec 的 `table` / `columns` / `key_cols`
#: 必须与 PG 的真实 DDL 对得上，否则 SQL 会在真库上炸（而炸之前可能已经写进去几万行）。
#: 放进启动路径的理由与 `assert_field_table_covers_columns()` 同源。
_AUX_TABLE_CONTRACT = {
    "hot_topic": ("ks_hot_topic", ("scope", "topic", "score", "expire_ts"), ("scope", "topic")),
    "hot_topic_seen": ("ks_hot_topic_seen", ("topic", "last_seen"), ("topic",)),
    "attention": ("ks_attention", ("scope", "topic", "score", "expire_ts"), ("scope", "topic")),
    "entity_timeline": ("ks_entity_timeline", ("entity", "frag_key", "ts"), ("entity", "frag_key")),
    "entity_cooc": ("ks_entity_cooc", ("pair", "score", "expire_ts"), ("pair",)),
    "synonym": ("ks_synonym", ("term", "synonyms"), ("term",)),
}


def _ddl_columns(ddl_sql: str) -> set:
    """从 `CREATE TABLE` 语句里抠出列名。

    为什么自己抠而不是对着 information_schema 查：这是**启动时**的自检，那时还没连库；
    而且它要抓的是「源码里的 DDL 与源码里的映射表对不上」，查真库抓不到源码级漂移。
    DDL 是我们自己写的固定格式（每行一列、行尾逗号），抠列名的解析因此可以很短。
    """
    body = ddl_sql[ddl_sql.index("(") + 1: ddl_sql.rindex(")")]
    cols = set()
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if line and not line.upper().startswith(("PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN")):
            cols.add(line.split()[0])
    return cols


def assert_aux_mapping_self_consistent() -> None:
    """启动即拒跑：辅助结构映射表与 PG DDL 对不上就别开始迁。"""
    from keepsake.storage_pg import _DDL_TABLES

    ddl = {name: _ddl_columns(sql) for name, sql in _DDL_TABLES}
    bad = []
    for _name, (table, columns, key_cols) in _AUX_TABLE_CONTRACT.items():
        if table not in ddl:
            bad.append(f"{table} 不在 storage_pg._DDL_TABLES 里")
            continue
        missing = set(columns) - ddl[table]
        if missing:
            bad.append(f"{table}: 映射表要写的列 {sorted(missing)} 在 DDL 里不存在")
        if not set(key_cols) <= set(columns):
            bad.append(f"{table}: key_cols {key_cols} 不是 columns {columns} 的子集")
    if bad:
        raise SystemExit("🔴 辅助结构映射表与 PG DDL 对不上：" + "；".join(bad))


# ---------------------------------------------------------------- 主流程


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Redis → PostgreSQL 记忆迁移（对 Redis 只读）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--config", default=os.environ.get("KEEPSAKE_CONFIG", ""),
                    help="keepsake config.json 路径（默认 ~/.config/keepsake/config.json）")
    ap.add_argument("--dry-run", action="store_true", help="只读 Redis、只打印计划，不写 PG")
    ap.add_argument("--limit", type=int, default=0, help="最多迁多少条碎片（0 = 全部）")
    ap.add_argument("--batch", type=int, default=200, help="每批条数（默认 200）")
    ap.add_argument("--progress-every", type=int, default=500, help="每多少条打一次进度")
    ap.add_argument("--skip-aux", action="store_true",
                    help="只搬碎片，跳过话题榜/注意力/时间线/共现/同义词")
    args = ap.parse_args(argv)

    used = assert_no_redis_writes()
    print(f"🔒 只读自检通过：源码里未出现任何 Redis 写命令（检测到调用 {used}）")
    assert_field_table_covers_columns()
    assert_aux_mapping_self_consistent()

    cfg = load_config(args.config)
    redis_host = f"{cfg.get('redis_host', '127.0.0.1')}:{cfg.get('redis_port', 6379)}"
    pg_cfg = (cfg.get("storage") or {}).get("postgres") or {}
    pg_host = (f"{pg_cfg.get('host', '127.0.0.1')}:{pg_cfg.get('port', 5432)}"
               f"/{pg_cfg.get('dbname', 'keepsake')}")
    print(f"Redis（只读）← {redis_host}    PG（写）→ {pg_host}")

    client = build_redis(cfg)
    try:
        client.ping()
    except Exception as e:
        # 错连必须**明确失败**，不能静默当成「没数据可迁」
        raise SystemExit(
            f"🔴 连不上 Redis {redis_host}：{type(e).__name__}: {e}。"
            "迁移中止 —— 这是「没连上」，不是「迁了 0 条」。"
        )

    embedder = build_embedder(cfg)
    embed_dim = embedder.dimension if embedder is not None else 1536

    pg = None
    if not args.dry_run:
        pg = build_pg(cfg, embedder, embed_dim)
    else:
        print("（dry-run：不会连接 PG，也不会写入任何一行）")

    t0 = time.time()
    total = moved = 0
    try:
        batch: List[Dict[str, Any]] = []
        for rows in iter_fragments(client, batch=args.batch, limit=args.limit):
            batch.extend(rows)
            if len(batch) >= args.batch:
                total += len(batch)
                moved += _flush(pg, batch, dry_run=False)
                batch = []
                if total % max(args.progress_every, 1) < args.batch:
                    rate = total / max(time.time() - t0, 0.001)
                    print(f"  … 已读 {total} 条，迁入 {moved} 条（{rate:.0f} 条/秒）")
        if batch:
            total += len(batch)
            moved += _flush(pg, batch, dry_run=False)

        aux = {} if args.skip_aux else migrate_aux(pg, client, dry_run=args.dry_run)
        if aux:
            _print_aux_report(aux, dry_run=args.dry_run)
    finally:
        if pg is not None:
            pg.close()

    mode = "完成（dry-run，未写入任何一行）" if args.dry_run else "完成"
    print(f"\n✅ 迁移{mode}：Redis 读到 {total} 条 → PG 写入 {moved} 条，"
          f"耗时 {time.time() - t0:.1f}s")
    if args.dry_run:
        print("   去掉 --dry-run 才会真正落库；重复执行是幂等的（按 key 覆盖，不新增行）。")
    return 0


def _print_aux_report(aux: Dict[str, Dict[str, Any]], dry_run: bool = False) -> None:
    """打印两侧计数对照；**对不上当场 FAIL**，并说清差在哪一项。

    为什么必须 FAIL 而不是打个 WARN 继续：主脑的对照评测正是拿「PG 有没有这份
    加权数据」当前提。迁少了却报告「完成」，评测照样出分，只是分数不可比 ——
    这类错要等到看结论时才发现，代价是整轮评测白跑。
    """
    print("\n--- 辅助结构两侧计数对照 ---")
    bad: List[str] = []
    for name, r in aux.items():
        if not r["found"]:
            print(f"  {name:<16} → {r['table']:<20} Redis 侧无此结构，未迁（不造数据）")
            continue
        if dry_run:
            # dry-run 没连 PG，PG 侧恒为 0 —— 打 ❌ 会让人以为迁失败了
            print(f"  {name:<16} → {r['table']:<20} Redis {r['redis_rows']} 行 "
                  f"（dry-run，未连 PG 校验）")
            continue
        flag = "✅" if r["match"] else "❌"
        print(f"  {name:<16} → {r['table']:<20} Redis {r['redis_rows']} 行 / "
              f"PG 命中 {r['pg_rows']} 行 {flag}")
        if not r["match"]:
            bad.append(f"{name}({r['table']})：Redis {r['redis_rows']} 行，"
                       f"PG 只命中 {r['pg_rows']} 行，缺 {r['redis_rows'] - r['pg_rows']} 行")
    if bad:
        raise SystemExit("🔴 辅助结构计数不对齐，迁移判定失败：\n   - " + "\n   - ".join(bad))


def _flush(pg, batch: List[Dict[str, Any]], dry_run: bool = False) -> int:
    if pg is None or dry_run:
        return len(batch)
    return sum(1 for frag in batch if pg.upsert_fragment(frag))


if __name__ == "__main__":
    sys.exit(main())
