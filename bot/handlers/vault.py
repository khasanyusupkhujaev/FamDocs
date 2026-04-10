from __future__ import annotations

import asyncio
import html
from datetime import datetime, timezone
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from bot import db
from bot.billing import effective_document_cap
from bot.branding import telegram_logo_input
from bot.config import WEBAPP_PUBLIC_URL
from bot.preview import build_preview_jpeg
from bot.keyboards import (
    CATEGORY_EMOJI,
    CATEGORY_LABELS,
    JOIN_FAMILY_TEXT,
    category_sidebar_inline,
    document_actions_inline,
    invite_wait_keyboard,
    main_reply_keyboard,
)
from bot.invite_parse import extract_join_token
from bot.states import VaultStates
from bot.storage import read_stored_file, save_upload
from bot.vault_resolve import vault_id_from_message

router = Router()


async def _apply_invite_token(message: Message, state: FSMContext, token: str) -> None:
    if not message.from_user:
        return
    ok, reason = await db.accept_invite(token, message.from_user.id)
    if ok:
        if reason == "already_member":
            txt = "You're already in this family vault."
        else:
            txt = (
                "Welcome! You now share the same document vault. "
                "Open the Mini App from the menu to browse and upload."
            )
    else:
        err = {
            "invalid": "This invite link is invalid or was already used.",
            "expired": "This invite has expired. Ask for a new link.",
            "already_in_family": (
                "You're already in another family vault. "
                "Only personal vaults can accept a new invite."
            ),
        }.get(reason, "Could not accept invite.")
        txt = err
    await message.answer(txt, parse_mode="HTML", reply_markup=main_reply_keyboard())
    await state.set_state(VaultStates.main)
    await state.update_data(category=None)


def _format_category_view(category: str, rows: list) -> str:
    cat_title = CATEGORY_LABELS.get(category, category)
    label = f"{CATEGORY_EMOJI.get(category, '📁')} <b>{html.escape(cat_title)}</b>"
    lines = [label, ""]
    if not rows:
        lines.append("<i>No files yet. Send a photo or document to add one.</i>")
    else:
        lines.append(
            f"<i>Showing up to 15 most recent ({len(rows)} total):</i>\n"
        )
        for r in rows[:15]:
            ts = r["uploaded_at"][:19].replace("T", " ")
            orig = html.escape(r["original_filename"])
            lines.append(f"• <code>{r['id']}</code> — {orig} <i>({ts} UTC)</i>")
    return "\n".join(lines)


def _format_all_view(rows: list, counts: dict[str, int]) -> str:
    lines = ["📚 <b>All documents</b>", ""]
    if counts:
        parts = [
            f"{CATEGORY_EMOJI.get(k, '📁')} {html.escape(CATEGORY_LABELS.get(k, k))}: <b>{v}</b>"
            for k, v in sorted(counts.items())
        ]
        lines.append(" | ".join(parts))
        lines.append("")
    if not rows:
        lines.append("<i>Nothing stored yet.</i>")
    else:
        lines.append(f"<i>Latest 20 of {len(rows)}:</i>\n")
        for r in rows[:20]:
            cat = r["category"]
            emoji = CATEGORY_EMOJI.get(cat, "📁")
            ts = r["uploaded_at"][:19].replace("T", " ")
            orig = html.escape(r["original_filename"])
            lines.append(
                f"• <code>{r['id']}</code> {emoji} {orig} <i>({ts})</i>"
            )
    return "\n".join(lines)


async def _show_folder(message: Message, category: str) -> None:
    family = await vault_id_from_message(message)
    rows = await db.list_documents(family, category)
    text = _format_category_view(category, rows)
    await message.answer(
        text,
        reply_markup=category_sidebar_inline(current_category=category),
        parse_mode="HTML",
    )


