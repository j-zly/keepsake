"""记忆提炼班车（in-process）：扫最近会话 → LLM 提炼 → 写 Keepsake。

收编自宿主 cron 的 `memory_distill_cron.sh` → `memory_distill.py`（仓外
`/root/.hermes/scripts/`）：**逻辑平移，路径参数化** —— 会话库 / watermark /
开关文件都**不写死 `/root/.hermes`**（默认按 `~` 展开，root 账号下与原路径
等价），且可由 `run()` 的参数逐项覆盖。

依赖一律走 keepsake 既有 core（不自己搓 HTTP / 存储）：

* LLM 通道：``keepsake.core.ops.consolidator.resolve_llm_channel_cached``
  （读 config.json 的 llm 节，mtime 缓存热生效；未配置 ⇒ 本轮跳过）
* LLM 调用：``keepsake.core.llm_http.chat_completion``
  （提炼 / 合并 / 检索同一条通道，协议分叉在 core 里）
* 存储：``keepsake.core.factory.storage_from_config``
  （**含 embedder** —— 漏传它写进去的记忆没有向量，KNN 永远搜不到）

窗口语义与原脚本一致：LLM 通道没配 ⇒ 跳过且**不推 watermark**；没有新对话 ⇒
跳过；**没提炼出东西 ⇒ 也不推 watermark**（下一轮接着看同一段对话）。

班车唯一入口是 ``run()``；**禁止模块级副作用**（import 只定义函数，
不连库、不发请求、不起子进程）。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("keepsake.bus.distill")

__all__ = ["run"]

#: 开关文件（{"enabled": true, "hours": 2, "max_chars": 4000}）——
#: 原脚本读 ~/scripts/memory_distill.conf（root 账号下即 /root/scripts/…）。
DEFAULT_CONF_FILE = "~/scripts/memory_distill.conf"

#: 会话库（宿主 Hermes 的 state.db）。原脚本写死 `/root/.hermes/state.db`，
#: 这里按 `~` 展开 —— **不写死 /root**（换账号 / 换机器不用改代码）。
DEFAULT_STATE_DB = "~/.hermes/state.db"

#: 增量水位（提炼到哪条消息了）。**沿用原路径**（/tmp）：换了路径等于把
#: watermark 归零 ⇒ 同一段对话被提炼两遍。
DEFAULT_WATERMARK_FILE = "/tmp/memory_distill_watermark"

#: 提炼提示词 —— 与仓内手跑入口 `cron/memory_distill.py` **逐字一致**
#:（换词 = 换产物，别在这里"优化"）。
DISTILL_PROMPT = """你是记忆提炼助手。从下面的对话中提炼「值得长期记住」的信息，输出 JSON 数组。
只提炼：用户偏好/习惯、项目事实、环境配置、技术决策、踩坑教训、用户身份信息。
忽略：寒暄、临时任务进度、纯工具输出、重复内容。
每条: {"content": "一句话记忆内容(中文)", "category": "preference|fact|lesson|project|identity", "tags": "逗号分隔关键词"}
要求:
- content 具体明确，不写模糊的废话
- 最多 8 条，宁缺毋滥
- 只输出 JSON 数组，不要任何解释

