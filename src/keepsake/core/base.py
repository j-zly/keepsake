"""
存储后端抽象层 — Redis 与 PostgreSQL 两个后端共用的接口定义。

**本模块只抽签名层，不放任何共用实现。**
共用逻辑（_rrf_fuse / _apply_v2_filters / _rerank_with_decay / match_* 等）
留到批 2 —— 那时两个后端都实现了检索，才真正有抽取的必要；
本批搬运 = 无谓的行为回归风险。

方法面以「实际被调用点」为准（src/ cron/ scripts/ hermes-plugin/ 实测统计）。
抽象方法全部带默认参数的实现约定：子类必须提供同名同签名的方法，
签名漂移由 tests/test_storage_interface.py 检出。

**唯一的例外（2026-10 ks_hit_tracking）**：本模块尾部有一小段「检索命中打点」
的**共用实现**（限频 / 后台线程 / fail-open）。它们与后端无关（三个后端的差别
只在「往哪儿 append 一行」），抄三份必然漂移，而漂移的后果不是报错而是
**统计口径悄悄不一致**（下一单 core_tiering 拿它当升常驻信号）。共用的检索
后处理历史上在 `storage_shared`，那一层是**纯函数**、不碰实例状态，打点这层要
读 `self`（限频表挂实例）且必须与三个后端类同处一个 import 图 ⇒ 落在这里，
两个理由都写在该节 docstring 里。
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 归属判据只有一处（与检索 / core 可见性同一把尺子）：
# `govern` 只依赖标准库（不 import `core.*`）⇒ 这里反向引用不成环。
from ..govern.visibility import fragment_owner
from .shared import UNATTRIBUTED_AGENT

logger = logging.getLogger("keepsake.storage_base")


class BackendUnsupportedError(RuntimeError):
    """后端**明确不支持**某个能力（不是「暂时失败」，是「这个后端做不了」）。

    🔴 为什么单独一个类，而不是随便抛个 RuntimeError：调用方要能区分
       「后端挂了，稍后重试」与「这个后端根本不支持这个语义」。后者**重试一万次
       也是同样结果**，必须立刻显式失败（降级成「不传 expected_rev 的老路径」
       之类）而不是静默当成功。

    🔴 **消息里必须带 `backend_unsupported` 字样**：那是本仓库对「显式降级」的
       统一标记（控制台 / MCP 层按它区分「不支持」与「失败」）。改文案前先看
       `mcp_server/tools.py` 与 `core/redis.py` 里的判据。
    """

    def __init__(self, message: str = "backend_unsupported: 该后端不支持此能力") -> None:
        super().__init__(message)


class StorageBase(ABC):
    """存储后端抽象基类。

    实现方：
      - `keepsake.storage.RedisStorage`（默认后端，Redis + RediSearch）
      - `keepsake.storage_pg.PgStorage`（可选后端，PostgreSQL）

    约定：
      - 读写类方法在「后端不可用」时行为由实现方自定（Redis 侧沿用既有
        falsy 返回；PG 侧故意抛错 —— 详见 storage_pg 模块注释）
      - 检索类方法若后端尚未实现，**必须抛 NotImplementedError**，
        绝不允许静默返回空列表（静默空 = 记忆搜不到且无告警，最危险的失败形态）
    """

    #: 本后端**是否支持 rev CAS**（乐观并发，2026-10 ks_memory_rev_cas）。
    #:
    #: 🔴 **默认 False（不支持）**：新写的后端**不会**因为「继承了基类」就
    #:    悄悄拿到一套假 CAS 语义 —— 假语义 = 并发照样分叉、但回执说成功。
    #:    真正实现了 `cas_update` / `cas_remove` 的后端必须显式改成 True。
    supports_rev_cas: bool = False

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    @abstractmethod
    def health_check(self) -> bool:
        """后端中立的后端存活探针（替代直接摸 Redis client 的泄漏写法）。

        返回 True = 后端可达。调用方（如插件/健康检查）应只用这个方法，
        不要直接拿后端专属连接对象。
        """

    @abstractmethod
    def ensure_index(self) -> bool:
        """初始化时创建/验证索引结构（幂等）。失败返回 False。"""

    @abstractmethod
    def close(self) -> None:
        """释放连接/连接池。必须可重复调用。"""

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    @abstractmethod
    def store(
        self,
        text: str,
        tags: str = "",
        category: str = "",
        source: str = "",
        fragment_type: str = "",
        sentiment_score: Optional[float] = None,
        sentiment_label: Optional[str] = None,
        applicability: str = "",
        owner_agent_id: str = "",
    ) -> bool:
        """将一段文本写入碎片库（同名内容去重：保留 feedback 并版本化归档旧版）。

        `applicability` 是契约 v2 的可选入参（默认空串 = 环境无关的记忆照常写，
        老调用方逐字不受影响）。**不传**时后端连列都不写；**传了**却缺列 ⇒
        写路径报人话错误（附迁移 SQL），绝不静默丢字段。

        `owner_agent_id` 是**归属章的覆盖值**（2026-10 ks_owner_stamp）：
        非空 ⇒ 落库时把 `agent:<id>` 盖成它（调用方自报的 `agent:xxx` 一律剥掉
        —— 不许自报归属）；为空 ⇒ 落**进程默认身份**（`self._agent_id`），
        本机直写/提炼管线路径逐字不变。
        """

    @abstractmethod
    def supersede_fragment(self, old_key: str, new_key: str, reason: str = "") -> bool:
        """封边：旧碎片标 superseded_by=new_key + superseded_at=now + is_latest='0'，不物理删。

        `reason` 是可选的**作废原因**（默认空串 = 落库值与加它之前逐字一致）：
          * 有新条 ⇒ 记在**新条**的 `supersede_reason` 上，同时旧条转链身；
          * `new_key` 为 `__void__`（或空串）⇒ 没有新条，`reason` 记在**旧条**上。
        """

    # ------------------------------------------------------------------
    # 乐观并发（CAS）—— 2026-10 ks_memory_rev_cas
    #
    # 🔴 **rev 是数据字段，不是检索信号**：它不进任何排序 / 打分公式，
    #    只用于「写之前确认我读到的还是不是最新版」。
    #
    #    共享记忆被多个代理读写时，没有这层并发控制就会出现「内容分叉」：
    #    两个代理基于同一版各自改写 ⇒ key=内容哈希 ⇒ 各存一条 ⇒ 互相看不见
    #    对方（谁都不知道对方改过）。
    #
    #    语义（HTTP ETag / If-Match、CouchDB _rev 的思路）：
    #      读：返回值里带 `rev`；
    #      写：声明 `expected_rev`，对不上 ⇒ **拒绝并回最新内容**（不覆盖）。
    #
    #    🔴 **刻意不是 `@abstractmethod`**（与 `daily_counts` 同款纪律）：
    #       只有 PG / SQLite 支持真 CAS，Redis 明确不支持 ⇒ 默认实现直接抛
    #       `BackendUnsupportedError`，**禁止静默降级成「无条件覆盖」**。
    #       抽象方法会让漏实现的后端在**构造期**就炸（波及既有测试），
    #       默认抛错则每个后端必须自己表态「支持」还是「不支持」。
    # ------------------------------------------------------------------

    def cas_update(
        self,
        key: str,
        fields: Dict[str, Any],
        expected_rev: str = "",
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """CAS 改写一条碎片（**新建版本 + 封旧边**，不是原地 UPDATE）。

        `fields` 是新版本的字段（至少含 `content`；其余字段缺省沿用旧条）。
        返回 `(状态, 载荷)`：

          * `("ok", {"new_key":…, "new_rev":…})` —— 改写成功；
          * `("conflict", {"current_rev":…, "current_content":…,
            "updated_owner":…, "updated_at":…})` —— 版本对不上，
            **载荷里带当前最新内容**，调用方据此合并后重试；
          * `("not_found", None)` —— key 不存在。

        默认实现 = **不支持**（抛 `BackendUnsupportedError`）。支持的后端必须
        在**同一个事务**里完成「读版本 → 比对 → 建新行 → 封旧边」。
        """
        raise BackendUnsupportedError(
            "backend_unsupported: rev CAS 需 PG/SQLite 后端"
        )

    def cas_remove(
        self,
        key: str,
        expected_rev: str = "",
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """CAS 删除一条碎片（`expected_rev` 对不上 ⇒ 不删，返回 conflict 载荷）。

        返回形状与 `cas_update` 同款：`("ok", None)` / `("conflict", 载荷)` /
        `("not_found", None)`。默认实现 = **不支持**（抛 `BackendUnsupportedError`）。
        """
        raise BackendUnsupportedError(
            "backend_unsupported: rev CAS 需 PG/SQLite 后端"
        )

    @abstractmethod
    def correct_fragments(self, keys: List[str]) -> int:
        """标记一批碎片为已纠正（打 corrected 标签 + 负反馈），返回处理条数。"""

    @abstractmethod
    def record_feedback(self, fragment_key: str, is_positive: bool) -> bool:
        """记录人工反馈：有用 +1 / 没用 -2，累加到 feedback_score。"""

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    @abstractmethod
    def get_fragment(self, key: str) -> Optional[Dict[str, Any]]:
        """读单个碎片全字段；不存在返回 None。所有值以 str 形态返回。"""

    @abstractmethod
    def get_fragments_batch(self, keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量读碎片，返回 {key: fragment}；缺失的 key 不出现。"""

    # ------------------------------------------------------------------
    # 细粒度能力探针（2026-10 ks_pcli）
    #
    # 这三个方法是 `_get_client()` 依赖链后端无关化的落点：调用方（ingest_gate /
    # pipeline / provider）只认这三个语义，不再摸 Redis 专有连接对象。
    # 语义约定：**返回 False 必须留有可辨识的原因**（日志），不许静默跳过。
    # ------------------------------------------------------------------

    @abstractmethod
    def fragment_exists(self, key: str) -> Optional[bool]:
        """按 key 判断碎片是否存在（替代 `client.exists(key)`）。

        **三态**（调用方靠它区分「不存在」与「查不到」，不能塌成 bool）：
          * True / False = 确实存在 / 确实不存在
          * None = 后端不可达，本次判断无效（调用方须按各自语义 fail-open 或告警）
        """

    @abstractmethod
    def touch_fragment(self, key: str) -> bool:
        """R6 命中后刷新「最近命中」状态；**绝不覆盖 content**。

        Redis 侧逐字保留原实现（pipeline 里 hincrby touch_count + hset updated_at）。
        PG 侧 ks_fragment 无这两列 ⇒ 返回 False 并打日志说明（见 storage_pg 实现）。
        """

    @abstractmethod
    def set_supersedes(self, new_key: str, old_key: str) -> bool:
        """给新碎片打单向 `supersedes=<old_key>` 留痕（替代 `client.hset`）。

        注意与 `supersede_fragment(old, new)` 方向相反：后者是反向封边，两后端都有；
        本方法只是单向元数据，Redis 侧至今无任何读取方。
        """

    # ------------------------------------------------------------------
    # 检索（PG 后端批 1 未实现 → 抛 NotImplementedError）
    # ------------------------------------------------------------------

    @abstractmethod
    def search(
        self,
        query: str,
        tag_filter: str = "",
        agent_id: str = "",
        is_primary: Optional[bool] = None,
        visible_agents: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        """统一检索入口（BM25 + KNN 融合 + v2 后置过滤）。

        `visible_agents`：**额外**可见的归属代理 id 集合（2026-10 ks_share_memory）。
        非 primary 时，可见标签 = `agent:<自己>` + `agent:<集合里每个>` +
        `shared` + `core`；集合为空 ⇒ 与开关上线前逐字相同。
        """

    @abstractmethod
    def search_bm25(
        self,
        query: str,
        tag_filter: str = "",
        agent_id: str = "",
        is_primary: Optional[bool] = None,
        visible_agents: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        """BM25 全文搜索。"""

    @abstractmethod
    def search_knn(
        self,
        query: str,
        tag_filter: str = "",
        agent_id: str = "",
        is_primary: Optional[bool] = None,
        visible_agents: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        """KNN 向量搜索。"""

    # ------------------------------------------------------------------
    # 加权信号（检索排序用）
    # ------------------------------------------------------------------

    @abstractmethod
    def match_attention(self, content: str, top_n: int = 10) -> float:
        """内容命中高注意力话题的加权值（1.0 ~ boost_max）。"""

    @abstractmethod
    def match_hot_topics(self, text: str, limit: int = 10) -> float:
        """内容命中热门话题关键词的衰减加权命中数。"""

    @abstractmethod
    def get_hot_topics(
        self,
        limit: int = 10,
        period: str = "all",
    ) -> List[Dict[str, Any]]:
        """热门话题榜；period ∈ {all, daily, weekly}。"""

    @abstractmethod
    def entity_timeline(self, entity: str, limit: int = 20) -> List[Dict[str, Any]]:
        """按时间倒序返回某实体的记忆时间线。"""

    # ------------------------------------------------------------------
    # 维护原语（合并 / 遗忘用）—— 202-10 ks_pmn
    #
    # 合并与遗忘要做的是「全库分页扫描 + 批量读写删」，原先它们直接摸 Redis
    # 的 SCAN 游标 + pipeline，于是 PG 后端整体 unsupported。这里给出**后端无关
    # 的四个原语**，两个后端各实现一份：
    #
    #   * `scan_fragment_keys`  分页扫描（Redis=SCAN 游标；PG=keyset 分页
    #     `WHERE key > :cursor ORDER BY key LIMIT n`，**禁止大 OFFSET**）
    #   * `get_fragments_batch` 批量读（已在上面声明，Redis=HMGETALL、PG=ANY）
    #   * `write_fragments_batch` 批量写（新碎片；PG 侧要重算 content_tsv/embedding）
    #   * `update_fragment_fields` 局部更新（标记 consumed 用，不动 content）
    #   * `delete_fragments_batch` 批量删
    #
    # 游标约定：`cursor` 是**不透明字符串**，`""` = 从头开始；
    # 返回的 `next_cursor` 为 `""` 表示**已扫完**（Redis 的游标 0 = PG 的空串）。
    # ------------------------------------------------------------------

    @abstractmethod
    def scan_fragment_keys(
        self,
        cursor: str = "",
        limit: int = 200,
        prefix: str = "memory:frag:",
    ) -> Tuple[str, List[str]]:
        """分页扫描碎片 key。返回 `(next_cursor, keys)`。

        `prefix` 是**前缀**（不带 `*`），Redis 侧转成 `SCAN MATCH prefix*`，
        PG 侧转成 `key LIKE prefix%`。两个后端对同一 prefix 必须给出等价 key 集
        —— 调用方（合并/遗忘）按 `prefix="memory:full:"` 扫描时，
        Redis 若无该 keyspace 就返回空，PG 侧 ks_fragment 里没有这些 key 也返回空。
        """
        ...

    def daily_counts(self, days: int = 7) -> List[Dict[str, Any]]:
        """近 N 天的**每日新增条目数**（`[{date, count}]`，按日期升序）。

        🔴 **默认实现走中性原语**（`scan_fragment_keys` + `get_fragments_batch`）——
        与合并/遗忘同一条读路径，后端语义天然等价。PG 侧覆写成一条
        `GROUP BY 日期` 的 SQL（`core/pg.py::daily_counts`）：同样的口径，
        少一次往返。

        `date` 是 `created` 的前 10 位（ISO 文本 ⇒ 取日期）；`created` 为空的
        老行**不计入**（判不出日期就不编一个）。

        🔴 **刻意不是 `@abstractmethod`**：加进抽象方法清单就要同步改
        `tests/test_storage_interface.py` 里那份写死的清单（那是本单范围外的
        文件），而漏实现的后端会**构造期就炸**、波及既有测试。默认值能跑，
        后端想更快就覆写它。
        """
        n = max(1, int(days))
        since = (datetime.now(timezone.utc).date() - timedelta(days=n - 1)).isoformat()
        cursor = ""
        keys: List[str] = []
        while True:
            cursor, batch = self.scan_fragment_keys(cursor=cursor, limit=500)
            keys.extend(batch)
            if not cursor or not batch:
                break
        counts: Dict[str, int] = {}
        for doc in self.get_fragments_batch(keys).values():
            created = str(doc.get("created") or "")
            if len(created) < 10 or created[:10] < since:
                continue
            day = created[:10]
            counts[day] = counts.get(day, 0) + 1
        return [{"date": day, "count": counts[day]} for day in sorted(counts)]

    def daily_counts_by_agent(self, days: int = 7) -> List[Dict[str, Any]]:
        """近 N 天的**每日新增按代理分列**（`[{d, agent, n}]`）。

        🔴 与 `daily_counts` **同一口径**（同一批行、同一 `created` 前 10 位判日、
        `created` 为空的老行同样不计入），只是多切一个**归属**维度：

        * 归属 = 条目 `tags` 里的 `agent:<id>` —— 判据用
          `govern/visibility.py::fragment_owner`（逗号边界的完整标签，
          `agent:win` 不命中 `agent:win-desktop`），与检索/core 可见性同一把尺子；
        * **没有归属的老行归入 `"-"` 桶**（前端显示「未标注」）—— 不假装它是
          某个代理写的，也不把它摊到别人头上。

        `d` 是 `YYYY-MM-DD`；行序 = 日期升序、同日按代理升序（回包好对账）。
        中性实现走 `scan_fragment_keys` + `get_fragments_batch`（Redis / SQLite
        用这条）；PG 侧覆写成一条 `GROUP BY 日期, 归属` 的 SQL（`core/pg.py`）。
        """
        n = max(1, int(days))
        since = (datetime.now(timezone.utc).date() - timedelta(days=n - 1)).isoformat()
        cursor = ""
        keys: List[str] = []
        while True:
            cursor, batch = self.scan_fragment_keys(cursor=cursor, limit=500)
            keys.extend(batch)
            if not cursor or not batch:
                break
        counts: Dict[Tuple[str, str], int] = {}
        for doc in self.get_fragments_batch(keys).values():
            created = str(doc.get("created") or "")
            if len(created) < 10 or created[:10] < since:
                continue
            day = created[:10]
            agent = fragment_owner(doc.get("tags", "")) or UNATTRIBUTED_AGENT
            key = (day, agent)
            counts[key] = counts.get(key, 0) + 1
        return [{"d": d, "agent": a, "n": cnt} for (d, a), cnt in sorted(counts.items())]

    @abstractmethod
    def write_fragments_batch(self, rows: List[Dict[str, Any]]) -> int:
        """批量写碎片（upsert）。`rows` 每项必须含 `key` 与 `content`，返回写入条数。

        PG 侧必须重算 `content_tsv` / `embedding`（否则新碎片进不了 BM25/KNN）。
        """
        ...

    @abstractmethod
    def update_fragment_fields(self, key: str, fields: Dict[str, Any]) -> bool:
        """局部更新一条碎片的若干字段（**不动 content / content_tsv / embedding**）。"""
        ...

    def update_fragment_content(
        self,
        key: str,
        content: str,
        tags: Optional[str] = None,
        entities: Optional[str] = None,
    ) -> bool:
        """改正文（可选同改标签/实体），**必须让检索索引跟着正文一起更新**。

        与 `update_fragment_fields` 的分工：那条原语是「维护面更新」（标记
        consumed / 打归档标），契约上**明写不动 content_tsv** —— 正文明改了索引
        不跟着改，检索会**静默召回旧内容**（不是召不回，是召回错的），这是
        PG 后端历史上真实存在的缺陷（它的 `update_fragment_fields` 只 UPDATE 列）。
        所以「改正文」必须走这一条。

        **默认实现 = 转调 `update_fragment_fields`**，两个后端天然满足语义，
        无需各自实现：
          * `SqliteStorage`：改了 content/entities/tags 就**就地重算
            `content_tsv` 并同步 FTS**（见其实现注释）；
          * `RedisStorage`：索引是 `FT.CREATE ... ON HASH PREFIX 1 memory:frag:`
            （无 `NOINDEX`/`SKIPINITIALSCAN` 排除），RediSearch 对命中前缀的
            hash **每次写都自动维护索引** ⇒ HSET 新正文后 BM25/KNN 立即跟随，
            且 Redis 没有 `content_tsv` 列，本就无第二份索引要同步。
          * `PgStorage`：**覆写**本方法（同事务内重算 `content_tsv`，A=正文
            / B=实体+标签，参数与拼接口径复用 `store()` 的同一份函数）。

        `tags` / `entities` 传 `None` = 这一列不动（B 权重按库里现值算）。
        """
        fields: Dict[str, Any] = {"content": content}
        if tags is not None:
            fields["tags"] = tags
        if entities is not None:
            fields["entities"] = entities
        return self.update_fragment_fields(key, fields)

    @abstractmethod
    def delete_fragments_batch(self, keys: List[str]) -> int:
        """批量删除碎片，返回实际删除条数。"""
        ...

    # ------------------------------------------------------------------
    # 常驻核心层（core tag）—— 2026-10 keepsake_core_tier_v1
    # ------------------------------------------------------------------

    def list_by_tag(self, tag: str) -> List[Dict[str, Any]]:
        """返回 `tags` 里含 `tag` 的碎片，按 `updated_at` 升序、`key` 升序（稳定）。

        每项至少含 `key` / `content` / `tags` / `updated_at`。

        常驻核心层（`system_prompt_block` 每轮全量注入）靠这个原语取全文，
        不是检索排序的子集 —— 所以**不许**复用 search 的打分/截断路径。

        tag 匹配必须是**分隔符边界上的完整标签相等**（与检索侧
        `storage_shared.tag_match_sql` 同一判据）：`core` 不许命中 `core-x`。
        Redis 侧 `tags` 落库形态是逗号串，按逗号切分比对。

        🔴 **只返回未归档条目**：已归档（`is_archived='1'` + `valid_until=now`，
        见 `archive_fragment`）的一律不返回 —— 归档是版本化失效的同款语义，
        列表视图只给活的那一份。除归档外的行为（排序 / 分页 / 标签判据）
        都不许变；判据按 `'1'` 精确相等，历史上从未写过该列的行照常返回。

        🔴 **未实现的后端必须抛 NotImplementedError，绝不静默返回空列表** ——
        静默空 = 核心层悄悄变空、agent 丢掉全部常驻记忆且无任何告警，
        与 `search_*` 的同一约定（见类 docstring）。

        刻意**不标 `@abstractmethod`**：标了就得同步改
        `tests/test_storage_interface.py` 里那份写死的 `REQUIRED_METHODS`
        （否则接口测试按设计失败），而那是本单改动范围外的文件。
        写成带 `raise` 的具体方法，语义等价且更强：漏实现的后端**仍能构造**
        （构造期失败会波及既有测试），只在真被调用时炸 —— 而调用方
        `system_prompt_block` 已经 fail-open，接得住。
        """
        raise NotImplementedError(
            f"{type(self).__name__} 未实现 list_by_tag（tag={tag!r}）"
        )

    # ------------------------------------------------------------------
    # 检索命中打点（2026-10 ks_hit_tracking）
    #
    # 用途：给下一单 core_tiering（常驻层自动升降级）提供「热不热」信号。
    # 本单只做**数据地基**（记事件 + 按天窗统计 + 过期清理），提名/升降级逻辑
    # 不在这里实现 —— 谁也不许在本文件塞「够热就升常驻」的判断。
    #
    # 🔴 三个方法都**刻意不标 `@abstractmethod`**：标了就得同步改
    # `tests/test_storage_interface.py` 那份写死的 `REQUIRED_METHODS`
    # （本单范围外），而漏实现的后端在构造期就炸会波及既有测试。语义与
    # `list_by_tag` 同一套：构造照常，真被调用时抛明确错误 ——
    # 绝不返回空列表装成功（那会让「从没打过点」与「真没命中」不可区分，
    # 下一单会据此把整批常驻层判冷）。
    # ------------------------------------------------------------------

    def record_hits(self, keys: List[str]) -> int:
        """追加一批命中事件（`(key, ts)`），返回写入条数。

        **纯追加、不 UPDATE 主表**（写放大零）：命中是流水事件，不是碎片字段 ——
        碎片主表上加 `last_hit_at`/`hit_count` 会让每次检索都变成一次写放大，
        而 PG 侧 `touch_fragment` 已经明写 unsupported（同款理由）。
        同 key 的重复命中由 `storage_shared.fire_hits` 的 300 秒限频在上游挡掉。
        """
        raise NotImplementedError(f"{type(self).__name__} 未实现 record_hits")

    def record_hits_repeat(self, key: str, times: int) -> int:
        """给**同一个 key** 补 `times` 条命中事件，返回写入条数。

        🔴 **为什么不能复用 `record_hits`**：它按 key 去重（同一批里同一个 key
        只落一行），而「提名基数」要的正是**同一个 key 落 N 行** —— 自动审核
        （规则二）数的是**事件行数**，一行不够。

        与 `record_hits` 同款纪律：**纯追加**，不碰碎片主表；**批量写**
        （一次多行 INSERT，不逐条 insert）。
        """
        raise NotImplementedError(f"{type(self).__name__} 未实现 record_hits_repeat")

    def hits_stats(
        self,
        window_days: int = 7,
        min_hits: int = 5,
        keys: Optional[List[str]] = None,
        limit: int = 500,
        since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """窗口内命中统计 → `[{key, hits, last_hit}]`，`hits` 降序。

        `keys` 给了就**只**算这些 key（供下一单核心查询：已知候选集、只要热度）；
        不给就返回窗口内 `hits >= min_hits` 的全部（`limit` 封顶）。
        空表返回空列表 —— 这是「真的没命中过」，不是错误。
        `hits` 口径 = 事件行数；受 300 秒去抖影响，允许 ±5 分钟误差。

        🔴 `since`（UTC 绝对时刻）= **窗口锚点**：给了就从那一刻起算，
        `window_days` 作废；不给才是「现在减 window_days」的滚动窗。
        为什么需要它：分级班车的复核口径是「**自提名日起**累计命中」，
        锚点是提名日（不是「现在减 7 天」）—— 用滚动窗表达这件事，
        窗口长度就得跟着每条候选的挂龄走，还得靠挂钟对齐，边界必然漂。
        """
        raise NotImplementedError(f"{type(self).__name__} 未实现 hits_stats")

    def prune_hit_events(self, days: int = 90) -> int:
        """删掉 `days` 天前的命中事件，返回删除条数（防表膨胀）。

        本单**只提供函数，不挂 cron**（运维接线由主脑做）：班车没接上时表会一直长，
        但那只是占用问题，绝不构成数据错误。
        """
        raise NotImplementedError(f"{type(self).__name__} 未实现 prune_hit_events")

    # ------------------------------------------------------------------
    # 语料维护（批 2 与检索一起做）
    # ------------------------------------------------------------------

    @abstractmethod
    def discover_synonyms(self, rebuild: bool = False) -> Dict[str, Any]:
        """扫描全库自动发现同义词组，返回统计信息。"""

    @abstractmethod
    def generate_jieba_dict(self, output_path: str = None) -> Dict[str, Any]:
        """从碎片库 + 同义词表生成 jieba 自定义词典，返回统计信息。"""

# ---------------------------------------------------------------------------
# 检索命中打点（2026-10 ks_hit_tracking）—— 三后端共用的那一半
# ---------------------------------------------------------------------------
#
# 为什么这一段落在 base.py（而不是 storage_shared）：
#   * 限频表要挂在**实例**上（`_hit_seen`，不同 DSN / 不同 sqlite 文件的实例
#     共享同一个 key 名空间是错的），所以它是「带 self 的共用实现」而不是纯函数；
#   * 三个后端类都已经 `from .base import StorageBase`，放这里不用给三个文件
#     各加一条 import，也就不会出现「某个后端漏接打点」的形态。
# 另一半（真落库）是各后端的 `record_hits` / `hits_stats` / `prune_hit_events`，
# 见 StorageBase 里的声明。
#
# 三条纪律（下一单 core_tiering 直接依赖，别改）：
#   1. **绝不阻塞检索热路径**：默认执行器是 daemon 后台线程（与
#      `query_expansion.schedule_background_expansion` 同一件做法）。
#   2. **fail-open**：打点抛错只 debug 日志，检索结果一个字都不许变。
#   3. **限频 300 秒**：同 key 五分钟内只记一次 —— 窗口统计因此有 ±5 分钟误差，
#      这是**有意接受**的口径（不是 bug）。

#: 同 key 打点去抖窗口（秒）。窗口统计允许 ±5 分钟误差，见上节纪律 3。
HIT_RATE_LIMIT_S = 300.0

#: 限频表的锁（表本身在各后端实例上，锁全局一张 ⇒ 临界区极短）
_HIT_LOCK = threading.Lock()


def hits_pending(storage: Any, keys: Any, now: Optional[float] = None) -> List[str]:
    """限频闸：返回本次该记的 key（并把它们的「上次打点时刻」推到 now）。

    限频表挂在 **storage 实例**上（`_hit_seen`）。空 key 一律丢掉；输入去重
    （同一批里出现两次只记一次 —— 一次检索每个 key 至多出现一次）。
    """
    stamp = time.time() if now is None else float(now)
    seen = getattr(storage, "_hit_seen", None)
    if seen is None:
        seen = {}
        storage._hit_seen = seen        # noqa: SLF001 — 实例上的进程内限频表
    out: List[str] = []
    with _HIT_LOCK:
        for key in dict.fromkeys(k for k in keys if k):
            last = seen.get(key)
            if last is None or (stamp - last) >= HIT_RATE_LIMIT_S:
                seen[key] = stamp
                out.append(key)
    return out


def submit_hit_work(fn: Any) -> bool:
    """默认执行器：后台 daemon 线程跑打点（测试可替换成同步执行以断言语义）。

    起线程失败也只 debug —— 打点是纯侧写，起不来就当没打，绝不影响检索。
    """
    try:
        threading.Thread(target=fn, name="keepsake-hits", daemon=True).start()
        return True
    except Exception as e:      # noqa: BLE001
        logger.debug("keepsake: 命中打点线程启动失败（fail-open）: %s: %s",
                     type(e).__name__, e)
        return False


def fire_hits(storage: Any, keys: Any, submit: Any = None) -> int:
    """检索命中打点：限频 → 后台落库 → fail-open。返回本次放行的 key 数。

    `submit` 是执行器（默认 `submit_hit_work` = 后台 daemon 线程）：PG 侧传自己
    那条复用线程池的执行器（worker 连接数才有上界，见 `PgStorage._submit_hit_work`）。
    调用点（各后端 `search` / `search_bm25` 的**返回前**）一律 `try/except` 包住：
    这层是纪律 2 的最后一道兜底 —— 连限频表本身出错也不许冒到检索。
    """
    try:
        pending = hits_pending(storage, keys)
    except Exception as e:      # noqa: BLE001
        logger.debug("keepsake: 命中去抖失败（fail-open）: %s: %s", type(e).__name__, e)
        return 0
    if not pending:
        return 0

    def _write() -> None:
        # 后台线程里落库；异常**只 debug** —— 打点失败绝不冒泡回检索路径。
        try:
            storage.record_hits(pending)
        except Exception as e:      # noqa: BLE001
            logger.debug("keepsake: 检索命中打点落库失败（fail-open，检索结果不受影响）: "
                         "%s: %s", type(e).__name__, e)

    (submit or submit_hit_work)(_write)
    return len(pending)
