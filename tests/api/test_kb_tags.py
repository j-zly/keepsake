"""知识库「自由标签」（tags）这一层的测试（keepsake_kb_tags）。

**标签是第三个维度，不是第二个内容类型**：content_type 是封闭枚举（写面白名单），
标签是开放维度（量化/部署/投资…），一篇可挂多个、随时可改、**只影响筛选**。
本文件钉死六件最容易悄悄坏掉的事：

  1. **净化函数**（`_clean_tags`，纯函数不碰库）：strip / 去空 / 去重保序 /
     单项 ≤32 字 / 最多 12 个 / 拒控制字符（含换行）；违规一律 400，**不静默截断**；
  2. `?tag=` 过滤是精确 `'tag' = ANY(tags)` 且**值走参数**（拼串 = 把用户输入
     当代码解释）；与 q / source_type / content_type 取 AND；
  3. update 带 tags 是**整体替换**（不是追加）—— 不带才是不动；
  4. `/kb/tags` 的计数来自库里（含中文标签），前端零写死；
  5. 导出 JSONL 带 tags、导入行吃 tags ⇒ **往返一致**；
  6. 老库未迁移 `tags` 列：读路径降级成空数组（出参不带键），写路径人话 500 带迁移 SQL。

真库用例与 test_kb_crud 共用同一套 fixture（同一个隔离 schema 的建表 DDL —— 那里
已同步加上 `tags text[] NOT NULL DEFAULT '{}'` 与 GIN 索引，保持与生产迁移**同形状**）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List

import pytest
from fastapi import HTTPException

from keepsake.api.routers import kb as kb_router

# 共用 fixture 与建表 DDL（测试库形状与生产同款，含 tags 列）
from test_kb_crud import (                                                    # noqa: F401
    AUTH, CREATE, EXPORT, IMPORT, PG_ONLY, create, docs, kb, legacy_kb,
    raw, search, seed_skill, _kb_with,
)

#: 生产迁移 SQL（与主脑手执行的形状逐字一致；测试跑它来保证幂等 + 真能迁）
MIGRATE_TAGS_SQL = (
    'ALTER TABLE "{s}".documents ADD COLUMN IF NOT EXISTS '
    "tags text[] NOT NULL DEFAULT '{{}}'",   # ⚠️ 双花括号：下面要 .format(s=schema)
    'CREATE INDEX IF NOT EXISTS idx_documents_tags_gin ON "{s}".documents USING gin(tags)',
)

#: 「一期已迁移、二期还没迁」的老表形状：**有 content_type、没有 tags** ——
#: 这是真实上线顺序下的窗口期（content_type 那期已上线），缺列防御必须在这张表上验，
#: 否则 create 会先被 `_need_ctype` 拦下、永远走不到标签那道人话 500。
DDL_CTYPE_NO_TAGS = (
    'CREATE SCHEMA "{s}"',
    'CREATE TABLE "{s}".documents ('
    ' id bigserial PRIMARY KEY, source_type text NOT NULL, source_key text NOT NULL,'
    ' content_type text NOT NULL DEFAULT \'note\','
    ' title text, meta jsonb, content_hash text,'
    ' updated_at timestamptz NOT NULL DEFAULT now(),'
    ' UNIQUE (source_type, source_key))',
    'CREATE TABLE "{s}".chunks ('
    ' id bigserial PRIMARY KEY, document_id bigint NOT NULL'
    ' REFERENCES "{s}".documents(id), ord int NOT NULL, text text, source_path text)',
)


@pytest.fixture()
def no_tags_kb(monkeypatch, tmp_path) -> Iterator[Any]:
    """隔离 schema：content_type 已迁移、**tags 列还没有**（缺列窗口期的真实形状）。"""
    yield from _kb_with(monkeypatch, tmp_path, DDL_CTYPE_NO_TAGS)


@pytest.fixture(autouse=True)
def _audit_tmp(monkeypatch, tmp_path) -> Iterator[None]:
    """纯函数用例也会走 `_reject` ⇒ `audit.record` 写文件；别让护栏测试往真审计目录里写。"""
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    yield


# ---------------------------------------------------------------- 净化函数（不碰库）

def clean(raw: Any) -> List[str]:
    return kb_router._clean_tags(raw, "test.tags", "t")


def bad(raw: Any) -> Dict[str, Any]:
    """跑净化并把 HTTPException 的 detail 掏出来（断言错误码与文案）。"""
    with pytest.raises(HTTPException) as e:
        clean(raw)
    assert e.value.status_code == 400, e.value.detail
    return e.value.detail


def test_净化_逐项strip_去空_去重保序():
    assert clean(["  量化  ", "", "部署", "量化", "  量化", "   "]) == ["量化", "部署"]
    assert clean(None) == [] and clean([]) == []
    assert clean(["量化"]) == ["量化"]


def test_净化_单项超32字_400不静默截断():
    """🔴 超长必须 400：悄悄砍到 32 字会让用户按原标签筛时「标签坏了」。"""
    assert clean(["量" * 32]) == ["量" * 32]
    d = bad(["量" * 33])
    assert d["error"] == "bad_tag" and "32" in d["message"]


def test_净化_超过12个_400():
    assert len(clean([f"标签{i}" for i in range(12)])) == 12   # 正好 12 个：放行
    d = bad([f"标签{i}" for i in range(13)])
    assert d["error"] == "too_many_tags" and "12" in d["message"]
    # 去重后不超过 12 ⇒ 放行（先按判据清干净再数数目）
    assert len(clean(["量化"] * 20)) == 1


@pytest.mark.parametrize("bad_tag", ["量化\n部署", "量化\t部署", "量化\r部署", "量\x00化", "量\x7f化"])
def test_净化_控制字符含换行_400(bad_tag: str):
    """🔴 换行/制表会同时搞坏 chip 行与导出的 JSONL 行 ⇒ 一律拒。"""
    d = bad([bad_tag])
    assert d["error"] == "bad_tag"


@pytest.mark.parametrize("raw", ["量化", 42, {"a": 1}, ["量化", 7], ["量化", None]])
def test_净化_非字符串项与非列表_400(raw: Any):
    d = bad(raw)
    assert d["error"] == "bad_tag"


def test_净化_中文字数按字算不按字节():
    assert clean(["部" * 32]) == ["部" * 32]           # 96 字节仍是 32 字，放行


# ---------------------------------------------------------------- 写面：create / update

@PG_ONLY
def test_新增带tags_落库且列表出参带tags(kb):
    body = create(kb.client, "量化笔记", "动量因子", tags=[" 量化 ", "部署", "量化", ""])
    assert raw(kb, 'SELECT tags FROM "{s}".documents WHERE id=%s',
               [body["id"]]) == [(["量化", "部署"],)]
    item = docs(kb.client)["items"][0]
    assert item["tags"] == ["量化", "部署"]            # 顺序 = 输入顺序（去重保序）


@PG_ONLY
def test_新增不带tags_出参不带这个键_不假装每篇都打了标(kb):
    """🔴 键缺席 ≠ 假装有标签：前端按「没打标」渲染（卡片上没有 chip）。"""
    create(kb.client, "没打标的手记", "正文")
    assert raw(kb, 'SELECT tags FROM "{s}".documents') == [([],)]
    assert "tags" not in docs(kb.client)["items"][0]


@PG_ONLY
@pytest.mark.parametrize("tags", [["量" * 33], [f"标签{i}" for i in range(13)], ["a\nb"]])
def test_新增_标签违规_400且一条都不落库(kb, tags):
    r = kb.client.post(CREATE, json={"confirm": True, "title": "标题", "content": "正文",
                                     "tags": tags}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] in ("bad_tag", "too_many_tags")
    assert docs(kb.client)["items"] == []


@PG_ONLY
def test_改_带tags是整体替换不是追加(kb):
    """🔴 替换语义：前端每次提交都是全量 chip，追加会让「删掉一个标签」永远删不掉。"""
    doc = create(kb.client, "量化笔记", "正文", tags=["量化", "部署"])
    r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/update",
                       json={"confirm": True, "tags": ["投资"]}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert raw(kb, 'SELECT tags FROM "{s}".documents WHERE id=%s',
               [doc["id"]]) == [(["投资"],)]
    # 给空数组 = 清空全部标签
    r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/update",
                       json={"confirm": True, "tags": []}, headers=AUTH)
    assert r.status_code == 200
    assert raw(kb, 'SELECT tags FROM "{s}".documents WHERE id=%s',
               [doc["id"]]) == [([],)]


@PG_ONLY
def test_改_不带tags_标签原样不动(kb):
    """不带 = 不动（`None` 与「给了空数组」是两件事：后者才是清空）。"""
    doc = create(kb.client, "量化笔记", "正文", tags=["量化"])
    r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/update",
                       json={"confirm": True, "title": "量化笔记（新）"}, headers=AUTH)
    assert r.status_code == 200
    assert raw(kb, 'SELECT tags FROM "{s}".documents WHERE id=%s',
               [doc["id"]]) == [(["量化"],)]


@PG_ONLY
def test_改_只改标签不重切切片(kb):
    """标签与正文无关：只改标签不该动 chunks（否则 updated_at 白抖一遍）。"""
    doc = create(kb.client, "长文", "# 甲\n" + "内容" * 2000)
    before = raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s ORDER BY ord',
                 [doc["id"]])
    r = kb.client.post(f"/api/v1/kb/documents/{doc['id']}/update",
                       json={"confirm": True, "tags": ["量化"]}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["chunks_count"] == len(before)
    assert raw(kb, 'SELECT text FROM "{s}".chunks WHERE document_id=%s ORDER BY ord',
               [doc["id"]]) == before


@PG_ONLY
def test_标签不进内容判据_改标签不换文档也不重复入库(kb):
    """🔴 标签**不进** source_key / content_hash：进了就等于「改个标签变成换一篇文档」。"""
    first = create(kb.client, "值班手册", "联系人是张三")
    r = kb.client.post(f"/api/v1/kb/documents/{first['id']}/update",
                       json={"confirm": True, "tags": ["部署"]}, headers=AUTH)
    assert r.json()["source_key"] == first["source_key"]
    again = create(kb.client, "值班手册", "联系人是张三", tags=["部署"])
    assert (again["status"], again["id"]) == ("unchanged", first["id"])
    assert len(docs(kb.client)["items"]) == 1


@PG_ONLY
def test_改skill文档带tags仍409(kb):
    """skill 域纪律不变：标签也归灌入器，界面不给入口、后端 409 兜底。"""
    doc_id = seed_skill(kb)
    r = kb.client.post(f"/api/v1/kb/documents/{doc_id}/update",
                       json={"confirm": True, "tags": ["偷打"]}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "kb_doc_immutable"
    assert raw(kb, 'SELECT tags FROM "{s}".documents WHERE id=%s', [doc_id]) == [([],)]


# ---------------------------------------------------------------- 筛选

@PG_ONLY
def test_列表按tag过滤_正负与AND(kb):
    create(kb.client, "量化一篇", "甲", tags=["量化", "投资"])
    create(kb.client, "部署一篇", "乙", tags=["部署"], content_type="article")
    create(kb.client, "没打标一篇", "丙")
    seed_skill(kb)

    got = docs(kb.client, tag="量化")
    assert [i["title"] for i in got["items"]] == ["量化一篇"]
    assert got["count"] == 1                                  # 不是全库 4
    # 负例：没挂这个标的一篇都不许回（精确匹配，不是前缀/模糊）
    assert docs(kb.client, tag="部")["items"] == []
    assert docs(kb.client, tag="不存在的标")["items"] == []
    # 与 content_type 取 AND
    assert docs(kb.client, tag="量化", content_type="article")["items"] == []
    assert docs(kb.client, tag="部署", content_type="article")["count"] == 1


def test_列表tag过滤_精确ANY且值走参数_与q取AND():
    """🔴 不碰库的接线判据：`'tag' = ANY(tags)` 精确匹配、值不拼进 SQL 文本，
    `count` 复用同一条 WHERE 与同一组参数（只断列表不断 count = 计数口径漂）。"""
    calls: List[tuple] = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            calls.append((" ".join(sql.split()), params))
        def fetchall(self):
            return [{"doc_id": "1", "title": "量化一篇", "updated_at": "",
                     "source_type": "manual", "content_type": "note",
                     "tags": ["量化"], "chunk_count": 1}]
        def fetchone(self):
            return {"n": 1}

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    cols = {"documents": {"id", "source_type", "title", "tags"},
            "chunks": {"id", "document_id", "text"}}
    saved_cols, saved_conn = kb_router._table_columns, kb_router._connect
    kb_router._table_columns = lambda schema, tables: cols       # type: ignore[assignment]
    kb_router._connect = lambda: _Conn()                         # type: ignore[assignment]
    try:
        body = kb_router.kb_documents(q="量化", source_type="", content_type="",
                                      tag="量化' OR 1=1 --", page=1, page_size=20)
    finally:
        kb_router._table_columns = saved_cols                    # type: ignore[assignment]
        kb_router._connect = saved_conn                          # type: ignore[assignment]

    list_sql, list_params = calls[0]
    count_sql, count_params = calls[1]
    assert "AS tags" in list_sql                                # 出参必须投影这根列
    assert "%s::text = ANY(d.tags)" in list_sql
    # 注入串只在参数里，SQL 文本里一个字都不许有
    assert "OR 1=1" not in list_sql and "OR 1=1" not in count_sql
    assert list_params == ["%量化%", "量化' OR 1=1 --", 20, 0]
    assert count_sql.rsplit("WHERE", 1)[1].strip() == \
        list_sql.rsplit("WHERE", 1)[1].split("ORDER BY")[0].strip()
    assert count_params == list_params[:2]
    assert body.items[0].tags == ["量化"]


# ---------------------------------------------------------------- /kb/tags 计数

@PG_ONLY
def test_标签计数接口_与库里同口径含中文(kb):
    """chip 上的数字必须来自库里（前端不写死）；中文标签照样算得出来。"""
    create(kb.client, "量化一篇", "甲", tags=["量化", "投资"])
    create(kb.client, "量化二篇", "乙", tags=["量化"])
    create(kb.client, "部署一篇", "丙", tags=["部署", "投资"])
    create(kb.client, "没打标", "丁")
    seed_skill(kb)                       # skill 域恒空标签，不该出现在计数里
    r = kb.client.get("/api/v1/kb/tags", headers=AUTH)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert {i["tag"]: i["count"] for i in items} == {"量化": 2, "投资": 2, "部署": 1}
    # 按计数倒序、同数按标签名（稳定 ⇒ 前端 chip 行不会每次刷新都跳）
    assert [i["tag"] for i in items] == ["投资", "量化", "部署"]


@PG_ONLY
def test_标签计数_一篇里重复的同一个标只算一篇(kb):
    """存量脏数据（同一篇里同一个标出现两次）不许把计数灌水。"""
    import psycopg

    with psycopg.connect(kb.dsn, autocommit=True, connect_timeout=30) as conn, \
            conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{kb.schema}".documents'
                    "(source_type, source_key, title, tags) VALUES"
                    " ('manual','manual:dirty','脏数据', ARRAY['量化','量化'])")
    got = kb.client.get("/api/v1/kb/tags", headers=AUTH).json()["items"]
    assert got == [{"tag": "量化", "count": 1}]


@PG_ONLY
def test_检索结果投影tags(kb):
    """切片命中行也带得出所属文档的标签（前端切片卡渲染 chip 靠它）。"""
    create(kb.client, "量化一篇", "讲讲动量因子", tags=["量化"])
    item = next(i for i in search(kb.client, "动量因子")["items"])
    assert item["tags"] == ["量化"]


# ---------------------------------------------------------------- 导出 / 导入往返

@PG_ONLY
def test_导出带tags_且skill域恒空(kb):
    """导出行带**数组** tags；skill 文档恒空数组（灌入器一个字都不往这根列写）。"""
    create(kb.client, "量化一篇", "甲", tags=["量化", "投资"])
    create(kb.client, "部署一篇", "乙", tags=["部署"])
    seed_skill(kb)
    rows = [json.loads(line) for line in
            kb.client.get(EXPORT, headers=AUTH).text.splitlines() if line.strip()]
    got = {r["title"]: r["tags"] for r in rows}
    assert got["量化一篇"] == ["量化", "投资"]
    assert got["部署一篇"] == ["部署"]
    assert got["灌入器技能文档"] == []               # skill 域恒空（灌入器不写这根列）
    # 导出与列表同源同口径（前端「导出的就是屏幕上看到的」）。
    # 只比手动域两篇：skill 文档出参**不带** tags 键（空数组被 exclude），
    # 顺手也钉住了「灌入器产物不打标」这条纪律。
    assert {i["title"]: i["tags"] for i in docs(kb.client)["items"]
            if i["source_type"] == "manual"} == \
        {"量化一篇": ["量化", "投资"], "部署一篇": ["部署"]}
    assert all("tags" not in i for i in docs(kb.client)["items"]
               if i["source_type"] != "manual")


@PG_ONLY
def test_导出再导入_标签往返一致(kb):
    """🔴 真往返：导出 → 真删 → 把导出的行原样喂回 import ⇒ 标签一模一样
    （导出丢标签 = 备份/迁移链路静默丢维度）。"""
    create(kb.client, "量化一篇", "甲", tags=["量化", "投资"])
    row = next(json.loads(line) for line in
               kb.client.get(EXPORT, headers=AUTH).text.splitlines() if line.strip())
    doc_id = row["id"]
    d = kb.client.post(f"/api/v1/kb/documents/{doc_id}/delete",
                       json={"confirm": "量化一篇"}, headers=AUTH)
    assert d.status_code == 200, d.text
    # 只保留导入认的键（id / source_key / meta / content_type / chunks 是导出侧字段，
    # 导入 extra=forbid 会 422）；正文在 `chunks` 里，拼回来就是原文（单块文档无损）
    back = {"title": row["title"], "content": "\n".join(row["chunks"]), "tags": row["tags"]}
    r = kb.client.post(IMPORT, json={"items": [back], "dry_run": False, "confirm": "1"},
                       headers=AUTH)
    assert r.status_code == 200, r.text
    assert raw(kb, 'SELECT tags FROM "{s}".documents WHERE title=%s',
               ["量化一篇"]) == [(["量化", "投资"],)]


@PG_ONLY
def test_导入条目可带tags_且走同一净化(kb):
    r = kb.client.post(IMPORT, json={"items": [
        {"title": "A", "content": "正文甲", "tags": [" 量化 ", "量化", ""]},
        {"title": "B", "content": "正文乙", "tags": ["投资"]},
    ], "dry_run": False, "confirm": "2"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert raw(kb, 'SELECT title, tags FROM "{s}".documents ORDER BY title') == \
        [("A", ["量化"]), ("B", ["投资"])]         # 去重 + strip 都在
    # 违规标签 ⇒ 400（导入不是后门，净化与创建端点同款）
    bad_r = kb.client.post(IMPORT, json={"items": [
        {"title": "C", "content": "正文丙", "tags": ["量" * 33]}]}, headers=AUTH)
    assert bad_r.status_code == 400
    assert bad_r.json()["detail"]["error"] == "bad_tag"


# ---------------------------------------------------------------- 缺列防御（老库）

@PG_ONLY
def test_迁移SQL_幂等且真能把老表迁起来(legacy_kb):
    """探针：隔离 schema 里造一张**没有 tags 列的老表** → 跑生产迁移 SQL → 列出现且
    存量行拿到 DEFAULT '{}'；再跑一遍 ⇒ 幂等不炸。"""
    import psycopg

    assert not raw(legacy_kb, "SELECT 1 FROM information_schema.columns "
                               "WHERE table_schema='{s}' AND table_name='documents' "
                               "AND column_name='tags'")
    with psycopg.connect(legacy_kb.dsn, autocommit=True, connect_timeout=30) as conn, \
            conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{legacy_kb.schema}".documents'
                    "(source_type, source_key, title) VALUES"
                    " ('manual','manual:old','老库手记')")
        for stmt in MIGRATE_TAGS_SQL:                       # 跑两遍：幂等
            cur.execute(stmt.format(s=legacy_kb.schema))
            cur.execute(stmt.format(s=legacy_kb.schema))
    assert raw(legacy_kb, "SELECT tags FROM \"{s}\".documents") == [([],)]


@PG_ONLY
def test_缺列_写端点人话报错带迁移SQL(no_tags_kb):
    """🔴 防「主脑忘了先迁移」的窗口期：老库上写标签要拿到带迁移 SQL 的**人话** 500，
    而不是 PG 的 `column "tags" does not exist` 裸 500。"""
    for url, body in ((CREATE, {"confirm": True, "title": "t", "content": "c",
                                "tags": ["量化"]}),
                      (IMPORT, {"items": [{"title": "t", "content": "c",
                                           "tags": ["量化"]}]})):
        r = no_tags_kb.client.post(url, json=body, headers=AUTH)
        assert r.status_code == 500, (url, r.status_code)
        detail = r.json()["detail"]
        assert detail["error"] == "kb_schema_unknown"
        assert "tags" in detail["message"] and "ALTER TABLE" in detail["message"]
    # 按标签筛也筛不了就说筛不了（不静默给全量）
    r = no_tags_kb.client.get("/api/v1/kb/documents?tag=%E9%87%8F%E5%8C%96", headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "kb_schema_unknown"
    # 只改标签也算写：人话 500，且文档一个字节都没动
    r = no_tags_kb.client.post("/api/v1/kb/documents/1/update",
                               json={"confirm": True, "tags": ["量化"]}, headers=AUTH)
    assert r.status_code == 500 and r.json()["detail"]["error"] == "kb_schema_unknown"


@PG_ONLY
def test_缺列_读路径降级为空数组(no_tags_kb):
    """🔴 缺 tags 列的库上只读端点照常跑：列表/检索/导出/标签计数都不炸，且**不带**
    tags 键（缺省不假装有值）；标签计数给空列表（前端整行隐藏，不编 chip）。
    content_type 还在（那期已上线）⇒ 出参照常带 content_type，不受本期影响。"""
    import psycopg

    with psycopg.connect(no_tags_kb.dsn, autocommit=True, connect_timeout=30) as conn, \
            conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{no_tags_kb.schema}".documents'
                    "(source_type, source_key, title, meta, content_hash) VALUES"
                    " ('manual','manual:abc','老库手记','{}'::jsonb,'sha:x')")
        cur.execute(f'INSERT INTO "{no_tags_kb.schema}".chunks(document_id, ord, text)'
                    " VALUES (1, 0, '老库正文')")

    items = docs(no_tags_kb.client)["items"]
    assert [i["title"] for i in items] == ["老库手记"]
    assert "tags" not in items[0]
    assert items[0]["content_type"] == "note"        # 一期那根列照常投影（零回归）
    hits = search(no_tags_kb.client, "老库正文")["items"]
    assert hits and "tags" not in hits[0]
    assert kb_router._tags_out(None) == []            # 内部归一：NULL ⇒ 空数组
    rows = [json.loads(line) for line in
            no_tags_kb.client.get(EXPORT, headers=AUTH).text.splitlines() if line.strip()]
    assert rows[0]["tags"] == []                     # 导出行给空数组（不假装有值）
    assert kb_router._tags_out(["量化", "部署"]) == ["量化", "部署"]


@PG_ONLY
def test_缺列_标签计数接口给空列表(no_tags_kb):
    assert kb_router._tags_out("{量化,部署}") == ["量化", "部署"]   # PG 文本形态也收得下
    r = no_tags_kb.client.get("/api/v1/kb/tags", headers=AUTH)
    assert r.status_code == 200 and r.json()["items"] == []
