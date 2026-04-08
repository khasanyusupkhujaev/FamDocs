"""Telegram Payments: invoice payload + effective document caps."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import NamedTuple

from aiogram import Bot
from aiogram.methods import CreateInvoiceLink
from aiogram.types import LabeledPrice

from bot import db
from bot.config import (
    BOT_TOKEN,
    FREE_DOCUMENT_LIMIT,
    PAYMENT_PROVIDER_TOKEN,
    UPGRADE_AMOUNT_MINOR,
    UPGRADE_CURRENCY,
    UPGRADE_EXTRA_SLOTS,
    UPGRADE_INVOICE_DESCRIPTION,
    UPGRADE_INVOICE_TITLE,
    UPGRADE_STARS,
)


class PayloadData(NamedTuple):
    vault_id: int
    slots: int
    ts: int


def _sign(vault_id: int, slots: int, ts: int) -> str:
    raw = f"{vault_id}:{slots}:{ts}".encode()
    return hmac.new(BOT_TOKEN.encode(), raw, hashlib.sha256).hexdigest()[:16]


def build_invoice_payload(vault_id: int, slots: int) -> str:
    ts = int(time.time())
    sig = _sign(vault_id, slots, ts)
    s = f"fd:v{vault_id}:s{slots}:t{ts}:h{sig}"
    if len(s.encode("utf-8")) > 128:
        raise ValueError("invoice payload too long")
    return s


def parse_invoice_payload(payload: str) -> PayloadData | None:
    if not payload.startswith("fd:"):
        return None
    try:
        parts = payload[3:].split(":")
        kv = {}
        for p in parts:
            if len(p) < 2 or p[0] not in "vsth":
                return None
            kv[p[0]] = p[1:]
        vault_id = int(kv["v"])
        slots = int(kv["s"])
        ts = int(kv["t"])
        sig = kv["h"]
    except (KeyError, ValueError):
        return None
    if sig != _sign(vault_id, slots, ts):
        return None
    if abs(int(time.time()) - ts) > 86400:
        return None
    if slots != UPGRADE_EXTRA_SLOTS:
        return None
    return PayloadData(vault_id=vault_id, slots=slots, ts=ts)


def billing_uses_stars() -> bool:
    return not PAYMENT_PROVIDER_TOKEN


def upgrade_price_minor_and_currency() -> tuple[int, str]:
    if billing_uses_stars():
        return UPGRADE_STARS, "XTR"
    return UPGRADE_AMOUNT_MINOR, UPGRADE_CURRENCY


def upgrade_price_label() -> str:
    minor, cur = upgrade_price_minor_and_currency()
    if cur == "XTR":
        return f"{minor} ⭐"
    return f"{cur} {minor / 100:.2f}"


async def effective_document_cap(vault_id: int) -> int | None:
    if FREE_DOCUMENT_LIMIT <= 0:
        return None
    extra = await db.get_purchased_extra_slots(vault_id)
    return FREE_DOCUMENT_LIMIT + extra


async def create_upgrade_invoice_link(vault_id: int) -> str:
    payload = build_invoice_payload(vault_id, UPGRADE_EXTRA_SLOTS)
    minor, currency = upgrade_price_minor_and_currency()
    provider = "" if billing_uses_stars() else PAYMENT_PROVIDER_TOKEN
    bot = Bot(token=BOT_TOKEN)
    try:
        link = await bot(
            CreateInvoiceLink(
                title=UPGRADE_INVOICE_TITLE[:32],
                description=UPGRADE_INVOICE_DESCRIPTION[:255],
                payload=payload,
                currency=currency,
                prices=[
                    LabeledPrice(
                        label=UPGRADE_INVOICE_TITLE[:32],
                        amount=minor,
                    )
                ],
                provider_token=provider,
            )
        )
        return link
    finally:
        await bot.session.close()
