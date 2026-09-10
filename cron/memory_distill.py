#!/usr/bin/env python3
"""自动记忆提炼 — 扫最近会话 → keepsake LLM 通道提炼 → 写入 Keepsake

通道来源：keepsake.consolidator.resolve_llm_channel_cached（读 config.json 的 llm 节，
mtime 缓存热生效）。通道未配置 (valid=False) → 本轮跳过，不调 LLM、不推 watermark，
exit 0（cron 不报红）。

用法: python3 memory_distill.py [--hours 2] [--max-chars 4000] [--dry-run]
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from urllib.parse import urlparse

from keepsake.consolidator import resolve_llm_channel_cached

DB = "/root/.hermes/state.db"
WATERMARK_FILE = "/tmp/memory_distill_watermark"
CONF_FILE = os.path.expanduser("~/scripts/memory_distill.conf")


def load_conf():
    """读开关配置: {"enabled": true, "hours": 2, "max_chars": 4000}"""
    try:
        with open(CONF_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


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


def get_recent_messages(hours, last_id=0):
    """从 state.db 读增量消息（watermark 之后）"""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    ts = time.time() - hours * 3600
    rows = conn.execute(
        """SELECT id, role, content, timestamp FROM messages
           WHERE role IN ('user','assistant') AND content IS NOT NULL
           AND id > ? AND timestamp > ?
           ORDER BY id DESC LIMIT 300""", (last_id, ts)).fetchall()
    conn.close()
    max_id = rows[0][0] if rows else last_id
    # 按时间正序
    rows.reverse()
    # 截断超长内容
    out = []
    for mid, role, content, ts in rows:
        c = content.strip()
        if not c or len(c) < 20:
            continue
        if len(c) > 3000:
            c = c[:3000]
        out.append(f"[{role}] {c}")
    return "\n".join(out), max_id


def build_chat_request(channel, prompt, extra_body=None):
    """构造 OpenAI 兼容 chat completions 请求 (url, body, headers)。纯函数，方便测试。

    channel: resolve_llm_channel_cached() 的返回 dict（必含 base_url/model/api_key）。
    prompt: user-role 单轮内容。
    extra_body: 可选 dict，合并进请求 body（厂商特异字段如 thinking 由此注入）。
                None/空 → 不合并，body 只含 OpenAI 标准字段。通道保持通用，厂商特异
                参数走配置（由调用方从 channel.get("request_extra") 取）。
    返回: (url, body_bytes, headers_dict)。
    """
    url = channel["base_url"].rstrip("/") + "/chat/completions"
    body_dict = {
        "model": channel["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        # 4096 足够装下 8 条记忆 + JSON 数组包装，留出思考段冗余
        "max_tokens": 4096,
    }
    if extra_body:
        # extra 覆盖默认（如有冲突；None/空 dict 跳过）
        body_dict.update(extra_body)
    body = json.dumps(body_dict).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {channel['api_key']}",
        "Content-Type": "application/json",
    }
    return url, body, headers


def distill(conversation, max_chars, channel):
    """走 keepsake 通道调 LLM 提炼记忆。失败返回 []，永不抛穿 cron。"""
    if len(conversation) > max_chars:
        conversation = conversation[-max_chars:]
    prompt = DISTILL_PROMPT.replace("{conversation}", conversation)
    # 厂商特异字段走配置 llm.request_extra 注入；缺省/未配置 → {} 不合并
    extra_body = channel.get("request_extra") or {}
    url, body, headers = build_chat_request(channel, prompt, extra_body)
    host = urlparse(url).netloc  # 仅 host 用于日志，绝不带 key/路径
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    finish_reason = "?"  # 用于截断日志，区分 stop/length/...
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        finish_reason = (
            (data.get("choices") or [{}])[0].get("finish_reason") or "?"
        )
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        print(f"llm 调用失败 ({host}): {e}")
        return []
    # 提取 JSON 数组（容错：可能有多余文字/code fence）
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        # 截断防御：不推 watermark 但记录 finish_reason 便于排查预算/截断
        print(
            f"distill: JSON extract failed (len={len(text)} finish={finish_reason})"
        )
        return []
    try:
        items = json.loads(text[start:end + 1])
        # 过滤: 太短/无实质内容
        out = []
        for i in items:
            if not isinstance(i, dict):
                continue
            c = (i.get("content") or "").strip()
            if len(c) < 10:
                continue
            i["content"] = c
            out.append(i)
        return out
    except Exception:
        # json.loads 抛（如截断导致非闭合数组）同样记录 finish_reason
        print(
            f"distill: JSON extract failed (len={len(text)} finish={finish_reason})"
        )
        return []


def read_watermark():
    try:
        with open(WATERMARK_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def write_watermark(mid):
    try:
        with open(WATERMARK_FILE, "w") as f:
            f.write(str(mid))
    except Exception:
        pass


def get_redis_password():
    """从 Keepsake 配置 (~/.config/keepsake/config.json) 读 Redis 连接（不硬编码）"""
    try:
        with open(os.path.expanduser("~/.config/keepsake/config.json")) as f:
            cfg = json.load(f)
        return cfg.get("redis_password", ""), cfg.get("redis_host", "127.0.0.1"), cfg.get("redis_port", 6379)
    except Exception:
        return "", "127.0.0.1", 6379


def store_to_keepsake(items, dry_run):
    """写入 Keepsake (RedisStorage 直连)"""
    sys.path.insert(0, "/opt/fragmented-memory/src")
    from keepsake.storage import RedisStorage
    pwd, r_host, r_port = get_redis_password()
    stor = RedisStorage(host=r_host, port=r_port,
                        password=pwd or None)
    saved = 0
    for it in items:
        cat = it.get("category", "fact")
        tags = f"auto-distill,{cat}"
        if it.get("tags"):
            tags += "," + it["tags"]
        if dry_run:
            print(f"[DRY] [{cat}] {it['content']}")
            continue
        ok = stor.store(text=it["content"], tags=tags, category=cat,
                        source="auto-distill", fragment_type=cat)
        if ok:
            saved += 1
    return saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=2)
    ap.add_argument("--max-chars", type=int, default=4000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # 开关配置: enabled=false 时直接退出（crontab 保持挂着，改配置即可开关）
    conf = load_conf()
    if conf.get("enabled") is False:
        print("memory_distill 已禁用 (memory_distill.conf enabled=false)")
        return
    hours = args.hours or conf.get("hours", 2)
    max_chars = args.max_chars or conf.get("max_chars", 4000)

    # 通道解析：未配置 → 本轮跳过，不调 LLM、不推 watermark（cron 报绿）
    channel = resolve_llm_channel_cached()
    if not channel.get("valid"):
        print("llm channel unconfigured — skip (watermark held)")
        return

    last_id = read_watermark()
    conv, max_id = get_recent_messages(hours, last_id)
    if not conv:
        print(f"无新对话可提炼 (watermark={last_id})")
        return
    print(f"对话长度: {len(conv)} 字符 (watermark {last_id} → {max_id})")

    items = distill(conv, max_chars, channel)
    print(f"提炼出 {len(items)} 条记忆")
    if not items:
        return
    saved = store_to_keepsake(items, args.dry_run)
    print(f"已写入 {saved} 条 (dry_run={args.dry_run})")
    if not args.dry_run and max_id > last_id:
        write_watermark(max_id)
        print(f"watermark 更新为 {max_id}")


if __name__ == "__main__":
    main()
