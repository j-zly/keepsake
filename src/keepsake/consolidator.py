"""Consolidation 引擎 — 将同主题碎片分层提炼为更高层记忆。

工作流程:
  1. 扫描所有未合并的碎片（fragment_type != "consolidated"）
  2. 用 jieba 关键词提取做主题分组
  3. 每组超过 min_group_size 条时，调 LLM 合并为一条高层次摘要
  4. 存为新碎片（fragment_type="consolidated", level=N+1）
  5. 删除原始碎片（或标记已合并）

配置参数:
  - min_group_size: 最少多少条碎片才触发合并（默认 3）
  - max_age_hours: 只合并超过此年龄的碎片（给新碎片时间积累，默认 72h）
  - llm_model: LLM 模型名（2026-09 起不再硬编码默认值——须由 config.json 的 llm 节提供）

2026-09 重大变更（任务 ks_noqwen）：
  * 移除硬编码 base_url / 默认 model 兜底 —— 无 llm 节 = 无 LLM 通道
    = 「unconfigured」返回，调用方按既有 v1 兜底路径走，绝不悄悄用付费模型
  * 移除 _get_api_key() 的多 provider env 兜底链 —— 仅保留 OPENAI_API_KEY 通用项
  * resolve_llm_channel 增加 mtime 感知缓存 → 改 config.json 后下一处理窗口生效
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _resolve_request_extra(llm_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """从 llm 节解析 request_extra —— 配错类型降级为 {}。

    设计点：
      * 仅 dict 类型被接受；非 dict（字符串/列表/int 等配错）→ 视为空 dict
      * 日志含 cfg host 而非 key 值，绝不打 raw payload 内容
      * 调用方按既有 schema 拿空 dict 不会 KeyError
    """
    raw = llm_cfg.get("request_extra") if llm_cfg else None
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    logger.warning(
        "resolve_llm_channel: llm.request_extra 非 dict 类型（%s）— 视为空 dict",
        type(raw).__name__,
    )
    return {}


# 默认参数
DEFAULT_MIN_GROUP_SIZE = 2  # 有重复内容就合
DEFAULT_MAX_AGE_HOURS = 72
DEFAULT_BATCH_SIZE = 200  # 每次 consolidate 扫描的碎片数

# LLM 超时
LLM_TIMEOUT = 30

# 合并提示词
CONSOLIDATE_PROMPT = """你是一位知识提炼专家。以下是一组关于同一话题的对话片段。

请将它们合并成一条简洁、信息完整的高层记忆条目，要求：
1. 保留所有关键事实和结论，不丢信息
2. 去掉重复内容
3. 用陈述句表达，像一条知识条目
4. 如果片段之间存在矛盾，指出矛盾但不选边
5. 控制在 200 字以内

对话片段：
{segments}

