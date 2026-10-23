"""ks_perf_stdlib —— `python -m keepsake.jobs` 定时任务 CLI 入口（2026-10）。

覆盖任务书验收点：
  1. 三个子命令（distill / dedup / tiering）`--dry-run` **均 exit 0**，
     且输出含**既有汇总关键词**（与现有 shell 脚本等价的那些行）；
  2. `--config` 指坏文件 → **明确报错**（一行人话 + 退出码 2），**不是裸 traceback**；
  3. 配置文件不存在 → 同样明确报错；
  4. CLI 不污染调用方的环境变量（KEEPSAKE_CONFIG 等跑完要还原）。

设计要点：
  * 全程不连生产：HOME 指到 tmp_path（既有脚本的 Redis 连接回落读
    `~/.config/keepsake/config.json`，指走就不会碰到真配置），
    Redis 端口指一个没人听的端口 ⇒ 去重走「后端不可达 ⇒ 跳过」那条路；
  * tiering 走**真 SQLite 后端**（tmp 目录里的库）⇒ 真的跑一轮班车规则，
    不是打桩；
  * distill 的 LLM 通道故意不配 ⇒ 既有脚本自己的「unconfigured — skip」那行
    就是它的汇总行。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest
from typer.testing import CliRunner

from keepsake.jobs import app

runner = CliRunner()

COMMANDS = ("distill", "dedup", "tiering")


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """HOME / 状态文件 / Redis 端口全部指到 tmp —— 绝不碰真配置与真库。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KEEPSAKE_TIERING_STATE", str(tmp_path / "tiering_state.json"))
    monkeypatch.delenv("KEEPSAKE_CONFIG", raising=False)
    monkeypatch.delenv("KEEPSAKE_REDIS_HOST", raising=False)
    monkeypatch.delenv("KEEPSAKE_REDIS_PORT", raising=False)
    monkeypatch.delenv("KEEPSAKE_REDIS_PASSWORD", raising=False)


def _write_config(path: Path, cfg: Dict[str, Any]) -> Path:
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. 三个子命令 --dry-run：exit 0 + 既有汇总关键词
# ---------------------------------------------------------------------------

class TestDryRunCommands:
    def test_tiering_dry_run_exits_zero_with_stats(self, tmp_path):
        """tiering --dry-run：真跑一轮（SQLite 后端）⇒ exit 0 + stats JSON。"""
        cfg = _write_config(tmp_path / "config.json", {
            "storage": {"backend": "sqlite",
                        "sqlite": {"path": str(tmp_path / "tiering.db")}},
            "tiering": {"enabled": True,
                        "promote_nominate": {"window_days": 7, "nominate_min_hits": 5}},
            "retrieval": {"core_char_budget": 1000},
        })
        result = runner.invoke(app, ["tiering", "--config", str(cfg), "--dry-run"])
        assert result.exit_code == 0, result.output
        # 既有汇总关键词（与 scripts/tiering_cron.py 打的是同一份 stats）
        assert "nominate" in result.output
        assert '"dry_run": true' in result.output

    def test_tiering_disabled_is_clean_skip(self, tmp_path):
        """tiering.enabled=false（缺省）⇒ skipped 一行 + exit 0（cron 不报红）。"""
        cfg = _write_config(tmp_path / "config.json", {"agent_id": "main-brain"})
        result = runner.invoke(app, ["tiering", "--config", str(cfg), "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "skipped" in result.output

    def test_distill_dry_run_exits_zero(self, tmp_path):
        """distill --dry-run：LLM 通道未配 ⇒ 既有脚本自己打「unconfigured — skip」。"""
        cfg = _write_config(tmp_path / "config.json", {"agent_id": "main-brain"})
        result = runner.invoke(app, ["distill", "--config", str(cfg), "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "unconfigured" in result.output
        assert "skip" in result.output

    def test_dedup_dry_run_exits_zero(self, tmp_path):
        """dedup --dry-run：Redis 指一个没人听的端口 ⇒ 跳过 + 既有汇总行。"""
        cfg = _write_config(tmp_path / "config.json", {
            "agent_id": "main-brain",
            "redis_host": "127.0.0.1",
            "redis_port": 1,
        })
        result = runner.invoke(app, ["dedup", "--config", str(cfg), "--dry-run"])
        assert result.exit_code == 0, result.output
        # 与既有脚本同格式的汇总行
        assert "Done: checked=" in result.output
        assert "matched=" in result.output

    @pytest.mark.parametrize("command", COMMANDS)
    def test_dry_run_never_raises(self, command, tmp_path):
        """三个子命令 --dry-run 一律 exit 0（不许抛异常出去）。"""
        cfg = _write_config(tmp_path / "config.json", {"agent_id": "main-brain"})
        result = runner.invoke(app, [command, "--config", str(cfg), "--dry-run"])
        assert result.exit_code == 0, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)


# ---------------------------------------------------------------------------
# 2/3. 配置坏了 → 明确报错，不是裸 traceback
# ---------------------------------------------------------------------------

class TestBadConfig:
    @pytest.mark.parametrize("command", COMMANDS)
    def test_broken_json_is_explicit_error(self, command, tmp_path):
        """坏 JSON ⇒ 一行人话 + 退出码 2；输出里不许有 traceback。"""
        bad = tmp_path / "bad.json"
        bad.write_text("{这不是 JSON", encoding="utf-8")
        result = runner.invoke(app, [command, "--config", str(bad), "--dry-run"])
        assert result.exit_code == 2, result.output
        assert "错误" in result.output
        assert "JSON" in result.output
        assert "Traceback" not in result.output

    @pytest.mark.parametrize("command", COMMANDS)
    def test_missing_file_is_explicit_error(self, command, tmp_path):
        """文件不存在 ⇒ 一行人话 + 退出码 2。"""
        result = runner.invoke(app, [command, "--config", str(tmp_path / "nope.json"),
                                      "--dry-run"])
        assert result.exit_code == 2, result.output
        assert "错误" in result.output
        assert "Traceback" not in result.output

    def test_non_object_root_is_explicit_error(self, tmp_path):
        """根节点不是 JSON 对象 ⇒ 明确报错。"""
        bad = tmp_path / "list.json"
        bad.write_text("[1, 2, 3]", encoding="utf-8")
        result = runner.invoke(app, ["tiering", "--config", str(bad), "--dry-run"])
        assert result.exit_code == 2, result.output
        assert "错误" in result.output


# ---------------------------------------------------------------------------
# 4. 不污染环境变量
# ---------------------------------------------------------------------------

class TestNoEnvPollution:
    def test_config_env_is_restored(self, tmp_path):
        """跑完 KEEPSAKE_CONFIG / KEEPSAKE_REDIS_* 全部还原。"""
        cfg = _write_config(tmp_path / "config.json", {
            "agent_id": "main-brain",
            "redis_host": "10.0.0.1",
            "redis_port": 6399,
        })
        before = {k: os.environ.get(k) for k in
                  ("KEEPSAKE_CONFIG", "KEEPSAKE_REDIS_HOST", "KEEPSAKE_REDIS_PORT")}
        result = runner.invoke(app, ["tiering", "--config", str(cfg), "--dry-run"])
        assert result.exit_code == 0, result.output
        after = {k: os.environ.get(k) for k in before}
        assert after == before, f"环境变量被污染了：{before} → {after}"
