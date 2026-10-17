"""认证端点：status / setup / login / logout / me。

🔴 公开面**只有** status / setup / login 三条，且：

* `setup` 仅在「还没有管理员」时可用，已初始化 ⇒ 409（否则任何人都能抢注超管）；
* `login` 的失败文案统一 —— 用户名不存在与口令错都是同一个 401；
* 失败次数超限 ⇒ 429（限速在**校验口令之前**，所以第 6 次错密码根本不会去
  碰 PBKDF2，也不会刷 PBKDF2 拖慢服务）。

🔴 **口令永不进响应体 / 审计 / 日志**：本模块只把 `password` 喂给
`auth.check_password`，出错时的 detail 是写死的文案，不带任何用户输入。

`change_password`（要会话）复用上面同一套限速与长度判据，处置口径：
**保留当前会话、作废其余全部**，改前备份旧凭据文件再原子写回。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import audit
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


class ChangePasswordBody(BaseModel):
    """`{"old_password": ..., "new_password": ..., "confirm": true}`。

    🔴 三个字段全走 **POST body**：口令绝不进 URL（URL 会进浏览器历史、
    网关访问日志、Referer 头）。`confirm` 与其余写端点同款 —— 改密不可撤销
    （别人登不进来，自己也得改用新口令），不许顺手点一下就生效。
    """
    old_password: str = ""
    new_password: str = ""
    confirm: bool = False


def _client_ip(request: Request) -> str:
    """限速用的来源标识。直连 uvicorn 时就是 peer IP，不解析 XFF
    （XFF 由客户端可控，拿它当限速 key 等于让攻击者随便换桶）。"""
    return request.client.host if request.client else ""


def _need_password_len(raw: str) -> None:
    """口令长度判据（400）—— **setup 首设与改密共用这一把尺**。

    两处各写一份判据，迟早会漂移成「初始化允许 8 位、改密要求 12 位」这种
    用户自己都绕不明白的规则。**不回显口令内容**。
    """
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


def _need_username_password(username: str, raw: str) -> None:
    """入参校验（400）：用户名空 / 口令长度越界。**不回显口令内容**。"""
    if not (username or "").strip():
        raise HTTPException(status_code=400,
                            detail={"error": "username_required", "message": "用户名不能为空"})
    _need_password_len(raw)


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


# ---------------------------------------------------------------- 改口令

#: 改密审计动作名（与其它写端点同一条流水，便于按 action 捞账）
_CP_ACTION = "auth.change_password"


def _cp_reject(status: int, error: str, message: str, username: str) -> None:
    """改密的拒绝分支：**先审计一行 ok=false 再抛** —— 被拒绝的尝试也要留痕。"""
    audit.record(_CP_ACTION, username, ok=False, reason=error)
    raise HTTPException(status_code=status,
                        detail={"error": error, "message": message})


@router.post("/change_password")
def auth_change_password(body: ChangePasswordBody, request: Request,
                         session: Dict[str, Any] = Depends(require_session)) -> Dict[str, Any]:
    """换管理员口令。**要会话**（静态 token 那条脚本通道不行，见下）。

    顺序是刻意的，任何一步都不许换：

    1. **限速先于 PBKDF2** —— 与 login 共用同一个 (IP, 用户名) 计数桶，
       所以「在线爆破旧口令」这条路和爆破登录口令是同一个天花板；
    2. **confirm 闸门** —— 不改数据就不该碰 PBKDF2；
    3. **新口令强度**（与 setup 同判据）；
    4. **旧口令校验**（`check_password`，PBKDF2 + 恒时比较），不过 → 403；
    5. **备份旧凭据文件 → 原子写（0600）→ 作废其余会话 → 审计**。

    🔴 **静态 token（脚本/MCP 通道）改不了口令**：它带来的 `session.username`
    是 `"static-token"`，与 admin 记录里的 username 比不上 ⇒ 第 4 步必然 403。
    这正是要的 —— 脚本通道不该具备换管理员口令的能力。
    🔴 口令明文不进审计 / 回包 / 错误消息：审计只记 `revoked_others` 与备份路径。
    """
    ip = _client_ip(request)
    username = str(session.get("username", ""))
    if auth_mod.rate_limited(ip, username):
        raise HTTPException(
            status_code=429,
            detail={"error": "too_many_attempts",
                    "message": f"{auth_mod.RATE_WINDOW_SEC} 秒内失败次数过多，请稍后再试"})
    if body.confirm is not True:
        _cp_reject(400, "confirm_required", "改口令必须勾选二次确认", username)
    # 强度先判：弱新口令没资格消耗一次 PBKDF2 校验旧口令
    _need_password_len(body.new_password)
    if not auth_mod.check_password(username, body.old_password or ""):
        auth_mod.note_failure(ip, username)
        _cp_reject(403, "wrong_password", "旧密码不正确", username)
    auth_mod.clear_failures(ip, username)

    # 🔴 备份必须在写之前：口令哈希丢了管理员就再也登不回来，这不是撤销能救的
    backup = audit.backup_json(f"{_CP_ACTION}.{audit.stamp()}.json", auth_mod.load_admin())
    auth_mod.set_password(username, body.new_password)
    # 当前会话保留（改完密不该被自己踢回登录页），其余一律作废
    revoked = auth_mod.revoke_other_sessions(str(session.get("token", "")))
    audit.record(_CP_ACTION, username, ok=True,
                 reason=f"revoked_others={revoked}; backup={backup}")
    return {"ok": True, "username": username,
            "revoked_sessions": revoked, "backup": backup}
