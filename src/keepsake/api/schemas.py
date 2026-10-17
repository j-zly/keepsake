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
    source_type: str = Field(
        "",
        exclude_if=lambda v: v == "",
        description="来源类型：manual=控制台手动文档（可改/删），skill/skill_hk=灌入器维护（只读）。"
                    "空串（表里没这列）时**不出这个键** —— 缺省不假装有值，前端按「非 manual 即只读」保守渲染",
    )


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
    #: 下次执行时间（jobs.json 的 next_run_at）；缺键退空串，前端 toLocal 出「—」
    next_run: str = ""
    #: jobs.json 里的任务 id。**认不出 / 不合 12 位十六进制就退空串** ——
    #: 前端据此不渲染管理按钮（防对未知形状的文件乱写），CLI 也永远收不到脏 id。
    job_id: str = ""


class SchedulerStatus(BaseModel):
    """`hermes cron status` 末行拆出来的两段（源码侧 `_parse_scheduler_status`）。

    结构化而非裸字符串：那一行的末段常是 `Next run: <ISO+08:00>`，原样透传
    会在界面上渲成「调度器：下次 X · Next run:」—— 同一件事说两遍。时间戳
    摘出来给前端 toLocal 显示成本地时间，界面就不必再从英文自由文本里刨。
    """
    #: 去掉 Next run 段之后剩下的人话（认不出该形状时就是原文）
    text: str = ""
    #: 下次执行的 ISO 串（前端 toLocal 渲染）；认不出 / 没有 ⇒ 空串
    next_run: str = ""


class CronList(BaseModel):
    jobs: List[CronJob]
    count: int
    source: str = Field(..., description="读到的 jobs.json 路径（不存在时也回显，便于定位）")
    #: 调度器状态（`hermes cron status`）；本机无 CLI ⇒ null，前端显示「未知」
    scheduler_status: Optional[SchedulerStatus] = None


class CronActionBody(BaseModel):
    """pause / resume / edit 三条管理端点共用的请求体。

    `confirm` 必须显式 true（与其余写端点同一道闸）；`schedule` 只对 edit 有
    意义，pause/resume 忽略它 —— 少写一个模型，少一处以后要同步的地方。
    """

    confirm: bool = False
    schedule: str = ""


# --------------------------------------------------------------------------
# 代理注册表（keepsake_agent_registry）
# --------------------------------------------------------------------------


class AgentItem(BaseModel):
    """注册表里的一行（对外形状与 `govern.registry` 的 `_public` 逐字一致）。

    🔴 **没有 `credential_hash` 也没有明文凭据**：读面永远不回显凭据，
    明文只在 `POST /agents/{id}/approve` 的一次性响应里出现。
    """

    agent_id: str
    name: str = ""
    role: str = Field(..., description="primary / sub；全库唯一 primary")
    status: str = Field(..., description="pending / approved / revoked")
    first_seen_at: str = ""
    last_seen_at: str = ""
    reads: int = 0
    writes: int = 0
    exclude_from_primary: bool = Field(
        False, description="该代理的条目是否排除出主脑上下文（退役键的新家）")
    credential_hint: str = Field("", description="凭据前 8 位，供人工比对（不是凭据）")
    meta: Dict[str, Any] = Field(default_factory=dict)


class AgentList(BaseModel):
    items: List[AgentItem]
    #: 待审数量（pending）—— 面板唯一要盯的数：新代理接入默认 pending、读写全拒
    pending_count: int
    count: int = 0


class AgentActionBody(BaseModel):
    """approve / revoke / exclude 三条端点共用的请求体。

    `confirm` 的口径按端点不同：approve 与 exclude 要 `true`，revoke 要手输
    agent_id 前 8 位 —— 同一个字段承载三道闸，具体判据在各自端点里。
    `exclude` 只对 exclude 端点有意义（其余端点忽略它）。
    """

    confirm: Any = False
    exclude: bool = False


class AgentRoleBody(BaseModel):
    """`{"role": "primary"|"sub", "confirm": "<完整 agent_id>"}`。"""

    role: str = ""
    confirm: Any = False
