from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import html as html_lib
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from bot import branding
from bot import db
from bot.billing import (
    UPGRADE_EXTRA_SLOTS,
    billing_uses_stars,
    create_upgrade_invoice_link,
    effective_document_cap,
    upgrade_price_label,
)
from bot.invite_parse import extract_join_token
from bot import paytech_integration
from bot.admin_web_auth import (
    ADMIN_SESSION_COOKIE,
    sign_admin_session,
    verify_admin_session_cookie,
    verify_telegram_login_query,
)
from bot.config import (
    ADMIN_PANEL_SECRET,
    BASE_DIR,
    BILLING_MODE,
    BOT_TOKEN,
    BOT_USERNAME,
    CLICK_SECRET_KEY,
    CLICK_SERVICE_ID,
    FREE_DOCUMENT_LIMIT,
    LOGO_SOLID_PATH,
    LOGO_TRANSPARENT_PATH,
    PAYME_MERCHANT_ID,
    PAYME_MERCHANT_KEY,
    SECURE_COOKIES,
    TRANSFER_CARD_DISPLAY,
    TRANSFER_INSTRUCTIONS,
    WEBAPP_PUBLIC_URL,
    is_miniapp_admin,
    manual_billing_tiers,
    manual_tier_allowed_amounts,
)
from bot.vault_auth import (
    SESSION_TTL_SECONDS,
    VAULT_SESSION_COOKIE,
    generate_kdf_salt_b64,
    hash_vault_password,
    sign_vault_session,
    verify_vault_password,
    verify_vault_session_cookie,
    vault_rate_limit_check,
    vault_rate_limit_fail,
    vault_rate_limit_reset,
)
from bot.keyboards import CATEGORY_EMOJI, CATEGORY_LABELS, CATEGORY_ORDER
from bot.preview import build_preview_jpeg
from bot.storage import (
    StorageAccessDeniedError,
    read_stored_file,
    remove_stored_file,
    resolve_display_filename,
    save_upload,
)
from bot.share_tokens import sign_file_share, verify_file_share
from bot.suggest_category import suggest_category
from bot.tg_api import get_chat_info, primary_name_from_chat, telegram_username_from_chat
from bot.tg_webapp import validate_init_data
from bot.vault_resolve import vault_id_from_init_vals
from bot.paytech_integration import (
    FamDocClickHandler,
    FamDocPaymeHandler,
    FamdocPaytechOrder,
    create_paytech_checkout,
    format_paytech_price_label,
    init_paytech_db,
    paytech_configured,
)

log = logging.getLogger(__name__)

_MAX_RECEIPT_BYTES = 10 * 1024 * 1024
_ALLOWED_RECEIPT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "application/pdf",
    }
)


