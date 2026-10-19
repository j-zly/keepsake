"""keepsake v2 — 写侧两相管线（Mem0 风格 + 封边取代）。

# 为什么做（2026-09-08 事故复盘的根因层）

v1 写闸门（ingest_gate）是正则垃圾过滤器：黑名单追不上语言变体，
一刀切长度会误杀长事实，**且存的是消息原文**——需求原话被存成永恒事实，
后续无人回改，导致「7 月已完成的分页需求被当待办复活」事故。

业界做法（Mem0 arXiv:2504.19413）= LLM 两相：

  * 提取相：从对话提炼「脱离本对话仍有长期价值」的 salient facts。
    废话自然无事实可提而消亡，不需黑名单。
  * 更新相：拿新事实对已有记忆做 ADD/UPDATE/DELETE/NOOP。

Zep/Graphiti 进一步：矛盾时旧记忆**不物理删而是封边**——记
superseded_by/superseded_at 时间窗，检索默认只出当前有效。

# 设计

  Pipeline  = 异步 daemon + 双相 LLM
  ├─ 队列    deque[(user, assistant, ts)]
  ├─ 线程    daemon=True；窗口触发：≥window_pairs 对 OR 距上次 drain ≥ window_seconds
  ├─ 提取相  一次 LLM（输入=本窗口 + 最近 8 条环形缓冲文本）→ JSON facts
  └─ 更新相  每条 fact 用 search_bm25 取 top-5（已自然排除 consumed/superseded）→
              一次 LLM 五选一（ADD / UPDATE / SUPERSEDE / VOID / NOOP）
              UPDATE：同一条记忆的改写（写新条 + 封旧，落库值与批 1 逐字一致）
              SUPERSEDE：两条独立事实、新顶旧（旧的不再成立）→ 走契约 v2 写路径
                         （supersede_fragment 带 reason），旧条 is_latest='0'
              VOID：旧条逻辑删（supersede_reason='voided'），不写新事实
              旧提示词输出的 DELETE **按 VOID 处理**（映射 + 一行日志，向后兼容）

# 调和协议移植（2026-10 keepsake_reconcile_v2，Hy-Memory 批 2）

四选一（实为五选一）的判据来自 Hy-Memory 被 few-shot 校准过的调和协议
（`hy_memory/agent/reconciler.py:489-592`、`prompts_zh.py:779-869`）。我们自己的
痛账：同一事实在 core 层同日沉淀两条不同措辞（本周一手动去重 4 对）、旧事实被
覆盖时链不可追溯、DELETE 走的是「封边/归档」而非逻辑删。四条移植进来的规则：

  1. **SUPERSEDE 仅限「同一维度、不能同时为真」**（换城市 / 偏好明确反转 /
     数值口径更新）。加细节、措辞差异、可共存的偏好**一律不许** SUPERSEDE ——
     「不许」是有代价的（宁可多留一条也不错顶），所以另配一条服务端兜底闸。
  2. **新条正文只写新状态**，禁止把被顶旧条的细节抄进来（抄进来链头就自相矛盾：
     同一条链上既有「租约到 3 月」又有「租约到 9 月」）。
  3. **主体不同永不合并**：「John 喝黑咖啡」不许动「用户喝黑咖啡」。裁决必须带
     `subject`，服务端在 SUPERSEDE/UPDATE 前过一道 normalize 等值闸
     （折叠空白 + 小写，Hy-Memory `coding/fact_reconciler.py:152-166` 同款）。
  4. **同一事件类型的另一次发生 = 独立 ADD**（复发不写次数长句）。

# 🔴 常驻层的否决权在人手里（治理拍板）

更新相的候选是 `search_bm25` 全库 top-K，**不排除 `core`** —— 于是「LLM 说这条
常驻过时了」本来会直接把它 `supersede_fragment` 掉：常驻层是每轮都注入 prompt
的东西，它的生死不该由一次自动提炼说了算。本模块的处理不是「排除 core」
（那等于把「这条常驻与新事实冲突」这个**最难得的一手信号**一起扔掉），而是：

  目标带 core 标 + 动作 ∈ {UPDATE, SUPERSEDE, VOID} ⇒ **不封边**，改打退居建议标 +
  冲突证据标（`tiering.DEMOTE_CANDIDATE_TAG` / `tiering.EVID_PREFIX:<新事实 key>`，
  见 `_nominate_core_conflict`）⇒ 转进审核队列「退居建议」，由人裁决。
  冲突证据（那条新事实）先落库再提名，审核页上看得见「它跟哪条新事实打架」。
  两个标签的字面量**只在 tiering 一处**（本模块 import，禁复制 —— 见 G5）。

# 故障兜底

  * LLM 提取/更新任一调用失败或 JSON 解析失败 → 窗口整体回落 v1 decide() 路径
  * 窗口内 LLM 调用数 > max_calls_per_window → 回落
  * 兜底保证：**绝不静默丢消息**

# 隔离

  * 不 import redis —— 存储操作全走注入的 storage 对象（带 mock 测试便利）
  * _call_llm 通过参数注入 —— 测试可 monkeypatch，不连真实 LLM
  * 启动/排空由 KeepsakeProvider 显式调用，不与 Hermes runtime 隐式耦合
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, NamedTuple, Optional, Tuple

# 受控元认知标签的判据（`low_confidence` / `context_dependent`）单点在 shared.py ——
# 与治理保留字（`core` / `core_candidate`）同一处口径表，见那里的两条注释。
from .shared import sanitize_distill_tags

# 常驻层治理的**标签口径**从 tiering 单点 import（core.pipeline → core.tiering
# 单向；tiering 不 import 本模块，没有环）。理由是 G5 那条单点闸：同一个标在两个
# 模块各写一份字面量，漂移之后的表现是「班车提了名、审核页按另一半前缀过滤」，
# 建议凭空消失而没人发现。**依赖方向别反过来**：tiering 反向依赖 pipeline 才是环。
from .shared import VOID_KEY as _VOID_KEY
from .tiering import CORE_TAG, DEMOTE_CANDIDATE_TAG, EVID_PREFIX, tag_list

# 日志名**钉在搬家前的模块名上**：logger 名是对外可观测行为（外部 log 配置、
# caplog 过滤都按名找），搬目录不改名，否则等于悄悄改行为。
logger = logging.getLogger("keepsake.pipeline")


# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULT_PIPELINE_CONFIG: Dict[str, Any] = {
    "enabled": True,
    # 2026-09 移除硬编码模型兜底 —— 空串表示「由 config.json 的 llm.model 字段提供」；
    # 若 config.json 也没给，pipeline 启动时检测不到有效 channel → 跳过 daemon 启动，
    # 调用方按既有「no-LLM 降级」路径（v1 规则闸门直存原文）走。
    "model": "",
    "window_pairs": 4,
    "window_seconds": 30.0,
    "max_calls_per_window": 8,
    "update_top_k": 5,            # UPDATE 阶段每条 fact 取的相似旧碎片数
    "recent_context_size": 8,     # 提取相的会话内环形缓冲大小
}

VALID_KINDS = frozenset({"fact", "decision", "preference", "pending", "state_change"})

#: VOID（= 旧 DELETE）路径专用 sentinel。唯一定义处在 `storage_shared`（契约 v2 起）——
#: 三个存储后端判「这条是不是作废封边」用的是**同一个对象**，字面量分头写一遍
#: 早晚会漂一处，而漂了的后果是 DELETE 封边被当成普通 UPDATE 封边。
VOID_KEY = _VOID_KEY

#: 更新相的五类裁决（Hy-Memory 调和协议移植）。
#: `SUPERSEDE` 与 `UPDATE` 的差别在**语义**不在写路径：两者都走
#: `supersede_fragment(old, new)`，SUPERSEDE 额外带 `reason`（旧的不再成立），
#: UPDATE 不带（它是同一条记忆的下一版，批 1 落库值逐字不变）。
VALID_ACTIONS = frozenset({"ADD", "UPDATE", "SUPERSEDE", "VOID", "NOOP"})

#: 旧提示词的四类输出 → 新五类裁决的映射（向后兼容，不炸线上）。
#: DELETE 就是 VOID 的旧名（它一直就是「封旧、不写新事实」），这里只做改名映射。
_LEGACY_ACTION_ALIASES = {"DELETE": "VOID"}

#: VOID 在旧条上落的 `supersede_reason` 值（写死在库里，检索/审计按字面查）。
VOID_REASON = "voided"

#: SUPERSEDE 裁决没给 reason 时的兜底值（reason 是必填字段，但 LLM 会漏）。
DEFAULT_SUPERSEDE_REASON = "superseded"


def normalize_subject(text: Any) -> str:
    """主体归一（Hy-Memory `coding/fact_reconciler.py:152-166` 同款）：折叠所有空白 + 小写。

    折叠（而不是压缩成单个空格）是因为中文正文里空格常是排版噪声：`用户 常驻上海`
    与 `用户常驻杭州` 指同一个人，判等闸不该被排版差异骗过去。
    """
    return "".join(str(text or "").split()).casefold()


def _parse_tag_list(raw: Any) -> str:
    """提取相的 `tags` 字段 → 逗号串（两种写法都收：数组 / 逗号串）。

    净化不在这里做 —— 放行判据单点在 `shared.sanitize_distill_tags`，写入时才过，
    这样「提炼输出」与「落库标签」两处不会各有一份白名单。
    """
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = str(raw or "").split(",")
    return ",".join(t.strip() for t in items if str(t).strip())


# ---------------------------------------------------------------------------
# 提示词（few-shot 内嵌，注释里写出设计点）
# ---------------------------------------------------------------------------

# 提取相：要点写在注释里
#   1. 只提「脱离本对话仍有长期价值」的内容 —— 纯确认/寒暄/状态询问 → 空数组
#   2. kind ∈ {fact, decision, preference, pending, state_change}
#   3. few-shot 必须给出三类典型：
#      - "可以的" → 空数组（废话）
#      - "资金总览要做分页" → pending（任务/待办）
#      - "我选了A方案今晚部署" → decision（用户决定）
#   4. tags：只有两个**受控**标签可写（low_confidence / context_dependent），
#      其余自造标签服务端一律剥掉 —— 所以提示词里明说「不要自造」。
EXTRACT_SYSTEM = "你是一位知识提取专家，擅长从对话中提炼长期有价值的事实条目。"

EXTRACT_USER_TEMPLATE = """从以下对话窗口中提取"脱离本对话仍有长期价值"的事实条目。

