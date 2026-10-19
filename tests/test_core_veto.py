"""常驻层的管线否决（core veto）—— UPDATE/VOID 撞上 core 时**不许封边**。

## 这个漏洞是怎么来的

`_update_phase` 用 `search_bm25(fact.content, tag_filter="")` 取相似旧碎片当候选，
**不排除 `core`**：LLM 判 UPDATE/VOID 之后 `_do_update` / `_do_void` 直接
`supersede_fragment(old_key, …)`。于是今天就能把用户的常驻条目静默封边 ——
而「常驻层只由人与审核决定」是治理拍板，不是实现细节。

## 堵法不是「排除 core」，而是「转成提名 + 证据」

LLM 说「这条常驻与新事实冲突」是**最难得的一手信号**（管线自己找到了矛盾）。
排除掉等于把证据一起扔了。所以：目标带 core + 动作 ∈ {UPDATE, SUPERSEDE, VOID} ⇒
不封边，改打 `tier:demote-candidate` + `tier:evid:<新事实 key>`，进审核队列由人裁决。

## 本文件钉死的事

  1. **正向**（VOID / UPDATE × core 目标）：core **不被 supersede**、两个标签都打上、
     触发性新事实**已入库**（证据得有实体，否则审核页点开是空的）；
  2. **反向**（非 core 目标）：UPDATE 仍 `set_supersedes` + `supersede_fragment`、
     VOID 仍 `superseded_by='__void__'` 且**不**写新事实 —— 逐字不变；
  3. **幂等**：已在退居队列的冲突不再重复提名（只记日志）；
  4. **标签边界**：`core_candidate` / `xcore` / `memory` 一律**不**触发否决。

用 FakeStorage（与 `tests/test_v2_pipeline.py` 同款思路，本文件自带一份最小实现）：
这里验的是**分支与写序**，真后端的标签落库由 `tests/test_tiering_signal.py`（真 SQLite）管。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple

import pytest

from keepsake.core.pipeline import VOID_KEY, VOID_REASON, Fact, Pipeline
from keepsake.core.tiering import (
    CORE_TAG,
    DEMOTE_CANDIDATE_TAG,
    EVID_PREFIX,
)

CORE_KEY = "memory:frag:core_pin"      # 被撞的那条常驻（全文件钉这一个 key）


class _CoreVetoStorage:
    """Pipeline 用到的最小存储面（三个真实实现都有同名方法）。

    记调用而不是真写库：这里要断言的是「**有没有**调 supersede / 有没有改 tags」。
    """

    def __init__(self) -> None:
        self.fragments: Dict[str, Dict[str, Any]] = {}
        self.stored: List[Dict[str, Any]] = []
        self.supersede_calls: List[Tuple[str, str, str]] = []
        self.set_supersedes_calls: List[Tuple[str, str]] = []
        self.tag_updates: List[Tuple[str, str]] = []

    # --- 种子 -----------------------------------------------------------
    def seed(self, key: str, content: str, tags: str) -> None:
        self.fragments[key] = {"key": key, "content": content, "tags": tags}

    # --- Pipeline 期望的接口 -------------------------------------------
    def get_fragment(self, key: str) -> Optional[Dict[str, Any]]:
        row = self.fragments.get(key)
        return dict(row) if row else None

    def store(self, text: str, tags: str = "", category: str = "",
              source: str = "", fragment_type: str = "", **kwargs: Any) -> bool:
        key = self.key_for(text)
        row = {"key": key, "content": text, "tags": tags, "category": category,
               "source": source, "fragment_type": fragment_type}
        self.stored.append(row)
        self.fragments[key] = row
        return True

    def search_bm25(self, query: str, tag_filter: str = "", **kwargs: Any) -> List[Dict[str, Any]]:
        return [{"_key": k, "content": r["content"], "tags": r["tags"],
                 "category": "fact_v2"} for k, r in self.fragments.items()]

    def supersede_fragment(self, old_key: str, new_key: str, reason: str = "") -> bool:
        self.supersede_calls.append((old_key, new_key, reason))
        row = self.fragments.get(old_key)
        if row is not None:
            row["superseded_by"] = new_key
        return True

    def set_supersedes(self, new_key: str, old_key: str) -> bool:
        self.set_supersedes_calls.append((new_key, old_key))
        return True

    def fragment_exists(self, key: str) -> Optional[bool]:
        return key in self.fragments

    def update_fragment_fields(self, key: str, fields: Dict[str, Any]) -> bool:
        row = self.fragments.get(key)
        if row is None:
            return False
        row.update(fields)
        self.tag_updates.append((key, str(fields.get("tags", ""))))
        return True

    # --- 小工具 ---------------------------------------------------------
    @staticmethod
    def key_for(text: str) -> str:
        return f"memory:frag:{hashlib.sha256(text.encode()).hexdigest()[:12]}"

    def tags_of(self, key: str) -> List[str]:
        row = self.fragments.get(key) or {}
        return [t.strip() for t in str(row.get("tags", "")).split(",") if t.strip()]


def _pipeline(storage: _CoreVetoStorage) -> Pipeline:
    """只造一个 Pipeline 实例：**直接调 `_do_update` / `_do_void`**（不经
    `_process_window`）—— 本单验的是**执行侧的分流**，更新相的 action 解析是既有
    行为，由 `tests/test_v2_pipeline.py` 管。llm_fn 永远不会被调到，给个空实现。
    """
    return Pipeline(storage=storage, llm_fn=lambda messages, model: '{"facts":[]}')


# ---------------------------------------------------------------------------
# 一、正向：撞上 core ⇒ 不封边，转提名 + 证据
# ---------------------------------------------------------------------------

def test_VOID打core_不封边且提名带证据():
    st = _CoreVetoStorage()
    st.seed(CORE_KEY, "用户固定用 A 方案", f"{CORE_TAG},memory")
    p = _pipeline(st)

    p._do_void(Fact("用户改用 B 方案了", "state_change"), CORE_KEY)   # noqa: SLF001

    # 漏洞本体：原来这里会 supersede_fragment(core, __void__)，常驻条目被静默封边
    assert st.supersede_calls == [], "常驻条目绝不许被管线封边"
    assert "superseded_by" not in (st.fragments[CORE_KEY] or {})
    tags = st.tags_of(CORE_KEY)
    assert DEMOTE_CANDIDATE_TAG in tags, "冲突要进退居建议队列，由人裁决"
    assert f"{EVID_PREFIX}:{st.key_for('用户改用 B 方案了')}" in tags
    # 证据要有实体：VOID 原语义不写库，这条路径得补一次落库
    assert any("改用 B 方案" in r["content"] for r in st.stored), "证据条目必须已入库"


def test_UPDATE打core_不封边且提名带证据():
    st = _CoreVetoStorage()
    st.seed(CORE_KEY, "分页还没做", f"{CORE_TAG},memory")
    p = _pipeline(st)

    p._do_update(Fact("分页已经上线了", "state_change"), CORE_KEY)      # noqa: SLF001

    assert st.supersede_calls == [], "常驻条目绝不许被管线封边"
    assert st.set_supersedes_calls == [], "封边链路整条不许启动"
    tags = st.tags_of(CORE_KEY)
    assert DEMOTE_CANDIDATE_TAG in tags
    evid = [t for t in tags if t.startswith(EVID_PREFIX + ":")]
    assert len(evid) == 1 and evid[0][len(EVID_PREFIX) + 1:].startswith("memory:frag:")
    # UPDATE 路径本来就要先 store 新事实（证据就是它）—— 逐字保留
    assert any("分页已经上线" in r["content"] for r in st.stored)


def test_否决走的是标签原语不是封边原语():
    """守卫在**分流处**，存储层原语零改动：写标签只经 update_fragment_fields。"""
    st = _CoreVetoStorage()
    st.seed(CORE_KEY, "老常驻", CORE_TAG)
    p = _pipeline(st)

    p._do_void(Fact("新事实", "fact"), CORE_KEY)                      # noqa: SLF001

    assert [key for key, _ in st.tag_updates] == [CORE_KEY]
    assert st.supersede_calls == []


# ---------------------------------------------------------------------------
# 二、反向：非 core 目标逐字不变（回归双向，红线）
# ---------------------------------------------------------------------------

def test_UPDATE打非core_仍走封边链路():
    st = _CoreVetoStorage()
    st.seed("memory:frag:plain_old", "用户要求做分页", "kind:pending,conversation")
    p = _pipeline(st)

    p._do_update(Fact("分页已完成上线", "state_change"), "memory:frag:plain_old")   # noqa: SLF001

    new_key = st.key_for("分页已完成上线")
    assert st.set_supersedes_calls == [(new_key, "memory:frag:plain_old")]
    assert st.supersede_calls == [("memory:frag:plain_old", new_key, "")]
    assert st.tag_updates == [], "非 core 目标一个字都不许多打"
    assert DEMOTE_CANDIDATE_TAG not in st.tags_of("memory:frag:plain_old")


def test_VOID打非core_仍逻辑删且不写新事实():
    st = _CoreVetoStorage()
    st.seed("memory:frag:plain_old", "用户要求做分页", "kind:pending,conversation")
    p = _pipeline(st)

    p._do_void(Fact("那条作废了", "fact"), "memory:frag:plain_old")  # noqa: SLF001

    assert st.supersede_calls == [("memory:frag:plain_old", VOID_KEY, VOID_REASON)]
    assert st.stored == [], "非 core 的 VOID 语义不变：不写新事实"
    assert st.tag_updates == []


def test_ADD路径不受影响():
    st = _CoreVetoStorage()
    p = _pipeline(st)

    p._do_add(Fact("一条全新事实", "fact"))                             # noqa: SLF001

    assert len(st.stored) == 1
    assert st.supersede_calls == [] and st.tag_updates == []


@pytest.mark.parametrize("tags", [
    "core_candidate,memory",      # 同前缀不同标签
    "xcore,memory",               # 后缀相接
    "memory,agent:worker",        # 子代理域，不带 core
    "memory",                     # 压根不是常驻
])
def test_非core标签不触发否决_VOID(tags: str):
    """逗号边界完整标签口径：`core` 之外的任何一个都不许把封边拦下来。"""
    st = _CoreVetoStorage()
    key = "memory:frag:lookalike"
    st.seed(key, "看着像常驻", tags)
    p = _pipeline(st)

    p._do_void(Fact("新事实", "fact"), key)                          # noqa: SLF001

    assert st.supersede_calls == [(key, VOID_KEY, VOID_REASON)], f"tags={tags} 不是 core，封边不该被拦"
    assert st.tag_updates == [], f"tags={tags} 不是 core，不该进退居队列"


@pytest.mark.parametrize("tags", [
    "core_candidate,memory", "xcore,memory", "memory,agent:worker", "memory",
])
def test_非core标签不触发否决_UPDATE照常封边(tags: str):
    """UPDATE 那条路（唯一今天真的在生效的封边链路）同样不被拦。"""
    st = _CoreVetoStorage()
    key = "memory:frag:lookalike"
    st.seed(key, "看着像常驻", tags)
    p = _pipeline(st)

    p._do_update(Fact("新事实正文", "fact"), key)                      # noqa: SLF001

    assert st.supersede_calls == [(key, st.key_for("新事实正文"), "")]
    assert st.tag_updates == []

# ---------------------------------------------------------------------------
# 三、幂等 + 日志
# ---------------------------------------------------------------------------

def test_已在退居队列的冲突不重复提名(caplog: pytest.LogCaptureFixture):
    st = _CoreVetoStorage()
    st.seed(CORE_KEY, "老常驻", f"{CORE_TAG},{DEMOTE_CANDIDATE_TAG}")
    p = _pipeline(st)

    with caplog.at_level(logging.INFO, logger="keepsake.pipeline"):
        p._do_void(Fact("又一条冲突事实", "fact"), CORE_KEY)          # noqa: SLF001

    assert st.tag_updates == [], "已在队列就不该再打一遍（幂等）"
    assert st.supersede_calls == []
    assert any("已在退居队列" in r.getMessage() for r in caplog.records)


def test_提名成功要留info日志(caplog: pytest.LogCaptureFixture):
    st = _CoreVetoStorage()
    st.seed(CORE_KEY, "老常驻", CORE_TAG)
    p = _pipeline(st)

    with caplog.at_level(logging.INFO, logger="keepsake.pipeline"):
        p._do_void(Fact("冲突事实正文", "fact"), CORE_KEY)            # noqa: SLF001

    msgs = [r.getMessage() for r in caplog.records]
    assert any(CORE_KEY in m and "转退居提名" in m and "VOID" in m for m in msgs), msgs


def test_存储读不到tags时按非core放行(caplog: pytest.LogCaptureFixture):
    """治理闸不许变成故障放大器：get_fragment 抛异常 ⇒ 放行 + 留痕。"""
    st = _CoreVetoStorage()
    st.seed("memory:frag:plain", "普通条目", "memory")

    def boom(key: str):
        raise RuntimeError("backend down")

    st.get_fragment = boom               # type: ignore[method-assign]
    p = _pipeline(st)

    with caplog.at_level(logging.WARNING, logger="keepsake.pipeline"):
        p._do_void(Fact("新事实", "fact"), "memory:frag:plain")      # noqa: SLF001

    assert st.supersede_calls == [("memory:frag:plain", VOID_KEY, VOID_REASON)], "读失败不许把管线整体瘫掉"
    assert any("按非 core 放行" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 四、单点判据（与 G5 同款，但作为可跑的检查钉在这）
# ---------------------------------------------------------------------------

def test_管线不复制退居标签字面量():
    """`tier:demote-candidate` / `tier:evid` 的字面量只许住在 tiering 一处。"""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src/keepsake/core/pipeline.py"
    text = src.read_text(encoding="utf-8")
    assert "DEMOTE_CANDIDATE_TAG" in text, "管线必须 import tiering 的常量"
    assert "tier:demote-candidate" not in text, "字面量不许复制到 pipeline"
    assert f'"{EVID_PREFIX}"' not in text, "证据前缀字面量也不许复制到 pipeline"


def test_依赖方向是pipeline到tiering():
    """反向 import（tiering → pipeline）会成环；钉住单向。"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src/keepsake/core"
    tiering = (root / "tiering.py").read_text(encoding="utf-8")
    assert "from .pipeline import" not in tiering, "tiering 不许反向依赖 pipeline（会成环）"
    pipeline = (root / "pipeline.py").read_text(encoding="utf-8")
    assert "from .tiering import" in pipeline, "管线必须从 tiering 单点取标签口径"
