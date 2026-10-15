#!/usr/bin/env python3
"""KB-PG 灌入器（仓库版）：PG skill_docs(唯一存储) → documents/chunks。

收编自 180 本机 `kb_ingest_v3.py`。**切块与 hash 判据不再本地一份** —— 一律
`from keepsake.kb.ingest import split_chunks, upsert_document, ...`，与控制台写
端点共用同一份（`src/keepsake/kb/ingest.py` 是唯一真源）。

行为等价约定（与 v3 同款，别顺手改）：

* 幂等判据：`(source_type, source_key)` 上 `content_hash` 嵌合式 `'sha256:file'`
  不变 ⇒ `unchanged`（连 chunks 都不动）；
* `key = "<source_node>/<skill_name>/<文件名>"`，`hash = "<sha256 列>:<文件名>"`；
* 死文档清理：**只**删 `source_type IN ('skill','skill_hk')` 且键不在 live 集里的
  —— 手动文档（`source_type='manual'`）是控制台的地盘，本脚本永不动它；
* stats 以 JSON 打到 stdout（`added/updated/unchanged/deleted/total/dry_run`）。

口令一律走 **环境变量或口令文件路径**（默认对齐 180 现部署），源码零明文：

* 目标库：env `KB_PG_DSN`；没给就读 `KB_PG_PASS_FILE`（默认 `/root/.kb_pgapp.pass`），
  配上 `KB_PG_HOST/PORT/DBNAME/USER`（默认 127.0.0.1:5432/keepsake/kbapp）；
* 源库（kbpublisher 的 skill_docs）：env `KB_SRC_DSN`；没给读
  `KB_SRC_PASS_FILE`（默认 `/root/.kbpublisher.pass`），默认库名 `kbpublisher`。

用法：`python3 scripts/kb_ingest.py --dry`（预演，零写入）/ 不带 `--dry` 实跑。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

# 仓库没装进 site-packages 时（180 直接跑脚本）把 src 挂上
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from keepsake.kb.ingest import (  # noqa: E402 —— 路径补齐后才能 import
    prune_ingested,
    split_chunks,
    upsert_document,
)

#: 重导出说明：本模块**不**自己切块（`upsert_document` 内部调的就是
#: `split_chunks`）。这里 import 一份是为了让「API 与 CLI 拿到的是同一个函数
#: 对象」能被直接断言（测试 test_kb_crud 里就是这么钉的）。
__all__ = ["split_chunks", "upsert_document", "prune_ingested",
           "iter_files", "source_key", "doc_hash", "main"]

#: 源库里一个正文里的「文件分界」标记行：`# file: xxx.md` / `# 文件：xxx.md`
_FILE_MARKER_RE = re.compile(r"^\s*#{1,6}\s*(?:file|文件)\s*[:：]\s*(\S+)\s*$",
                             re.IGNORECASE)

#: 源表里一个技能的可切分文件名后缀（v3 的 TEXT_EXT 口径）
TEXT_EXT = (".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".sql", ".py", ".sh")


# --------------------------------------------------------------------------
# 连库
# --------------------------------------------------------------------------

def _connect(dsn_env: str, pass_env: str, default_pass_file: str,
             *, host: str, port: str, dbname: str, user: str):
    """开一条连接：env DSN 优先；否则口令文件 + 默认连接参数。

    口令**只**从文件/环境读，源码里不出现任何明文；文件读不到直接报错退出
    （静默退化成无口令连接只会得到一个看不懂的认证错误）。
    """
    dsn = (os.environ.get(dsn_env) or "").strip()
    if not dsn:
        pw_path = Path((os.environ.get(pass_env) or "").strip() or default_pass_file)
        if not pw_path.exists():
            raise SystemExit(f"{dsn_env} 未设，且口令文件不存在：{pw_path}")
        password = pw_path.read_text(encoding="utf-8").strip()
        dsn = (f"host={os.environ.get('KB_PG_HOST', host)} "
               f"port={os.environ.get('KB_PG_PORT', port)} "
               f"dbname={os.environ.get('KB_PG_DBNAME', dbname)} "
               f"user={os.environ.get('KB_PG_USER', user)} password={password}")
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
# 源数据 → (文件名, 正文)
# --------------------------------------------------------------------------

def iter_files(body: str, fallback_name: str) -> Iterator[Tuple[str, str]]:
    """把一条 `skill_docs.body` 切成 `(文件名, 正文)`。

    有文件标记行就按标记切段（一个文件一段）；**没有标记就整篇当一个文件**
    （文件名由 sha 派生）—— 两种情况下都保证至少产出一段，灌入不会因为
    某条记录没有标记而悄悄少一篇。
    """
    buf: List[str] = []
    pending = ""            # 最近一次文件标记里的文件名
    found = False
    for line in (body or "").splitlines():
        m = _FILE_MARKER_RE.match(line)
        if m:
            found = True
            text = "\n".join(buf).strip()
            if text:
                yield pending or fallback_name, text
            buf = []
            pending = m.group(1)
        else:
            buf.append(line)
    text = "\n".join(buf).strip()
    if text or not found:
        yield pending or fallback_name, text


def source_key(node: str, skill: str, fname: str) -> str:
    return f"{node}/{skill}/{fname}"


def doc_hash(sha: str, fname: str) -> str:
    """v3 的嵌合式 hash：`"<sha256 列>:<文件名>"`。"""
    return f"{sha or ''}:{fname}"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def ingest(args: argparse.Namespace) -> Dict[str, Any]:
    schema = args.schema
    stats = {"added": 0, "updated": 0, "unchanged": 0,
             "deleted": 0, "total": 0, "dry_run": bool(args.dry)}
    live: List[str] = []

    src = _connect("KB_SRC_DSN", "KB_SRC_PASS_FILE", "/root/.kbpublisher.pass",
                   host="127.0.0.1", port="5432", dbname="kbpublisher", user="kbpublisher")
    dst = _connect("KB_PG_DSN", "KB_PG_PASS_FILE", "/root/.kb_pgapp.pass",
                   host="127.0.0.1", port="5432", dbname="keepsake", user="kbapp")
    with src, src.cursor() as scur, dst, dst.cursor() as dcur:
        scur.execute("SELECT source_node, skill_name, category, sha256, body "
                     "FROM skill_docs")
        for node, skill, category, sha, body in scur.fetchall():
            meta = {"category": category, "source": "skill_docs"}
            for fname, text in iter_files(body or "", f"{(sha or '')[:12] or 'body'}.md"):
                key = source_key(str(node or ""), str(skill or ""), fname)
                live.append(key)
                stats["total"] += 1
                if args.dry:
                    continue
                status, _doc_id, _n = upsert_document(
                    dcur, schema, source_type="skill", source_key=key,
                    title=f"{skill} · {fname}" if skill else fname,
                    meta=meta, content_hash=doc_hash(str(sha or ""), fname), text=text)
                stats[status] += 1
        if args.dry:
            dst.rollback()
        else:
            stats["deleted"] = prune_ingested(dcur, schema, live)
            dst.commit()
    return stats


def main(argv: Any = None) -> int:
    ap = argparse.ArgumentParser(description="KB-PG 灌入器（skill_docs → documents/chunks）")
    ap.add_argument("--dry", action="store_true",
                    help="预演：只统计不写入（零写入，结束时回滚）")
    ap.add_argument("--schema", default=(os.environ.get("KB_PG_SCHEMA") or "public"),
                    help="目标 schema（默认 public，可用 KB_PG_SCHEMA 覆盖）")
    args = ap.parse_args(argv)
    stats = ingest(args)
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
