"""
存储后端抽象层 — Redis 与 PostgreSQL 两个后端共用的接口定义。

**本模块只抽签名层，不放任何共用实现。**
共用逻辑（_rrf_fuse / _apply_v2_filters / _rerank_with_decay / match_* 等）
留到批 2 —— 那时两个后端都实现了检索，才真正有抽取的必要；
本批搬运 = 无谓的行为回归风险。

方法面以「实际被调用点」为准（src/ cron/ scripts/ hermes-plugin/ 实测统计）。
抽象方法全部带默认参数的实现约定：子类必须提供同名同签名的方法，
签名漂移由 tests/test_storage_interface.py 检出。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


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
    ) -> bool:
        """将一段文本写入碎片库（同名内容去重：保留 feedback 并版本化归档旧版）。"""

    @abstractmethod
    def supersede_fragment(self, old_key: str, new_key: str) -> bool:
        """封边：旧碎片标 superseded_by=new_key + superseded_at=now，不物理删。"""

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
    ) -> List[Dict[str, Any]]:
        """统一检索入口（BM25 + KNN 融合 + v2 后置过滤）。"""

    @abstractmethod
    def search_bm25(
        self,
        query: str,
        tag_filter: str = "",
        agent_id: str = "",
        is_primary: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """BM25 全文搜索。"""

    @abstractmethod
    def search_knn(
        self,
        query: str,
        tag_filter: str = "",
        agent_id: str = "",
        is_primary: Optional[bool] = None,
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

    @abstractmethod
    def delete_fragments_batch(self, keys: List[str]) -> int:
        """批量删除碎片，返回实际删除条数。"""
        ...

    # ------------------------------------------------------------------
    # 语料维护（批 2 与检索一起做）
    # ------------------------------------------------------------------

    @abstractmethod
    def discover_synonyms(self, rebuild: bool = False) -> Dict[str, Any]:
        """扫描全库自动发现同义词组，返回统计信息。"""

    @abstractmethod
    def generate_jieba_dict(self, output_path: str = None) -> Dict[str, Any]:
        """从碎片库 + 同义词表生成 jieba 自定义词典，返回统计信息。"""