"""Map Telegram Web App / chat context to a document vault id (family_chat_id)."""

from __future__ import annotations

import json
from typing import Any

from bot import db


async def vault_id_from_init_vals(vals: dict[str, str]) -> tuple[int, str]:
    """
    Returns (vault_id, mode) where mode is 'group' or 'private'.
    Group/supergroup: vault is the chat id (shared chat vault, no family invites).
    Private: vault is resolved via vault_members (family sharing).
    """
    if vals.get("chat"):
        chat: dict[str, Any] = json.loads(vals["chat"])
        cid = int(chat["id"])
        ctype = (chat.get("type") or "").lower()
        if ctype in ("group", "supergroup", "channel"):
            return cid, "group"
    if not vals.get("user"):
        raise ValueError("no user in init data")
    user: dict[str, Any] = json.loads(vals["user"])
    uid = int(user["id"])
    vid = await db.get_vault_for_user(uid)
    return vid, "private"


async def vault_id_from_message(message: Any) -> int:
    """Bot message: group chat uses chat id; private chat uses shared vault for the user."""
    chat = message.chat
    if chat.type in ("group", "supergroup", "channel"):
        return int(chat.id)
    uid = int(message.from_user.id) if message.from_user else int(chat.id)
    return await db.get_vault_for_user(uid)