规则：
1. 只提取「脱离本对话仍有长期价值」的内容
2. 纯确认（"可以的"）、寒暄、状态询问 → 返回空数组
3. kind 取值：fact / decision / preference / pending / state_change
4. 每条 fact 包含 content 和 kind
5. tags 是可选数组：你**拿不准**这条时写 "low_confidence"，只有**在特定语境下
   才成立**时写 "context_dependent"，两者都不必写就是空数组
   ⚠️ 不要自造其他标签（系统会全部剥掉）
6. content 只写事实本身，**不要**把旧表述或上一轮的细节抄进来
7. content 必须**以主体开头**（"用户…"/"John…"/"订单A…"）：更新相靠主体判
   「是不是同一个人/同一个对象」，正文里没有主体，那条判据就无从比对
8. 输出严格 JSON，格式：`{{"facts":[{{"content":"...","kind":"...","tags":[]}}]}}`

会话内最近 {recent_count} 条上下文（仅供语境参考，不要从中提事实）：
{recent_context}

本窗口对话（{window_count} 条 user/assistant 对）：
{window_turns}

few-shot：
- "可以的" → {{"facts":[]}}
- "资金总览要做分页" → {{"facts":[{{"content":"资金总览需要做分页","kind":"pending","tags":[]}}]}}
- "我选了A方案今晚部署" → {{"facts":[{{"content":"用户决定选择A方案今晚部署","kind":"decision","tags":[]}}]}}
- "好像是三四月份吧" → {{"facts":[{{"content":"用户提到上线时间记不太准","kind":"fact","tags":["low_confidence"]}}]}}
"""


# 更新相（调和协议）：要点写在注释里
#   1. ADD：无等价物 → 写新碎片
#   2. UPDATE：**同一条**记忆的改写（它是自己的下一版）→ 写新条 + 封旧
#   3. SUPERSEDE：两条独立事实、新顶旧（旧的不再成立）→ 写新条 + 封旧 + 记 reason
#   4. VOID：旧的不再成立且**没有**替代它的新事实 → 只封旧（逻辑删，不写新条）
#   5. NOOP：忽略
#   6. 每条裁决必须带 subject（主体）；服务端会拿它做等值闸，主体不同直接降级 ADD
#   7. few-shot 四例（顶替链 / 链头禁抄 / 异主体拆 / 复发独立）—— 移植自
#      Hy-Memory prompts_zh.py:779-869 的校准集，格式随本模块既有风格
UPDATE_SYSTEM = "你是一位记忆整理专家，决定新事实与已有记忆的关系（ADD/UPDATE/SUPERSEDE/VOID/NOOP）。"

UPDATE_USER_TEMPLATE = """新事实：{new_fact}
kind: {new_kind}

