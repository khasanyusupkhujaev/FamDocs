"""
Logos for Telegram /start photo, Mini App inlined HTML, and /brand/* routes.

With FAMDOC_STORAGE=s3 (e.g. Cloudflare R2), loads PNGs from the same bucket using
FAMDOC_LOGO_*_KEY or defaults under {FAMDOC_S3_PREFIX}/brand/. Falls back to local
Logo_transparent.png / Logo.png in the project root.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

from aiogram.types import BufferedInputFile, FSInputFile

from bot.config import (
    LOGO_SOLID_PATH,
    LOGO_SOLID_S3_KEY,
    LOGO_TRANSPARENT_PATH,
    LOGO_TRANSPARENT_S3_KEY,
    S3_PREFIX,
    STORAGE_BACKEND,
)
from bot.storage import StorageAccessDeniedError, read_bucket_key

log = logging.getLogger(__name__)

_transparent: bytes | None = None
_solid: bytes | None = None
_loaded: bool = False


def _default_transparent_key() -> str:
    return f"{S3_PREFIX}/brand/Logo_transparent.png"


def _default_solid_key() -> str:
    return f"{S3_PREFIX}/brand/Logo.png"


async def _try_bucket_keys(
    primary: str, fallbacks: tuple[str, ...], label: str
) -> bytes | None:
    """Try primary key, then fallbacks (only used when env override is not set)."""
    for key in (primary,) + fallbacks:
        try:
            data = await read_bucket_key(key)
        except StorageAccessDeniedError as e:
            log.warning("Branding: %s logo %r: %s", label, key, e)
            continue
        if data:
            if key != primary:
                log.info("Branding: using %s logo from %r", label, key)
            return data
    return None


async def load_branding_assets() -> None:
    """Idempotent: fetch from R2/S3 when configured, then fill gaps from local files."""
    global _transparent, _solid, _loaded
    if _loaded:
        return
    _loaded = True
    _transparent = None
    _solid = None

    if STORAGE_BACKEND == "s3":
        # Default path uses FAMDOC_S3_PREFIX; many uploads use plain brand/... without prefix.
        if LOGO_TRANSPARENT_S3_KEY:
            try:
                _transparent = await read_bucket_key(LOGO_TRANSPARENT_S3_KEY)
            except StorageAccessDeniedError as e:
                log.warning(
                    "Branding: transparent logo %r: %s", LOGO_TRANSPARENT_S3_KEY, e
                )
        else:
            _transparent = await _try_bucket_keys(
                _default_transparent_key(),
                (
                    "brand/logo-transparent.png",
                    "brand/Logo_transparent.png",
                ),
                "transparent",
            )
        if LOGO_SOLID_S3_KEY:
            try:
                _solid = await read_bucket_key(LOGO_SOLID_S3_KEY)
            except StorageAccessDeniedError as e:
                log.warning("Branding: solid logo %r: %s", LOGO_SOLID_S3_KEY, e)
        else:
            _solid = await _try_bucket_keys(
                _default_solid_key(),
                (
                    "brand/logo.png",
                    "brand/Logo.png",
                ),
                "solid",
            )

    if _transparent is None and LOGO_TRANSPARENT_PATH.is_file():
        _transparent = LOGO_TRANSPARENT_PATH.read_bytes()
    if _solid is None and LOGO_SOLID_PATH.is_file():
        _solid = LOGO_SOLID_PATH.read_bytes()


def _data_uri(png: bytes) -> str:
    b64 = base64.b64encode(png).decode("ascii")
    return f"data:image/png;base64,{b64}"


def spa_data_uri_transparent() -> str | None:
    if _transparent:
        return _data_uri(_transparent)
    return None


def spa_data_uri_solid() -> str | None:
    if _solid:
        return _data_uri(_solid)
    return None


def get_transparent_bytes() -> bytes | None:
    return _transparent


def get_solid_bytes() -> bytes | None:
    return _solid


async def telegram_logo_input() -> BufferedInputFile | FSInputFile | None:
    await load_branding_assets()
    if _transparent:
        return BufferedInputFile(_transparent, filename="logo.png")
    if _solid:
        return BufferedInputFile(_solid, filename="logo.png")
    if LOGO_TRANSPARENT_PATH.is_file():
        return FSInputFile(LOGO_TRANSPARENT_PATH)
    if LOGO_SOLID_PATH.is_file():
        return FSInputFile(LOGO_SOLID_PATH)
    return None


def png_data_uri_from_path(path: Path) -> str | None:
    if not path.is_file():
        return None
    return _data_uri(path.read_bytes())