对话:
{conversation}"""


# --------------------------------------------------------------------------
# 小工具（纯函数）
# --------------------------------------------------------------------------

def _expand(path: str) -> Path:
    """`~` 展开（路径常量参数化：默认按家目录走，不写死 /root）。"""
    return Path(os.path.expanduser(str(path)))


def load_conf(path: str = DEFAULT_CONF_FILE) -> Dict[str, Any]:
    """读开关配置（读不到 / 坏了 ⇒ 空 dict ⇒ 按缺省跑）。

    与原脚本同款：读不到不是错（缺省 enabled=true、hours=2、max_chars=4000）。
    """
    try:
        raw = json.loads(_expand(path).read_text(encoding="utf-8"))
    except Exception:      # noqa: BLE001 —— 开关文件读不到一律按缺省跑
        return {}
    return raw if isinstance(raw, dict) else {}


def read_watermark(path: str = DEFAULT_WATERMARK_FILE) -> int:
    """读增量水位（读不到 ⇒ 0 = 从头看）。"""
    try:
        return int(_expand(path).read_text(encoding="utf-8").strip())
    except Exception:      # noqa: BLE001
        return 0


def write_watermark(mid: int, path: str = DEFAULT_WATERMARK_FILE) -> None:
    """写增量水位（写不进去只记日志 —— 水位丢了顶多重提炼一遍，不该炸班车）。"""
    try:
        p = _expand(path)
        if p.parent and str(p.parent):
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(mid), encoding="utf-8")
    except Exception as e:   # noqa: BLE001
        logger.warning("distill: watermark 写不进去 %s: %s", path, e)


# --------------------------------------------------------------------------
# 会话 → 提炼 → 入库（三步）
# --------------------------------------------------------------------------

def recent_messages(db_path: str, hours: int, last_id: int) -> Tuple[str, int]:
    """会话库里 watermark 之后的增量消息 → (对话文本, 新的 max_id)。

    与原脚本同款口径：只看 user / assistant 两种角色、正文 < 20 字丢弃、
    超长正文截到 3000 字、最多取 300 条（按 id 倒序取，再翻正序）。
    """
    conn = sqlite3.connect(f"file:{_expand(db_path)}?mode=ro", uri=True)
    try:
        ts = time.time() - hours * 3600
        rows = conn.execute(
            """SELECT id, role, content, timestamp FROM messages
               WHERE role IN ('user','assistant') AND content IS NOT NULL
               AND id > ? AND timestamp > ?
               ORDER BY id DESC LIMIT 300""", (last_id, ts)).fetchall()
    finally:
        conn.close()
    max_id = rows[0][0] if rows else last_id
    out: List[str] = []
    for _mid, role, content, _ts in reversed(rows):     # 按时间正序
        c = (content or "").strip()
        if not c or len(c) < 20:
            continue
        if len(c) > 3000:
            c = c[:3000]
        out.append(f"[{role}] {c}")
    return "\n".join(out), max_id


def distill(conversation: str, max_chars: int, channel: Dict[str, Any]) -> List[Dict[str, Any]]:
    """走 keepsake 的 LLM 通道提炼记忆 → 记忆条目列表。

    🔴 失败**照抛**（不再像原脚本那样吞成空列表）：班车契约是「异常由调度层
    捕获并落 ok=False」—— 提炼失败就该红，静默绿等于记忆悄悄不更新。
    """
    from keepsake.core.llm_http import chat_completion

    if len(conversation) > max_chars:
        conversation = conversation[-max_chars:]
    prompt = DISTILL_PROMPT.replace("{conversation}", conversation)
    llm_cfg = {
        # 传输层键 + body 参数：**只把该进 body 的交给 core**（protocol /
        # request_extra 等传输层键 core 自己认，不会漏进请求体）。
        "base_url": channel.get("base_url", ""),
        "model": channel.get("model", ""),
        "api_key": channel.get("api_key", ""),
        "protocol": channel.get("protocol", ""),
        "request_extra": channel.get("request_extra") or {},
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    text = chat_completion(llm_cfg, [{"role": "user", "content": prompt}], timeout=120)
    # 提取 JSON 数组（容错：可能有多余文字 / code fence）
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise RuntimeError(f"LLM 回包不是 JSON 数组（提炼失败，长度 {len(text)}）")
    items = json.loads(text[start:end + 1])
    out: List[Dict[str, Any]] = []
    for i in items if isinstance(items, list) else []:
        if not isinstance(i, dict):
            continue
        c = (i.get("content") or "").strip()
        if len(c) < 10:              # 太短 / 无实质内容
            continue
        i["content"] = c
        out.append(i)
    return out


def store_items(items: List[Dict[str, Any]], *, dry_run: bool = False) -> int:
    """写入 Keepsake → 写成功条数。

    标签口径与原脚本一致：``auto-distill,<category>`` + LLM 自带的标签；
    🔴 LLM 自造的**治理保留标签**（core / shared / agent:*）一律剥除
    （判据单点在 ``keepsake.core.shared.sanitize_reserved_tags``）——
    自动提炼无权声明常驻 / 共享 / 代理归属。
    """
    from keepsake.core.factory import load_config, storage_from_config
    from keepsake.core.shared import sanitize_reserved_tags

    storage = storage_from_config(config=load_config())
    saved = 0
    try:
        for it in items:
            cat = it.get("category", "fact")
            tags = f"auto-distill,{cat}"
            llm_tags, touched = sanitize_reserved_tags(it.get("tags", ""))
            for tag, _reason in touched:
                logger.info("distill: 剥除 LLM 自造保留标签: %r", tag)
            if llm_tags:
                tags += "," + llm_tags
            if dry_run:
                logger.info("distill: [DRY] [%s] %s", cat, it["content"])
                continue
            if storage.store(text=it["content"], tags=tags, category=cat,
                             source="auto-distill", fragment_type=cat):
                saved += 1
    finally:
        try:
            storage.close()
        except Exception:     # noqa: BLE001 —— 关不掉的私有资源不牵连班车结论
            pass
    return saved


# --------------------------------------------------------------------------
# 班车入口
# --------------------------------------------------------------------------

def run(*, hours: Optional[int] = None, max_chars: Optional[int] = None,
        dry_run: bool = False, conf_file: str = DEFAULT_CONF_FILE,
        db_path: str = DEFAULT_STATE_DB,
        watermark_file: str = DEFAULT_WATERMARK_FILE) -> Tuple[bool, str]:
    """跑一轮记忆提炼 → ``(ok, 人读摘要)``。

    **班车唯一入口**（调度器按 ``keepsake.buses.distill:run`` 调用它）。

    参数都是可选的**路径 / 窗口常量**（生产走缺省；测试与别的部署可覆盖）。
    """
    from keepsake.core.ops.consolidator import resolve_llm_channel_cached

    conf = load_conf(conf_file)
    if conf.get("enabled") is False:
        return True, "提炼班车已禁用（memory_distill.conf enabled=false），本轮跳过"

    h = int(hours or conf.get("hours", 2) or 2)
    mc = int(max_chars or conf.get("max_chars", 4000) or 4000)

    # 通道未配置 ⇒ 本轮跳过，不调 LLM、不推 watermark（与原脚本同口径）
    channel = resolve_llm_channel_cached()
    if not channel.get("valid"):
        return True, "LLM 通道未配置，本轮跳过（watermark 不推进）"

    last_id = read_watermark(watermark_file)
    conv, max_id = recent_messages(db_path, h, last_id)
    if not conv:
        return True, f"无新对话可提炼（watermark={last_id}）"

    items = distill(conv, mc, channel)
    if not items:
        # 一条都没提炼出来 ⇒ **不推 watermark**（下一轮接着看同一段对话）
        return True, f"本轮未提炼出记忆（watermark={last_id}）"

    saved = store_items(items, dry_run=dry_run)
    if not dry_run and max_id > last_id:
        write_watermark(max_id, watermark_file)
    return True, (f"提炼 {len(items)} 条，已写入 {saved} 条"
                  f"（watermark {last_id}→{max_id}）")
