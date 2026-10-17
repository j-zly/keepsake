"""管理员凭据与会话（P3 登录）—— **纯文件，不建表、不碰 DB**。

四件事，各自有硬约束：

1. **口令只存哈希**：`hashlib.pbkdf2_hmac('sha256', ...)`，随机 16 字节 salt，
   ≥100000 轮；落盘的是 `{"username","salt","hash","iters","created"}`，
   文件权限 0600。**明文口令从不出现在代码 / 日志 / 审计 / 响应体 / git 里** ——
   本模块没有任何一条把口令写出去或拼进错误消息的路径。
2. **会话**：32 字节随机 urlsafe token；服务端只存 token 的 **sha256**
   （丢了会话文件 = 全部会话作废，而不是「拿到文件就能冒充任何人」），
   过期会话在**校验时顺手清掉**。
3. **旧静态 token 兼容**：见 `deps.require_session` —— 老脚本/MCP 继续可用。
4. **登录限速**：同 IP + 同用户名 60 秒内 5 次失败，第 6 次 429。
   进程内计数（不引第三方库）：单进程 uvicorn 部署够用；
   多进程部署请把 `_FAILS` 换成 Redis 计数 —— 升级路径就这一处。

🔴 **路径全部可用环境变量改**（`KEEPSAKE_CONSOLE_ADMIN_FILE` /
`KEEPSAKE_CONSOLE_SESSION_FILE` / `KEEPSAKE_CONSOLE_SESSION_TTL`），
测试与非 root 环境一律指到临时目录，绝不碰 `/root/.hermes/secrets/`。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import config

#: PBKDF2 轮数（任务书要求 ≥100000；200000 是当前 OWASP 对 sha256 的口径）
PBKDF2_ITERS = 200_000

#: 口令最短长度（任务书：<8 位 ⇒ 400）
MIN_PASSWORD_LEN = 8

#: 口令最长长度（防 PBKDF2 被超长输入拖住；也避免 1MB 口令进内存）
MAX_PASSWORD_LEN = 256

#: 登录限速：窗口秒数 / 窗口内最大失败次数（超出 ⇒ 429）
RATE_WINDOW_SEC = 60
RATE_MAX_FAILS = 5

#: 文件锁：会话是「读-改-写」一个 JSON 文件，并发下必须串行
_FILE_LOCK = threading.RLock()

#: 限速计数：key = (客户端 IP, 用户名) → 失败时间戳列表
_FAILS: Dict[Tuple[str, str], List[float]] = {}
_FAILS_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(ts: Any) -> Optional[datetime]:
    """解析 ISO 时间串；坏值返回 None（**绝不当成永不过期**）。"""
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(str(text).encode("ascii"), validate=True)


def token_hash(token: str) -> str:
    """会话 token 的落盘形态 = **sha256(token) 的十六进制**（不存原值）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 管理员凭据
# --------------------------------------------------------------------------

def hash_password(raw: str, salt: bytes, iters: int) -> str:
    """PBKDF2-HMAC-SHA256 ⇒ base64 串。这是口令在磁盘上的唯一形态。"""
    return _b64(hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, iters))


def load_admin() -> Optional[Dict[str, Any]]:
    """读管理员记录；文件不存在 / 坏 JSON / 字段缺失 ⇒ None（= 未初始化）。"""
    try:
        raw = config.admin_file().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(rec, dict) or not rec.get("username") or not rec.get("hash"):
        return None
    return rec


def admin_exists() -> bool:
    """是否已有管理员（决定前端进 /setup 还是 /login）。"""
    return load_admin() is not None


