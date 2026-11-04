"""两级 Token 生成、常数时间摘要校验与任务 Token HMAC 派生。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class GeneratedCredentials:
    connection_token: str
    connection_token_hash: str
    session_token_secret: str


def hash_connection_token(token: str) -> str:
    """计算连接 token 的 `sha256:<64位小写十六进制>` 摘要。"""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def generate_credentials() -> GeneratedCredentials:
    """生成 256 位随机连接 token、其 SHA-256 摘要与 256 位随机任务密钥。"""
    conn_token = secrets.token_urlsafe(32)
    conn_hash = hash_connection_token(conn_token)
    session_secret = secrets.token_urlsafe(32)
    return GeneratedCredentials(
        connection_token=conn_token,
        connection_token_hash=conn_hash,
        session_token_secret=session_secret,
    )


def format_generated_credentials(creds: GeneratedCredentials) -> str:
    """格式化 `v2n rec-daemon token` 的标准输出文本。"""
    return (
        "# 请将 CONNECTION_TOKEN 填入手机端 RecNote，\n"
        "# 将 SERVER_AUTH_TOKEN_HASH 与 SESSION_TOKEN_SECRET 填入电脑端 rec-daemon.yaml：\n"
        f"CONNECTION_TOKEN: {creds.connection_token}\n"
        f"SERVER_AUTH_TOKEN_HASH: {creds.connection_token_hash}\n"
        f"SESSION_TOKEN_SECRET: {creds.session_token_secret}"
    )


def verify_connection_token(candidate_token: object, expected_hash: str) -> bool:
    """常数时间校验连接 token 与配置中的 `sha256:<hex>` 摘要。"""
    if not isinstance(candidate_token, str) or not candidate_token:
        return False
    if not isinstance(expected_hash, str) or not expected_hash.startswith("sha256:"):
        return False
    candidate_hash = hash_connection_token(candidate_token)
    return hmac.compare_digest(
        candidate_hash.encode("ascii"), expected_hash.encode("ascii")
    )


def derive_session_token(session_token_secret: str, session_id: str) -> str:
    """按 `base64url(HMAC-SHA256(SESSION_TOKEN_SECRET, session_id))` 派生 43 字符任务 token。"""
    normalized_id = session_id.strip().lower()
    mac = hmac.new(
        session_token_secret.encode("utf-8"),
        normalized_id.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def verify_session_token_for_id(
    candidate_token: object, session_token_secret: str, session_id: str
) -> bool:
    """常数时间比较候选任务 token 是否匹配指定 `session_id`。"""
    if not isinstance(candidate_token, str) or not candidate_token:
        return False
    expected = derive_session_token(session_token_secret, session_id)
    return hmac.compare_digest(
        candidate_token.encode("utf-8"), expected.encode("utf-8")
    )


def rebuild_session_token_map(
    session_token_secret: str, session_ids: list[str] | set[str] | tuple[str, ...]
) -> dict[str, str]:
    """根据已有任务标识集合重算 `session_token -> session_id` 映射。"""
    mapping: dict[str, str] = {}
    for sid in session_ids:
        norm_sid = sid.strip().lower()
        tok = derive_session_token(session_token_secret, norm_sid)
        mapping[tok] = norm_sid
    return mapping
