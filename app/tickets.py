"""一次性启用凭证的生成、哈希与时效工具。"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Tuple

# 凭证默认 15 分钟有效；Docker Compose 或本地可用环境变量覆盖
DEFAULT_TTL_SECONDS = int(os.environ.get("COAL_TICKET_TTL_SECONDS", "900"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def expires_at(ttl_seconds: int, now: str | None = None) -> str:
    base = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")


def is_expired(value: str, now: str | None = None) -> bool:
    try:
        deadline = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return True
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    current = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return deadline <= current


def new_ticket() -> Tuple[str, str]:
    """返回 (明文凭证, 存库用的 sha256 摘要)。明文只在预检响应中出现一次。"""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