合并后的知识条目："""


def _get_api_key() -> str:
    """获取 API key（2026-09 仅保留 OPENAI_API_KEY 通用 env）。

    设计点：
      * 仅 OPENAI_API_KEY —— 它是 Hermes 通用 OpenAI 兼容 key 的事实标准
      * 配置唯一源是 config.json 的 llm 节；本函数仅在 env 显式给 key 时才返回非空
      * key_file 路径由 resolve_llm_channel 直接读取，不由本函数介入
    """
    return os.environ.get("OPENAI_API_KEY", "")


def resolve_llm_channel(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 LLM 通道配置 —— base_url / model / api_key。

    优先级（高→低）:
      1. `cfg["llm"]` 节存在且 base_url/model/api_key（或 key_file）齐全 → 完整通道
      2. `cfg["llm"]` 节缺失/字段缺失 → 视为无有效 LLM 通道，返回 source="unconfigured"
      3. key_file 读失败 → api_key="" 但其它字段保留；source 反映读文件失败
      4. 配置文件中途损坏（非法 JSON） → 本窗按 unconfigured 处理，不抛穿

    返回 dict 字段: base_url, model, api_key, source, key_file, valid, request_extra。
    日志安全：logger 只允许出现 key_file 路径 / 端点 host，绝不打印 key 内容。

    request_extra（r3 起新增）:
      * 取自 llm_cfg["request_extra"]；厂商特异字段（如 thinking 开关）走该 dict 注入
      * 非 dict 类型 → 视为 {} 并 logger.warning（保持 schema 稳定防 KeyError）
      * unconfigured 各早退分支也带 request_extra={} —— schema 一致，调用方安全

    热生效（2026-09 起）：
      * 缓存按 (config_path 的 mtime_ns, size) 命中；变了才重读重解析
      * 调一次 = O(stat)，无 IO 放大；pipeline 每次 _drain_now 开头调用即可窗口级生效
      * 缓存清理：invalidate_channel_cache() 给测试 / 强刷场景用
    """
    llm_cfg: Dict[str, Any] = {}
    if cfg and isinstance(cfg, dict):
        llm_cfg = cfg.get("llm") or {}
        if not isinstance(llm_cfg, dict):
            llm_cfg = {}

    # 0. 缺节 → 无有效 LLM 通道（2026-09 起移除付费模型硬编码兜底）
    if not llm_cfg:
        return {
            "base_url": "",
            "model": "",
            "api_key": "",
            "source": "unconfigured",
            "key_file": "",
            "valid": False,
            "request_extra": _resolve_request_extra(llm_cfg),
        }

    # 1. base_url —— 必填；缺则视为无有效通道
    base_url_raw = llm_cfg.get("base_url") or ""
    base_url = base_url_raw.rstrip("/")
    if not base_url:
        return {
            "base_url": "",
            "model": (llm_cfg.get("model") or "").strip(),
            "api_key": "",
            "source": "unconfigured",
            "key_file": llm_cfg.get("key_file") or "",
            "valid": False,
            "request_extra": _resolve_request_extra(llm_cfg),
        }

    # 2. model —— 缺 model = 该通道无效
    model = (llm_cfg.get("model") or "").strip()
    if not model:
        return {
            "base_url": base_url,
            "model": "",
            "api_key": "",
            "source": "unconfigured",
            "key_file": llm_cfg.get("key_file") or "",
            "valid": False,
            "request_extra": _resolve_request_extra(llm_cfg),
        }

    # 3. api_key：api_key 直填 > key_file 读取 > OPENAI_API_KEY env 兜底
    api_key = (llm_cfg.get("api_key") or "").strip()
    key_file_path = llm_cfg.get("key_file") or ""
    key_file_failed = False
    if not api_key and key_file_path:
        try:
            with open(key_file_path) as f:
                api_key = f.read().strip()
            # 仅打印路径（host 等价信息），绝不打印 key 内容
            logger.debug(
                "resolve_llm_channel: loaded key_file=%s (len=%d)",
                key_file_path, len(api_key),
            )
        except (OSError, IOError) as e:
            # 读文件失败 → 视同无 key，不抛
            logger.debug(
                "resolve_llm_channel: key_file=%s unreadable: %s — no key",
                key_file_path, e,
            )
            api_key = ""
            key_file_failed = True
    if not api_key:
        # 最后兜底：OPENAI_API_KEY env（2026-09 起仅此一项）
        api_key = _get_api_key()

    # 4. source 仅用于日志/监控归类（不影响行为）
    source = "configured"
    if "bigmodel.cn" in base_url:
        source = "bigmodel"
    elif "openai.com" in base_url:
        source = "openai"

    return {
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "source": source,
        "key_file": key_file_path,
        "valid": bool(api_key) and not key_file_failed,
        "request_extra": _resolve_request_extra(llm_cfg),
    }


# ---------------------------------------------------------------------------
# 热生效缓存（2026-09）
# ---------------------------------------------------------------------------
# 路径 → (mtime_ns, size, cached_dict)
# 当 config.json 被改写（v2 pipeline 下一处理窗口开始时）→ stat 变了就重读
_channel_cache: Dict[str, Tuple[int, int, Dict[str, Any]]] = {}


def _config_path() -> str:
    """KEEPSAKE_CONFIG 环境变量优先，其次默认 ~/.config/keepsake/config.json。"""
    return os.environ.get("KEEPSAKE_CONFIG") or "~/.config/keepsake/config.json"


def _stat_fingerprint(path: str) -> Optional[Tuple[int, int]]:
    """拿 (mtime_ns, size)；文件不存在/不可读 → None（视同无效）。"""
    try:
        st = os.stat(path)
    except (OSError, FileNotFoundError):
        return None
    # st_mtime_ns 在 py3.7+ 可用
    return (getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), st.st_size)


