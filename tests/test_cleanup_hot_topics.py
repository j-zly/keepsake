"""cleanup_hot_topics.py 单测（用 fake redis client 验逻辑）。

覆盖：
  * 扫描三 zset + last_seen hash，识别污染词（虚词表 + 纯 ASCII 短词）
  * dry-run 路径：仅打印不清
  * --yes 路径：实际 zrem/hdel
  * 没有污染词时直接返回
  * 干净词（中文 / 长英文 / 技术词）不被误删

不连真实 Redis（任务红线：零网络）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "cleanup_hot_topics",
    ROOT / "scripts" / "cleanup_hot_topics.py",
)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)

# 复用 cleanup 模块里的常量
HOT_ZSETS = cleanup.HOT_ZSETS
HOT_TOPIC_LAST_SEEN = cleanup.HOT_TOPIC_LAST_SEEN


# ---------------------------------------------------------------------------
# 假 redis client：只实现本脚本用到的接口
# ---------------------------------------------------------------------------

class FakeRedis:
    """最小 fake redis — 仅实现 zrange/zrem/hgetall/hdel/ping。"""

    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def ping(self):
        return True

    def zrange(self, key, start, end, withscores=False):
        members = sorted(self.zsets.get(key, {}).items(), key=lambda x: -x[1])
        sliced = members[start:end + 1] if end != -1 else members[start:]
        if withscores:
            return [(m, s) for m, s in sliced]
        return [m for m, _ in sliced]

    def zrem(self, key, *members):
        z = self.zsets.setdefault(key, {})
        removed = 0
        for m in members:
            if m in z:
                del z[m]
                removed += 1
        return removed

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hdel(self, key, *fields):
        h = self.hashes.setdefault(key, {})
        removed = 0
        for f in fields:
            if f in h:
                del h[f]
                removed += 1
        return removed


# ---------------------------------------------------------------------------
# _is_polluted 单测
# ---------------------------------------------------------------------------

class TestIsPolluted:

    @pytest.mark.parametrize("member", [
        # 虚词表里的成员
        "the", "and", "for", "you", "are", "is", "be",
        # 纯 ASCII 短词（≤2 字母）
        "in", "an", "at", "ce", "be", "dd", "em", "g", "i",
        "A", "OK",  # OK 是 2 字母英文 → 长度 <3 → 算短词
    ])
    def test_polluted_detected(self, member):
        assert cleanup._is_polluted(member) is True, member

    @pytest.mark.parametrize("member", [
        # 中文词
        "服务器", "部署", "配置",
        # 长英文（≥3 字母且不在虚词表）
        "redis", "mysql", "binance", "ethereum", "telegram",
        # 技术词
        "api", "sql", "log", "ssh", "key", "jwt",
        # 数字串
        "12345",
        # 空字符串
        "",
    ])
    def test_clean_kept(self, member):
        assert cleanup._is_polluted(member) is False, member


# ---------------------------------------------------------------------------
# scan_polluted + execute_cleanup 集成测
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_client():
    cli = FakeRedis()
    # 在三个 zset + hash 里塞一些混合数据
    cli.zsets["keepsake:hot_topics"] = {
        "服务器": 10.0, "部署": 8.0,
        "in": 3.0, "an": 3.0, "ce": 3.0, "be": 2.0,
        "the": 5.0, "and": 5.0, "for": 5.0,
        "redis": 4.0, "binance": 7.0,
        "api": 6.0, "sql": 4.0,
    }
    cli.zsets["keepsake:hot_topics:daily"] = {
        "部署": 5.0, "of": 3.0, "to": 3.0,
        "ok": 2.0, "no": 2.0, "redis": 4.0,
    }
    cli.zsets["keepsake:hot_topics:weekly"] = {
        "eth": 4.0, "btc": 4.0,
        "in": 1.0, "at": 1.0,
    }
    cli.hashes["keepsake:hot_topics:last_seen"] = {
        "服务器": "1700000000", "部署": "1700000000",
        "in": "1700000000", "the": "1700000000",
        "redis": "1700000000",
    }
    return cli


class TestScanAndExecute:

    def test_scan_identifies_all_polluted(self, fake_client):
        plan = cleanup.scan_polluted(fake_client)
        # 三个 zset + 一个 hash 都应在 plan 里
        assert "keepsake:hot_topics" in plan
        assert "keepsake:hot_topics:daily" in plan
        assert "keepsake:hot_topics:weekly" in plan
        assert "keepsake:hot_topics:last_seen" in plan

        # 全局 zset 里的污染词：in/an/ce/be/the/and/for（不含 redis/binance/api/sql）
        main = plan["keepsake:hot_topics"]
        assert {"in", "an", "ce", "be", "the", "and", "for"} <= set(main)
        assert "redis" not in main
        assert "binance" not in main
        assert "api" not in main
        assert "sql" not in main
        assert "服务器" not in main
        assert "部署" not in main

    def test_dry_run_does_not_mutate(self, fake_client, capsys):
        plan = cleanup.scan_polluted(fake_client)
        cleanup.execute_cleanup(fake_client, plan, apply=False)
        # 数据未动
        assert "in" in fake_client.zsets["keepsake:hot_topics"]
        assert "the" in fake_client.hashes["keepsake:hot_topics:last_seen"]
        # 输出包含 DRY-RUN 标记
        out = capsys.readouterr().out
        assert "ZREM" in out or "HDEL" in out

    def test_apply_deletes_polluted(self, fake_client, capsys):
        plan = cleanup.scan_polluted(fake_client)
        cleanup.execute_cleanup(fake_client, plan, apply=True)
        # 污染词全删了
        for z in HOT_ZSETS:
            for member in plan.get(z, []):
                assert member not in fake_client.zsets[z], (
                    f"{member} 未删（{z}）"
                )
        for member in plan.get(HOT_TOPIC_LAST_SEEN, []):
            assert member not in fake_client.hashes[HOT_TOPIC_LAST_SEEN], (
                f"{member} 未删（last_seen）"
            )
        # 干净词还在
        assert "服务器" in fake_client.zsets["keepsake:hot_topics"]
        assert "redis" in fake_client.zsets["keepsake:hot_topics"]
        assert "binance" in fake_client.zsets["keepsake:hot_topics"]
        assert "api" in fake_client.zsets["keepsake:hot_topics"]
        assert "sql" in fake_client.zsets["keepsake:hot_topics"]

    def test_no_pollution_returns_empty(self):
        cli = FakeRedis()
        cli.zsets["keepsake:hot_topics"] = {
            "服务器": 5.0, "redis": 4.0, "binance": 3.0,
        }
        plan = cleanup.scan_polluted(cli)
        assert plan == {}


# ---------------------------------------------------------------------------
# main() 入口路径（用 monkeypatch 注入 fake client + fake config）
# ---------------------------------------------------------------------------

class TestMain:
    """main() 函数的 happy path + 错误路径。"""

    def _write_fake_config(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "redis_host": "127.0.0.1",
            "redis_port": 6399,
            "redis_password": "fake",
        }))
        monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_path))
        return cfg_path

    def test_main_dry_run_exit_zero(self, tmp_path, monkeypatch, capsys, fake_client):
        self._write_fake_config(tmp_path, monkeypatch)
        monkeypatch.setattr(cleanup, "_connect_redis", lambda cfg: fake_client)
        rc = cleanup.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "DRY-RUN" in out or "DONE" in out

    def test_main_json_output(self, tmp_path, monkeypatch, capsys, fake_client):
        self._write_fake_config(tmp_path, monkeypatch)
        monkeypatch.setattr(cleanup, "_connect_redis", lambda cfg: fake_client)
        rc = cleanup.main(["--json"])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["dry_run"] is True
        assert isinstance(payload["plan"], dict)
        assert payload["total"] > 0

    def test_main_yes_applies(self, tmp_path, monkeypatch, capsys, fake_client):
        self._write_fake_config(tmp_path, monkeypatch)
        monkeypatch.setattr(cleanup, "_connect_redis", lambda cfg: fake_client)
        rc = cleanup.main(["--yes"])
        assert rc == 0
        # in 应已删
        assert "in" not in fake_client.zsets["keepsake:hot_topics"]
        # 干净词还在
        assert "redis" in fake_client.zsets["keepsake:hot_topics"]
