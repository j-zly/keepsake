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
from typing import Any, Dict, List, Optional


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
    # 语料维护（批 2 与检索一起做）
    # ------------------------------------------------------------------

    @abstractmethod
    def discover_synonyms(self, rebuild: bool = False) -> Dict[str, Any]:
        """扫描全库自动发现同义词组，返回统计信息。"""

    @abstractmethod
    def generate_jieba_dict(self, output_path: str = None) -> Dict[str, Any]:
        """从碎片库 + 同义词表生成 jieba 自定义词典，返回统计信息。"""