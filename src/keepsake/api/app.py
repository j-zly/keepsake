"""Keepsake 控制台后端（P1 只读 + P2 写操作）。

    uvicorn keepsake.api.app:app --host 127.0.0.1 --port 8211

🔴 **写面被闸门圈死**：只有 `routers/ops.py` 里那 6 条 POST（打标 / 改 / 归档
+ merge / forget）能改数据，其余全是 GET。写面每条都要求显式二次确认、
写前自动备份、事后审计（见 `api/audit.py`），且**一律经 keepsake 存储层原语**
落库，不自拼 SQL。G4 会断言「非 GET 路由恰好是这 6 条」。

📌 **FastAPI 钉在 0.115.x**：0.116+ 的 `include_router` 改成惰性 `_IncludedRouter`，
`app.routes` 里就不出现真实路由了 —— G4 那句 `app.routes` 断言会**空转通过**
（既没查到写路由，也没查到读路由）。0.115.x 把路由摊平，闸门才有断言价值。
装法：`.venv/bin/python -m pip install "fastapi==0.115.6" uvicorn httpx pytest`。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .routers import agents, auth, core, crons, fragments, hits, kb, messages, ops, stats

logger = logging.getLogger("keepsake.api")

API_PREFIX = "/api/v1"


def _error(status_code: int, error: str, message: str) -> JSONResponse:
    """统一错误体：`{"detail": {"error": ..., "message": ...}}`。

    🔴 message 里**不许**出现 token / 连接串 / 口令 —— 异常信息只报类名与阶段。
    """
    return JSONResponse(status_code=status_code,
                        content={"detail": {"error": error, "message": message}})


def create_app() -> FastAPI:
    """造一个 FastAPI 实例：挂只读路由 → 挂写路由 → 挂静态文件 → 全局异常处理。"""
    app = FastAPI(
        title="Keepsake Console",
        version="1.2.0",
        description="记忆可视化后台 · 账号登录（会话 token）+ 只读查询 + 带二次确认的写操作",
    )

    # ---- 健康检查（唯一免认证路由）----
    @app.get("/health", tags=["meta"])
    def health() -> dict:
        """存活探针。只报「进程活着 + token 是否已配置」，**不报 token 本身**。"""
        return {"status": "ok", "token_configured": bool(config.console_token())}

    # ---- 认证路由（P3：status/setup/login 公开，logout/me 要会话）----
    app.include_router(auth.router)

    # ---- 只读业务路由 ----
    for r in (core.router, fragments.router, hits.router, kb.router,
              stats.router, crons.router):
        app.include_router(r)

    # ---- 写操作路由（全部 POST / 全部需 token / 全部二次确认+备份+审计）----
    app.include_router(ops.router)
    # 代理注册表治理面（keepsake_agent_registry）：读面一条 GET + 四条写端点，
    # 写端点与 ops 同款纪律（require_session + write_guard + 备份 + 审计）。
    app.include_router(agents.router)
    # 消息中心（代理互聊信箱的治理面）：读面一条 GET + 私信开关一条写端点，
    # 写端点与 agents 同款纪律（require_session + write_guard + 备份 + 审计）。
    app.include_router(messages.router)

    # ---- 全局异常：任一数据源异常 ⇒ 500 + 结构化 detail，绝不吞成空结果 ----
    @app.exception_handler(Exception)
    def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.error("api 未处理异常 %s %s: %s",
                     request.method, request.url.path, exc.__class__.__name__)
        return _error(500, "internal_error",
                      f"数据源或处理异常：{exc.__class__.__name__}")

    # ---- 静态文件（前端构建产物）：**放在最后**，别盖住 /api ----
    # SPA 外壳必须免认证可达 —— 否则没 token 时连「输入 token」那一页都打不开。
    static_dir = config.static_dir()
    index = static_dir / "index.html"
    if index.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="console")
    else:
        logger.warning("api: 前端产物不存在（%s），只挂 API；先跑 web/ 的 npm run build", index)

    return app


app = create_app()