def _atomic_json(path, payload: Any) -> None:
    """把 payload 原子写进 path，权限 0600 —— **本文件所有凭据/会话落盘的唯一入口**。

    🔴 顺序是刻意的：`chmod` 打在临时文件上、`os.replace` 之前 ——
    落地的瞬间就是 0600，中间不存在「文件已可见但还是 644」那半秒
    （凭据文件可读窗口 = 别的账号把 PBKDF2 拿去离线爆破）。
    tmp 与目标同目录 ⇒ `os.replace` 是同分区的原子改名，不存在写坏一半的中间态。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def create_admin(username: str, password: str) -> Dict[str, Any]:
    """建管理员并落盘（0600）。

    🔴 **一次性，靠 link 的原子性保证**：先写同目录临时文件（内容完整 + 0600），
    再 `os.link` 到最终路径 —— 目标已存在时 link 直接失败（FileExistsError），
    两个并发 setup 只可能有一个成功，不存在「后写的覆盖先写的」这条抢注路径。
    （直接 open(O_EXCL) 也原子，但中途崩会留下半截 JSON ⇒ 永远登不进。）
    """
    salt = secrets.token_bytes(16)
    rec = {
        "username": username,
        "salt": _b64(salt),
        "hash": hash_password(password, salt, PBKDF2_ITERS),
        "iters": PBKDF2_ITERS,
        "created": _iso(_now()),
    }
    path = config.admin_file()
    with _FILE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)   # 成功时它只是多出来的一个硬链接，删掉不影响 path
    return rec


def check_password(username: str, password: str) -> bool:
    """校验账号口令。

    🔴 **用户名不存在与口令错误返回同一个 False**：调用方只能报统一 401 文案，
    否则「用户名存在」就成了枚举预言机（用一半时间戳做用户名爆破）。
    用户名不存在时也照跑一遍 PBKDF2，让两条分支耗时同量级。
    """
    rec = load_admin()
    if rec is None:
        hash_password(password, b"\x00" * 16, PBKDF2_ITERS)
        return False
    try:
        salt = _unb64(rec["salt"])
    except (KeyError, ValueError):
        return False
    try:
        iters = int(rec.get("iters") or PBKDF2_ITERS)
    except (TypeError, ValueError):
        return False
    same_user = hmac.compare_digest(str(username or "").encode("utf-8"),
                                    str(rec.get("username", "")).encode("utf-8"))
    got = hash_password(password, salt, iters)
    return same_user and hmac.compare_digest(got.encode("ascii"),
                                             str(rec["hash"]).encode("ascii"))


def set_password(username: str, password: str) -> Dict[str, Any]:
    """改口令：换新 salt 重算哈希，原子写回 0600（username / created 原样保留）。

    🔴 **调用方必须先 `check_password` 通过** —— 本函数不校验旧口令，
    它是「校验」与「落盘」之间的那道纯写入闸。放在这层是为了让「改密」只有
    一条落盘路径，不会出现 setup 与改密两套写文件的口径。
    🔴 落盘前**必须先备份旧文件**（`audit.backup_json`）—— 口令哈希丢了，
    管理员就再也登不进了，而这不是任何一条撤销操作能救回来的。
    """
    with _FILE_LOCK:
        rec = load_admin()
        if rec is None:
            raise FileNotFoundError("管理员尚未初始化，无法改口令")
        salt = secrets.token_bytes(16)
        new_rec = dict(rec)
        new_rec["salt"] = _b64(salt)
        new_rec["hash"] = hash_password(password, salt, PBKDF2_ITERS)
        new_rec["iters"] = PBKDF2_ITERS
        _atomic_json(config.admin_file(), new_rec)
    return new_rec


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------

def _read_sessions() -> Dict[str, Dict[str, Any]]:
    """读会话文件；不存在 / 坏 ⇒ 空字典（**不许把坏文件当成「全放行」**）。"""
    try:
        raw = config.session_file().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    items = data.get("sessions") if isinstance(data, dict) else data
    out: Dict[str, Dict[str, Any]] = {}
    for it in items or []:
        if isinstance(it, dict) and it.get("token_hash"):
            out[str(it["token_hash"])] = it
    return out


def _write_sessions(sessions: Dict[str, Dict[str, Any]]) -> None:
    _atomic_json(config.session_file(), {"sessions": list(sessions.values())})


def _purge_expired(sessions: Dict[str, Dict[str, Any]]) -> bool:
    """剔掉过期会话，返回「有没有动过」（动过才值得回写文件）。"""
    now = _now()
    dead = [h for h, s in sessions.items()
            if (_parse(s.get("expires_at")) or now) <= now]
    for h in dead:
        sessions.pop(h, None)
    return bool(dead)


def issue_session(username: str, remember: bool = False) -> Dict[str, Any]:
    """签发会话：随机 32 字节 urlsafe token，落盘只留它的 sha256。

    `remember=true` ⇒ 30 天；否则走 `KEEPSAKE_CONSOLE_SESSION_TTL`（默认 12 小时）。
    返回体里带 token 原值 —— **只有这一次**，之后任何日志/审计都不许再碰它。
    """
    ttl = config.REMEMBER_SESSION_TTL if remember else config.session_ttl()
    token = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(seconds=ttl)
    rec = {"token_hash": token_hash(token), "username": username,
           "expires_at": _iso(expires_at)}
    with _FILE_LOCK:
        sessions = _read_sessions()
        _purge_expired(sessions)
        sessions[rec["token_hash"]] = rec
        _write_sessions(sessions)
    return {"token": token, "username": username, "expires_at": rec["expires_at"],
            "ttl_seconds": ttl}


def verify_session(token: str) -> Optional[Dict[str, Any]]:
    """校验会话 token；有效 ⇒ 返回 `{username, expires_at, token}`，否则 None。

    过期 / 未知 token 都会**顺手从文件里清掉**（过期记录留着只会让文件无限涨）。
    """
    if not token:
        return None
    h = token_hash(token)
    with _FILE_LOCK:
        sessions = _read_sessions()
        rec = sessions.get(h)
        if rec is None:
            return None
        expires = _parse(rec.get("expires_at"))
        if expires is None or expires <= _now():
            _purge_expired(sessions)
            _write_sessions(sessions)
            return None
        return {"username": str(rec.get("username", "")),
                "expires_at": str(rec.get("expires_at")),
                "token": token}


def revoke_session(token: str) -> bool:
    """注销当前会话（立即生效）。找不到 ⇒ False。"""
    if not token:
        return False
    with _FILE_LOCK:
        sessions = _read_sessions()
        if sessions.pop(token_hash(token), None) is None:
            return False
        _write_sessions(sessions)
    return True


def revoke_other_sessions(keep_token: str) -> int:
    """作废**除 `keep_token` 外**的全部会话，返回被作废的条数。

    改口令时用：口令泄露的处置语义是「别的设备上的会话一律作废」，
    但**改密的那一次不能把自己踢出登录页**（否则用户改完得重新输一遍新口令，
    还不知道是哪一步错了）。所以这里保留当前 token、清掉其余全部。

    `keep_token` 空 ⇒ 一条都不留（没有「当前会话」这个概念时不该默认全放行）。
    """
    with _FILE_LOCK:
        sessions = _read_sessions()
        keep = token_hash(keep_token) if keep_token else ""
        others = [h for h in sessions if h != keep]
        for h in others:
            sessions.pop(h, None)
        if others:
            _write_sessions(sessions)
        return len(others)


# --------------------------------------------------------------------------
# 登录限速
# --------------------------------------------------------------------------

def _prune(fails: List[float]) -> List[float]:
    cut = time.monotonic() - RATE_WINDOW_SEC
    return [t for t in fails if t > cut]


def rate_limited(ip: str, username: str) -> bool:
    """该 (IP, 用户名) 是否已经超限（≥RATE_MAX_FAILS 次失败还在窗口内）。"""
    with _FAILS_LOCK:
        key = (str(ip or ""), str(username or "").lower())
        fails = _prune(_FAILS.get(key, []))
        if fails:
            _FAILS[key] = fails
        return len(fails) >= RATE_MAX_FAILS


def note_failure(ip: str, username: str) -> None:
    """记一次失败尝试。**成功登录必须调 `clear_failures`**，否则正常用户也会被锁。"""
    with _FAILS_LOCK:
        key = (str(ip or ""), str(username or "").lower())
        _FAILS[key] = _prune(_FAILS.get(key, [])) + [time.monotonic()]


def clear_failures(ip: str, username: str) -> None:
    """登录成功 ⇒ 清空该 key 的失败计数。"""
    with _FAILS_LOCK:
        _FAILS.pop((str(ip or ""), str(username or "").lower()), None)


def reset_rate_limit() -> None:
    """清空全部计数（测试用：跨用例别互相污染）。"""
    with _FAILS_LOCK:
        _FAILS.clear()
