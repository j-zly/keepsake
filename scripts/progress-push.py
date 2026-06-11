#!/usr/bin/env python3
"""
进度推送守护 — 订阅 Redis task:progress 频道，
收到消息后自动推送到 QQ。
"""
import json, os, sys, time

DEBUG_LOG = "/tmp/progress-push-debug.log"

def debug(msg: str):
    with open(DEBUG_LOG, "a") as f:
        f.write(f"{time.time()} {msg}\n")

debug("STARTING")

REDIS_HOST = os.environ.get("REDIS_HOST", "180.76.115.8")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "M/P8ps5H7V/zE1x6M0gosyhMmmj60Eu4")
TELEGRAM_BOT_TOKEN = "8749508666:***"
TELEGRAM_CHAT_ID = ""
HERMES_HOME = os.environ.get("HERMES_HOME", "/root/.hermes")
HERMES_BIN = "/usr/local/lib/hermes-agent/venv/bin/hermes"

debug(f"ENV: HERMES_HOME={HERMES_HOME}, REDIS_HOST={REDIS_HOST}")


def send_qq(msg: str):
    import subprocess
    try:
        r = subprocess.run(
            [HERMES_BIN, "send", "--to", "qqbot", msg],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "HERMES_HOME": HERMES_HOME},
        )
        if r.returncode != 0:
            debug(f"send_qq FAIL: rc={r.returncode} stderr={r.stderr}")
        else:
            debug(f"send_qq OK: stdout={r.stdout.strip()}")
    except Exception as e:
        debug(f"send_qq EXCEPTION: {e}")


def send_telegram(msg: str):
    if not TELEGRAM_CHAT_ID:
        return
    import urllib.request
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        debug(f"send_telegram FAIL: {e}")


def main():
    import redis as redis_mod

    debug("connecting to redis...")
    r = redis_mod.Redis(
        host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD,
        decode_responses=True, socket_keepalive=True, socket_connect_timeout=10,
    )
    r.ping()
    debug(f"connected to Redis {REDIS_HOST}:{REDIS_PORT}")

    pubsub = r.pubsub()
    pubsub.subscribe("task:progress")
    debug("subscribed to task:progress, sending startup message to QQ")
    send_qq("🧠 任务进度推送已启动，分析进度将实时推送至此")

    debug("entering listen loop")
    for msg in pubsub.listen():
        if msg["type"] != "message":
            continue
        try:
            raw = msg["data"]
            parts = raw.split("|", 2)
            if len(parts) < 3:
                continue
            tid, tag, content = parts
            text = f"{tag} {content}"
            debug(f"received progress: {text[:80]}")
            send_qq(text)
            send_telegram(text)
        except Exception as e:
            debug(f"parse error: {e}")


if __name__ == "__main__":
    main()