async def _show_all_documents(message: Message) -> None:
    family = await vault_id_from_message(message)
    rows = await db.list_documents(family, None)
    counts = await db.count_by_category(family)
    text = _format_all_view(rows, counts)
    await message.answer(
        text,
        reply_markup=category_sidebar_inline(current_category=None),
        parse_mode="HTML",
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        arg = parts[1].strip()
        if arg.startswith("join_") and message.from_user:
            await _apply_invite_token(message, state, arg[5:])
            return

    await state.set_state(VaultStates.main)
    await state.update_data(category=None)
    mini = (
        "Tap the blue <b>FamDocs</b> button next to the message field (menu) to open the "
        "<b>Mini App</b> full screen — same as other Telegram shop / service bots.\n\n"
        if WEBAPP_PUBLIC_URL
        else ""
    )
    intro = (
        "Welcome to <b>FamDoc</b> — your family document vault.\n\n"
        f"{mini}"
        "Use the blue <b>FamDocs</b> menu button for the full experience (browse, search, upload, tags).\n\n"
        "To send a <b>photo or file</b> from chat: run <code>/browse</code>, pick a folder with the "
        "<b>buttons under the message</b>, then send your file.\n\n"
        "Commands: <code>/help</code>, <code>/browse</code>, <code>/join</code>."
    )
    logo = await telegram_logo_input()
    kb = main_reply_keyboard()
    if logo:
        await message.answer_photo(logo)
    await message.answer(intro, reply_markup=kb, parse_mode="HTML")


async def _prompt_join_family(message: Message, state: FSMContext) -> None:
    await state.set_state(VaultStates.waiting_invite_link)
    await message.answer(
        "Paste your <b>invite link</b> (full <code>https://t.me/...</code>), "
        "or the text <code>join_...</code>, or <b>only the secret code</b> "
        "(long letters/numbers from the link).\n\n"
        "Use <code>/join</code> any time. Send <code>/cancel</code> or tap "
        "<b>❌ Cancel</b> to stop.",
        parse_mode="HTML",
        reply_markup=invite_wait_keyboard(),
    )


@router.message(Command("join"))
@router.message(F.text == JOIN_FAMILY_TEXT)
async def cmd_join(message: Message, state: FSMContext) -> None:
    await _prompt_join_family(message, state)


@router.message(StateFilter(VaultStates.waiting_invite_link), Command("cancel"))
@router.message(StateFilter(VaultStates.waiting_invite_link), F.text == "❌ Cancel")
async def cancel_join(message: Message, state: FSMContext) -> None:
    await state.set_state(VaultStates.main)
    await message.answer(
        "Cancelled. Use <code>/join</code> when you have a link.",
        parse_mode="HTML",
        reply_markup=main_reply_keyboard(),
    )


@router.message(StateFilter(VaultStates.waiting_invite_link), F.text)
async def process_pasted_invite(message: Message, state: FSMContext) -> None:
    token = extract_join_token(message.text or "")
    if not token:
        await message.answer(
            "I couldn't read that invite. Paste the full <code>t.me</code> link, "
            "or <code>join_…</code>, or the <b>secret code</b> only.",
            parse_mode="HTML",
        )
        return
    await _apply_invite_token(message, state, token)


@router.message(StateFilter(VaultStates.waiting_invite_link), F.photo | F.document)
async def invite_waiting_not_file(message: Message) -> None:
    await message.answer(
        "Please paste the invite as <b>text</b> (the <code>t.me/...</code> link), "
        "not a photo or file.",
        parse_mode="HTML",
    )


@router.message(Command("help"))
@router.message(F.text == "ℹ️ Help")
async def cmd_help(message: Message) -> None:
    mini_line = (
        "• Open the <b>Mini App</b> with the blue <b>FamDocs</b> button next to the message field.\n"
        if WEBAPP_PUBLIC_URL
        else ""
    )
    await message.answer(
        mini_line
        + "• <code>/browse</code> — pick a folder (inline buttons), then send a photo or file.\n"
        "• Tap <b>Download</b> on a message after upload, or <code>/get &lt;id&gt;</code>.\n"
        "• <code>/join</code> — paste an invite to share someone else's vault.\n"
        "• In a <b>group</b>, the whole group shares one vault (this chat).",
        parse_mode="HTML",
        reply_markup=main_reply_keyboard(),
    )


@router.message(Command("browse"))
async def cmd_browse(message: Message, state: FSMContext) -> None:
    await state.update_data(category=None)
    await _show_all_documents(message)


@router.callback_query(F.data.startswith("cat:"))
async def folder_from_callback(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    raw = query.data or ""
    key = raw.split(":", 1)[1]
    if not query.message:
        return

    family = await vault_id_from_message(query.message)
    if key == "__all__":
        await state.update_data(category=None)
        rows = await db.list_documents(family, None)
        counts = await db.count_by_category(family)
        text = _format_all_view(rows, counts)
        await query.message.edit_text(
            text,
            reply_markup=category_sidebar_inline(current_category=None),
            parse_mode="HTML",
        )
        return

    await state.update_data(category=key)
    rows = await db.list_documents(family, key)
    text = _format_category_view(key, rows)
    await query.message.edit_text(
        text,
        reply_markup=category_sidebar_inline(current_category=key),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("dl:"))
async def download_doc(query: CallbackQuery, bot: Bot) -> None:
    if not query.message:
        await query.answer("No message context.", show_alert=True)
        return
    family = await vault_id_from_message(query.message)
    try:
        doc_id = int((query.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("Invalid.", show_alert=True)
        return
    meta = await db.get_document(family, doc_id)
    if not meta:
        await query.answer("File not found.", show_alert=True)
        return
    data = await read_stored_file(family, meta["stored_filename"])
    if not data:
        await query.answer("Missing on disk.", show_alert=True)
        return
    await query.answer()
    await bot.send_document(
        chat_id=family,
        document=BufferedInputFile(
            data,
            filename=meta["original_filename"],
        ),
        caption=f"#{doc_id} · {CATEGORY_LABELS.get(meta['category'], meta['category'])}",
    )


@router.message(Command("get"))
async def get_by_id(message: Message, bot: Bot) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Usage: <code>/get &lt;id&gt;</code>", parse_mode="HTML")
        return
    doc_id = int(parts[1].strip())
    family = await vault_id_from_message(message)
    meta = await db.get_document(family, doc_id)
    if not meta:
        await message.answer("Not found.")
        return
    data = await read_stored_file(family, meta["stored_filename"])
    if not data:
        await message.answer("File missing on server.")
        return
    await bot.send_document(
        chat_id=family,
        document=BufferedInputFile(
            data,
            filename=meta["original_filename"],
        ),
        caption=f"#{doc_id} · {CATEGORY_LABELS.get(meta['category'], meta['category'])}",
    )


def _original_name_from_document(message: Message) -> str:
    if message.document and message.document.file_name:
        return message.document.file_name
    if message.photo:
        return f"photo_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jpg"
    return "upload.bin"


@router.message(StateFilter(VaultStates.main), F.photo | F.document)
async def save_upload_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    category = data.get("category")
    if not category:
        await message.answer(
            "Pick a folder first: open the <b>Mini App</b> or run <code>/browse</code> "
            "and tap a category, then send your file.",
            parse_mode="HTML",
            reply_markup=main_reply_keyboard(),
        )
        return

    mime = None
    size = None
    if message.document:
        file_obj = message.document
        mime = message.document.mime_type
        size = message.document.file_size
    elif message.photo:
        file_obj = message.photo[-1]
        mime = "image/jpeg"
        size = message.photo[-1].file_size
    else:
        return

    family = await vault_id_from_message(message)
    cap = await effective_document_cap(family)
    if cap is not None:
        n_docs = await db.count_documents(family)
        if n_docs >= cap:
            await message.answer(
                f"You've reached your limit of <b>{cap}</b> documents. "
                "Open the <b>FamDocs</b> Mini App to buy more slots or delete a document.",
                parse_mode="HTML",
                reply_markup=main_reply_keyboard(),
            )
            return

    if await db.vault_has_crypto_password(family):
        await message.answer(
            "This vault uses <b>end-to-end encryption</b>. "
            "Upload files from the <b>FamDocs Mini App</b> so they are encrypted on your device.",
            parse_mode="HTML",
            reply_markup=main_reply_keyboard(),
        )
        return

    buf = await bot.download(file=file_obj)
    raw = buf.read()
    orig = _original_name_from_document(message)
    stored = await save_upload(family, orig, raw)
    new_id = await db.add_document(
        family_chat_id=family,
        category=category,
        stored_filename=stored,
        original_filename=orig,
        mime_type=mime,
        file_size=size,
    )
    jpeg = await asyncio.to_thread(build_preview_jpeg, raw, mime)
    if jpeg:
        pstored = await save_upload(family, "preview.jpg", jpeg)
        await db.set_document_preview(family, new_id, pstored)
    label = CATEGORY_LABELS.get(category, category)
    await message.answer(
        f"Saved to <b>{html.escape(label)}</b> (id <code>{new_id}</code>).",
        parse_mode="HTML",
        reply_markup=document_actions_inline(new_id),
    )


@router.message(F.photo | F.document)
async def upload_without_fsm(message: Message) -> None:
    await message.answer(
        "Tap <code>/start</code> first, then use the Mini App or <code>/browse</code> "
        "to pick a folder before sending a file.",
        parse_mode="HTML",
        reply_markup=main_reply_keyboard(),
    )


@router.message(StateFilter(VaultStates.main), F.text)
async def remind_pick_folder(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("category"):
        return
    if message.text and message.text.startswith("/"):
        return
    await message.answer(
        "Open the <b>FamDocs</b> Mini App or run <code>/browse</code> to pick a folder, "
        "or use <code>/help</code>.",
        reply_markup=main_reply_keyboard(),
        parse_mode="HTML",
    )