已有相似记忆（top {top_k}）：
{candidates_block}

五选一：
- ADD：新事实无等价物 → 写新记忆
- UPDATE：同一条记忆的改写（旧表述过时，新表述是它自己的下一版）→ 写新记忆 + 封旧
- SUPERSEDE：两条独立事实，新事实顶掉旧事实（旧的不再成立）→ 写新记忆 + 封旧 + 记原因
- VOID：旧记忆已经不成立且**没有**替代它的新事实 → 只封旧，不写新记忆
- NOOP：新事实已被旧记忆覆盖 → 忽略

判定规则（违反任意一条都会算你判错）：
1. SUPERSEDE 只用于「同一维度、不能同时为真」：换城市 / 偏好明确反转 / 数值口径更新。
   **加细节、措辞不同、可以共存的两条偏好，一律不许 SUPERSEDE**（用 UPDATE 或 ADD）。
2. SUPERSEDE 时新条正文**只写新状态**：不许把被顶旧条的细节抄进来（链头会自相矛盾）。
3. **主体不同永不合并**：「John 喝黑咖啡」不许动「用户喝黑咖啡」→ 判 ADD。
4. 同一事件类型的**另一次发生**是独立事实（判 ADD），不要写"第 N 次"这种次数长句。

subject：本条事实的主体（人/组织/对象，如"用户"/"John"/"订单A"），必填。
reason：仅 SUPERSEDE 必填，一句话说明旧事实为什么不再成立。

输出严格 JSON：`{{"action":"ADD|UPDATE|SUPERSEDE|VOID|NOOP","target_key":"<旧 key 或空>","subject":"...","reason":"..."}}`

few-shot：
- 顶替链：旧"用户常驻上海" + 新"用户常驻杭州" →
  {{"action":"SUPERSEDE","target_key":"<旧 key>","subject":"用户","reason":"居住城市由上海变更为杭州"}}
- 链头禁抄：旧"用户常驻上海，租约 2026 年 3 月到期" + 新"用户已搬到杭州，租约转签到 2026 年 9 月" →
  {{"action":"SUPERSEDE","target_key":"<旧 key>","subject":"用户","reason":"搬家后租约随之转签"}}（新条正文只写杭州与 9 月，不许抄"3 月到期"）
- 异主体拆：旧"用户喝黑咖啡" + 新"John 喝黑咖啡" →
  {{"action":"ADD","target_key":"","subject":"John","reason":""}}（主体不同，永不合并）
- 复发独立：旧"用户 10 月 8 日完成了周报" + 新"用户 10 月 15 日又完成了一次周报" →
  {{"action":"ADD","target_key":"","subject":"用户","reason":""}}（同一事件的另一次发生 = 独立事实）
- 状态变更：旧"用户要求做前端分页" + 新"分页已完成上线并验证" →
  {{"action":"UPDATE","target_key":"<旧 key>","subject":"用户","reason":""}}
