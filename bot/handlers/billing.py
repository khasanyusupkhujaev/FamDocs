"""Telegram Payments: pre-checkout + successful payment."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message, PreCheckoutQuery

from bot import db
from bot.billing import UPGRADE_EXTRA_SLOTS, parse_invoice_payload
from bot.config import FREE_DOCUMENT_LIMIT

router = Router()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    if FREE_DOCUMENT_LIMIT <= 0:
        await query.answer(ok=False, error_message="Document limit is disabled.")
        return
    data = parse_invoice_payload(query.invoice_payload)
    if not data:
        await query.answer(ok=False, error_message="Invalid or expired offer.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    if FREE_DOCUMENT_LIMIT <= 0:
        return
    sp = message.successful_payment
    if not sp:
        return
    data = parse_invoice_payload(sp.invoice_payload)
    if not data:
        await message.answer("Payment received but the offer was invalid. Contact support.")
        return
    charge_id = sp.telegram_payment_charge_id
    granted = await db.grant_extra_slots_for_payment(
        charge_id,
        data.vault_id,
        UPGRADE_EXTRA_SLOTS,
    )
    if granted:
        cap = FREE_DOCUMENT_LIMIT + await db.get_purchased_extra_slots(data.vault_id)
        await message.answer(
            f"Thank you! Added <b>{UPGRADE_EXTRA_SLOTS}</b> document slots. "
            f"Your vault can now hold up to <b>{cap}</b> documents (including free tier).",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "This payment was already applied to your vault. "
            "Open the Mini App to upload.",
            parse_mode="HTML",
        )