def resolve_llm_channel_cached(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """resolve_llm_channel 的 mtime 缓存版。

    调用约定：pipeline._drain_now 开头调用一次；其它路径保持走
    resolve_llm_channel 直接解析（行为不变）。

    缓存粒度：按 KEEPSAKE_CONFIG 路径（或默认值）做 (mtime_ns, size) 比对。
    缓存命中 → 直接返回旧 dict；未命中 → 重读文件 + 重解析 + 写入缓存。

    文件读取失败（OSError / 损坏 JSON） → 不抛，本窗返回 unconfigured。
    """
    path = os.path.expanduser(_config_path())
    fp = _stat_fingerprint(path)

    # 文件不存在 / 不可读 → 视同 unconfigured（不抛穿 daemon）
    if fp is None:
        return {
            "base_url": "",
            "model": "",
            "api_key": "",
            "source": "unconfigured",
            "key_file": "",
            "valid": False,
        }

    cached = _channel_cache.get(path)
    if cached is not None and cached[0] == fp[0] and cached[1] == fp[1]:
        return cached[2]

    # 缓存 miss → 重读文件 + 重解析
    try:
        with open(path) as f:
            raw = f.read()
        on_disk_cfg = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError) as e:
        # 配置文件中途损坏 → 本窗按 unconfigured → 调用方按既有 v1 兜底走
        logger.warning(
            "resolve_llm_channel_cached: config %s unreadable/JSON-broken: %s — window uses unconfigured",
            path, e,
        )
        result: Dict[str, Any] = {
            "base_url": "",
            "model": "",
            "api_key": "",
            "source": "unconfigured",
            "key_file": "",
            "valid": False,
        }
        _channel_cache[path] = (fp[0], fp[1], result)
        return result

    # cfg 参数若显式传入则覆盖磁盘（兼容测试 inline 场景）；默认用磁盘值
    effective_cfg = cfg if cfg is not None else on_disk_cfg
    result = resolve_llm_channel(effective_cfg)
    _channel_cache[path] = (fp[0], fp[1], result)
    return result


def invalidate_channel_cache(path: Optional[str] = None) -> None:
    """清空缓存（测试 / 强刷场景）。path=None → 清全部。"""
    global _channel_cache
    if path is None:
        _channel_cache = {}
    else:
        _channel_cache.pop(path, None)


def _call_llm(messages: List[Dict[str, str]], model: str = "",
              *, channel: Optional[Dict[str, Any]] = None,
              max_retries: int = 2) -> Optional[str]:
    """调用 chat API 获取 LLM 回复。带重试。

    channel: 由 resolve_llm_channel 解析出的通道字典（含 base_url/model/api_key/request_extra）；
             None → 仅查 OPENAI_API_KEY env（无任何付费模型硬编码兜底）。
    channel['model'] 优先于入参 model；二者都缺 → 返回 None（不静默用付费模型）。
    channel['request_extra']（r3 起消费）: 非空 dict → 合并进请求 body，厂商特异字段
        （如 {"thinking": {"type": "disabled"}}）走配置注入；缺/非 dict → 不合并，
        body 保持 OpenAI 兼容基线。
    """
    if channel is None:
        # 旧调用方兜底：仅查 OPENAI_API_KEY
        # base_url/model 仍要求调用方提供——保留 compat 仅给 env-only 测试场景
        channel = {
            "base_url": "",
            "model": "",
            "api_key": _get_api_key(),
            "source": "env_only",
            "key_file": "",
            "valid": False,
        }
        if not channel["api_key"]:
            logger.warning(
                "consolidator: _call_llm called with channel=None and no OPENAI_API_KEY env",
            )
            return None

    api_key = channel.get("api_key", "")
    if not api_key:
        logger.warning(
            "consolidator: no API key for LLM calls (source=%s)",
            channel.get("source", "?"),
        )
        return None

    base_url = channel["base_url"]
    # 2026-09：移除硬编码 model 兜底；channel/model 都缺 → 直接返回 None
    # （不静默用付费模型）—— 调用方按既有 v1 兜底路径走
    actual_model = channel.get("model") or model or ""
    if not actual_model:
        logger.warning(
            "consolidator: no model configured (source=%s); refusing to use paid fallback",
            channel.get("source", "?"),
        )
        return None
    url = f"{base_url}/chat/completions"
    # r3：构造 body 后合并 channel.request_extra（厂商特异字段走 config 注入，
    # 与 cron/memory_distill.build_chat_request 语义一致 —— extra 可覆盖默认参数）。
    body_dict = {
        "model": actual_model,
        "messages": messages,
        "max_tokens": 512,
        "temperature": 0.3,
    }
    request_extra = channel.get("request_extra")
    if isinstance(request_extra, dict) and request_extra:
        body_dict.update(request_extra)
    payload = json.dumps(body_dict).encode("utf-8")

    for attempt in range(1 + max_retries):
        if attempt > 0:
            wait = 2.0 * (2 ** (attempt - 1))  # 2s, 4s
            logger.debug("consolidator: retry %d/%d after %.0fs", attempt, max_retries, wait)
            time.sleep(wait)

        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
                data = json.loads(resp.read())
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "").strip()
            # Got response but no choices — don't retry
            logger.warning("consolidator: LLM returned no choices (attempt %d)", attempt + 1)
            return None
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            if attempt < max_retries:
                logger.debug("consolidator: LLM attempt %d failed: %s", attempt + 1, e)
            else:
                logger.warning("consolidator: LLM call failed after %d attempts: %s",
                               attempt + 1, e)
    return None


