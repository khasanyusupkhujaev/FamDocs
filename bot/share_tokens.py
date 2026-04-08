"""Time-limited HMAC links for /api/shared/* (Telegram share URL, etc.)."""

from __future__ import annotations

import hashlib
import hmac
import time

from bot.config import BOT_TOKEN

_SHARE_KEY = hmac.new(b"famdoc_file_share_v1", BOT_TOKEN.encode(), hashlib.sha256).digest()


def sign_file_share(doc_id: int, vault_id: int, exp: int) -> str:
    msg = f"{doc_id}:{vault_id}:{exp}".encode()
    return hmac.new(_SHARE_KEY, msg, hashlib.sha256).hexdigest()


def verify_file_share(doc_id: int, vault_id: int, exp: int, sig: str) -> bool:
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_i < int(time.time()):
        return False
    expected = sign_file_share(doc_id, vault_id, exp_i)
    return hmac.compare_digest(expected, sig)
