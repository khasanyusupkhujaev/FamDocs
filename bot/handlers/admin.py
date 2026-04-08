"""Admin-only Telegram commands: statistics and manual slot grants."""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot import db
from bot.config import ADMIN_TELEGRAM_IDS, ADMIN_USERNAMES, FREE_DOCUMENT_LIMIT

log = logging.getLogger(__name__)

router = Router()

# Merged numeric ids (from FAMDOC_ADMIN_TELEGRAM_IDS + resolved FAMDOC_ADMIN_USERNAMES).
RESOLVED_ADMIN_IDS: frozenset[int] = frozenset(ADMIN_TELEGRAM_IDS)


async def resolve_admin_users(bot: Bot) -> None:
    """Resolve @usernames via Bot API; merge with FAMDOC_ADMIN_TELEGRAM_IDS."""
    global RESOLVED_ADMIN_IDS
    ids: set[int] = set(ADMIN_TELEGRAM_IDS)
    for name in ADMIN_USERNAMES:
        uname = (name or "").strip().lstrip("@")
        if not uname:
            continue
        try:
            chat = await bot.get_chat(f"@{uname}")
            tid = int(chat.id)
            ids.add(tid)
            log.info("Admin username @%s resolved to user id %s", uname, tid)
        except Exception as e:
            log.warning("Could not resolve admin username @%s: %s", uname, e)
    RESOLVED_ADMIN_IDS = frozenset(ids)
    if not RESOLVED_ADMIN_IDS:
        log.warning(
            "No admin users configured (empty FAMDOC_ADMIN_TELEGRAM_IDS and "
            "FAMDOC_ADMIN_USERNAMES resolution failed or unset)."
        )
    else:
        log.info("Admin allowlist: %s user id(s)", len(RESOLVED_ADMIN_IDS))


def _is_admin(uid: int | None) -> bool:
    return bool(RESOLVED_ADMIN_IDS) and uid is not None and uid in RESOLVED_ADMIN_IDS


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "<b>FamDoc admin</b>\n"
        "/stats — registered users and upload activity\n"
        "/grant &lt;telegram_user_id&gt; &lt;slots&gt; — add document slots after you "
        "verify bank payment (user puts <code>FAMDOC-&lt;their id&gt;</code> in the "
        "transfer comment).",
        parse_mode="HTML",
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None):
        return
    s = await db.admin_statistics()
    cap_note = ""
    if FREE_DOCUMENT_LIMIT <= 0:
        cap_note = (
            "\n<i>Per-vault document cap is off "
            "(FAMDOC_FREE_DOCUMENT_LIMIT=0).</i>"
        )
    await message.answer(
        "<b>Statistics</b>\n"
        f"Users (vault memberships): <b>{s['users_registered']}</b>\n"
        f"Vaults with at least one document: <b>{s['vaults_with_uploads']}</b>\n"
        f"Total documents: <b>{s['total_documents']}</b>"
        + cap_note,
        parse_mode="HTML",
    )


@router.message(Command("grant"))
async def cmd_grant(message: Message) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer(
            "Usage: <code>/grant &lt;telegram_user_id&gt; &lt;slots&gt;</code>\n"
            "Example: <code>/grant 123456789 10</code>",
            parse_mode="HTML",
        )
        return
    try:
        target_uid = int(parts[1])
        slots = int(parts[2])
    except ValueError:
        await message.answer("telegram_user_id and slots must be whole numbers.")
        return
    if slots <= 0 or slots > 10_000:
        await message.answer("Slots must be between 1 and 10000.")
        return
    vault_id = await db.get_vault_for_user(target_uid)
    await db.add_extra_slots(vault_id, slots)
    await db.delete_manual_payment_claim(target_uid)
    extra = await db.get_purchased_extra_slots(vault_id)
    if FREE_DOCUMENT_LIMIT > 0:
        cap = FREE_DOCUMENT_LIMIT + extra
        cap_txt = f"up to <b>{cap}</b> documents (incl. free tier)"
    else:
        cap_txt = "no document cap (limit disabled in config)"
    await message.answer(
        f"Granted <b>{slots}</b> extra slot(s) to Telegram user <code>{target_uid}</code> "
        f"(vault <code>{vault_id}</code>).\n"
        f"Total purchased extra slots for this vault: <b>{extra}</b>. Effective limit: {cap_txt}.",
        parse_mode="HTML",
    )
