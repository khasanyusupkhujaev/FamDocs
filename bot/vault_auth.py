"""
Vault password authentication (server-side verification only).

Flow (see also webapp/vault-crypto.js for the client):
1) First open: no row in vault_crypto → client shows "Create password".
   POST /api/vault/password sends the password once over HTTPS; we Argon2-hash it and
   store only the hash plus a random kdf_salt (public, for client PBKDF2). We never log it.
2) Later opens: row exists → client shows "Enter password".
   POST /api/vault/unlock verifies with Argon2; on success we set an HttpOnly session cookie.
3) File encryption uses a key derived on the client from password + kdf_salt (PBKDF2).
   The server never receives the AES key and never decrypts document blobs.

Session cookie: signed payload (vault_id, user_id, exp) — same pattern as admin_web_auth.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException

VAULT_SESSION_COOKIE = "famdoc_vault"
SESSION_TTL_SECONDS = 604800  # 7 days

# Argon2id with conservative defaults (argon2-cffi defaults are reasonable).
_ph = PasswordHasher()

# --- Rate limiting (per-process; good enough for basic abuse control) ---
_VAULT_RL_LOCK = asyncio.Lock()
# key -> list of unix times of failed attempts
_vault_fail_times: dict[str, list[float]] = {}
MAX_FAILED_ATTEMPTS = 8
_FAIL_WINDOW_SEC = 900.0


def _rate_key(vault_id: int, user_id: int) -> str:
    return f"{vault_id}:{user_id}"


async def vault_rate_limit_check(vault_id: int, user_id: int) -> None:
    """Raises HTTPException 429 if too many recent failures."""
    key = _rate_key(vault_id, user_id)
    async with _VAULT_RL_LOCK:
        now = time.time()
        lst = _vault_fail_times.setdefault(key, [])
        lst[:] = [t for t in lst if now - t < _FAIL_WINDOW_SEC]
        if len(lst) >= MAX_FAILED_ATTEMPTS:
            raise HTTPException(
                status_code=429,
                detail="vault_login_rate_limited",
            )


async def vault_rate_limit_fail(vault_id: int, user_id: int) -> None:
    async with _VAULT_RL_LOCK:
        key = _rate_key(vault_id, user_id)
        _vault_fail_times.setdefault(key, []).append(time.time())


async def vault_rate_limit_reset(vault_id: int, user_id: int) -> None:
    async with _VAULT_RL_LOCK:
        _vault_fail_times.pop(_rate_key(vault_id, user_id), None)


def hash_vault_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_vault_password(password_hash: str, plain: str) -> bool:
    try:
        _ph.verify(password_hash, plain)
        return True
    except VerifyMismatchError:
        return False


def generate_kdf_salt_b64() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _vault_session_signing_key(bot_token: str) -> bytes:
    return hashlib.sha256((bot_token + "|famdoc_vault_unlock").encode()).digest()


def sign_vault_session(
    bot_token: str,
    vault_id: int,
    user_id: int,
    *,
    ttl_sec: int = SESSION_TTL_SECONDS,
) -> str:
    exp = int(time.time()) + ttl_sec
    payload: dict[str, Any] = {
        "vid": vault_id,
        "uid": user_id,
        "exp": exp,
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    b64 = base64.urlsafe_b64encode(body).decode().rstrip("=")
    sig = hmac.new(
        _vault_session_signing_key(bot_token), body, hashlib.sha256
    ).hexdigest()
    return f"{b64}.{sig}"


def verify_vault_session_cookie(
    bot_token: str, raw: str | None
) -> tuple[int, int] | None:
    """
    Returns (vault_id, user_id) if valid and not expired.
    """
    if not raw or "." not in raw or not bot_token:
        return None
    b64, sig = raw.rsplit(".", 1)
    pad = "=" * (-len(b64) % 4)
    try:
        body = base64.urlsafe_b64decode(b64 + pad)
    except (ValueError, OSError):
        return None
    expect = hmac.new(
        _vault_session_signing_key(bot_token), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        payload = json.loads(body.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    try:
        vault_id = int(payload.get("vid", 0))
        user_id = int(payload.get("uid", 0))
        exp = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return None
    if vault_id < 1 or user_id < 1 or exp < int(time.time()):
        return None
    return vault_id, user_id
