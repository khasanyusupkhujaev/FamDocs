"""Validate Telegram Mini App initData (https://core.telegram.org/bots/webapps)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86400,
) -> dict[str, str]:
    if not init_data or not bot_token:
        raise ValueError("missing init data or token")
    vals: dict[str, str] = dict(parse_qsl(init_data, keep_blank_values=True))
    recv_hash = vals.pop("hash", None)
    if not recv_hash:
        raise ValueError("missing hash")
    try:
        auth_date = int(vals.get("auth_date", "0"))
    except ValueError as e:
        raise ValueError("bad auth_date") from e
    if max_age_seconds > 0 and time.time() - auth_date > max_age_seconds:
        raise ValueError("stale auth_date")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(vals.items()))
    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    sig = hmac.new(
        secret_key,
        check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, recv_hash):
        raise ValueError("invalid hash")
    return vals


def family_chat_id_from_init(vals: dict[str, str]) -> int:
    """Private chat: user id. Group/supergroup Web App: chat id from initData."""
    if vals.get("chat"):
        chat: dict[str, Any] = json.loads(vals["chat"])
        return int(chat["id"])
    if not vals.get("user"):
        raise ValueError("no user in init data")
    user: dict[str, Any] = json.loads(vals["user"])
    return int(user["id"])
