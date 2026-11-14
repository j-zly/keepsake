"""凭据的**可还原存档**（Fernet 对称加密）—— 2026-11-14 用户拍板：
「肯定是不能换发啊，是直接能复制」。

# 这个模块存在的原因
注册表原本只存 `sha256(凭据)`：校验路径快且不可逆，但**明文一经签发就再也取不出来**
—— 用户当时没复制成功，唯一出路是「重新生成凭据」（换发），而换发会让那个代理**当场断线**
（旧钥匙立刻失效，它自己配置里的串变成废串）。这条出路对用户是不可接受的。

# 本模块的做法（也是它的边界）
1. **校验面一个都不动**：`credential_hash`（sha256）照旧是唯一的验证依据，
   MCP / 读写闸仍然只比 hash —— 本模块不参与任何鉴权路径，只在治理面「取出明文」时用。
2. 签发时**额外**存一份 Fernet 密文（`credential_enc`），让控制台在登录后能把明文再取出来。
3. 主密钥**只从环境来**，两个来源按优先级：
   * `KEEPSAKE_CREDENTIAL_KEY` —— 直接给 Fernet key（urlsafe-base64 的 32 字节）；
   * `KEEPSAKE_CREDENTIAL_KEY_FILE` —— 给一个 600 权限的文件路径，内容就是那把 key。
   密钥**不进库、不进 git、不进审计、不进任何返回值**；本模块的任何函数都不返回密钥材料，
   诊断只回「有没有 / 从哪个来源来」。
4. 🔴 **fail-closed，不静默降级成明文入库**：没有主密钥 ⇒ `encrypt()` 返回 `None`
   （签发照旧成功，只是这条不可还原，面板会显示「不可还原」并给出回填入口）；
   密钥换了 / 密文坏了 ⇒ `decrypt()` 返回 `None`（报「不可还原」，不崩、不猜）。
5. 🔴 **解密失败与「本来就没存」要分得开**：调用方据 `reveal_credential()` 的 `why`
   区分 `not_recoverable`（老行，机制之前签发的）/ `key_missing`（服务端没配主密钥）/
   `key_mismatch`（密文在但解不开 —— 换过密钥）⇒ 三种后果的处置动作完全不同，
   混成一句「取不出来」会把运维引去查错方向。

# 已知代价（写在这里，不是藏在注释里）
数据库导出 + 主密钥**同时**泄漏 ⇒ 存量凭据可批量还原。单泄漏都不够：
只拿到库 ⇒ 是密文；只拿到密钥 ⇒ 没有密文。要收窄这层，正解是 KMS/HSM 或按代理派生密钥，
本实现是「单机自管」那一档，够用且比明文入库强一个量级。
"""

from __future__ import annotations

import os
import re
from typing import Optional

#: 环变量名（两个来源的名字；值一律不落日志）
KEY_ENV = "KEEPSAKE_CREDENTIAL_KEY"
KEY_FILE_ENV = "KEEPSAKE_CREDENTIAL_KEY_FILE"

#: 「真正的赋值形状」：行首是大写标识符 + `=`，值取到行尾（可含 `=`，base64 填充要紧）。
#: 只用来识别 systemd EnvironmentFile 那种 `KEY=value` 文件；裸 key 一行不匹配 ⇒ 整行当值。
_ASSIGN_RE = re.compile(r"^(?P<name>[A-Z_][A-Z0-9_]*)=(?P<value>.*)$")

#: key 来源的三档诊断值（只用于「密钥从哪来」的表述，绝不含 key 本身）
SOURCE_ENV = "env"
SOURCE_FILE = "file"
SOURCE_NONE = ""


def _raw_key() -> tuple[str, str]:
    """按优先级取出主密钥原文与其来源。

    返回 `(key, source)`；没有可用密钥 ⇒ `("", SOURCE_NONE)`。
    🔴 文件读失败（不存在 / 没权限 / 空文件）按「没有密钥」处理，不抛 ——
    抛出去会把签发这条路一起打死，而签发本身不需要可还原。
    """
    key = (os.environ.get(KEY_ENV) or "").strip()
    if key:
        return key, SOURCE_ENV
    path = (os.environ.get(KEY_FILE_ENV) or "").strip()
    if not path:
        return "", SOURCE_NONE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # 🔴 只有**真正的赋值形状**（`NAME=value`，NAME 是大写字母/下划线）
                #    才切一刀 —— Fernet key 本身是 urlsafe-base64、**结尾带 `=` 填充**，
                #    拿"行里含 `=`"当判据会把裸 key 从第一个 `=` 处截断成空串
                #    （2026-11-14 实踩：available() 报 False 而 key_source() 报 file，
                #    表现就是"密文列一直是空"）。裸 key 一行必须整行当值用。
                m = _ASSIGN_RE.match(line)
                if m:
                    line = m.group("value")
                return line.strip().strip('"').strip("'"), SOURCE_FILE
    except OSError:
        return "", SOURCE_NONE
    return "", SOURCE_NONE


def available() -> bool:
    """服务端当前**能不能**做可还原存档（只答是/否，不答密钥是什么）。"""
    key, _ = _raw_key()
    return bool(key)


def key_source() -> str:
    """诊断用：主密钥来自哪档（`env` / `file` / 空 = 没配）。"""
    _, source = _raw_key()
    return source


def generate_key() -> str:
    """给运维生成一把新主密钥（一次性打印用；不入库、不进审计）。"""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode("ascii")


def encrypt(plaintext: str) -> Optional[str]:
    """明文 → Fernet 密文（字符串，可直接进 text 列）。

    没有主密钥、或加密封本身坏了 ⇒ **返回 None**（调用方按「这条不可还原」处理）。
    空串也返回 None：没有内容可存，存个空密文只会让面板显示成「可还原但取不出」。
    """
    if not plaintext:
        return None
    key, _ = _raw_key()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet

        return Fernet(key.encode("ascii", errors="ignore") if isinstance(key, str) else key
                      ).encrypt(plaintext.encode("utf-8")).decode("ascii")
    except Exception:      # noqa: BLE001 —— 密钥格式不对等：按「不可还原」处理，别打死签发
        return None


def decrypt(token: Optional[str]) -> Optional[str]:
    """密文 → 明文。解不开（没密钥 / 换过密钥 / 密文损坏）⇒ None。"""
    if not token:
        return None
    key, _ = _raw_key()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet

        return Fernet(key.encode("ascii", errors="ignore") if isinstance(key, str) else key
                      ).decrypt(token.encode("ascii", errors="ignore")).decode("utf-8")
    except Exception:      # noqa: BLE001 —— InvalidToken 等，一律「解不开」
        return None


def decrypt_with_reason(token: Optional[str]) -> tuple[Optional[str], str]:
    """解密并给出**为什么解不开**（治理面要把三种处置分开）。

    返回 `(明文或 None, why)`，why ∈ `ok` / `no_ciphertext` / `key_missing` / `key_mismatch`。
    `key_mismatch` 的判据：密文形状像 Fernet 且密钥在场，但 `decrypt` 失败 ⇒
    要么换过密钥、要么密文被改坏 —— 两种都只能靠「回填已有凭据」恢复，不是重试能解决的。
    """
    if not token:
        return None, "no_ciphertext"
    if not available():
        return None, "key_missing"
    out = decrypt(token)
    return (out, "ok") if out else (None, "key_mismatch")