"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    """单轮对话（user + assistant）。"""
    user: str
    assistant: str
    timestamp: float


@dataclass
class Fact:
    """提取相输出：单条事实。"""
    content: str
    kind: str  # fact / decision / preference / pending / state_change
    #: 提炼相自评的**受控**标签（逗号串；只有 `low_confidence` /
    #: `context_dependent` 会被 `sanitize_distill_tags` 放行，其余自造照剥）。
    #: 默认空串 ⇒ 既有构造点 `Fact("x", "fact")` 逐字不受影响。
    tags: str = ""


@dataclass
class DrainResult:
    """单次 drain 的统计。"""
    window_turns: int = 0
    facts_extracted: int = 0
    adds: int = 0
    updates: int = 0
    #: SUPERSEDE（新顶旧）计数。UPDATE 仍记在 `updates` —— 两者写路径同款、
    #: 只差 reason，而 reason 落在碎片上不落在统计里，拆两个计数器是给看板看的。
    supersedes: int = 0
    #: VOID（逻辑删）计数。字段名沿用 `deletes`：它是既有对外字段，而 VOID 就是
    #: 老 DELETE 的正名（映射见 `_parse_action`），改名会让既有看板断线。
    deletes: int = 0
    noops: int = 0
    llm_calls: int = 0
    fallback_v1: bool = False
    fallback_reason: str = ""


class Verdict(NamedTuple):
    """更新相裁决：`action` + 目标 + 主体 + 顶替原因。

    用 NamedTuple 而不是普通 dataclass：既有断言改写成 `Verdict("ADD")` 后，
    读起来仍与老写法一一对应，且多出的两个字段带着默认值 = 少写两处的构造噪音。

    ⚠️ 注意它**不是**二元组了：与 `("ADD", None)` 的相等比较会因长度不等而 False
    （元组相等比的是逐元素**且等长**）。既有的 `== ("ADD", None)` 断言已改成
    `== Verdict("ADD")` —— 这正是加字段该有的样子：改断言，别改判据。
    """
    action: str
    target_key: Optional[str] = None
    subject: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# 核心：Pipeline
# ---------------------------------------------------------------------------

class Pipeline:
    """写侧两相管线。

    调用方（KeepsakeProvider）负责：
      * initialize() 时 start()
      * 每轮对话 enqueue(user, assistant)
      * shutdown() 时 stop(drain=True)

    Pipeline 自身不连 Redis（依赖注入的 storage），不调真实 LLM
    （_call_llm 通过构造函数注入，单测可 monkeypatch）。
    """

    def __init__(
        self,
        storage: Any,
        *,
        llm_fn: Optional[Callable[..., Optional[str]]] = None,
        model: str = DEFAULT_PIPELINE_CONFIG["model"],
        window_pairs: int = DEFAULT_PIPELINE_CONFIG["window_pairs"],
        window_seconds: float = DEFAULT_PIPELINE_CONFIG["window_seconds"],
        max_calls_per_window: int = DEFAULT_PIPELINE_CONFIG["max_calls_per_window"],
        update_top_k: int = DEFAULT_PIPELINE_CONFIG["update_top_k"],
        recent_context_size: int = DEFAULT_PIPELINE_CONFIG["recent_context_size"],
        gate_fallback: Optional[Callable[[str, str], None]] = None,
        # 2026-09 热生效：可选通道刷新回调。每次 _drain_now 开头调用一次；
        # 返回新 channel 字典（resolved llm channel），无值时返回 None → 走构造时绑定的 llm_fn。
        # 单测可 monkeypatch 此回调以验证 mtime 感知 + 中途换 model 生效。
        channel_refresher: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    ):
        """
        参数:
            storage: StorageBase 实例（必须实现 search_bm25/store/get_fragment/supersede_fragment/
            fragment_exists/set_supersedes）
            llm_fn: LLM 调用函数，签名 (messages, model) -> Optional[str]；None 则全部走 v1 兜底
            model: LLM 模型名
            window_pairs: 队列攒够多少对触发 drain
            window_seconds: 距上次 drain 超过多少秒触发 drain
            max_calls_per_window: 单窗口 LLM 调用硬顶
            update_top_k: 更新相每条 fact 取的相似旧碎片数
            recent_context_size: 提取相注入的会话内环形缓冲大小
            gate_fallback: v1 兜底函数 (text, category) -> None（通常就是 sync_turn 走 decide() 的那段）
            channel_refresher: 通道刷新回调 → 返回 (channel_dict, llm_fn)；用于热生效
        """
        self._storage = storage
        self._llm_fn = llm_fn
        self._model = model
        self._window_pairs = int(window_pairs)
        self._window_seconds = float(window_seconds)
        self._max_calls = int(max_calls_per_window)
        self._update_top_k = int(update_top_k)
        self._recent_size = int(recent_context_size)
        self._gate_fallback = gate_fallback
        # 热生效：回调返回 (channel_dict, llm_fn)；None 表示本窗不刷新（保留原 llm_fn）
        self._channel_refresher = channel_refresher

        self._queue: Deque[Turn] = deque()
        self._lock = threading.Lock()
        self._recent_ring: Deque[Tuple[str, str]] = deque(maxlen=self._recent_size * 2)
        self._last_drain_ts: float = time.time()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动 daemon 线程。幂等。"""
        if self._started:
            return
        self._stop_event.clear()
        # 初始化 last_drain_ts 为当前时间，否则第一次 enqueue 会被 window_seconds 条件
        # 误判为「距上次 drain 超 30s」而立即 drain（首次启动不该这样）
        self._last_drain_ts = time.time()
        self._thread = threading.Thread(
            target=self._run, name="keepsake-pipeline", daemon=True,
        )
        self._thread.start()
        self._started = True
        logger.info("keepsake pipeline started (window_pairs=%d, window_seconds=%.0f)",
                    self._window_pairs, self._window_seconds)

    def stop(self, *, drain: bool = True, timeout: float = 5.0) -> None:
        """停止 daemon；可选先排空队列（shutdown 路径）。"""
        if drain:
            try:
                self._drain_now()
            except Exception as e:
                logger.warning("keepsake pipeline drain on shutdown failed: %s", e)
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._started = False
        logger.info("keepsake pipeline stopped")

    def enqueue(self, user_text: str, assistant_text: str = "") -> None:
        """主路径调用（sync_turn 内）：入队 + 视情况立即 drain。"""
        if not user_text or not user_text.strip():
            return
        with self._lock:
            self._queue.append(Turn(
                user=user_text,
                assistant=assistant_text or "",
                timestamp=time.time(),
            ))
            self._recent_ring.append((user_text, assistant_text or ""))
            # 触发条件 1：攒够 window_pairs
            if len(self._queue) >= self._window_pairs:
                should_drain = True
            else:
                # 触发条件 2：距上次 drain 超 window_seconds
                should_drain = (time.time() - self._last_drain_ts) >= self._window_seconds
        if should_drain:
            self._drain_now()

    # ------------------------------------------------------------------
    # Daemon 主循环
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """daemon 主循环：每秒检查一次窗口条件。"""
        while not self._stop_event.is_set():
            try:
                # 检查条件（持锁拷贝必要状态）
                should_drain = False
                with self._lock:
                    if self._queue:
                        if len(self._queue) >= self._window_pairs:
                            should_drain = True
                        elif (time.time() - self._last_drain_ts) >= self._window_seconds:
                            should_drain = True
                if should_drain:
                    self._drain_now()
            except Exception as e:
                logger.warning("keepsake pipeline loop error: %s", e)
            # 节流 1s（细粒度足够；shutdown 信号最长 1s 延迟）
            self._stop_event.wait(timeout=1.0)

    # ------------------------------------------------------------------
    # 单次 drain
    # ------------------------------------------------------------------

    def _drain_now(self) -> DrainResult:
        """立即排空一次窗口。无 turn 也返回空结果。

        2026-09 起：开头调一次 channel_refresher → 拿到新 channel / llm_fn 后用本窗。
        若回调返回 None → 沿用构造时绑定的 llm_fn（向后兼容）。
        channel 不存在（unconfigured）→ self._llm_fn 暂置 None → 后续走 llm_unavailable 兜底。
        """
        # 1. 热生效：窗口开头刷一次 channel
        self._refresh_channel()
        with self._lock:
            turns = list(self._queue)
            self._queue.clear()
            recent = list(self._recent_ring)
            self._last_drain_ts = time.time()
        if not turns:
            return DrainResult()
        return self._process_window(turns, recent)

    def _refresh_channel(self) -> None:
        """调 channel_refresher 把最新 channel 绑到本窗 llm_fn。

        回调约定：返回 (channel_dict, llm_fn)；None 表示本窗不刷新（保留原 llm_fn）。
        channel_dict.valid=False → 视同无 LLM → self._llm_fn = None（v1 兜底）。
        channel_dict.valid=True → 用回调给的 llm_fn（已 partial 绑定新 channel）。
        """
        if self._channel_refresher is None:
            return
        try:
            result = self._channel_refresher()
        except Exception as e:
            logger.warning("pipeline: channel_refresher raised: %s — keeping previous llm_fn", e)
            return
        if result is None:
            return  # 本窗不刷新（保留原 llm_fn + channel）
        channel, llm_fn = result
        if channel is None:
            return
        # channel 解析为 unconfigured → 关掉本窗 LLM 通道（走 v1 兜底）
        if not channel.get("valid"):
            logger.debug("pipeline: channel invalid (source=%s) — using v1 fallback", channel.get("source"))
            self._llm_fn = None
            return
        self._llm_fn = llm_fn
        # model 也跟着刷新（让本窗用新 model 值）
        new_model = channel.get("model") or self._model
        if new_model:
            self._model = new_model

    def _process_window(self, turns: List[Turn], recent: List[Tuple[str, str]]) -> DrainResult:
        """处理一个窗口：提取相 → 更新相；任何一步失败整体兜底 v1。"""
        result = DrainResult(window_turns=len(turns))

        # 0. 兜底前置：LLM 不可用 → 全部走 v1 兜底
        if self._llm_fn is None:
            return self._fallback_to_v1(turns, result, reason="llm_unavailable")

        # 1. 提取相
        try:
            facts = self._extract_phase(turns, recent)
        except _LLMFailure as e:
            return self._fallback_to_v1(turns, result, reason=f"extract_failed:{e}")
        except _BudgetExceeded:
            return self._fallback_to_v1(turns, result, reason="budget_exceeded")

        result.facts_extracted = len(facts)
        result.llm_calls += 1  # 提取相算一次

        if not facts:
            return result

        # 2. 更新相（每条 fact 一次 LLM）
        for fact in facts:
            if result.llm_calls >= self._max_calls:
                return self._fallback_to_v1(turns, result, reason="budget_exceeded")
            try:
                verdict = self._update_phase(fact)
            except _LLMFailure as e:
                return self._fallback_to_v1(turns, result, reason=f"update_failed:{e}")
            except _BudgetExceeded:
                return self._fallback_to_v1(turns, result, reason="budget_exceeded")
            result.llm_calls += 1

            action = verdict.action
            target_key = verdict.target_key
            if action == "ADD":
                self._do_add(fact)
                result.adds += 1
            elif action == "UPDATE":
                self._do_update(fact, target_key)
                result.updates += 1
            elif action == "SUPERSEDE":
                self._do_supersede(fact, target_key, verdict.reason)
                result.supersedes += 1
            elif action == "VOID":
                self._do_void(fact, target_key)
                result.deletes += 1
            else:  # NOOP 或未知 action
                result.noops += 1

        return result

    # ------------------------------------------------------------------
    # 提取相
    # ------------------------------------------------------------------

    def _extract_phase(self, turns: List[Turn], recent: List[Tuple[str, str]]) -> List[Fact]:
        """一次 LLM 调用，返回 facts 列表。失败抛 _LLMFailure。"""
        recent_lines: List[str] = []
        for u, a in recent:
            recent_lines.append(f"user: {u[:200]}")
            if a:
                recent_lines.append(f"assistant: {a[:200]}")
        recent_text = "\n".join(recent_lines[-self._recent_size * 2:]) or "(无)"

        window_lines: List[str] = []
        for t in turns:
            window_lines.append(f"user: {t.user[:500]}")
            if t.assistant:
                window_lines.append(f"assistant: {t.assistant[:500]}")
        window_text = "\n".join(window_lines)

        messages = [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": EXTRACT_USER_TEMPLATE.format(
                recent_count=self._recent_size,
                recent_context=recent_text,
                window_count=len(turns),
                window_turns=window_text,
            )},
        ]
        try:
            raw = self._llm_fn(messages, self._model)  # type: ignore[misc]
        except Exception as e:
            raise _LLMFailure(f"extract LLM raised: {e}") from e
        if raw is None:
            raise _LLMFailure("llm returned None")
        facts = self._parse_facts(raw)
        return facts

    @staticmethod
    def _parse_facts(raw: str) -> List[Fact]:
        """LLM 输出 → Fact 列表。失败抛 _LLMFailure。"""
        # 兼容 LLM 在 JSON 外层包 ```json ... ``` 或前缀废话
        text = raw.strip()
        # 找到第一个 { 和最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < 0 or end <= start:
            raise _LLMFailure("no JSON object")
        candidate = text[start:end + 1]
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError as e:
            raise _LLMFailure(f"json decode error: {e}") from e
        facts_raw = obj.get("facts")
        if not isinstance(facts_raw, list):
            return []  # 空数组合法
        out: List[Fact] = []
        for item in facts_raw:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            kind = item.get("kind") or "fact"
            if not content or not isinstance(content, str):
                continue
            kind = str(kind).strip().lower()
            if kind not in VALID_KINDS:
                kind = "fact"  # 未知 kind 兜底为 fact
            out.append(Fact(content=content.strip(), kind=kind,
                            tags=_parse_tag_list(item.get("tags"))))
        return out

    # ------------------------------------------------------------------
    # 更新相
    # ------------------------------------------------------------------

    def _update_phase(self, fact: Fact) -> Verdict:
        """一次 LLM 调用：拿新 fact + 已有相似 → `Verdict`（action/target/subject/reason）。

        target_key 仅在 UPDATE/SUPERSEDE/VOID 时有意义（要封边哪个旧 key）；
        ADD/NOOP 时为 None —— 由执行函数按需 search 兜底。

        target_key 必须过 _sane_target 双校验（白名单 + exists）才返回；幻觉 key
        一律打 warning + 退回 None，让执行函数走 _find_supersede_target。

        🔴 **主体闸在这里**（`_subject_gate_ok`）：裁决声明的 subject 必须同时命中
        新正文与目标旧正文，否则 SUPERSEDE/UPDATE 一律**降级为 ADD** + 一行日志 ——
        「主体不同永不合并」是提示词教不会的那条，必须服务端兜底。
        """
        # 取 top-K 相似旧碎片（search_bm25 已经自然排除 consumed/superseded）
        candidates: List[Dict[str, Any]] = []
        try:
            results = self._storage.search_bm25(fact.content, tag_filter="")
            candidates = results[:self._update_top_k]
        except Exception as e:
            logger.debug("pipeline: search_bm25 for fact failed: %s", e)

        # 构造候选块
        if candidates:
            lines = []
            for i, c in enumerate(candidates, 1):
                key = c.get("_key", "?")
                content = c.get("content", "")[:200]
                kind = c.get("category", "?")
                lines.append(f"[{i}] key={key} kind={kind}\n    {content}")
            candidates_block = "\n".join(lines)
        else:
            candidates_block = "(无相似旧记忆)"

        messages = [
            {"role": "system", "content": UPDATE_SYSTEM},
            {"role": "user", "content": UPDATE_USER_TEMPLATE.format(
                new_fact=fact.content[:300],
                new_kind=fact.kind,
                top_k=self._update_top_k,
                candidates_block=candidates_block,
            )},
        ]
        try:
            raw = self._llm_fn(messages, self._model)  # type: ignore[misc]
        except Exception as e:
            raise _LLMFailure(f"update LLM raised: {e}") from e
        if raw is None:
            raise _LLMFailure("llm returned None")
        verdict = self._parse_action(raw)

        # 校验 LLM 给的 target_key：白名单 + exists 双保险；任一不过 → 拒为幻觉 key
        if verdict.target_key is not None and verdict.action in ("UPDATE", "SUPERSEDE", "VOID"):
            sane_key = self._sane_target(verdict.target_key, candidates, raw_from_llm=True)
            if sane_key is None:
                logger.warning(
                    "pipeline: rejected hallucinated target_key fact=%r key=%s",
                    fact.content[:60], verdict.target_key,
                )
                verdict = verdict._replace(target_key=None)

        # 🔴 主体闸：主体不同不许合并 → 降级 ADD（只对「LLM 明确点了目标」的路径
        # 生效：target_key 被拒或走 _find 兜底时手里没有旧正文可比，见闸的注释）。
        if verdict.action in ("UPDATE", "SUPERSEDE") and verdict.target_key:
            old_row = next((c for c in candidates
                            if c.get("_key") == verdict.target_key), None)
            if old_row is not None and not self._subject_gate_ok(
                    verdict.subject, fact.content, old_row.get("content", "")):
                logger.warning(
                    "pipeline: subject gate tripped (降级 ADD) action=%s subject=%r "
                    "fact=%r key=%s", verdict.action, verdict.subject,
                    fact.content[:60], verdict.target_key,
                )
                verdict = verdict._replace(action="ADD", target_key=None)

        return verdict

    @staticmethod
    def _subject_gate_ok(subject: str, new_content: str, old_content: str) -> bool:
        """主体闸：归一后的 subject 必须**同时**出现在新正文与旧正文里（任一缺 → 拒）。

        为什么要这道闸：`SUPERSEDE`/`UPDATE` 会让旧条退出结果面（`is_latest='0'`），
        而「主体不同」（John vs 用户）是唯一一种**错了也不报错**的错顶 —— 内容看着
        相近、旧条照样被打上链身、损失是静默的。提示词里教了主体不许合并，但
        few-shot 校准压不住每一次，所以服务端再判一道。

        ponytail: 判据是「声明的 subject 双边命中正文」而不是「旧条主体 == 新条主体
        逐字相等」—— 旧条的主体**没有落库**（契约 v2 四列里没有 subject 列，加列
        要动三后端迁移，超出本单范围），只能反查正文。因此它的精度挂在提取相
        「content 以主体开头」那条提示词上（`EXTRACT_USER_TEMPLATE` 第 7 条）。
        误伤有两种：声明了抽象主体（如"工作地点"）、或新条省略了主语 ⇒ 一次本该
        SUPERSEDE 的裁决被降级成 ADD。**方向是安全的**（多留一条 + 一行日志，
        绝不错顶别人的记忆），这也是刻意选它的原因。升级路径：契约补 `subject`
        列后改成两值逐字相等，误伤随之归零。
        """
        s = normalize_subject(subject)
        if not s:
            return True   # 没声明主体 = 老式输出，不拦（拦了会把线上 UPDATE 全打成 ADD）
        return (s in normalize_subject(new_content)
                and s in normalize_subject(old_content))

    @staticmethod
    def _parse_action(raw: str) -> Verdict:
        """LLM 输出 → `Verdict`（action / target_key / subject / reason）。

        严格策略：只接受合法 JSON。JSON 缺失/解析失败/action 不在
        {ADD, UPDATE, SUPERSEDE, VOID, NOOP} 内 → 一律返回 `Verdict("NOOP")`。

        拒绝文本关键字兜底——「无需 UPDATE」这类解释性废话会误触发 UPDATE 封边。

        `DELETE` 经 `_LEGACY_ACTION_ALIASES` 映射成 `VOID`（旧提示词输出的正名，
        行为不变 + 一行日志），这样**换提示词不需要同时换模型**：升级当天线上仍可能
        吐出 DELETE。
        """
        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return Verdict("NOOP")
        candidate = text[start:end + 1]
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            return Verdict("NOOP")
        action = str(obj.get("action", "")).strip().upper()
        tk = obj.get("target_key")
        target_key: Optional[str] = None
        if isinstance(tk, str) and tk:
            target_key = tk
        if action in _LEGACY_ACTION_ALIASES:
            action = _LEGACY_ACTION_ALIASES[action]
            logger.info("pipeline: legacy action DELETE mapped to VOID (target=%s)",
                        target_key or "-")
        if action not in VALID_ACTIONS:
            return Verdict("NOOP")
        return Verdict(action, target_key,
                       subject=str(obj.get("subject") or "").strip(),
                       reason=str(obj.get("reason") or "").strip())

    # ------------------------------------------------------------------
    # 写入 / 封边（不 import redis，全走注入 storage）
    # ------------------------------------------------------------------

    def _store_fact(self, fact: Fact) -> Optional[str]:
        """落一条新事实（ADD / UPDATE / 常驻提名的证据共用），返回它的 key；失败 None。

        证据必须**先有实体**：常驻否决提名的证据标（EVID_PREFIX + ':' + key）指向这条新事实，
        而 DELETE 路径原来根本不写库（只封旧）⇒ 走提名时得在这里补一次落库，
        否则审核页上的证据点开是空的（比没有证据更糟：看起来像编的）。

        R7 接线：写库前先 scrub_secrets（凭据打码源头闸门）；返回值用
        scrub 后的正文算 key（否则算出来的 key 与库里那条对不上）。
        """
        from .ingest_gate import scrub_secrets
        try:
            scrubbed, _ = scrub_secrets(fact.content)
            content = scrubbed if scrubbed else fact.content
        except Exception as e:
            logger.debug("pipeline: scrub_secrets failed: %s; storing raw", e)
            content = fact.content
        try:
            self._storage.store(
                text=content,
                tags=self._tags_for(fact),
                category="fact_v2",
                source="pipeline_v2",
                fragment_type="memory",
            )
        except Exception as e:
            logger.warning("pipeline: store failed for fact %r: %s", fact.content[:60], e)
            return None
        return self._key_for_content(content)

    def _do_add(self, fact: Fact) -> None:
        """ADD：直接写新碎片（category=fact_v2 让消费侧可识别）。"""
        self._store_fact(fact)

    # ------------------------------------------------------------------
    # 常驻否决：管线对 core 的 UPDATE/DELETE 意图**不封边**，转成退居提名
    # ------------------------------------------------------------------

    def _is_core(self, key: Optional[str]) -> bool:
        """目标碎片是不是常驻条目（tags 含完整标签 `core`，逗号边界口径同 `tag_list`）。

        读不到（后端异常 / 存储没实现 get_fragment）⇒ **按非 core 放行**：
        这是治理闸不是存储闸，一次读失败就把正常管线的 UPDATE 全变成提名，
        等于把「记忆写不进去」换成了「审核队列被灌满」，两个都坏。
        放行的代价（漏封一次常驻）比另一种代价（管线整体瘫痪）小，且有日志。
        """
        if not key:
            return False
        try:
            row = self._storage.get_fragment(key) or {}
        except Exception as e:
            logger.warning("pipeline: core 判定读不到 %s（按非 core 放行）: %s", key, e)
            return False
        return CORE_TAG in tag_list(row.get("tags"))

    def _nominate_core_conflict(self, core_key: str, *, action: str,
                                evid_key: Optional[str]) -> bool:
        """管线判 UPDATE/DELETE、目标是常驻 ⇒ 打退居提名标（带冲突证据），**不封边**。

        为什么不是「直接拒绝」：LLM 说「这条常驻与新事实冲突」是**最难得的一手
        信号** —— 管线自己找到了矛盾。直接丢掉等于把最有价值的证据扔进垃圾桶。
        转成提名 + 证据标（EVID_PREFIX:<新事实 key>）交到审核队列，批不批仍然是人的事
        （「常驻层只由人与审核决定」这条治理拍板一个字没让）。

        幂等：已在退居队列的**不重复打标**（只 warning）—— 一天里同一场对话触发
        多次也只提一次，队列里也不会堆出 N 条同义建议。
        """
        try:
            row = self._storage.get_fragment(core_key) or {}
        except Exception as e:
            logger.warning("pipeline: 常驻提名读不到 %s（放弃提名）: %s", core_key, e)
            return False
        tags = tag_list(row.get("tags"))
        if DEMOTE_CANDIDATE_TAG in tags:
            logger.info("pipeline: core 冲突已在退居队列，跳过重复提名 core=%s "
                        "evid=%s action=%s", core_key, evid_key or "-", action)
            return False
        new_tags = tags + [DEMOTE_CANDIDATE_TAG]
        if evid_key:
            new_tags.append(f"{EVID_PREFIX}:{evid_key}")
        try:
            ok = self._storage.update_fragment_fields(core_key, {"tags": ",".join(new_tags)})
        except Exception as e:
            logger.warning("pipeline: 常驻提名写标签失败 core=%s: %s", core_key, e)
            return False
        # 提名标本身不作为检索词（`tier:` 前缀词不进分词面），改 tags 不重算 tsv
        # 是既有 `update_fragment_fields` 语义（tiering 打标同款），此处接受。
        logger.info("pipeline: core 冲突转退居提名 core=%s evid=%s action=%s ok=%s",
                    core_key, evid_key or "-", action, ok)
        return True

    def _do_update(self, fact: Fact, target_key: Optional[str] = None) -> None:
        """UPDATE：写新碎片 supersedes=<old>；旧碎片 superseded_by=<new>。

        target_key 由 _update_phase 的 LLM 提供（已过 _sane_target 校验）；
        若为 None（LLM 没给 / 幻觉被拒）→ 走 _find_supersede_target（同样过 exists 校验）。

        R7 接线：新碎片写库前 scrub_secrets（见 `_store_fact`）；supersedes 链路仍用
        scrubbed 后的内容计算 key（否则新 key 与原 key 不一致，封边断裂）。

        🔴 目标是常驻条目时**不封边**，改走退居提名（`_nominate_core_conflict`）。

        reason 恒为空串（UPDATE = 同一条记忆的下一版，没有「旧的不再成立」可言）
        ⇒ 落库值与批 1 逐字一致，本方法体就是 SUPERSEDE 的 `reason=""` 特例。
        """
        self._seal_with_new(fact, target_key, reason="", action="UPDATE")

    def _do_supersede(self, fact: Fact, target_key: Optional[str] = None,
                      reason: str = "") -> None:
        """SUPERSEDE：写新碎片 + 旧碎片 superseded_by（带 reason，走契约 v2 写路径）。

        与 UPDATE 的差别只有**语义**（写入层同一段代码）：
          * SUPERSEDE = 两条独立事实、旧的不再成立 ⇒ 旧条 `superseded_by` + 转链身
            （`is_latest='0'`）+ 新条 `supersedes=[旧 key]` + `supersede_reason`；
          * UPDATE = 同一条记忆的下一版 ⇒ 不带 reason（批 1 的既有行为逐字不变）。

        reason 缺失（LLM 漏填）时兜底成 `DEFAULT_SUPERSEDE_REASON`，不留空 ——
        「为什么顶掉」是这条链唯一可解释的东西，空串等于把查证路堵死。
        """
        self._seal_with_new(
            fact, target_key,
            reason=reason.strip() or DEFAULT_SUPERSEDE_REASON,
            action="SUPERSEDE",
        )

    def _seal_with_new(self, fact: Fact, target_key: Optional[str], *,
                       reason: str, action: str) -> None:
        """UPDATE / SUPERSEDE 共用体：写新条 → 常驻否决 → 封旧 + 记 reason。

        两个动作共用不是「抽象洁癖」，是因为它们**必须逐字同款地**走同一条链维护
        路径（set_supersedes + supersede_fragment，且都是 store 在前、封边在后）；
        抄两份就是下一单「改了 A 忘了 B」的标准现场。差别只由 `reason` 承载。
        """
        old_key = target_key or self._find_supersede_target(fact.content)
        if not old_key:
            # 找不到候选 → 退化 ADD（避免静默丢）
            self._do_add(fact)
            return
        new_key = self._store_fact(fact)
        if not new_key:
            return
        # 🔴 常驻否决：目标是 core ⇒ **不封边**，把「新事实 vs 这条常驻」的冲突
        # 转成退居提名（新事实已入库，正好当证据）。放在 store 之后、supersede
        # 之前 —— 两个动作互斥且顺序固定（先有证据实体，再提名）。
        if self._is_core(old_key):
            self._nominate_core_conflict(old_key, action=action, evid_key=new_key)
            return
        try:
            # 新碎片加 supersedes 字段（单向留痕，走后端中立接口）
            self._storage.set_supersedes(new_key, old_key)
        except Exception as e:
            logger.debug("pipeline: set supersedes field failed: %s", e)
        try:
            self._storage.supersede_fragment(old_key, new_key, reason)
        except Exception as e:
            logger.warning("pipeline: supersede_fragment failed: %s", e)

    def _do_void(self, fact: Fact, target_key: Optional[str] = None) -> None:
        """VOID（= 旧 DELETE 的正名）：旧条**逻辑删**，不写新事实。

        落库形态 = 旧条 `superseded_by='__void__'` + `supersede_reason='voided'`
        + `is_latest='0'`（检索侧据此退出结果面），**正文保留** —— 「永不召回但
        保留正文」是可查证与可回滚的前提，物理删就查不动「当初为什么删的」了。

        与「supersede 封边」的实现关系：三后端的 `supersede_fragment` 对
        `new_key == __void__` 走的就是**逻辑删分支**（不打新条、reason 记旧条），
        与封边分支同函数不同分支，所以**不必拆函数**；差别体现在 reason 这个新参数上
        —— 封边传裁决给的 reason，逻辑删恒传 `VOID_REASON`。

        🔴 目标是 core 时**例外**：不封边，改走退居提名（见 `_nominate_core_conflict`）；
        这条路径要把触发性新事实**先落库**（VOID 原语义不写库），否则证据 key
        指向一个不存在的条目。
        """
        old_key = target_key or self._find_supersede_target(fact.content)
        if not old_key:
            return
        if self._is_core(old_key):
            evid_key = self._store_fact(fact)
            self._nominate_core_conflict(old_key, action="VOID", evid_key=evid_key)
            return
        try:
            self._storage.supersede_fragment(old_key, VOID_KEY, VOID_REASON)
        except Exception as e:
            logger.warning("pipeline: VOID failed: %s", e)

    def _find_supersede_target(self, fact_content: str) -> Optional[str]:
        """取 search_bm25 top 结果作为封边目标。带 exists 校验（防 LLM 幻觉 key 盲封）。

        search_bm25 自然排除 consumed/superseded；若首个结果存在（客户端可用时
        额外确认 exists），返回其 _key，否则继续向后扫；都不可用 → None。
        """
        try:
            results = self._storage.search_bm25(fact_content, tag_filter="")
        except Exception as e:
            logger.debug("pipeline: _find_supersede_target failed: %s", e)
            return None
        for c in results or []:
            key = c.get("_key")
            if not key:
                continue
            if self._key_exists(key):
                return key
        return None

    def _sane_target(
        self,
        target_key: Optional[str],
        candidates: List[Dict[str, Any]],
        *,
        raw_from_llm: bool,
    ) -> Optional[str]:
        """校验 target_key 是否可用（白名单 + exists 双保险）。

        参数:
            target_key: 待校验的 key（LLM 给的或 _find_supersede_target 返回的）
            candidates: 本次 update_phase 给 LLM 看的候选列表（白名单源）
            raw_from_llm: True=LLM 直接给的；False=内部 _find 兜底结果（白名单可放宽）

        规则:
          - target_key 为空 → None
          - raw_from_llm=True 且 key 不在 candidates 白名单 → None
          - exists(target_key) 为假 → None（client 不可用时容错放行）
          - 都通过 → 原样返回
        """
        if not target_key:
            return None
        if raw_from_llm:
            allowed = {c.get("_key") for c in candidates if c.get("_key")}
            if allowed and target_key not in allowed:
                return None
        if not self._key_exists(target_key):
            return None
        return target_key

    def _key_exists(self, key: str) -> bool:
        """检查碎片是否存在（双保险）。

        后端不可达（None）/ 查询失败 → 容错放行 True（白名单已守一道）。
        与改动前 `client is None → True` 逐字等价。
        """
        try:
            exists = self._storage.fragment_exists(key)
        except Exception:
            return True
        return True if exists is None else bool(exists)

    @staticmethod
    def _key_for_content(text: str) -> str:
        """根据文本推 Redis key（与 storage.store 一致的 sha256[:12] 算法）。"""
        import hashlib
        return f"memory:frag:{hashlib.sha256(text.encode()).hexdigest()[:12]}"

    @staticmethod
    def _tags_for(fact: Fact) -> str:
        """根据 fact.kind 派生 tags，保留 agent 注入由 storage.store 自己加。

        末尾追加提炼相的**受控**自评标签（`low_confidence` / `context_dependent`，
        判据单点在 `shared.sanitize_distill_tags`）：这两个不剥，其余自造照剥。
        检索渲染层不因它们改任何行为 —— 标签只是原样透出给人看。
        """
        base = f"kind:{fact.kind},pipeline:v2"
        extra = sanitize_distill_tags(fact.tags)
        return f"{base},{extra}" if extra else base

    # ------------------------------------------------------------------
    # 兜底：v1 路径
    # ------------------------------------------------------------------

    def _fallback_to_v1(self, turns: List[Turn], result: DrainResult, *, reason: str) -> DrainResult:
        """窗口级兜底：每条 user 走 v1（gate_fallback 或 storage.store 直存）。

        设计点：
          * 优先用 gate_fallback（封装了 decide() + store 的完整 v1 路径）
          * 没有 gate_fallback 时直接 storage.store（最原始兜底）
          * 不抛异常 —— 上层 sync_turn 已经返回，必须保证消息落地
        """
        result.fallback_v1 = True
        result.fallback_reason = reason
        logger.warning(
            "keepsake pipeline: fallback to v1 (reason=%s, turns=%d)",
            reason, len(turns),
        )
        for t in turns:
            if self._gate_fallback is not None:
                try:
                    self._gate_fallback(t.user, "turn_memory")
                    continue
                except Exception as e:
                    logger.debug("pipeline: gate_fallback raised: %s; trying raw store", e)
            # 最后兜底：直接存（不经过闸门 — 风险高，但保证不丢）
            # R7 接线：raw store 前 scrub_secrets（保持写侧源头闸门一致性）
            from .ingest_gate import scrub_secrets
            try:
                scrubbed, _ = scrub_secrets(t.user)
                content = scrubbed if scrubbed else t.user
            except Exception as e:
                logger.debug("pipeline: scrub_secrets failed in fallback: %s; storing raw", e)
                content = t.user
            try:
                self._storage.store(
                    text=content,
                    tags="conversation,fallback:v1",
                    category="turn_memory",
                    source="pipeline_v2_fallback",
                    fragment_type="memory",
                )
            except Exception as e:
                logger.warning("pipeline: raw store fallback failed: %s", e)
        return result


# ---------------------------------------------------------------------------
# 内部异常（不导出）
# ---------------------------------------------------------------------------

class _LLMFailure(Exception):
    """LLM 调用或 JSON 解析失败 —— 触发窗口级 v1 兜底。"""


class _BudgetExceeded(Exception):
    """窗口内 LLM 调用数超 max_calls_per_window —— 触发窗口级 v1 兜底。"""


# ---------------------------------------------------------------------------
# 公共导出
# ---------------------------------------------------------------------------

__all__ = [
    "Pipeline",
    "Turn",
    "Fact",
    "Verdict",
    "DrainResult",
    "DEFAULT_PIPELINE_CONFIG",
    "VALID_KINDS",
    "VALID_ACTIONS",
    "VOID_KEY",
    "VOID_REASON",
    "normalize_subject",
]
