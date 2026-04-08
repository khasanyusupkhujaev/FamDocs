"""Apply PayTech (Payme/Click) payments to FamDoc vault entitlements (sync sqlite)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from bot.config import DB_PATH


def grant_paytech_payment(
    ledger_charge_id: str,
    vault_id: int,
    slots: int,
) -> bool:
    """
    Idempotent grant using the same tables as Telegram Stars/card payments.
    Returns True if this charge was new and slots were added.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO payment_ledger
                (telegram_payment_charge_id, vault_id, slots_granted, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (ledger_charge_id, vault_id, slots, now),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        conn.execute(
            """
            INSERT INTO vault_entitlements (vault_id, extra_slots, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(vault_id) DO UPDATE SET
                extra_slots = vault_entitlements.extra_slots + excluded.extra_slots,
                updated_at = excluded.updated_at
            """,
            (vault_id, slots, now),
        )
        conn.commit()
        return True
    finally:
        conn.close()
