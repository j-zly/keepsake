"""定时任务管理端点（pause / resume / edit）测试（keepsake_cron_manage）。

🔴 **绝不碰真实 jobs.json / 真实 hermes**：`subprocess.run` 在每个用例里被
monkeypatch 成假执行器，既不派进程也不读文件 —— 88 上本来也没有 hermes。
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.routers import crons as cron_router

TOKEN = "cron-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
JOB_ID = "a1b2c3d4e5f6"


class FakeCLI:
    """`subprocess.run` 替身：记录 argv，按剧本回 (rc, stdout, stderr)。"""

    def __init__(self, rc: int = 0, stdout: str = "ok", stderr: str = "",
                 raise_exc: Exception = None) -> None:
        self.rc, self.stdout, self.stderr, self.raise_exc = rc, stdout, stderr, raise_exc
        self.calls: List[Tuple[Any, ...]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)

    @property
    def argvs(self) -> List[Any]:
        return [c[0] for c in self.calls]

    @property
    def kwargs0(self) -> Dict[str, Any]:
        return self.calls[0][1]


@pytest.fixture()
def audit_dir(tmp_path, monkeypatch):
    d = tmp_path / "console_audit"
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(d))
    return d


@pytest.fixture()
def client(monkeypatch, tmp_path, audit_dir):
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    monkeypatch.setenv("KEEPSAKE_CRON_JOBS", str(tmp_path / "jobs.json"))
    app = create_app()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def cli(monkeypatch):
    """默认剧本：CLI 成功。返回假执行器，用例要失败就改它的 rc/stderr。"""
    fake = FakeCLI()
    monkeypatch.setattr(cron_router.subprocess, "run", fake)
    return fake


def audit_lines(audit_dir) -> List[dict]:
    out: List[dict] = []
    for f in sorted(audit_dir.glob("audit-*.jsonl")):
        out.extend(json.loads(line) for line in f.read_text(encoding="utf-8").splitlines())
    return out


# ---------------------------------------------------------------- 认证 / 闸门

def test_三个管理端点都要token(client, cli):
    """漏挂 require_session 就是裸奔写面 —— 逐条打一遍（401，不是 400/500）。"""
    cases = [("pause", {}), ("resume", {}), ("edit", {"schedule": "30 4 * * *"})]
    for act, extra in cases:
        r = client.post(f"/api/v1/crons/{JOB_ID}/{act}",
                        json={"confirm": True, **extra})
        assert r.status_code == 401, (act, r.status_code)
        assert r.json()["detail"]["error"] == "missing_token"
        assert TOKEN not in r.text
    assert cli.calls == []          # 401 早于任何子进程


def test_缺confirm_400且不起子进程(client, cli, audit_dir):
    r = client.post(f"/api/v1/crons/{JOB_ID}/pause", json={}, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "confirm_required"
    assert cli.calls == []
    assert [l for l in audit_lines(audit_dir) if l["action"] == "cron.pause"
            and l["ok"] is False]


def test_job_id不合法_400且不起子进程(client, cli, audit_dir):
    # 带 `/` 的形状连路由都匹配不上（%/ 编码同理，httpx 先解码），测不到闸门；
    # 真正要挡的是「像 id 但不是 id」的：位数不对、含非十六进制、含分隔符。
    for bad in ("a1b2c3d4e5", "a1b2c3d4e5f6f", "A1B2C3D4E5F7X",
                "a1b2c3d4e5f6;rm", "zzzzzzzzzzzz", "-a1b2c3d4e5f"):
        r = client.post(f"/api/v1/crons/{bad}/pause", json={"confirm": True}, headers=AUTH)
        assert r.status_code == 400, bad
        assert r.json()["detail"]["error"] == "bad_job_id"
    assert cli.calls == []
    assert audit_lines(audit_dir)     # 被拒的写也要留痕


# ---------------------------------------------------------------- argv 纪律

def test_pause_走argv列表不经shell(client, cli, audit_dir):
    r = client.post(f"/api/v1/crons/{JOB_ID}/pause", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "job_id": JOB_ID, "action": "pause", "cli_output": "ok"}
    argv, kwargs = cli.calls[0]
    assert argv == ["hermes", "cron", "pause", JOB_ID]     # 列表形态
    assert isinstance(argv, list)
    assert kwargs.get("shell") is not True
    assert [l for l in audit_lines(audit_dir)
            if l["action"] == "cron.pause" and l["ok"] is True and l["target"] == JOB_ID]


def test_resume_走argv列表(client, cli):
    r = client.post(f"/api/v1/crons/{JOB_ID}/resume", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200 and r.json()["action"] == "resume"
    assert cli.argvs[0] == ["hermes", "cron", "resume", JOB_ID]


def test_edit_改调度带schedule参数(client, cli, audit_dir):
    r = client.post(f"/api/v1/crons/{JOB_ID}/edit",
                    json={"confirm": True, "schedule": "30 4 * * *"}, headers=AUTH)
    assert r.status_code == 200
    assert cli.argvs[0] == ["hermes", "cron", "edit", JOB_ID, "--schedule", "30 4 * * *"]
    line = [l for l in audit_lines(audit_dir) if l["action"] == "cron.edit"][-1]
    assert line["ok"] is True and line["reason"] == "schedule=30 4 * * *"


def test_注入串被job_id闸挡住_不进子进程(client, cli):
    """`a1b2c3d4e5f6; rm` 这类：闸门挡住，且永远不会拼成 shell 字符串。"""
    r = client.post("/api/v1/crons/a1b2c3d4e5f6%3B%20rm%20-rf/pause",
                    json={"confirm": True}, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_job_id"
    assert cli.calls == []


# ---------------------------------------------------------------- CLI 失败 / 缺失

def test_CLI非零退出_500带stderr末行(client, monkeypatch, audit_dir):
    monkeypatch.setattr(cron_router.subprocess, "run",
                        FakeCLI(rc=2, stderr="noise\njob not found: a1b2c3d4e5f6\n"))
    r = client.post(f"/api/v1/crons/{JOB_ID}/resume", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 500
    d = r.json()["detail"]
    assert d["error"] == "cron_cli_failed"
    assert d["message"] == "job not found: a1b2c3d4e5f6"      # stderr 末行透传
    assert [l for l in audit_lines(audit_dir)
            if l["action"] == "cron.resume" and l["ok"] is False]


def test_本机无hermes_CLI_503人话(client, monkeypatch):
    monkeypatch.setattr(cron_router.subprocess, "run",
                        FakeCLI(raise_exc=FileNotFoundError(2, "No such file")))
    r = client.post(f"/api/v1/crons/{JOB_ID}/pause", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 503
    d = r.json()["detail"]
    assert d["error"] == "hermes_cli_missing"
    assert d["message"] == "本机无 hermes CLI，控制台无法管理定时任务"


def test_CLI超时_504(client, monkeypatch):
    monkeypatch.setattr(cron_router.subprocess, "run",
                        FakeCLI(raise_exc=subprocess.TimeoutExpired("hermes", 20)))
    r = client.post(f"/api/v1/crons/{JOB_ID}/pause", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 504 and r.json()["detail"]["error"] == "cron_cli_timeout"


# ---------------------------------------------------------------- schedule 校验

@pytest.mark.parametrize("expr", [
    "30 4 * * *", "*/15 * * * *", "0 0 1,15 * *", "0 9-18 * * 1-5", "@daily",
])
def test_合法schedule(client, cli, expr):
    r = client.post(f"/api/v1/crons/{JOB_ID}/edit", json={"confirm": True, "schedule": expr}, headers=AUTH)
    assert r.status_code == 200, expr
    assert cli.argvs[0][-1] == expr


@pytest.mark.parametrize("expr", [
    "", "30 4 * *", "30 4 * * * *", "60 4 * * *", "30 24 * * *",
    "30 4 32 * *", "30 4 * 13 *", "30 4 * * 8", "abc", "* * * * * ; rm -rf /",
    "--schedule", "@daily @daily",
])
def test_非法schedule_400带example且不起子进程(client, cli, audit_dir, expr):
    r = client.post(f"/api/v1/crons/{JOB_ID}/edit", json={"confirm": True, "schedule": expr}, headers=AUTH)
    assert r.status_code == 400, expr
    d = r.json()["detail"]
    assert d["error"] == "bad_schedule"
    assert "30 4 * * *" in d["message"]          # 回显 example
    assert cli.calls == []


def test_schedule校验函数_边界():
    assert cron_router.valid_schedule("0 0 * * 0")
    assert cron_router.valid_schedule("0 0 * * 7")        # 周日 0 与 7 都行
    assert not cron_router.valid_schedule("0 0 * * 8")
    assert not cron_router.valid_schedule("0 0 * * */0")   # 步长 0 非法
    assert not cron_router.valid_schedule(None)
    assert not cron_router.valid_schedule("  ")


# ---------------------------------------------------------------- 读面连带

def test_读面带job_id_认不出就退空串(client, monkeypatch, tmp_path):
    """管理按钮只对认得出 id 的行出现 ⇒ 读面必须老实说「有」还是「没有」。"""
    f = tmp_path / "jobs.json"
    f.write_text(json.dumps({"jobs": [
        {"name": "记忆提炼", "id": JOB_ID},
        {"name": "记忆去重", "id": "不是12位十六进制"},
        {"name": "记忆维护"},                                   # 缺键
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CRON_JOBS", str(f))
    body = client.get("/api/v1/crons", headers=AUTH).json()
    got = {j["name"]: j["job_id"] for j in body["jobs"]}
    assert got == {"记忆提炼": JOB_ID, "记忆去重": "", "记忆维护": ""}


def test_读面scheduler_status_CLI缺失退null不炸(client, monkeypatch, tmp_path):
    """读面不许因「这台机器没装 CLI」整页报错 —— 那一行退 null，前端显示「未知」。"""
    f = tmp_path / "jobs.json"
    f.write_text(json.dumps({"jobs": [{"name": "记忆提炼", "id": JOB_ID}]}, ensure_ascii=False),
                 encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CRON_JOBS", str(f))
    monkeypatch.setattr(cron_router.subprocess, "run",
                        FakeCLI(raise_exc=FileNotFoundError(2, "No such file")))
    r = client.get("/api/v1/crons", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["scheduler_status"] is None


def test_读面scheduler_status_取CLI末行(client, monkeypatch, tmp_path):
    f = tmp_path / "jobs.json"
    f.write_text(json.dumps({"jobs": [{"name": "记忆提炼", "id": JOB_ID}]}, ensure_ascii=False),
                 encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CRON_JOBS", str(f))
    fake = FakeCLI(stdout="hermes cron status\ndaemon running, 6 jobs\n")
    monkeypatch.setattr(cron_router.subprocess, "run", fake)
    body = client.get("/api/v1/crons", headers=AUTH).json()
    assert body["scheduler_status"] == "daemon running, 6 jobs"
    assert fake.argvs[0] == ["hermes", "cron", "status"]     # 只读命令，同样 argv 列表


def test_静态判据_不许出现shell等于True():
    """🔴 红线：subprocess 一律 argv 列表 + shell=False。"""
    import inspect
    src = inspect.getsource(cron_router)
    assert "shell=True" not in src
