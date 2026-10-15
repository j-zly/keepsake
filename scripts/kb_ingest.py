#!/usr/bin/env python3
"""KB-PG 灌入器（仓库版）：PG skill_docs(唯一存储) → documents/chunks。

收编自 180 本机 `kb_ingest_v3.py`，**v3 是行为的唯一权威**。下面每条判据都与
v3 逐字对齐，改任何一条都等于偏离生产：

* **切块与 hash 判据不在本地一份** —— 一律
  `from keepsake.kb.ingest import split_chunks, upsert_document, ...`，与控制台
  写端点共用同一份（`src/keepsake/kb/ingest.py` 是唯一真源）；
* 正文来源：`body` 是 jsonb，取 `body["files"]` 这个 dict，逐 `(文件名, 内容)`；
  非 str 的 value 跳过、`TEXT_EXT` 后缀之外的跳过、含 `\\x00` 的先去 NUL；
* 分域：`source_type = 'skill' if category == 'main' else 'skill_hk'`；
* `key = "<source_node>/<skill_name>/<文件名>"`，
  `hash = "<sha256 列>:<文件名>"`（幂等判据，变了才重切 chunks）；
* `title = <skill>`，只有非 `SKILL.md` 的文件才拼 ` · <文件名>`；
* `meta = {node, skill, file, col, skill_sha}` —— **死文档清理就是拿
  `meta->>'node'`/`meta->>'skill'` 跟 live 集合比**，meta 漂移 = 清理失效；
* 死文档清理：只删 `source_type IN ('skill','skill_hk')` 且
  `(source_type, meta->>'node', meta->>'skill')` 不在 live 集里的；
  手动文档（`source_type='manual'`）是控制台的地盘，本脚本永不动它；
* stats 以 JSON 打到 stdout（`added/updated/unchanged/files/skills/
  dead_docs_removed/dead_docs_would_remove/db`）。

口令一律走 **环境变量或口令文件路径**（默认对齐 180 现部署），源码零明文：

* 目标库：env `KB_PG_DSN`；没给就读 `KB_PG_PASS_FILE`（默认 `/root/.kb_pgapp.pass`），
  配上 `KB_PG_HOST/PORT/DBNAME/USER`（默认 127.0.0.1:5432/keepsake/kbapp）；
* 源库（`keepsake.skill_docs`，182 行）：env `KB_SRC_DSN`；没给读
  `KB_SRC_PASS_FILE`（默认 `/root/.kbpublisher.pass`），配上
  `KB_SRC_HOST/PORT/DBNAME/USER`（默认 127.0.0.1:5432/**keepsake**/kbpublisher）——
  源和目标是**两个独立 env 前缀**，设目标库的口令不会把源库连歪。

用法：`python3 scripts/kb_ingest.py --dry`（预演，末尾回滚）/ 不带 `--dry` 实跑。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

# 仓库没装进 site-packages 时（180 直接跑脚本）把 src 挂上
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from keepsake.kb.ingest import (  # noqa: E402 —— 路径补齐后才能 import
    INGEST_SOURCE_TYPES,
    DEFAULT_NAMES,
    split_chunks,
    upsert_document,
)

#: 重导出说明：本模块**不**自己切块（`upsert_document` 内部调的就是
#: `split_chunks`）。这里 import 一份是为了让「API 与 CLI 拿到的是同一个函数
#: 对象」能被直接断言（测试 test_kb_crud 里就是这么钉的）。
__all__ = ["split_chunks", "upsert_document", "iter_files", "stype_for_category",
           "source_key", "doc_hash", "doc_title", "build_meta",
           "find_dead_docs", "delete_dead_docs", "ingest", "main"]

#: 源表里一个技能的可切分文件名后缀（v3 的 TEXT_EXT 口径）
TEXT_EXT = (".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".sql", ".py", ".sh")

#: v3 里 `category` 为这个值时走 `skill` 域，其余一律 `skill_hk`（含空/None）
MAIN_CATEGORY = "main"

#: 主文档文件名：它不拼进 title（v3 口径）
MAIN_DOC = "SKILL.md"


# --------------------------------------------------------------------------
# 连库
# --------------------------------------------------------------------------

def _connect(dsn_env: str, pass_env: str, default_pass_file: str, *,
             host: str, port: str, dbname: str, user: str, prefix: str):
    """开一条连接：env DSN 优先；否则口令文件 + 默认连接参数。

    口令**只**从文件/环境读，源码里不出现任何明文；文件读不到直接报错退出
    （静默退化成无口令连接只会得到一个看不懂的认证错误）。

    `prefix` 是这条连接的**分侧 env 前缀**（源库 `KB_SRC_*`、目标库 `KB_PG_*`）——
    共用一个前缀的话，给目标库设的 `DBNAME` 会把源库一起连歪。
    """
    dsn = (os.environ.get(dsn_env) or "").strip()
    if not dsn:
        pw_path = Path((os.environ.get(pass_env) or "").strip() or default_pass_file)
        if not pw_path.exists():
            raise SystemExit(f"{dsn_env} 未设，且口令文件不存在：{pw_path}")
        password = pw_path.read_text(encoding="utf-8").strip()
        dsn = (f"host={os.environ.get(prefix + '_HOST', host)} "
               f"port={os.environ.get(prefix + '_PORT', port)} "
               f"dbname={os.environ.get(prefix + '_DBNAME', dbname)} "
               f"user={os.environ.get(prefix + '_USER', user)} password={password}")
    driver = _driver()
    return driver.connect(dsn, connect_timeout=30)


def _driver():
    """psycopg2 优先，退 psycopg3 —— 与 `api/routers/kb.py::_pg()` 同一取舍。"""
    try:
        import psycopg2  # noqa: PLC0415

        return psycopg2
    except ImportError:
        pass
    try:
        import psycopg  # noqa: PLC0415

        return psycopg
    except ImportError as e:                     # pragma: no cover —— 环境问题
        raise SystemExit("未安装 PostgreSQL 驱动（psycopg2 或 psycopg）") from e


# --------------------------------------------------------------------------
# 源数据 → (文件名, 正文)：v3 的 body jsonb 口径
# --------------------------------------------------------------------------

def iter_files(body: Any) -> Iterator[Tuple[str, str]]:
    """把一条 `skill_docs.body`（jsonb dict）里的 `files` 逐个吐出 `(文件名, 正文)`。

    v3 逐条口径（**都别顺手改**）：
    1. `body` 是 dict、正文在 `body["files"]` 里（不是一段大字符串）；
    2. `files` 的 value **不是 str 就跳过**（jsonb 里塞了 dict/list 的条目不炸）；
    3. 文件名后缀不在 `TEXT_EXT` 里就跳过（图片/二进制不进知识库）；
    4. 正文含 NUL（`chr(0)`）先 `replace(chr(0), "")` —— PG 的 text 存不下 NUL，
       不清掉整条 INSERT 会报 `unsupported Unicode escape sequence`。
    """
    files = body.get("files") if isinstance(body, dict) else None
    if not isinstance(files, dict):
        return
    for fname, fcontent in files.items():
        if not isinstance(fcontent, str):
            continue
        name = str(fname)
        if not name.endswith(TEXT_EXT):
            continue
        if chr(0) in fcontent:
            fcontent = fcontent.replace(chr(0), "")
        yield name, fcontent


# --------------------------------------------------------------------------
# 逐字判据（纯函数，单测直接钉这几条）
# --------------------------------------------------------------------------

def stype_for_category(category: Any) -> str:
    """`category == 'main'` ⇒ `'skill'`，其余（含空/None）⇒ `'skill_hk'`（v3 同款）。"""
    return "skill" if category == MAIN_CATEGORY else "skill_hk"


def source_key(node: Any, skill: Any, fname: str) -> str:
    """v3 的 `source_key`：`"<source_node>/<skill_name>/<文件名>"`。"""
    return f"{node}/{skill}/{fname}"


def doc_hash(sha: Any, fname: str) -> str:
    """v3 的嵌合式 hash：`"<sha256 列>:<文件名>"`（sha 为 NULL 时前段空串）。"""
    return f"{sha or ''}:{fname}"


def doc_title(skill: Any, fname: str) -> str:
    """v3 的标题规则：主文档 `SKILL.md` 用裸技能名，其余拼 `"<skill> · <文件名>"`。"""
    return f"{skill}" if fname == MAIN_DOC else f"{skill} · {fname}"


def build_meta(node: Any, skill: Any, fname: str, category: Any, sha: Any) -> Dict[str, Any]:
    """v3 的 `meta` 五键：`node/skill/file/col/skill_sha`（**键名不许改**）。

    死文档清理就是拿 `meta->>'node'`/`meta->>'skill'` 跟 live 集合比 —— 少一个键
    或改个键名，历史行就再也认不出来，一轮 changed 就可能把它们全判成 dead 删掉。
    """
    return {"node": node, "skill": skill, "file": fname,
            "col": category, "skill_sha": sha or ""}


# --------------------------------------------------------------------------
# 死文档清理（v3 的元组口径，灌入器专属逻辑 —— 只在本文件里有）
# --------------------------------------------------------------------------

def _cell(row: Any, idx: int, key: str) -> Any:
    """按列名取值；默认游标（回元组）时退回列序号。"""
    return row[key] if isinstance(row, dict) else row[idx]


def find_dead_docs(cur: Any, schema: str, live: Set[Tuple[Any, Any, Any]],
                   *, names: Optional[Dict[str, str]] = None,
                   source_types: Sequence[str] = INGEST_SOURCE_TYPES) -> List[Any]:
    """列出要清理的死文档 id：**只**扫 `source_type IN ('skill','skill_hk')`。

    判据是 v3 的 `(source_type, meta->>'node', meta->>'skill')` 三元组，不在 live
    集里就死 —— 所以 `meta` 的键名（`node`/`skill`）是这条判据的地基。
    `meta` 里取不到值时得到 `(stype, None, None)`，同样不在 live 里 ⇒ 判死，
    与 v3 同款（不额外兜底免得把该清的留在库里）。

    手动域（`source_type='manual'`）连查都查不到：它不在 `source_types` 白名单里。
    """
    n = {**DEFAULT_NAMES, **(names or {})}
    cur.execute(f"SELECT {n['doc_id']}, {n['src_type']}, "
                f"({n['meta']}->>'node') AS node, ({n['meta']}->>'skill') AS skill "
                f"FROM {schema}.{n['documents']} "
                f"WHERE {n['src_type']} = ANY(%s) ORDER BY {n['doc_id']}",
                [list(source_types)])
    dead: List[Any] = []
    for row in cur.fetchall():
        stype = _cell(row, 1, n["src_type"])
        if (stype, _cell(row, 2, "node"), _cell(row, 3, "skill")) not in live:
            dead.append(_cell(row, 0, n["doc_id"]))
    return dead


def delete_dead_docs(cur: Any, schema: str, doc_ids: Sequence[Any],
                     *, names: Optional[Dict[str, str]] = None) -> int:
    """按 id 批量删死文档的 chunks + 文档行，返回删掉的文档数。

    先 chunks 后 documents（生产表不一定建了级联外键）。`--dry` 走不到这里 ——
    预演只报 `dead_docs_would_remove`，不删。
    """
    n = {**DEFAULT_NAMES, **(names or {})}
    if not doc_ids:
        return 0
    ids = [int(i) for i in doc_ids]
    cur.execute(f"DELETE FROM {schema}.{n['chunks']} WHERE {n['chunk_doc']} = ANY(%s::bigint[])",
                [ids])
    cur.execute(f"DELETE FROM {schema}.{n['documents']} WHERE {n['doc_id']} = ANY(%s::bigint[]) "
                f"RETURNING {n['doc_id']}", [ids])
    deleted = 0
    while cur.fetchone() is not None:
        deleted += 1
    return deleted


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def ingest(args: argparse.Namespace) -> Dict[str, Any]:
    schema = args.schema
    names = DEFAULT_NAMES
    stats: Dict[str, Any] = {
        "added": 0, "updated": 0, "unchanged": 0,
        "files": 0, "skills": 0,
        "dead_docs_removed": 0, "dead_docs_would_remove": 0, "db": [],
    }
    #: live = {(source_type, source_node, skill_name)}，清理判据的 live 集（v3 口径）
    live: Set[Tuple[Any, Any, Any]] = set()

    src = _connect("KB_SRC_DSN", "KB_SRC_PASS_FILE", "/root/.kbpublisher.pass",
                   host="127.0.0.1", port="5432", dbname="keepsake", user="kbpublisher",
                   prefix="KB_SRC")
    dst = _connect("KB_PG_DSN", "KB_PG_PASS_FILE", "/root/.kb_pgapp.pass",
                   host="127.0.0.1", port="5432", dbname="keepsake", user="kbapp",
                   prefix="KB_PG")
    with src, src.cursor() as scur, dst, dst.cursor() as dcur:
        # ORDER BY：幂等对照要行序同构，逐字段对 JSON 才干净
        scur.execute("SELECT source_node, skill_name, category, sha256, body "
                     "FROM skill_docs ORDER BY source_node, skill_name")
        for node, skill, category, sha, body in scur.fetchall():
            stats["skills"] += 1
            stype = stype_for_category(category)
            for fname, text in iter_files(body):
                stats["files"] += 1
                live.add((stype, node, skill))
                status, _doc_id, _n = upsert_document(
                    dcur, schema, names=names, source_type=stype,
                    source_key=source_key(node, skill, fname),
                    title=doc_title(skill, fname),
                    meta=build_meta(node, skill, fname, category, sha),
                    content_hash=doc_hash(sha, fname), text=text)
                stats[status] += 1

        dead = find_dead_docs(dcur, schema, live, names=names)
        if args.dry:
            stats["dead_docs_would_remove"] = len(dead)
        else:
            stats["dead_docs_removed"] = delete_dead_docs(dcur, schema, dead, names=names)

        # db 汇总：每个 source_type 的文档数与切片数（v3 的 `db` 字段）
        dcur.execute(
            f"SELECT d.{names['src_type']}, COUNT(DISTINCT d.{names['doc_id']}), "
            f"COUNT(c.{names['chunk_doc']}) "
            f"FROM {schema}.{names['documents']} d "
            f"LEFT JOIN {schema}.{names['chunks']} c "
            f"ON c.{names['chunk_doc']} = d.{names['doc_id']} "
            f"GROUP BY d.{names['src_type']} ORDER BY d.{names['src_type']}")
        stats["db"] = [list(r) for r in dcur.fetchall()]

        if args.dry:
            dst.rollback()
        else:
            dst.commit()
    return stats


def main(argv: Any = None) -> int:
    ap = argparse.ArgumentParser(description="KB-PG 灌入器（skill_docs → documents/chunks）")
    ap.add_argument("--dry", action="store_true",
                    help="预演：统计口径照算，零写入（结束时回滚）")
    ap.add_argument("--schema", default=(os.environ.get("KB_PG_SCHEMA") or "public"),
                    help="目标 schema（默认 public，可用 KB_PG_SCHEMA 覆盖）")
    args = ap.parse_args(argv)
    stats = ingest(args)
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
