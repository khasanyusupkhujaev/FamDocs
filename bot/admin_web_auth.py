"""Telegram Login widget verification and signed cookie session for the /admin web UI."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

# Only these query keys participate in the Telegram data-check-string (ignore extra params).
_TELEGRAM_LOGIN_KEYS = frozenset(
    {"auth_date", "first_name", "id", "last_name", "photo_url", "username"}
)

ADMIN_SESSION_COOKIE = "famdoc_admin"
SESSION_TTL_SECONDS = 604800  # 7 days


def _session_signing_key(bot_token: str) -> bytes:
    return hashlib.sha256((bot_token + "|famdoc_admin_web").encode()).digest()


def verify_telegram_login_query(
    params: dict[str, str],
    *,
    bot_token: str,
    max_age_seconds: int = 86400,
) -> tuple[int, str | None] | None:
    """
    Verify Telegram Login Widget callback query parameters.
    Returns (telegram_user_id, username_or_none) or None.
    """
    check_hash = (params.get("hash") or "").strip()
    if not check_hash or not bot_token:
        return None
    try:
        auth_date = int(params.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date <= 0 or int(time.time()) - auth_date > max_age_seconds:
        return None
    parts: list[str] = []
    for key in sorted(_TELEGRAM_LOGIN_KEYS):
        if key not in params:
            continue
        val = params[key]
        if val is None or val == "":
            continue
        parts.append(f"{key}={val}")
    data_check_string = "\n".join(parts)
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    digest = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(digest, check_hash):
        return None
    try:
        uid = int(params["id"])
    except (KeyError, ValueError):
        return None
    if uid < 1:
        return None
    un = (params.get("username") or "").strip() or None
    return uid, un


def sign_admin_session(
    bot_token: str,
    user_id: int,
    username: str | None,
    *,
    ttl_sec: int = SESSION_TTL_SECONDS,
) -> str:
    exp = int(time.time()) + ttl_sec
    payload: dict[str, Any] = {
        "uid": user_id,
        "un": (username or ""),
        "exp": exp,
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    b64 = base64.urlsafe_b64encode(body).decode().rstrip("=")
    sig = hmac.new(_session_signing_key(bot_token), body, hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def verify_admin_session_cookie(
    bot_token: str, raw: str | None
) -> tuple[int, str | None] | None:
    if not raw or "." not in raw or not bot_token:
        return None
    b64, sig = raw.rsplit(".", 1)
    pad = "=" * (-len(b64) % 4)
    try:
        body = base64.urlsafe_b64decode(b64 + pad)
    except (ValueError, OSError):
        return None
    expect = hmac.new(_session_signing_key(bot_token), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        payload = json.loads(body.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    try:
        uid = int(payload.get("uid", 0))
        exp = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return None
    if uid < 1 or exp < int(time.time()):
        return None
    un = (payload.get("un") or "").strip() or None
    return uid, un
