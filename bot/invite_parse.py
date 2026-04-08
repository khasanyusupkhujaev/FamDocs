"""Parse Telegram invite links or raw invite tokens (from secrets.token_urlsafe)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# token_urlsafe(24) → ~32 chars, [A-Za-z0-9_-]
_RAW_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def extract_join_token(text: str) -> str | None:
    """
    Returns the secret token to verify with db.accept_invite (plaintext, before hashing).

    Accepts:
    - Full t.me / telegram.me link with ?start=join_<token>
    - Literal join_<token>
    - Raw token only (url-safe string, one line), e.g. from copying the link fragment
    """
    raw = (text or "").strip()
    if not raw:
        return None
    # Single-line raw token (what users paste when they copy only the secret)
    lines = raw.splitlines()
    if len(lines) == 1 and _RAW_TOKEN_RE.match(raw) and "t.me/" not in raw.lower():
        return raw
    if raw.startswith("join_"):
        return raw[5:]
    low = raw.lower()
    if "t.me/" in low or "telegram.me/" in low:
        url = raw if raw.startswith(("http://", "https://")) else "https://" + raw.lstrip("/")
        try:
            p = urlparse(url)
            qs = parse_qs(p.query)
            for key in ("start", "startattach"):
                for v in qs.get(key) or []:
                    if v.startswith("join_"):
                        return v[5:]
        except Exception:
            return None
    return None
