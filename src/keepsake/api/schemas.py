"""Pydantic 响应模型（只读控制台的出参契约）。

字段名与前端 `web/src/api` 一一对应；**任何模型里都不出现 token / DSN**。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# 常驻核心层
# --------------------------------------------------------------------------


class CoreItem(BaseModel):
    """常驻层条目（列表态：content 已截断）。"""

    key: str
    tags: str = ""
    content: str = Field("", description="截断后的正文；单条详情接口才是全文")
    updated_at: str = ""


class CoreList(BaseModel):
    items: List[CoreItem]
    count: int = Field(..., description="本条命中的总条数（非本页条数）")
    page: int
    page_size: int


class CoreStats(BaseModel):
    """常驻层统计：条数 / 字符数 / 注入预算占用。

    `chars` 与 `GET /api/v1/core` 的 items 同一数据源同一次读取口径
    （content 全文长度之和），所以 `count` 恒等于该接口 items 的总条数。
    """

    count: int
    chars: int
    budget: int = Field(..., description="注入字符预算（与 provider 注入路径同源）")
    percent: float = Field(..., description="chars / budget * 100，保留两位")


# --------------------------------------------------------------------------
# 记忆条目（检索层）
# --------------------------------------------------------------------------


class FragmentItem(BaseModel):
    """记忆条目列表项（含检索打分，q 为空时 score 为 None）。"""

    key: str
    tags: str = ""
    content: str = Field("", description="截断后的正文")
    updated_at: str = ""
    score: Optional[float] = None


class FragmentList(BaseModel):
    items: List[FragmentItem]
    count: Optional[int] = Field(
        None, description="过滤后的总条数（全库浏览扫到扫描上限时为 None，见 count_reason）")
    page: int
    page_size: int
    q: str = ""
    tag: str = ""
    has_more: bool = Field(..., description="是否还有下一页（前端翻页依据，与 count 无关也成立）")
    count_reason: str = Field(
        "", description="count 的口径来源：search_candidates（BM25 候选池）/ tag_exact / "
                        "scan_total（全库扫描真实总数）/ scan_capped（触扫描上限，count=null）")


class FragmentDetail(BaseModel):
    """记忆条目详情：全字段、全文。"""

    key: str
    content: str = ""
    tags: str = ""
    created_at: str = ""
    updated_at: str = ""
    score: Optional[float] = None
    raw: Dict[str, Any] = Field(default_factory=dict,
                                description="存储层返回的其余字段（BLOB 字段已剔除）")


# --------------------------------------------------------------------------
# 知识库
# --------------------------------------------------------------------------


class KbSummary(BaseModel):
    skill_docs: int
    documents: int
    chunks: int


class KbHit(BaseModel):
    chunk_id: str
    doc_id: str = ""
    title: str = ""
    snippet: str = Field("", description="命中片段（截断后的正文）")
    rank: float = 0.0


class KbSearchResult(BaseModel):
    q: str
    items: List[KbHit]
    count: int
    page: int
    page_size: int
    mode: str = Field(..., description="tsvector 或 ilike —— 命中路径，便于定位索引状态")


class KbDocument(BaseModel):
    """知识库文档列表项（chunk_count = 该文档的切片条数）。"""

    doc_id: str
    title: str = ""
    updated_at: str = Field("", description="表内可用的更新时间列；没有时间列时为空串")
    chunk_count: int = 0


class KbDocumentList(BaseModel):
    items: List[KbDocument]
    count: int = Field(..., description="过滤后的文档总数（非本页条数）")
    page: int
    page_size: int


class KbChunkList(BaseModel):
    """单个文档的切片列表；切片项复用 KbHit（浏览单个文档没有检索词，rank 恒 0、title 空串）。"""

    doc_id: str
    items: List[KbHit]
    count: int
    page: int
    page_size: int


# --------------------------------------------------------------------------
# 总览
# --------------------------------------------------------------------------


class SectionError(BaseModel):
    """分域失败标记：**显式报错，不是「空数据装成功」**。"""

    available: bool = False
    error: str


class Overview(BaseModel):
    core: CoreStats
    retrieval: Dict[str, Any] = Field(
        ..., description="{count, truncated}：存储层无 count 原语，按 keyset 扫描有上限地统计")
    kb: Dict[str, Any] = Field(..., description="同 KbSummary；不可用时为 {available:false,error:...}")


# --------------------------------------------------------------------------
# 定时任务
# --------------------------------------------------------------------------


class CronJob(BaseModel):
    name: str
    script: str = ""
    schedule: str = ""
    enabled: bool = True
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None


class CronList(BaseModel):
    jobs: List[CronJob]
    count: int
    source: str = Field(..., description="读到的 jobs.json 路径（不存在时也回显，便于定位）")
