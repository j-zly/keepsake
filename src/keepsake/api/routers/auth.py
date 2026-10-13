"""认证端点：status / setup / login / logout / me。

🔴 公开面**只有** status / setup / login 三条，且：

* `setup` 仅在「还没有管理员」时可用，已初始化 ⇒ 409（否则任何人都能抢注超管）；
* `login` 的失败文案统一 —— 用户名不存在与口令错都是同一个 401；
* 失败次数超限 ⇒ 429（限速在**校验口令之前**，所以第 6 次错密码根本不会去
  碰 PBKDF2，也不会刷 PBKDF2 拖慢服务）。

🔴 **口令永不进响应体 / 审计 / 日志**：本模块只把 `password` 喂给
`auth.check_password`，出错时的 detail 是写死的文案，不带任何用户输入。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import auth as auth_mod
from ..deps import require_session

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

#: 登录失败的统一文案（不区分「用户不存在」与「口令错」—— 区分即枚举预言机）
_BAD_CREDENTIALS = {"error": "invalid_credentials", "message": "账号或口令不正确"}


class SetupBody(BaseModel):
    """`{"username": ..., "password": ...}`。"""

    username: str = ""
    password: str = ""


class LoginBody(BaseModel):
    """`{"username": ..., "password": ..., "remember": true}`。"""

    username: str = ""
    password: str = ""
    remember: bool = False


def _client_ip(request: Request) -> str:
    """限速用的来源标识。直连 uvicorn 时就是 peer IP，不解析 XFF
    （XFF 由客户端可控，拿它当限速 key 等于让攻击者随便换桶）。"""
    return request.client.host if request.client else ""


def _need_username_password(username: str, raw: str) -> None:
    """入参校验（400）：用户名空 / 口令长度越界。**不回显口令内容**。"""
    if not (username or "").strip():
        raise HTTPException(status_code=400,
                            detail={"error": "username_required", "message": "用户名不能为空"})
    if len(raw or "") < auth_mod.MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail={"error": "password_too_short",
                    "message": f"口令至少 {auth_mod.MIN_PASSWORD_LEN} 位"})
    if len(raw or "") > auth_mod.MAX_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail={"error": "password_too_long",
                    "message": f"口令至多 {auth_mod.MAX_PASSWORD_LEN} 位"})


@router.get("/status")
def auth_status() -> Dict[str, Any]:
    """**公开**：前端首屏据此决定「去初始化」还是「去登录」。

    只回 `initialized` 布尔 —— 不回用户名、不回有没有静态 token（那是信息泄漏）。
    """
    return {"initialized": auth_mod.admin_exists(), "auth_mode": "login"}


@router.post("/setup")
def auth_setup(body: SetupBody) -> Dict[str, Any]:
    """**公开**（仅未初始化时）：建管理员并**自动登录**（直接发会话 token）。"""
    if auth_mod.admin_exists():
        raise HTTPException(status_code=409,
                            detail={"error": "already_initialized",
                                    "message": "已初始化，管理员已存在"})
    username = (body.username or "").strip()
    _need_username_password(username, body.password)
    try:
        auth_mod.create_admin(username, body.password)
    except FileExistsError:
        # 并发 setup：前面的 admin_exists() 检查没能挡住这条竞态，落盘时兜住
        raise HTTPException(status_code=409,
                            detail={"error": "already_initialized",
                                    "message": "已初始化，管理员已存在"})
    sess = auth_mod.issue_session(username, remember=False)
    return {"ok": True, "username": sess["username"], "token": sess["token"],
            "expires_at": sess["expires_at"]}


@router.post("/login")
def auth_login(body: LoginBody, request: Request) -> Dict[str, Any]:
    """**公开**：账号口令换会话。`remember=true` ⇒ 30 天，否则 12 小时。"""
    ip = _client_ip(request)
    username = (body.username or "").strip()
    # 限速先于任何哈希计算：被限速的请求不许消耗 CPU 打 PBKDF2。
    if auth_mod.rate_limited(ip, username):
        raise HTTPException(
            status_code=429,
            detail={"error": "too_many_attempts",
                    "message": f"{auth_mod.RATE_WINDOW_SEC} 秒内失败次数过多，请稍后再试"})
    if not username or not auth_mod.check_password(username, body.password or ""):
        auth_mod.note_failure(ip, username)
        raise HTTPException(status_code=401, detail=dict(_BAD_CREDENTIALS))
    auth_mod.clear_failures(ip, username)
    sess = auth_mod.issue_session(username, remember=bool(body.remember))
    return {"token": sess["token"], "username": sess["username"],
            "expires_at": sess["expires_at"], "ttl_seconds": sess["ttl_seconds"]}


@router.post("/logout")
def auth_logout(session: Dict[str, Any] = Depends(require_session)) -> Dict[str, Any]:
    """注销**当前会话**（立即失效）。回包不含 token。"""
    auth_mod.revoke_session(session.get("token", ""))
    return {"ok": True, "username": session.get("username", "")}


@router.get("/me")
def auth_me(session: Dict[str, Any] = Depends(require_session)) -> Dict[str, Any]:
    """当前登录者（前端「刷新页面仍在登录态」的依据）。"""
    return {"username": session.get("username", ""),
            "expires_at": session.get("expires_at", ""),
            "legacy_token": bool(session.get("legacy"))}
