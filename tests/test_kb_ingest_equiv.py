"""灌入器与 v3 的**行为等价**单测（纯函数，不连库）。

`scripts/kb_ingest.py` 是 180 上 `/opt/kb_ingest_v3.py` 的收编版，v3 是唯一行为
权威。这里把 D1-D10 逐条钉成断言 —— 收编版曾经在这几条上栽过：

* D1 `body` 是 jsonb，正文在 `body["files"]` 里（收编版曾把 body 当字符串切）；
* D2 `source_type` 按 category 分域（曾硬编码 `"skill"`）；
* D3 `meta` 五键（曾写成 `{"category","source"}` ⇒ 死文档清理认不出历史行）；
* D4 清理判据是 `(stype, node, skill)` 元组（曾按 source_key 前缀比）；
* D5 `SKILL.md` 的 title 不拼文件名（曾一律拼）；
* D6 `TEXT_EXT` 后缀过滤 + 非 str 跳过；
* D8 key/hash 逐字；D10 源库默认库名 `keepsake`。

任何一条变红 = 与生产 v3 行为分叉，别改测试去迁就脚本。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import kb_ingest  # noqa: E402


# --------------------------------------------------------------------------
# D1 / D6：body jsonb → (文件名, 正文)
# --------------------------------------------------------------------------

def test_body_jsonb_files_dict():
    """D1：`body` 是 jsonb dict，取 `files` 子 dict 逐条出（不是 splitlines）。"""
    body = {"files": {"SKILL.md": "主文档", "ref/x.py": "print(1)"}}
    assert list(kb_ingest.iter_files(body)) == [("SKILL.md", "主文档"),
                                                 ("ref/x.py", "print(1)")]


def test_iter_files_跳过非str与非法后缀_并清NUL():
    """D6：value 非 str 跳过、后缀不在 TEXT_EXT 跳过、含 NUL 先清掉。"""
    body = {"files": {
        "a.md": "甲\x00乙",          # NUL 要清
        "b.md": {"nested": 1},       # 非 str → 跳过
        "c.md": ["x"],               # 非 str → 跳过
        "d.png": "二进制不进库",      # 后缀不在 TEXT_EXT → 跳过
    }}
    assert list(kb_ingest.iter_files(body)) == [("a.md", "甲乙")]


def test_iter_files_body无files不炸():
    """body 缺 `files`/不是 dict ⇒ 产不出文件（不猜、不炸）。"""
    assert list(kb_ingest.iter_files({})) == []
    assert list(kb_ingest.iter_files(None)) == []
    assert list(kb_ingest.iter_files("一段裸字符串")) == []


# --------------------------------------------------------------------------
# D2 / D3 / D5 / D8：逐字判据
# --------------------------------------------------------------------------

def test_stype_by_category():
    """D2：`main` ⇒ `skill`，其余（含空/None/别的值）⇒ `skill_hk`。"""
    assert kb_ingest.stype_for_category("main") == "skill"
    assert kb_ingest.stype_for_category("sub") == "skill_hk"
    assert kb_ingest.stype_for_category("") == "skill_hk"
    assert kb_ingest.stype_for_category(None) == "skill_hk"


def test_title_rule():
    """D5：`SKILL.md` 用裸技能名；其余文件拼 `"<skill> · <文件名>"`。"""
    assert kb_ingest.doc_title("缠论", "SKILL.md") == "缠论"
    assert kb_ingest.doc_title("缠论", "ref.md") == "缠论 · ref.md"


def test_source_key_and_hash():
    """D8：key=`node/skill/fname`、hash=`sha:fname`（sha 为 NULL 时前段空串）。"""
    assert kb_ingest.source_key("n1", "缠论", "ref.md") == "n1/缠论/ref.md"
    assert kb_ingest.doc_hash("abc123", "ref.md") == "abc123:ref.md"
    assert kb_ingest.doc_hash(None, "ref.md") == ":ref.md"


def test_meta_shape_matches_v3():
    """D3：`meta` 键集必须正好是 `{node, skill, file, col, skill_sha}`。

    死文档清理拿 `meta->>'node'`/`meta->>'skill'` 跟 live 集合比 —— 键名漂了
    历史行就再也认不出来，一轮 changed 就会把它们全判成 dead 删掉。
    """
    meta = kb_ingest.build_meta("n1", "缠论", "ref.md", "main", "sha1")
    assert meta == {"node": "n1", "skill": "缠论", "file": "ref.md",
                    "col": "main", "skill_sha": "sha1"}
    assert set(meta) == {"node", "skill", "file", "col", "skill_sha"}


def test_meta_sha空时归一为空串():
    assert kb_ingest.build_meta("n1", "s", "SKILL.md", "main", None)["skill_sha"] == ""


# --------------------------------------------------------------------------
# D4：死文档清理按 (stype, node, skill) 元组，只扫 skill/skill_hk
# --------------------------------------------------------------------------

class _FakeCursor:
    """只记 SQL 与参数、把行集回给 fetchall 的假游标。"""

    def __init__(self, rows: List[Tuple[Any, ...]]) -> None:
        self.rows = rows
        self.sql: List[str] = []
        self.args: List[Any] = []

    def execute(self, sql: str, args: Any = None) -> None:
        self.sql.append(sql)
        self.args.append(args)

    def fetchall(self) -> List[Tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> Any:
        return self.rows.pop(0) if self.rows else None


@pytest.mark.parametrize("source_types", [("skill", "skill_hk")])
def test_live_tuple_prune_scope(source_types):
    """D4：live 集是 `(stype, node, skill)` 三元组；只查 skill/skill_hk 两域。"""
    live = {("skill", "n1", "缠论"), ("skill_hk", "n1", "缠论复盘")}
    cur = _FakeCursor([
        (1, "skill", "n1", "缠论"),        # live → 活
        (2, "skill_hk", "n1", "缠论复盘"),  # live → 活
        (3, "skill", "n1", "已下线"),       # 不在 live → 死
        (4, "skill_hk", "n2", "旧技能"),    # 别的节点 → 死
        (5, "skill", "n1", "别的技能"),     # 同节点不同技能 → 死
        (6, "skill_hk", None, None),        # meta 缺键 → v3 同款判死
    ])
    dead = kb_ingest.find_dead_docs(cur, "public", live, source_types=source_types)
    assert dead == [3, 4, 5, 6]
    # 判据只查 skill/skill_hk：手动域（manual）连查都不查
    assert "= ANY(%s)" in cur.sql[0]
    assert "node" in cur.sql[0] and "skill" in cur.sql[0]
    assert cur.args[0] == [["skill", "skill_hk"]]


def test_find_dead_docs_手动域永不在扫描范围():
    """手动文档（`source_type='manual'`）不进白名单 ⇒ 控制台写的东西永不被清。"""
    from keepsake.kb.ingest import MANUAL_SOURCE_TYPE, INGEST_SOURCE_TYPES

    assert MANUAL_SOURCE_TYPE not in INGEST_SOURCE_TYPES
    cur = _FakeCursor([(7, "manual", "n1", "我手写的")])
    kb_ingest.find_dead_docs(cur, "public", set())
    assert cur.args[0] == [["skill", "skill_hk"]]


def test_delete_dead_docs_空集合不发SQL():
    assert kb_ingest.delete_dead_docs(_FakeCursor([]), "public", []) == 0


# --------------------------------------------------------------------------
# D7 / D10：stats 结构与默认连接参数
# --------------------------------------------------------------------------

def test_stats结构_字段名与顺序对齐v3():
    """D7：stats 必须是 v3 那八个字段（既有日志监控按这个口径抓）。"""
    import inspect
    import re as _re

    src = inspect.getsource(kb_ingest.ingest)
    keys = _re.findall(r'"(added|updated|unchanged|files|skills|'
                       r'dead_docs_removed|dead_docs_would_remove|db)":', src)
    assert keys == ["added", "updated", "unchanged", "files", "skills",
                    "dead_docs_removed", "dead_docs_would_remove", "db"]


def test_源库默认连keepsake_不是kbpublisher():
    """D10：`skill_docs` 就在 `keepsake` 库（源侧 user 仍是 kbpublisher）。"""
    import inspect

    src = inspect.getsource(kb_ingest.ingest)
    assert 'dbname="keepsake", user="kbpublisher"' in src
    assert 'dbname="kbpublisher"' not in src


def test_源查询带ORDER_BY():
    """D9：行序同构 ⇒ 幂等对照时 diff 才干净。"""
    import inspect

    assert "ORDER BY source_node, skill_name" in inspect.getsource(kb_ingest.ingest)
