"""Pydantic 响应模型（只读控制台的出参契约）。

字段名与前端 `web/src/api` 一一对应；**任何模型里都不出现 token / DSN**。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# 缺省内容类型取共用层那份（drift 到两处就会出「列表标手记、库里其实是链接」）
from keepsake.kb.ingest import DEFAULT_CONTENT_TYPE


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


class CoreAgentUsage(BaseModel):
    """**按代理**的常驻层占用（`GET /api/v1/stats/core-per-agent` 的一格）。

    归属判据 = 条目 tags 里的 `agent:<id>`（`govern.visibility.fragment_owner`
    同一把尺子）；预算 = 该代理自己的 `core_budget_chars`，没有 ⇒ 全局。
    """

    count: int = Field(..., description="该代理名下的常驻条目数")
    chars: int = Field(..., description="这些条目的正文字符合计（与 core.chars 同一口径）")
    budget_effective: int = Field(
        ..., description="该代理生效的注入预算：自己的 core_budget_chars ?? 全局")


# --------------------------------------------------------------------------
# 记忆条目（检索层）
# --------------------------------------------------------------------------


class FragmentItem(BaseModel):
    """记忆条目列表项（含检索打分，q 为空时 score 为 None）。"""

    key: str
    tags: str = ""
    agent: str = Field(
        "",
        description="**归属代理 id**（条目 tags 里的 `agent:<id>`；判据 = "
                    "`govern.visibility.fragment_owner` —— 全库同一把尺子；无归属 = ''）")
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


class KbTypeCounts(BaseModel):
    """各内容类型的文档数（界面 Tab 上的计数 —— **后端给，前端不写死**）。"""

    counts: Dict[str, int] = Field(default_factory=dict,
                                    description="content_type -> 文档数；老库未迁移 content_type "
                                                "列时为空字典（前端按无计数渲染，不编数字）")


class KbTagCount(BaseModel):
    """一个标签 + 挂着它的文档数（筛选 chip 行的一格）。"""

    tag: str
    count: int = 0


class KbTagCounts(BaseModel):
    """全部自由标签及计数（**后端给，前端不写死数字**）。

    按计数倒序、同数按标签名排序（老库未迁移 tags 列时为空列表，前端整行隐藏）。

    🆕 `total_distinct` = **过滤后**（机器标签已剔除）的真实去重标签数：
    `items` 默认只回 Top-N（chip 行一屏放不下 140 个），这个数让前端能说清
    「还有多少个没显示」（+N 更多），**不靠前端自己数**。
    """

    items: List[KbTagCount] = Field(default_factory=list,
                                    description="按计数倒序、同数按标签名排序；"
                                                "老库未迁移 tags 列时为空列表")
    total_distinct: int = Field(0, description="过滤后真实去重标签数（Top-N 之外的也在内）")


class KbHit(BaseModel):
    chunk_id: str
    doc_id: str = ""
    title: str = ""
    snippet: str = Field("", description="命中片段（截断后的正文）")
    rank: float = 0.0
    content_type: str = Field(
        "",
        exclude_if=lambda v: v == "",
        description="所属文档的内容类型（skill/note/article/link/image）。"
                    "空串（表里没这列）时不出这个键 —— 缺省不假装有值",
    )
    tags: List[str] = Field(
        default_factory=list,
        exclude_if=lambda v: not v,
        description="所属文档的自由标签（开放维度，一篇可多个）。"
                    "空数组（没打标 / 表里没这列）时不出这个键 —— 缺省不假装有值",
    )


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
    content_type: str = Field(
        "",
        exclude_if=lambda v: v == "",
        description="内容类型：skill=技能灌入，note=手记（缺省），article=长文章，link=链接卡片，"
                    "image=图文（枚举占位，创建端点暂拒）。与 source_type 正交："
                    "空串（表里没这列）时**不出这个键**，前端按 note 缺省渲染",
    )
    tags: List[str] = Field(
        default_factory=list,
        exclude_if=lambda v: not v,
        description="自由标签（开放维度，与 content_type 正交）：card 上渲成 chip、"
                    "`GET /kb/documents?tag=` 按它精确筛。空数组（没打标 / 表里没这列）"
                    "时**不出这个键**，前端当「没打标」渲染",
    )
    agent_id: str = Field(
        "",
        exclude_if=lambda v: v == "",
        description="**来源代理**（存这篇的代理 id；`documents.agent_id` 列）。"
                    "空串（表里没这列 / 这篇无主）时**不出这个键** —— "
                    "无主 = 收紧前的公共数据（历史文档全是），前端按「无主」渲染",
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


class KbPendingItem(BaseModel):
    """待审队列的一行（pending + rejected 都在这里，只读）。

    🔴 **待审文档此刻一块切片都没有**（未审正文不进检索索引），所以正文预览只能
    从 `review_meta.body` 里取 —— 那是提交时的原文，`preview` 截到后端定的
    `PENDING_PREVIEW_LEN` 字（前端照抄那个数，别自己定另一档）。
    """

    doc_id: str
    title: str = ""
    status: str = Field("pending", description="pending=待审；rejected=已驳回（台账，仍可再审）")
    submitted_by: str = Field("", description="提交这条的代理 agent_id（站内手建不会有）")
    submitted_at: str = Field("", description="提交时间（表里探不到时间列时为空串，不编一个）")
    content_type: str = Field(DEFAULT_CONTENT_TYPE, description="内容类型（按它切块）")
    source_key: str = ""
    tags: List[str] = Field(default_factory=list, description="自由标签（与 content_type 正交）")
    preview: str = Field("", description="正文预览（提交原文的前若干字；pending 阶段无切片可读）")
    dup_of: str = Field("", description="同标题/近重复的既有文档 id（空=没查到重复）")
    dup_of_title: str = Field("", description="那一篇的标题（审核页显示警示用）")
    dup_exact: bool = Field(False, description="True=标题完全相同；False=仅近重复（标题互相包含）")
    reject_reason: str = Field("", description="驳回理由（已驳回的行才有；正文未删，可再审）")


class KbPendingList(BaseModel):
    """待审面板一页。`pending_count` 是**全量口径**（顶部「待审 N」chip 的唯一来源，
    不受分页影响）；`count` 是面板总数（pending + rejected）。老库未迁移 status 列
    ⇒ 两个数都是 0（缺列 = 全 effective，队列恒空，不编数字）。"""

    items: List[KbPendingItem] = Field(default_factory=list)
    count: int = 0
    pending_count: int = 0
    page: int = 1
    page_size: int = 20


# --------------------------------------------------------------------------
# 总览
# --------------------------------------------------------------------------


class SectionError(BaseModel):
    """分域失败标记：**显式报错，不是「空数据装成功」**。"""

    available: bool = False
    error: str


class DailyCount(BaseModel):
    """一天的条目增量（`GET /api/v1/stats?days=7` 的一段）。

    `date` 是 `YYYY-MM-DD`（UTC）；**没有新增的那天也占一格（count=0）** ——
    折线图上「那天没写入」是一条真实的 0，不该从横轴上消失。
    """

    date: str = Field(..., description="YYYY-MM-DD（UTC）")
    count: int = Field(..., description="这一天新增的记忆条目数")


class DailyAgentCount(BaseModel):
    """一天的条目增量**按代理分列**（`GET /api/v1/stats?days=7` 的一段）。

    * `d` 是 `YYYY-MM-DD`（UTC，与 `daily[].date` 同一把尺子）；
    * `agent` 是**归属代理 id**（条目 `tags` 里的 `agent:<id>`）；
      **没有归属的老行归 `"-"` 桶**（前端显示「未标注」—— 不假装它属于某个代理）；
    * `n` 是这一天这个代理的新增条目数。

    🔴 **只给「有写入」的格子**（不补 0）：补零/补日是**图表口径**，由前端按
    `daily`（那一段补齐了每一天）铺横轴 —— 后端不重复发明一份横轴。
    """

    d: str = Field(..., description="YYYY-MM-DD（UTC）")
    agent: str = Field(..., description="归属代理 id；无归属的老行 = '-'")
    n: int = Field(..., description="这一天该代理新增的记忆条目数")


class Overview(BaseModel):
    core: CoreStats
    retrieval: Dict[str, Any] = Field(
        ..., description="{count, truncated}：存储层无 count 原语，按 keyset 扫描有上限地统计")
    kb: Dict[str, Any] = Field(..., description="同 KbSummary；不可用时为 {available:false,error:...}")
    daily: List[DailyCount] = Field(
        default_factory=list,
        description="近 N 天每日新增（?days=N 时给；缺省 = 空列表 = 不查这一段）")
    daily_by_agent: List[DailyAgentCount] = Field(
        default_factory=list,
        description="近 N 天每日新增按代理分列（?days=N 时给；缺省 = 空列表 = 不查这一段）")


# --------------------------------------------------------------------------
# 待办（ks_todo）
# --------------------------------------------------------------------------

class TodoAgentSummary(BaseModel):
    """**按代理**分列的待办计数（`GET /api/v1/todos/summary` 的 `by_agent` 段）。

    * `agent` 是归属代理的**显示名**（注册表 `ks_agent` 的 `name`）；
      **查不到注册表的（owner 为空 / 代理已删）归 `"-"` 桶**（前端显示「未标注」
      —— 不假装它属于某个代理，与 `daily_by_agent` 的 `-` 桶同一口径）；
    * `open` / `done` / `obsolete` / `total` 是这代理名下的计数
      （一条 `GROUP BY owner, status` 聚合出来的，只读）；
    * `ratio` 是**完成率百分比**（`(done + obsolete) / total * 100`，保留两位）——
      与全局那个完成率**同一把尺子**（口径见 `utils/todo.js::completionPercent`）。
    """

    agent: str = Field(..., description="归属代理显示名；查不到注册表的 = '-'（未标注桶）")
    agent_id: str = Field("-", description="归属代理 id（owner 那一列）；未标注桶 = '-'")
    open: int = 0
    done: int = 0
    obsolete: int = 0
    total: int = 0
    ratio: float = Field(0, description="完成率百分比（(done+obsolete)/total*100，保留两位）")


class TodoSummary(BaseModel):
    """待办计数（`GET /api/v1/todos/summary`）。

    🔴 `overdue` = **开放且已过期**的条数（销掉的不算 —— 过期是活儿没干完）。
    `by_agent` 是**新增的一段**（按代理分列），老字段一字不改。
    """

    open: int = 0
    done: int = 0
    obsolete: int = 0
    total: int = 0
    overdue: int = 0
    by_agent: List[TodoAgentSummary] = Field(
        default_factory=list, description="按代理分列的待办计数（`by_agent` 段）")


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
    #: 私信开关（`dm_enabled`）：false = 这个代理**不收**私信（别人投不进来）。
    #: 默认**开**（老行缺列也算开 —— 新增字段不许改存量语义）。
    dm_enabled: bool = Field(True, description="是否接收私信（默认开）")
    #: 记忆共享开关（`share_memory`）：true = 这个代理的记忆进**别的**已批准代理
    #: 的检索可见范围。默认**关**（老行缺列也算关 —— 共享是放宽权限的开关，
    #: 缺省必须最严）。
    share_memory: bool = Field(False, description="是否共享记忆给其他已批准代理（默认关）")
    #: 私信实时推送开关（`push_enabled`）：true = 允许这个代理接 SSE 长连接
    #: （有人给它投私信时顺流推一帧**元数据**）。默认**关**（老行缺列也算关 ——
    #: 往外放行的开关，缺省必须最严）。
    push_enabled: bool = Field(False, description="是否允许接 SSE 长连接（默认关）")
    #: 这个代理**自己**的常驻层注入字符预算：None = 沿用全局默认
    #: （新增字段不许改存量语义：老行没有这一列 ⇒ 一律沿用全局）。
    core_budget_chars: Optional[int] = Field(
        None, description="该代理专用的注入字符预算；null = 沿用全局默认")


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

    🔴 `reissue` 也只对 approve 端点有意义：**已 approved 的行再 approve 默认
    409**（不换发、不动凭据），只有显式 `reissue: true` 才换发那条新凭据
    （旧的随即失效）。默认关 = 重复批准不会静默把线上代理的旧凭据顶掉。
    """

    confirm: Any = False
    exclude: bool = False
    reissue: bool = False


class AgentRoleBody(BaseModel):
    """`{"role": "primary"|"sub", "confirm": "<完整 agent_id>"}`。"""

    role: str = ""
    confirm: Any = False
