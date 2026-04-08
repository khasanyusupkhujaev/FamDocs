"""Thin Telegram Bot API helpers (no aiogram Bot instance required)."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

from bot.config import BOT_TOKEN


def _get_chat_sync(chat_id: int) -> dict[str, Any] | None:
    if not BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChat?chat_id={chat_id}"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            body = json.load(r)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    if not body.get("ok") or not body.get("result"):
        return None
    return body["result"]


async def get_chat_info(chat_id: int) -> dict[str, Any] | None:
    """Private user id or group id — returns Telegram Chat object or None."""
    return await asyncio.to_thread(_get_chat_sync, chat_id)


def format_chat_display_name(ch: dict[str, Any]) -> str:
    """Human-readable label for family list."""
    uid = ch.get("id")
    fn = (ch.get("first_name") or "").strip()
    ln = (ch.get("last_name") or "").strip()
    title = (ch.get("title") or "").strip()
    un = (ch.get("username") or "").strip()
    if title:
        base = title
    else:
        base = f"{fn} {ln}".strip() or (fn or ln) or ""
    if not base:
        base = f"User {uid}" if uid is not None else "User"
    if un:
        return f"{base} (@{un})"
    return base


def primary_name_from_chat(ch: dict[str, Any] | None, user_id: int) -> str:
    """First line: real name or title, no @username (shown separately)."""
    if not ch:
        return f"User {user_id}"
    title = (ch.get("title") or "").strip()
    if title:
        return title
    fn = (ch.get("first_name") or "").strip()
    ln = (ch.get("last_name") or "").strip()
    base = f"{fn} {ln}".strip() or fn or ln
    if base:
        return base
    return f"User {user_id}"


def telegram_username_from_chat(ch: dict[str, Any] | None) -> str | None:
    if not ch:
        return None
    u = (ch.get("username") or "").strip()
    return u or None