class Consolidator:
    """碎片分层提炼引擎。"""

    def __init__(
        self,
        storage: Any,  # RedisStorage instance (avoid circular import)
        min_group_size: int = DEFAULT_MIN_GROUP_SIZE,
        max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
        llm_model: str = "",  # 2026-09 移除硬编码模型兜底；须由 channel 解析提供
        batch_size: int = DEFAULT_BATCH_SIZE,
        channel: Optional[Dict[str, Any]] = None,
    ):
        self._storage = storage
        self._min_group_size = min_group_size
        self._max_age_hours = max_age_hours
        self._llm_model = llm_model
        self._batch_size = batch_size
        # channel：可选 resolve_llm_channel 输出；None → _call_llm 内部兜底
        self._channel = channel

    def consolidate(self) -> Dict[str, Any]:
        """执行一轮碎片合并。返回操作统计。"""
        client = self._storage._get_client()
        if not client:
            return {"status": "error", "reason": "Redis not available"}

        stats = {"scanned": 0, "groups_found": 0, "merged": 0, "skipped": 0, "errors": 0}

        # 1. 扫描未合并的碎片
        fragments = self._scan_unconsolidated(client)
        stats["scanned"] = len(fragments)
        if not fragments:
            return stats

        # 2. 按主题聚类
        groups = self._cluster_by_topic(fragments)
        stats["groups_found"] = len(groups)

        # 3. 对每个符合条件的组执行合并
        for group in groups:
            if len(group) < self._min_group_size:
                stats["skipped"] += len(group)
                continue

            result = self._merge_group(client, group)
            if result:
                stats["merged"] += len(group)
            else:
                stats["errors"] += len(group)

        return stats

    def _scan_unconsolidated(self, client) -> List[Dict[str, Any]]:
        """扫描符合合并条件的碎片。

        条件:
          - fragment_type != "consumed"（未被更高层合并吞掉的）
          - 创建时间 > max_age_hours（给新碎片时间积累）
          - 已合并的（consolidated）也参与扫描，实现多级提炼
        """
        try:
            cutoff = (datetime.now(timezone.utc).timestamp() - self._max_age_hours * 3600)
            cursor = 0
            fragments = []

            while True:
                cursor, keys = client.scan(
                    cursor=cursor,
                    match="memory:frag:*",
                    count=self._batch_size,
                )

                if not keys:
                    if cursor == 0:
                        break
                    continue

                # 用 pipeline 批量 HMGETALL，减少网络往返
                pipe = client.pipeline()
                for key_b in keys:
                    pipe.hgetall(key_b)
                pipe_results = pipe.execute()

                for key_b, data in zip(keys, pipe_results):
                    key = key_b.decode("utf-8") if isinstance(key_b, bytes) else key_b
                    if not data:
                        continue

                    # 解码
                    doc = {}
                    for k_b, v_b in data.items():
                        k = k_b.decode("utf-8") if isinstance(k_b, bytes) else k_b
                        v = v_b.decode("utf-8") if isinstance(v_b, bytes) else v_b
                        doc[k] = v

                    # 跳过已被更高层合并吞掉的
                    if doc.get("fragment_type", "") == "consumed":
                        continue

                    # 检查年龄
                    created_str = doc.get("created", "")
                    if created_str:
                        try:
                            created_ts = datetime.fromisoformat(created_str).timestamp()
                            if created_ts > cutoff:
                                continue  # 太新，等下次
                        except (ValueError, TypeError):
                            pass

                    doc["_key"] = key
                    fragments.append(doc)

                if cursor == 0:
                    break

            return fragments

        except Exception as e:
            logger.warning("consolidator: scan error: %s", e)
            return []

    def _cluster_by_topic(self, fragments: List[Dict]) -> List[List[Dict]]:
        """按关键词重叠做简单聚类。

        策略:
          - 对每个碎片提取关键词（用 jieba）
          - 关键词重叠 >= 2 的归为一组
          - 贪心算法，不追求最优聚类
        """
        from .splitter import extract_keywords

        # 提取每个碎片的关键词
        frag_data = []
        for f in fragments:
            content = f.get("content", "")
            if not content:
                continue
            kws = set(extract_keywords(content, max_keywords=5))
            frag_data.append({"frag": f, "keywords": kws})

        if not frag_data:
            return []

        # 贪心聚类
        groups: List[List[Dict]] = []
        assigned = set()

        for i, data in enumerate(frag_data):
            if i in assigned:
                continue
            group = [data["frag"]]
            assigned.add(i)

            for j, other in enumerate(frag_data):
                if j in assigned:
                    continue
                # 重叠 >= 2 个关键词
                overlap = len(data["keywords"] & other["keywords"])
                if overlap >= 2:
                    group.append(other["frag"])
                    assigned.add(j)

            groups.append(group)

        return groups

    def _merge_group(self, client, group: List[Dict]) -> bool:
        """用 LLM 合并一组碎片。"""
        # 计算新层级：取组内最高 level + 1
        max_level = 1
        for f in group:
            try:
                lv = int(f.get("level", "1"))
                if lv > max_level:
                    max_level = lv
            except (ValueError, TypeError):
                pass
        new_level = max_level + 1

        # 判断是否已有合并过的碎片
        has_consolidated = any(
            f.get("fragment_type") == "consolidated" or int(f.get("level", "1")) > 1
            for f in group
        )

        # 准备片段文本
        segments = []
        tags_set = set()
        for f in group:
            content = f.get("content", "")
            if content:
                segments.append(f"• {content[:300]}")
            tags = f.get("tags", "")
            if tags:
                for t in tags.split(","):
                    t = t.strip()
                    if t and not t.startswith("session:"):
                        tags_set.add(t)

        if len(segments) < self._min_group_size:
            return False

        # 根据是否已有提炼过的内容选择不同 prompt
        if has_consolidated:
            prompt = (
                "以下是一组已经提炼过的记忆条目和相关的原始对话片段。"
                "请将它们进一步提炼合并成一条更精炼的高层知识条目。\n\n"
                + "\n".join(segments)
                + "\n\n提炼后的高层知识条目："
            )
        else:
            prompt = CONSOLIDATE_PROMPT.format(segments="\n".join(segments))
        result = _call_llm([
            {"role": "system", "content": "你是一位知识提炼专家，擅长从对话中提取核心信息。"},
            {"role": "user", "content": prompt},
        ], model=self._llm_model, channel=self._channel)

        if not result:
            return False

        # 分析情绪
        from .splitter import analyze_sentiment
        sent_score, sent_label = analyze_sentiment(result)
        now_str = datetime.now(timezone.utc).isoformat()

        mapping = {
            "content": result,
            "tags": ",".join(sorted(tags_set)) if tags_set else "",
            "category": "consolidated",
            "source": "consolidator",
            "created": now_str,
            "fragment_type": "consolidated",
            "level": str(new_level),  # 多级：原始=1，首次合并=2，二次合并=3...
            "sentiment_score": str(sent_score),
            "sentiment_label": sent_label,
            "feedback_score": "0",
        }

        # 存 consolidated 碎片
        import hashlib
        content_hash = hashlib.sha256(result.encode()).hexdigest()[:12]
        consolidated_key = f"memory:frag:{content_hash}"

        # 如果没有相同 key（去重检查），就存
        existing = client.exists(consolidated_key)
        if existing:
            logger.debug("consolidator: duplicate consolidated result, skipping")
        else:
            client.hset(consolidated_key, mapping=mapping)

        # 软删除原始碎片（标记为已消费，不硬删）
        from datetime import datetime as _dt
        now_iso = _dt.now(timezone.utc).isoformat()
        consumed_count = 0
        for f in group:
            key = f.get("_key")
            if key:
                try:
                    client.hset(key, "consumed_by", consolidated_key)
                    client.hset(key, "consumed_at", now_iso)
                    client.hset(key, "fragment_type", "consumed")
                    consumed_count += 1
                except Exception:
                    pass

        logger.info(
            "consolidator: merged %d fragments → '%s...' (marked %d as consumed)",
            len(group), result[:60], consumed_count,
        )
        return True