def _admin_panel_secret_matches(got: str, expected: str) -> bool:
    """Constant-time compare without requiring equal string lengths."""
    if not expected or not got:
        return False
    return hmac.compare_digest(
        hashlib.sha256(got.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def _require_admin_web_user(request: Request) -> int:
    raw = request.cookies.get(ADMIN_SESSION_COOKIE)
    pair = verify_admin_session_cookie(BOT_TOKEN, raw)
    if pair is None:
        raise HTTPException(status_code=401, detail="admin_session_required")
    uid, un = pair
    if not is_miniapp_admin(uid, un):
        raise HTTPException(status_code=403, detail="forbidden")
    return uid


def render_admin_page_html(webapp_dir: Path) -> str:
    template = (webapp_dir / "admin.html").read_text(encoding="utf-8")
    base = (WEBAPP_PUBLIC_URL or "").strip().rstrip("/")
    bot_u = (BOT_USERNAME or "").strip().lstrip("@")
    if bot_u and base:
        auth_url = f"{base}/admin/auth/callback"
        widget_html = (
            '<script async src="https://telegram.org/js/telegram-widget.js?22" '
            f'data-telegram-login="{html_lib.escape(bot_u, quote=True)}" '
            'data-size="large" data-userpic="false" '
            f'data-auth-url="{html_lib.escape(auth_url, quote=True)}" '
            'data-request-access="write"></script>'
        )
    else:
        widget_html = (
            '<p class="adm-hint adm-widget-fallback">Telegram button needs '
            "<code>WEBAPP_PUBLIC_URL</code> and <code>TELEGRAM_BOT_USERNAME</code> "
            "on the server. You can still use <strong>Telegram ID + panel secret</strong> below.</p>"
        )
    return template.replace("__TELEGRAM_LOGIN_WIDGET__", widget_html)


class DocPatch(BaseModel):
    original_filename: str | None = None
    category: str | None = None
    tags: str | None = None
    notes: str | None = None


class BulkIds(BaseModel):
    ids: list[int] = Field(..., min_length=1)


class BulkMove(BaseModel):
    ids: list[int] = Field(..., min_length=1)
    category: str


class FamilyInviteBody(BaseModel):
    phone: str = ""


class InviteAcceptBody(BaseModel):
    invite: str = Field(..., min_length=1)


class AdminGrantBody(BaseModel):
    target_user_id: int = Field(..., ge=1)
    slots: int = Field(..., ge=1, le=10_000)


class AdminDenyClaimBody(BaseModel):
    target_user_id: int = Field(..., ge=1)


class AdminPasswordLoginBody(BaseModel):
    telegram_user_id: int = Field(..., ge=1)
    secret: str = Field(..., min_length=1, max_length=500)
    telegram_username: str = Field("", max_length=64)


class VaultPasswordBody(BaseModel):
    """Plain password sent once over HTTPS for Argon2 verification only (never stored)."""

    password: str = Field(..., min_length=10, max_length=256)


@dataclass
class InitContext:
    vault_id: int
    user_id: int
    mode: str  # "private" | "group"
    telegram_username: str | None = None
    first_name: str = ""
    last_name: str = ""


def _document_is_encrypted(meta: dict) -> bool:
    if (meta.get("encryption_state") or "").strip() == "encrypted":
        return True
    return int(meta.get("crypto_encrypted") or 0) != 0


def _famdoc_crypto_headers(meta: dict) -> dict[str, str]:
    """Expose IV/tag for client decryption (salt comes from bootstrap vault_crypto)."""
    if not _document_is_encrypted(meta):
        return {}
    iv = (meta.get("crypto_iv") or "").strip()
    tag = (meta.get("crypto_tag") or "").strip()
    h = {
        "X-FamDoc-Encrypted": "1",
        "Access-Control-Expose-Headers": (
            "Content-Disposition, X-FamDoc-Encrypted, X-FamDoc-Crypto-IV, "
            "X-FamDoc-Crypto-Tag, X-FamDoc-Preview-Encrypted"
        ),
    }
    if iv:
        h["X-FamDoc-Crypto-IV"] = iv
    if tag:
        h["X-FamDoc-Crypto-Tag"] = tag
    return h


def _preview_crypto_headers(meta: dict) -> dict[str, str]:
    if not _document_is_encrypted(meta):
        return {}
    piv = (meta.get("preview_crypto_iv") or "").strip()
    ptag = (meta.get("preview_crypto_tag") or "").strip()
    if not piv or not ptag:
        return {}
    return {
        "X-FamDoc-Preview-Encrypted": "1",
        "X-FamDoc-Crypto-IV": piv,
        "X-FamDoc-Crypto-Tag": ptag,
        "Access-Control-Expose-Headers": (
            "Content-Disposition, X-FamDoc-Encrypted, X-FamDoc-Crypto-IV, "
            "X-FamDoc-Crypto-Tag, X-FamDoc-Preview-Encrypted"
        ),
    }


def _download_file_response(
    data: bytes,
    media: str,
    fname: str,
    *,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    """Attachment + CORS for Telegram WebApp downloadFile (Bot API 8.0+)."""
    cd = "attachment; filename*=UTF-8''" + quote(fname, safe="")
    base: dict[str, str] = {
        "Content-Disposition": cd,
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Disposition",
    }
    if extra_headers:
        ex = extra_headers.get("Access-Control-Expose-Headers")
        if ex:
            base["Access-Control-Expose-Headers"] = ex
        for k, v in extra_headers.items():
            if k == "Access-Control-Expose-Headers":
                continue
            base[k] = v
    return Response(content=data, media_type=media, headers=base)


async def _init_context_from_raw(init_data: str) -> InitContext:
    try:
        vals = validate_init_data(init_data.strip(), BOT_TOKEN)
        vault_id, mode = await vault_id_from_init_vals(vals)
        if not vals.get("user"):
            raise ValueError("no user")
        user = json.loads(vals["user"])
        uid = int(user["id"])
        raw_un = (user.get("username") or "").strip()
        telegram_username = raw_un if raw_un else None
        first_name = (user.get("first_name") or "").strip()
        last_name = (user.get("last_name") or "").strip()
        return InitContext(
            vault_id=vault_id,
            user_id=uid,
            mode=mode,
            telegram_username=telegram_username,
            first_name=first_name,
            last_name=last_name,
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid init data") from None


def _manual_tier_valid(price_uzs: int, slots: int) -> bool:
    for t in manual_billing_tiers():
        if int(t["price_uzs"]) == price_uzs and int(t["slots"]) == slots:
            return True
    return False


def build_spa_html(webapp_dir: Path) -> str:
    """
    Serve index.html with external /static/* scripts and styles only (CSP: no inline JS/CSS).
    Logo <img> src may be replaced with data URIs below.
    """
    html = (webapp_dir / "index.html").read_text(encoding="utf-8")
    transparent = (
        branding.spa_data_uri_transparent()
        or branding.png_data_uri_from_path(LOGO_TRANSPARENT_PATH)
        or branding.spa_data_uri_solid()
        or branding.png_data_uri_from_path(LOGO_SOLID_PATH)
    )
    solid = (
        branding.spa_data_uri_solid()
        or branding.png_data_uri_from_path(LOGO_SOLID_PATH)
        or branding.spa_data_uri_transparent()
        or branding.png_data_uri_from_path(LOGO_TRANSPARENT_PATH)
    )
    if transparent:
        html = html.replace('"/brand/logo-transparent.png"', f'"{transparent}"')
    if solid:
        html = html.replace('"/brand/logo.png"', f'"{solid}"')
    return html


def create_webapp_app() -> FastAPI:
    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        await db.init_db()
        init_paytech_db()
        await branding.load_branding_assets()
        yield

    app = FastAPI(
        title="FamDoc Mini App API",
        version="2.0",
        lifespan=_lifespan,
    )

    _CSP_MINIAPP = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' blob: data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-src 'self' blob:; "
        # Telegram Mini App opens in web.telegram.org / telegram.org frames (not 'none').
        "frame-ancestors https://web.telegram.org https://telegram.org;"
    )

    @app.middleware("http")
    async def _security_headers_middleware(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = _CSP_MINIAPP
        return response

    @app.exception_handler(StorageAccessDeniedError)
    async def _storage_access_denied(
        _request: Request, exc: StorageAccessDeniedError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "storage_access_denied",
                "message": str(exc),
            },
        )

    webapp_dir = BASE_DIR / "webapp"

    async def init_context_dep(
        x_telegram_init_data: str | None = Header(None, alias="X-Telegram-Init-Data"),
    ) -> InitContext:
        if not x_telegram_init_data:
            raise HTTPException(status_code=401, detail="Missing X-Telegram-Init-Data")
        return await _init_context_from_raw(x_telegram_init_data)

    Init = Depends(init_context_dep)

    async def _ensure_vault_unlocked(request: Request, ctx: InitContext) -> None:
        row = await db.get_vault_crypto(ctx.vault_id)
        if row is None:
            return
        raw_ck = request.cookies.get(VAULT_SESSION_COOKIE)
        tok = verify_vault_session_cookie(BOT_TOKEN, raw_ck)
        if tok is None or tok[0] != ctx.vault_id or tok[1] != ctx.user_id:
            raise HTTPException(status_code=401, detail="vault_unlock_required")

    async def require_vault_unlocked(
        request: Request,
        ctx: InitContext = Init,
    ) -> InitContext:
        await _ensure_vault_unlocked(request, ctx)
        return ctx

    VaultOK = Depends(require_vault_unlocked)

    def _set_vault_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            key=VAULT_SESSION_COOKIE,
            value=token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=SECURE_COOKIES,
            path="/",
        )

    async def _remove_doc_blobs(vault_id: int, meta: dict) -> None:
        await remove_stored_file(vault_id, meta["stored_filename"])
        prev = (meta.get("preview_stored_filename") or "").strip()
        if prev:
            await remove_stored_file(vault_id, prev)

    @app.get("/api/config")
    async def api_config() -> dict:
        return {
            "categories": [
                {"id": k, "label": CATEGORY_LABELS[k], "emoji": CATEGORY_EMOJI[k]}
                for k in CATEGORY_ORDER
            ]
        }

    @app.post("/api/vault/password")
    async def vault_create_password(
        body: VaultPasswordBody,
        ctx: InitContext = Init,
    ):
        """First-time: store Argon2 hash + public KDF salt; set HttpOnly session cookie."""
        if await db.get_vault_crypto(ctx.vault_id):
            raise HTTPException(status_code=400, detail="vault_password_already_set")
        salt = generate_kdf_salt_b64()
        pw_hash = hash_vault_password(body.password)
        await db.create_vault_crypto(ctx.vault_id, pw_hash, salt)
        token = sign_vault_session(BOT_TOKEN, ctx.vault_id, ctx.user_id)
        resp = JSONResponse(
            {"ok": True, "vault_crypto": {"state": "unlocked", "kdf_salt_b64": salt}}
        )
        _set_vault_session_cookie(resp, token)
        return resp

    @app.post("/api/vault/unlock")
    async def vault_unlock_post(
        body: VaultPasswordBody,
        ctx: InitContext = Init,
    ):
        """Verify password (Argon2); rate-limited; refresh session cookie."""
        await vault_rate_limit_check(ctx.vault_id, ctx.user_id)
        row = await db.get_vault_crypto(ctx.vault_id)
        if not row:
            raise HTTPException(status_code=400, detail="vault_no_password")
        if not verify_vault_password(row["password_hash"], body.password):
            await vault_rate_limit_fail(ctx.vault_id, ctx.user_id)
            raise HTTPException(status_code=401, detail="vault_password_invalid")
        await vault_rate_limit_reset(ctx.vault_id, ctx.user_id)
        token = sign_vault_session(BOT_TOKEN, ctx.vault_id, ctx.user_id)
        resp = JSONResponse(
            {
                "ok": True,
                "vault_crypto": {
                    "state": "unlocked",
                    "kdf_salt_b64": row["kdf_salt_b64"],
                },
            }
        )
        _set_vault_session_cookie(resp, token)
        return resp

    @app.post("/api/vault/logout")
    async def vault_logout_post() -> Response:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(
            VAULT_SESSION_COOKIE,
            path="/",
            samesite="lax",
            secure=SECURE_COOKIES,
        )
        return resp

    @app.get("/api/bootstrap")
    async def api_bootstrap(request: Request, ctx: InitContext = Init) -> dict:
        await db.increment_bootstrap_count()
        paytech_ok = paytech_configured()
        if BILLING_MODE == "manual":
            manual_ok = bool(TRANSFER_CARD_DISPLAY.strip())
            billing_out = {
                "upgrade_enabled": FREE_DOCUMENT_LIMIT > 0 and manual_ok,
                "slots_per_purchase": UPGRADE_EXTRA_SLOTS,
                "price_label": "",
                "uses_stars": False,
                "mode": "manual",
                "paytech_ready": False,
                "manual": {
                    "card": TRANSFER_CARD_DISPLAY,
                    "instructions": TRANSFER_INSTRUCTIONS,
                    "tiers": manual_billing_tiers(),
                    "currency": "UZS",
                },
            }
        elif BILLING_MODE in ("payme", "click"):
            pl = format_paytech_price_label()
            billing_out = {
                "upgrade_enabled": FREE_DOCUMENT_LIMIT > 0 and paytech_ok,
                "slots_per_purchase": UPGRADE_EXTRA_SLOTS,
                "price_label": pl,
                "uses_stars": False,
                "mode": BILLING_MODE,
                "paytech_ready": paytech_ok,
            }
        else:
            pl = upgrade_price_label()
            billing_out = {
                "upgrade_enabled": FREE_DOCUMENT_LIMIT > 0
                and (BILLING_MODE == "telegram" or paytech_ok),
                "slots_per_purchase": UPGRADE_EXTRA_SLOTS,
                "price_label": pl,
                "uses_stars": billing_uses_stars(),
                "mode": BILLING_MODE,
                "paytech_ready": paytech_ok,
            }

        vrow = await db.get_vault_crypto(ctx.vault_id)
        cookie_v = verify_vault_session_cookie(
            BOT_TOKEN, request.cookies.get(VAULT_SESSION_COOKIE)
        )
        vault_unlocked = bool(
            vrow
            and cookie_v
            and cookie_v[0] == ctx.vault_id
            and cookie_v[1] == ctx.user_id
        )
        if vrow is None:
            vc_state = "none"
        elif vault_unlocked:
            vc_state = "unlocked"
        else:
            vc_state = "locked"

        if vrow and not vault_unlocked:
            return {
                "vault_locked": True,
                "vault_crypto": {
                    "state": vc_state,
                    "kdf_salt_b64": vrow["kdf_salt_b64"],
                },
                "categories": [
                    {
                        "id": k,
                        "label": CATEGORY_LABELS[k],
                        "emoji": CATEGORY_EMOJI[k],
                        "count": 0,
                    }
                    for k in CATEGORY_ORDER
                ],
                "total": 0,
                "context_mode": ctx.mode,
                "document_limit": 0,
                "free_document_limit": FREE_DOCUMENT_LIMIT,
                "purchased_extra_slots": 0,
                "telegram_user_id": ctx.user_id,
                "telegram_username": ctx.telegram_username,
                "is_admin": is_miniapp_admin(ctx.user_id, ctx.telegram_username),
                "billing": billing_out,
            }

        counts = await db.count_by_category(ctx.vault_id)
        cats = [
            {
                "id": k,
                "label": CATEGORY_LABELS[k],
                "emoji": CATEGORY_EMOJI[k],
                "count": counts.get(k, 0),
            }
            for k in CATEGORY_ORDER
        ]
        total = sum(counts.values())
        extra = await db.get_purchased_extra_slots(ctx.vault_id)
        cap = await effective_document_cap(ctx.vault_id)
        doc_limit = cap if cap is not None else 0
        return {
            "vault_locked": False,
            "vault_crypto": {
                "state": vc_state,
                "kdf_salt_b64": (vrow["kdf_salt_b64"] if vrow else ""),
            },
            "categories": cats,
            "total": total,
            "context_mode": ctx.mode,
            "document_limit": doc_limit,
            "free_document_limit": FREE_DOCUMENT_LIMIT,
            "purchased_extra_slots": extra,
            "telegram_user_id": ctx.user_id,
            "telegram_username": ctx.telegram_username,
            "is_admin": is_miniapp_admin(ctx.user_id, ctx.telegram_username),
            "billing": billing_out,
        }

    @app.get("/api/admin/stats")
    async def api_admin_stats_miniapp(ctx: InitContext = Init):
        if not is_miniapp_admin(ctx.user_id, ctx.telegram_username):
            raise HTTPException(status_code=403, detail="forbidden")
        return await db.admin_statistics()

    @app.get("/api/admin/manual-claims")
    async def api_admin_manual_claims(ctx: InitContext = Init):
        if not is_miniapp_admin(ctx.user_id, ctx.telegram_username):
            raise HTTPException(status_code=403, detail="forbidden")
        rows = await db.list_manual_payment_claims()
        items = []
        for r in rows:
            fn = (r.get("first_name") or "").strip()
            ln = (r.get("last_name") or "").strip()
            parts = [p for p in (fn, ln) if p]
            display_name = " ".join(parts) if parts else ""
            has_receipt = bool((r.get("receipt_stored_filename") or "").strip())
            rmt = (r.get("receipt_mime") or "").strip()
            items.append(
                {
                    "user_id": r["user_id"],
                    "first_name": fn,
                    "last_name": ln,
                    "display_name": display_name,
                    "username": r.get("username"),
                    "price_uzs": r["price_uzs"],
                    "slots_requested": r["slots_requested"],
                    "claimed_at": r["claimed_at"],
                    "has_receipt": has_receipt,
                    "receipt_mime": rmt,
                }
            )
        return {"items": items}

    @app.post("/api/admin/grant")
    async def api_admin_grant_miniapp(
        body: AdminGrantBody,
        ctx: InitContext = Init,
    ):
        if not is_miniapp_admin(ctx.user_id, ctx.telegram_username):
            raise HTTPException(status_code=403, detail="forbidden")
        vault_id = await db.get_vault_for_user(body.target_user_id)
        await db.add_extra_slots(vault_id, body.slots)
        await db.delete_manual_payment_claim(body.target_user_id)
        extra = await db.get_purchased_extra_slots(vault_id)
        cap: int | None = None
        if FREE_DOCUMENT_LIMIT > 0:
            cap = FREE_DOCUMENT_LIMIT + extra
        return {
            "ok": True,
            "vault_id": vault_id,
            "target_user_id": body.target_user_id,
            "slots_added": body.slots,
            "purchased_extra_slots_total": extra,
            "document_cap": cap,
        }

    @app.post("/api/admin/deny-claim")
    async def api_admin_deny_claim(
        body: AdminDenyClaimBody,
        ctx: InitContext = Init,
    ):
        if not is_miniapp_admin(ctx.user_id, ctx.telegram_username):
            raise HTTPException(status_code=403, detail="forbidden")
        row = await db.get_manual_payment_claim(body.target_user_id)
        if not row:
            raise HTTPException(status_code=404, detail="no_such_claim")
        await db.delete_manual_payment_claim(body.target_user_id)
        return {"ok": True, "target_user_id": body.target_user_id}

    @app.get("/api/documents")
    async def api_list_documents(
        category: str | None = None,
        q: str | None = None,
        ctx: InitContext = VaultOK,
    ):
        if category in (None, "", "all"):
            cat = None
        else:
            cat = category
        if cat and cat not in CATEGORY_LABELS:
            raise HTTPException(status_code=400, detail="Unknown category")
        rows = await db.list_documents(ctx.vault_id, cat, search=q)
        return {
            "items": [
                {
                    "id": r["id"],
                    "category": r["category"],
                    "category_label": CATEGORY_LABELS.get(
                        r["category"], r["category"]
                    ),
                    "emoji": CATEGORY_EMOJI.get(r["category"], "📁"),
                    "original_filename": r["original_filename"],
                    "uploaded_at": r["uploaded_at"],
                    "mime_type": r["mime_type"],
                    "file_size": r["file_size"],
                    "tags": r.get("tags") or "",
                    "notes": r.get("notes") or "",
                    "has_preview": bool((r.get("preview_stored_filename") or "").strip()),
                    "crypto_encrypted": bool(int(r.get("crypto_encrypted") or 0)),
                    "encryption_state": (r.get("encryption_state") or "legacy_plaintext"),
                }
                for r in rows
            ]
        }

    @app.post("/api/documents/{doc_id}/migrate-crypto")
    async def api_migrate_document_crypto(
        doc_id: int,
        ctx: InitContext = VaultOK,
        crypto_iv: str = Form(...),
        crypto_tag: str = Form(...),
        file: UploadFile = File(...),
        preview_crypto_iv: str = Form(""),
        preview_crypto_tag: str = Form(""),
        preview_file: UploadFile | None = File(None),
    ):
        """Replace legacy_plaintext blob with client-encrypted ciphertext (same vault password)."""
        if not await db.get_vault_crypto(ctx.vault_id):
            raise HTTPException(status_code=400, detail="vault_no_password")
        meta = await db.get_document(ctx.vault_id, doc_id)
        if not meta:
            raise HTTPException(status_code=404, detail="Not found")
        if (meta.get("encryption_state") or "") != "legacy_plaintext":
            raise HTTPException(status_code=409, detail="already_encrypted")
        if not (crypto_iv or "").strip() or not (crypto_tag or "").strip():
            raise HTTPException(status_code=400, detail="missing_crypto_metadata")
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Empty file")
        p_iv = (preview_crypto_iv or "").strip()
        p_tag = (preview_crypto_tag or "").strip()
        preview_raw: bytes | None = None
        if preview_file is not None:
            preview_raw = await preview_file.read()
            if preview_raw and (not p_iv or not p_tag):
                raise HTTPException(
                    status_code=400, detail="missing_preview_crypto_metadata"
                )

        await remove_stored_file(ctx.vault_id, meta["stored_filename"])
        prev_old = (meta.get("preview_stored_filename") or "").strip()
        if prev_old:
            await remove_stored_file(ctx.vault_id, prev_old)

        orig_name = meta["original_filename"]
        stored = await save_upload(ctx.vault_id, orig_name, raw)
        preview_stored = ""
        if preview_raw:
            preview_stored = await save_upload(
                ctx.vault_id, "preview.enc.jpg", preview_raw
            )

        ok = await db.apply_document_crypto_migration(
            ctx.vault_id,
            doc_id,
            stored_filename=stored,
            file_size=len(raw),
            crypto_iv=crypto_iv.strip(),
            crypto_tag=crypto_tag.strip(),
            preview_stored_filename=preview_stored,
            preview_crypto_iv=p_iv if preview_raw else "",
            preview_crypto_tag=p_tag if preview_raw else "",
        )
        if not ok:
            raise HTTPException(status_code=409, detail="migrate_race")
        return {"ok": True, "id": doc_id}

    @app.patch("/api/documents/{doc_id}")
    async def api_patch_document(
        doc_id: int,
        body: DocPatch,
        ctx: InitContext = VaultOK,
    ):
        if body.category is not None and body.category not in CATEGORY_LABELS:
            raise HTTPException(status_code=400, detail="Unknown category")
        ok = await db.update_document(
            ctx.vault_id,
            doc_id,
            original_filename=body.original_filename,
            category=body.category,
            tags=body.tags,
            notes=body.notes,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Not found")
        meta = await db.get_document(ctx.vault_id, doc_id)
        return {"ok": True, "document": meta}

    @app.delete("/api/documents/{doc_id}")
    async def api_delete_document(doc_id: int, ctx: InitContext = VaultOK):
        meta = await db.get_document(ctx.vault_id, doc_id)
        if not meta:
            raise HTTPException(status_code=404, detail="Not found")
        await db.delete_document_row(ctx.vault_id, doc_id)
        await _remove_doc_blobs(ctx.vault_id, meta)
        return {"ok": True}

    @app.post("/api/documents/bulk-delete")
    async def api_bulk_delete(payload: BulkIds, ctx: InitContext = VaultOK):
        metas = await db.get_documents_meta(ctx.vault_id, payload.ids)
        found = {m["id"] for m in metas}
        if found != set(payload.ids):
            raise HTTPException(status_code=400, detail="Invalid or foreign ids")
        await db.delete_documents(ctx.vault_id, payload.ids)
        for m in metas:
            await _remove_doc_blobs(ctx.vault_id, m)
        return {"ok": True, "deleted": len(payload.ids)}

    @app.post("/api/documents/bulk-move")
    async def api_bulk_move(payload: BulkMove, ctx: InitContext = VaultOK):
        if payload.category not in CATEGORY_LABELS:
            raise HTTPException(status_code=400, detail="Unknown category")
        metas = await db.get_documents_meta(ctx.vault_id, payload.ids)
        found = {m["id"] for m in metas}
        if found != set(payload.ids):
            raise HTTPException(status_code=400, detail="Invalid ids")
        for i in payload.ids:
            await db.update_document(ctx.vault_id, i, category=payload.category)
        return {"ok": True, "moved": len(payload.ids)}

    async def _bulk_zip_response(ctx: InitContext, doc_ids: list[int]) -> FileResponse:
        metas = await db.get_documents_meta(ctx.vault_id, doc_ids)
        found = {m["id"] for m in metas}
        if found != set(doc_ids):
            raise HTTPException(status_code=400, detail="Invalid ids")
        fd, tmp_path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for m in metas:
                    blob = await read_stored_file(ctx.vault_id, m["stored_filename"])
                    if blob is None:
                        continue
                    arc = f"{m['id']}_{m['original_filename']}"
                    zf.writestr(arc, blob)
        except OSError:
            Path(tmp_path).unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail="Could not build zip") from None

        def _cleanup() -> None:
            Path(tmp_path).unlink(missing_ok=True)

        return FileResponse(
            tmp_path,
            filename="famdoc_documents.zip",
            media_type="application/zip",
            background=BackgroundTask(_cleanup),
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Expose-Headers": "Content-Disposition",
            },
        )

    @app.post("/api/documents/bulk-zip")
    async def api_bulk_zip(payload: BulkIds, ctx: InitContext = VaultOK):
        return await _bulk_zip_response(ctx, payload.ids)

    @app.get("/api/documents/bulk-zip")
    async def api_bulk_zip_get(
        request: Request,
        ids: str,
        tgWebAppData: str | None = Query(None),
        x_telegram_init_data: str | None = Header(None, alias="X-Telegram-Init-Data"),
    ):
        raw = (x_telegram_init_data or "").strip() or (tgWebAppData or "").strip()
        if not raw:
            raise HTTPException(
                status_code=401,
                detail="Missing Telegram auth (header or tgWebAppData query)",
            )
        ctx = await _init_context_from_raw(raw)
        await _ensure_vault_unlocked(request, ctx)
        id_list: list[int] = []
        for part in ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                id_list.append(int(part))
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid ids") from None
        if not id_list:
            raise HTTPException(status_code=400, detail="Invalid ids")
        return await _bulk_zip_response(ctx, id_list)

    @app.options("/api/documents/{doc_id}/file")
    async def api_download_file_options() -> Response:
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "X-Telegram-Init-Data, Content-Type",
                "Access-Control-Max-Age": "86400",
            },
        )

    @app.get("/api/documents/{doc_id}/file")
    async def api_download_file(
        request: Request,
        doc_id: int,
        x_telegram_init_data: str | None = Header(None, alias="X-Telegram-Init-Data"),
        tgWebAppData: str | None = Query(None),
    ):
        raw = (x_telegram_init_data or "").strip() or (tgWebAppData or "").strip()
        if not raw:
            raise HTTPException(
                status_code=401,
                detail="Missing Telegram auth (header or tgWebAppData query)",
            )
        ctx = await _init_context_from_raw(raw)
        await _ensure_vault_unlocked(request, ctx)
        meta = await db.get_document(ctx.vault_id, doc_id)
        if not meta:
            raise HTTPException(status_code=404, detail="Not found")
        data = await read_stored_file(ctx.vault_id, meta["stored_filename"])
        if not data:
            raise HTTPException(status_code=404, detail="Missing file")
        media = meta.get("mime_type") or "application/octet-stream"
        fname = meta["original_filename"]
        xh = _famdoc_crypto_headers(meta)
        return _download_file_response(data, media, fname, extra_headers=xh or None)

    @app.get("/api/shared/documents/{doc_id}")
    async def api_shared_document(
        request: Request,
        doc_id: int,
        vault_id: int,
        exp: int,
        sig: str,
        tgWebAppData: str | None = Query(None),
        x_telegram_init_data: str | None = Header(None, alias="X-Telegram-Init-Data"),
    ):
        if not verify_file_share(doc_id, vault_id, exp, sig):
            raise HTTPException(status_code=404, detail="Invalid or expired link")
        raw = (x_telegram_init_data or "").strip() or (tgWebAppData or "").strip()
        if not raw:
            raise HTTPException(
                status_code=401,
                detail="telegram_auth_required",
            )
        ctx = await _init_context_from_raw(raw)
        if int(vault_id) != int(ctx.vault_id):
            raise HTTPException(status_code=403, detail="vault_mismatch")
        await _ensure_vault_unlocked(request, ctx)
        meta = await db.get_document(vault_id, doc_id)
        if not meta:
            raise HTTPException(status_code=404, detail="Not found")
        data = await read_stored_file(vault_id, meta["stored_filename"])
        if not data:
            raise HTTPException(status_code=404, detail="Missing file")
        media = meta.get("mime_type") or "application/octet-stream"
        fname = meta["original_filename"]
        xh = _famdoc_crypto_headers(meta)
        return _download_file_response(data, media, fname, extra_headers=xh or None)

    @app.get("/api/documents/{doc_id}/share-link")
    async def api_document_share_link(doc_id: int, ctx: InitContext = VaultOK):
        if not WEBAPP_PUBLIC_URL:
            raise HTTPException(status_code=503, detail="share_requires_public_url")
        meta = await db.get_document(ctx.vault_id, doc_id)
        if not meta:
            raise HTTPException(status_code=404, detail="Not found")
        ttl = 86400
        exp = int(time.time()) + ttl
        sig = sign_file_share(doc_id, ctx.vault_id, exp)
        q = urlencode(
            {"vault_id": ctx.vault_id, "exp": exp, "sig": sig},
        )
        base = WEBAPP_PUBLIC_URL.rstrip("/")
        file_url = f"{base}/api/shared/documents/{doc_id}?{q}"
        name = meta["original_filename"]
        telegram_url = "https://t.me/share/url?" + urlencode({"url": file_url, "text": name})
        return {
            "file_url": file_url,
            "telegram_url": telegram_url,
            "expires_at": exp,
        }

    @app.get("/api/documents/{doc_id}/preview")
    async def api_doc_preview(doc_id: int, ctx: InitContext = VaultOK):
        meta = await db.get_document(ctx.vault_id, doc_id)
        if not meta:
            raise HTTPException(status_code=404, detail="Not found")
        enc = _document_is_encrypted(meta)
        prev = (meta.get("preview_stored_filename") or "").strip()
        if prev:
            data = await read_stored_file(ctx.vault_id, prev)
            if data:
                ph = _preview_crypto_headers(meta)
                if ph:
                    return Response(content=data, media_type="image/jpeg", headers=ph)
                return Response(content=data, media_type="image/jpeg")
        if enc:
            raise HTTPException(status_code=404, detail="No preview")
        raw = await read_stored_file(ctx.vault_id, meta["stored_filename"])
        if not raw:
            raise HTTPException(status_code=404, detail="Missing file")
        jpeg = await asyncio.to_thread(
            build_preview_jpeg, raw, meta.get("mime_type")
        )
        if not jpeg:
            raise HTTPException(status_code=404, detail="No preview")
        pstored = await save_upload(ctx.vault_id, "preview.jpg", jpeg)
        await db.set_document_preview(ctx.vault_id, doc_id, pstored)
        return Response(content=jpeg, media_type="image/jpeg")

    @app.get("/api/family")
    async def api_family(ctx: InitContext = VaultOK):
        if ctx.mode != "private":
            return {"mode": ctx.mode, "members": [], "family_features": False}
        members = await db.list_vault_members(ctx.vault_id)
        out: list[dict] = []
        for m in members:
            uid = int(m["user_id"])
            ch = await get_chat_info(uid)
            primary = primary_name_from_chat(ch, uid)
            un = telegram_username_from_chat(ch)
            out.append(
                {
                    "user_id": uid,
                    "role": m["role"],
                    "joined_at": m["joined_at"],
                    "display_name": primary,
                    "username": un,
                }
            )
        return {
            "mode": ctx.mode,
            "family_features": True,
            "vault_id": ctx.vault_id,
            "members": out,
        }

    @app.post("/api/family/invite")
    async def api_family_invite(
        body: FamilyInviteBody,
        ctx: InitContext = VaultOK,
    ):
        if ctx.mode != "private":
            raise HTTPException(status_code=400, detail="Invites only in private chat")
        try:
            token, _exp = await db.create_invite(
                vault_id=ctx.vault_id,
                created_by_user_id=ctx.user_id,
                phone=body.phone,
            )
        except PermissionError:
            raise HTTPException(status_code=403, detail="Not allowed") from None
        if not BOT_USERNAME:
            return {
                "token": token,
                "invite_url": None,
                "warning": "Set TELEGRAM_BOT_USERNAME in .env for a t.me link.",
            }
        invite_url = f"https://t.me/{BOT_USERNAME}?start=join_{token}"
        return {"token": token, "invite_url": invite_url}

    @app.post("/api/family/accept")
    async def api_family_accept(body: InviteAcceptBody, ctx: InitContext = VaultOK):
        if ctx.mode != "private":
            raise HTTPException(
                status_code=400, detail="family_accept_private_only"
            )
        token = extract_join_token(body.invite)
        if not token:
            raise HTTPException(
                status_code=400, detail="invalid_invite_format"
            )
        ok, reason = await db.accept_invite(token, ctx.user_id)
        if not ok:
            raise HTTPException(status_code=400, detail=reason)
        return {"ok": True, "reason": reason}

    @app.post("/api/billing/manual-claim")
    async def api_manual_claim(
        ctx: InitContext = VaultOK,
        price_uzs: int = Form(...),
        slots: int = Form(...),
        receipt: UploadFile = File(...),
    ):
        if BILLING_MODE != "manual":
            raise HTTPException(status_code=400, detail="not_manual_billing")
        if FREE_DOCUMENT_LIMIT <= 0:
            raise HTTPException(status_code=400, detail="billing_disabled")
        if not _manual_tier_valid(price_uzs, slots):
            raise HTTPException(status_code=400, detail="invalid_tier")
        raw = await receipt.read()
        if not raw or len(raw) > _MAX_RECEIPT_BYTES:
            raise HTTPException(
                status_code=413,
                detail="receipt_too_large",
            )
        mt = (receipt.content_type or "").split(";")[0].strip().lower()
        if mt not in _ALLOWED_RECEIPT_TYPES:
            raise HTTPException(status_code=400, detail="invalid_receipt_type")
        old = await db.get_manual_payment_claim(ctx.user_id)
        if old:
            ov = int(old.get("vault_id") or 0)
            ofn = (old.get("receipt_stored_filename") or "").strip()
            if ov and ofn:
                await remove_stored_file(ov, ofn)
        orig = resolve_display_filename("", receipt.filename) or "receipt"
        stored = await save_upload(ctx.vault_id, orig, raw)
        await db.upsert_manual_payment_claim(
            ctx.user_id,
            first_name=ctx.first_name,
            last_name=ctx.last_name,
            username=ctx.telegram_username,
            price_uzs=price_uzs,
            slots_requested=slots,
            vault_id=ctx.vault_id,
            receipt_stored_filename=stored,
            receipt_mime=mt,
        )
        return {"ok": True}

    @app.get("/api/admin/claim-receipt/{user_id}")
    async def api_admin_claim_receipt(user_id: int, ctx: InitContext = Init):
        if not is_miniapp_admin(ctx.user_id, ctx.telegram_username):
            raise HTTPException(status_code=403, detail="forbidden")
        row = await db.get_manual_payment_claim(user_id)
        if not row:
            raise HTTPException(status_code=404, detail="not_found")
        fn = (row.get("receipt_stored_filename") or "").strip()
        vid = int(row.get("vault_id") or 0)
        if not fn or not vid:
            raise HTTPException(status_code=404, detail="no_receipt")
        data = await read_stored_file(vid, fn)
        if not data:
            raise HTTPException(status_code=404, detail="missing_file")
        media = (row.get("receipt_mime") or "").strip() or "application/octet-stream"
        return Response(content=data, media_type=media)

    @app.get("/admin/stats")
    async def admin_stats_http(request: Request):
        _require_admin_web_user(request)
        return await db.admin_statistics()

    @app.get("/admin/auth/callback")
    async def admin_telegram_auth_callback(request: Request):
        params = {str(k): str(v) for k, v in request.query_params.multi_items()}
        verified = verify_telegram_login_query(params, bot_token=BOT_TOKEN)
        if verified is None:
            return HTMLResponse(
                "<!DOCTYPE html><html><body><h1>Invalid Telegram login</h1>"
                "<p>The sign-in link expired or was tampered with. Close this tab and try again from "
                '<a href="/admin">/admin</a>.</p></body></html>',
                status_code=400,
                media_type="text/html; charset=utf-8",
            )
        uid, un = verified
        if not is_miniapp_admin(uid, un):
            return HTMLResponse(
                "<!DOCTYPE html><html><body><h1>Access denied</h1>"
                "<p>This Telegram account is not an admin. Add your numeric user ID to "
                "<code>FAMDOC_ADMIN_TELEGRAM_IDS</code> or your @username to "
                "<code>FAMDOC_ADMIN_USERNAMES</code> in the server .env, then redeploy.</p>"
                '<p><a href="/admin">Back</a></p></body></html>',
                status_code=403,
                media_type="text/html; charset=utf-8",
            )
        session_val = sign_admin_session(BOT_TOKEN, uid, un)
        resp = RedirectResponse(url="/admin", status_code=302)
        secure = request.url.scheme == "https"
        resp.set_cookie(
            ADMIN_SESSION_COOKIE,
            session_val,
            max_age=604800,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
        )
        return resp

    @app.get("/admin/api/me")
    async def admin_api_me(request: Request):
        raw = request.cookies.get(ADMIN_SESSION_COOKIE)
        pair = verify_admin_session_cookie(BOT_TOKEN, raw)
        if pair is None:
            return {"ok": True, "authenticated": False}
        uid, un = pair
        if not is_miniapp_admin(uid, un):
            return {"ok": True, "authenticated": False}
        return {"ok": True, "authenticated": True, "user_id": uid}

    @app.get("/admin/api/config")
    async def admin_public_config():
        telegram_widget_ok = bool(
            (WEBAPP_PUBLIC_URL or "").strip() and (BOT_USERNAME or "").strip()
        )
        password_login_ok = bool(ADMIN_PANEL_SECRET)
        return {
            "telegram_widget_ok": telegram_widget_ok,
            "password_login_ok": password_login_ok,
        }

    @app.post("/admin/api/login")
    async def admin_password_login(
        request: Request,
        body: AdminPasswordLoginBody,
    ):
        if not ADMIN_PANEL_SECRET:
            raise HTTPException(
                status_code=503,
                detail="admin_panel_secret_not_configured",
            )
        if not _admin_panel_secret_matches(body.secret, ADMIN_PANEL_SECRET):
            raise HTTPException(status_code=401, detail="invalid_credentials")
        raw_un = (body.telegram_username or "").strip().lstrip("@")
        un = raw_un if raw_un else None
        if not is_miniapp_admin(body.telegram_user_id, un):
            raise HTTPException(status_code=403, detail="not_admin")
        session_val = sign_admin_session(BOT_TOKEN, body.telegram_user_id, un)
        resp = JSONResponse({"ok": True})
        secure = request.url.scheme == "https"
        resp.set_cookie(
            ADMIN_SESSION_COOKIE,
            session_val,
            max_age=604800,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
        )
        return resp

    @app.post("/admin/api/logout")
    async def admin_api_logout():
        r = JSONResponse({"ok": True})
        r.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
        return r

    @app.get("/admin")
    async def admin_dashboard():
        admin_html = webapp_dir / "admin.html"
        if not admin_html.is_file():
            raise HTTPException(status_code=404, detail="admin_ui_missing")
        return HTMLResponse(
            content=render_admin_page_html(webapp_dir),
            media_type="text/html; charset=utf-8",
        )

    @app.get("/admin/api/data")
    async def admin_data_api(request: Request):
        _require_admin_web_user(request)
        stats = await db.admin_statistics()
        rows = await db.list_manual_payment_claims()
        claims: list[dict] = []
        for r in rows:
            fn = (r.get("first_name") or "").strip()
            ln = (r.get("last_name") or "").strip()
            parts = [p for p in (fn, ln) if p]
            display_name = " ".join(parts) if parts else ""
            rmt = (r.get("receipt_mime") or "").strip()
            claims.append(
                {
                    "user_id": r["user_id"],
                    "display_name": display_name,
                    "username": r.get("username"),
                    "price_uzs": r["price_uzs"],
                    "slots_requested": r["slots_requested"],
                    "claimed_at": r["claimed_at"],
                    "has_receipt": bool(
                        (r.get("receipt_stored_filename") or "").strip()
                    ),
                    "receipt_mime": rmt,
                }
            )
        return {"stats": stats, "claims": claims}

    @app.get("/admin/api/receipt/{user_id}")
    async def admin_receipt_api(user_id: int, request: Request):
        _require_admin_web_user(request)
        row = await db.get_manual_payment_claim(user_id)
        if not row:
            raise HTTPException(status_code=404, detail="not_found")
        fn = (row.get("receipt_stored_filename") or "").strip()
        vid = int(row.get("vault_id") or 0)
        if not fn or not vid:
            raise HTTPException(status_code=404, detail="no_receipt")
        data = await read_stored_file(vid, fn)
        if not data:
            raise HTTPException(status_code=404, detail="missing_file")
        media = (row.get("receipt_mime") or "").strip() or "application/octet-stream"
        return Response(content=data, media_type=media)

    @app.post("/admin/api/grant")
    async def admin_grant_web(body: AdminGrantBody, request: Request):
        _require_admin_web_user(request)
        vault_id = await db.get_vault_for_user(body.target_user_id)
        await db.add_extra_slots(vault_id, body.slots)
        await db.delete_manual_payment_claim(body.target_user_id)
        extra = await db.get_purchased_extra_slots(vault_id)
        cap: int | None = None
        if FREE_DOCUMENT_LIMIT > 0:
            cap = FREE_DOCUMENT_LIMIT + extra
        return {
            "ok": True,
            "vault_id": vault_id,
            "target_user_id": body.target_user_id,
            "slots_added": body.slots,
            "purchased_extra_slots_total": extra,
            "document_cap": cap,
        }

    @app.post("/admin/api/deny-claim")
    async def admin_deny_claim_web(
        body: AdminDenyClaimBody,
        request: Request,
    ):
        _require_admin_web_user(request)
        row = await db.get_manual_payment_claim(body.target_user_id)
        if not row:
            raise HTTPException(status_code=404, detail="no_such_claim")
        await db.delete_manual_payment_claim(body.target_user_id)
        return {"ok": True, "target_user_id": body.target_user_id}

    @app.post("/api/billing/invoice")
    async def api_billing_invoice(ctx: InitContext = VaultOK):
        if FREE_DOCUMENT_LIMIT <= 0:
            raise HTTPException(status_code=400, detail="billing_disabled")
        if BILLING_MODE == "manual":
            raise HTTPException(
                status_code=400, detail="billing_manual_transfer"
            )
        if BILLING_MODE in ("payme", "click"):
            if not paytech_configured():
                raise HTTPException(
                    status_code=503, detail="paytech_not_configured"
                )
            try:
                url, open_with = create_paytech_checkout(ctx.vault_id)
            except Exception:
                log.exception("PayTech checkout failed")
                raise HTTPException(
                    status_code=503, detail="checkout_unavailable"
                ) from None
            return {"checkout_url": url, "open_with": open_with}
        try:
            url = await create_upgrade_invoice_link(ctx.vault_id)
        except Exception:
            log.exception("Telegram invoice failed")
            raise HTTPException(
                status_code=503, detail="invoice_unavailable"
            ) from None
        return {"checkout_url": url, "open_with": "telegram_invoice"}

    @app.post("/payments/payme/webhook")
    async def paytech_payme_webhook(request: Request):
        if BILLING_MODE != "payme":
            raise HTTPException(status_code=404)
        if paytech_integration.SessionLocal is None:
            raise HTTPException(status_code=503)
        db = paytech_integration.SessionLocal()
        try:
            handler = FamDocPaymeHandler(
                db=db,
                payme_id=PAYME_MERCHANT_ID,
                payme_key=PAYME_MERCHANT_KEY or "",
                account_model=FamdocPaytechOrder,
                account_field="order_id",
                amount_field="amount",
                one_time_payment=True,
            )
            return await handler.handle_webhook(request)
        finally:
            db.close()

    @app.post("/payments/click/webhook")
    async def paytech_click_webhook(request: Request):
        if BILLING_MODE != "click":
            raise HTTPException(status_code=404)
        if paytech_integration.SessionLocal is None:
            raise HTTPException(status_code=503)
        db = paytech_integration.SessionLocal()
        try:
            handler = FamDocClickHandler(
                db=db,
                service_id=CLICK_SERVICE_ID,
                secret_key=CLICK_SECRET_KEY,
                account_model=FamdocPaytechOrder,
                commission_percent=0.0,
                account_field="id",
                one_time_payment=True,
            )
            return await handler.handle_webhook(request)
        finally:
            db.close()

    @app.post("/api/upload")
    async def api_upload(
        ctx: InitContext = VaultOK,
        category: str = Form(...),
        file: UploadFile = File(...),
        display_name: str = Form(""),
        tags: str = Form(""),
        notes: str = Form(""),
        crypto_mode: str = Form("plaintext"),
        crypto_iv: str = Form(""),
        crypto_tag: str = Form(""),
        preview_crypto_iv: str = Form(""),
        preview_crypto_tag: str = Form(""),
        preview_file: UploadFile | None = File(None),
    ):
        if category not in CATEGORY_LABELS:
            raise HTTPException(status_code=400, detail="Unknown category")
        cap = await effective_document_cap(ctx.vault_id)
        if cap is not None:
            doc_count = await db.count_documents(ctx.vault_id)
            if doc_count >= cap:
                raise HTTPException(
                    status_code=402,
                    detail="document_limit_reached",
                )
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Empty file")
        vcrypto = await db.get_vault_crypto(ctx.vault_id)
        vault_e2e = vcrypto is not None
        is_e2e = (crypto_mode or "").strip().lower() == "e2e"
        if vault_e2e and not is_e2e:
            raise HTTPException(status_code=400, detail="vault_requires_client_encryption")
        if is_e2e and not vault_e2e:
            raise HTTPException(status_code=400, detail="e2e_not_enabled_for_vault")
        if is_e2e:
            if not (crypto_iv or "").strip() or not (crypto_tag or "").strip():
                raise HTTPException(status_code=400, detail="missing_crypto_metadata")

        orig = resolve_display_filename(display_name, file.filename)
        stored = await save_upload(ctx.vault_id, orig, raw)
        suggested = suggest_category(orig, file.content_type)

        preview_stored = ""
        p_iv = (preview_crypto_iv or "").strip()
        p_tag = (preview_crypto_tag or "").strip()
        preview_raw: bytes | None = None
        if preview_file is not None:
            preview_raw = await preview_file.read()
            if preview_raw and is_e2e and (not p_iv or not p_tag):
                raise HTTPException(
                    status_code=400, detail="missing_preview_crypto_metadata"
                )

        jpeg: bytes | None = None
        if not is_e2e:
            jpeg = await asyncio.to_thread(build_preview_jpeg, raw, file.content_type)
            if jpeg:
                preview_stored = await save_upload(ctx.vault_id, "preview.jpg", jpeg)
        elif preview_raw:
            preview_stored = await save_upload(
                ctx.vault_id, "preview.enc.jpg", preview_raw
            )

        new_id = await db.add_document(
            family_chat_id=ctx.vault_id,
            category=category,
            stored_filename=stored,
            original_filename=orig,
            mime_type=file.content_type,
            file_size=len(raw),
            tags=tags.strip(),
            notes=notes.strip(),
            preview_stored_filename=preview_stored,
            crypto_encrypted=1 if is_e2e else 0,
            crypto_iv=(crypto_iv.strip() if is_e2e else ""),
            crypto_tag=(crypto_tag.strip() if is_e2e else ""),
            preview_crypto_iv=(p_iv if is_e2e and preview_raw else ""),
            preview_crypto_tag=(p_tag if is_e2e and preview_raw else ""),
            encryption_state="encrypted" if is_e2e else "legacy_plaintext",
        )
        return {
            "id": new_id,
            "category": category,
            "suggested_category": suggested,
            "suggested_match": suggested == category,
        }

    @app.get("/brand/logo-transparent.png")
    async def brand_logo_transparent():
        await branding.load_branding_assets()
        data = branding.get_transparent_bytes() or branding.get_solid_bytes()
        if data:
            return Response(content=data, media_type="image/png")
        if LOGO_TRANSPARENT_PATH.is_file():
            return FileResponse(LOGO_TRANSPARENT_PATH, media_type="image/png")
        if LOGO_SOLID_PATH.is_file():
            return FileResponse(LOGO_SOLID_PATH, media_type="image/png")
        raise HTTPException(
            status_code=404,
            detail="Add logos to R2 (see FAMDOC_LOGO_*_KEY) or Logo_*.png in the project root",
        )

    @app.get("/brand/logo.png")
    async def brand_logo_solid():
        await branding.load_branding_assets()
        data = branding.get_solid_bytes() or branding.get_transparent_bytes()
        if data:
            return Response(content=data, media_type="image/png")
        if LOGO_SOLID_PATH.is_file():
            return FileResponse(LOGO_SOLID_PATH, media_type="image/png")
        if LOGO_TRANSPARENT_PATH.is_file():
            return FileResponse(LOGO_TRANSPARENT_PATH, media_type="image/png")
        raise HTTPException(
            status_code=404,
            detail="Add logos to R2 (see FAMDOC_LOGO_*_KEY) or Logo_*.png in the project root",
        )

    # Static assets MUST NOT be mounted at "/" — that catches /api/* and returns 404
    # from StaticFiles. Serve the SPA entry at GET / and assets under /static/.
    app.mount(
        "/static",
        StaticFiles(directory=str(webapp_dir)),
        name="webapp_static",
    )

    @app.get("/")
    async def spa_index():
        await branding.load_branding_assets()
        return HTMLResponse(
            content=build_spa_html(webapp_dir),
            media_type="text/html",
        )

    return app
