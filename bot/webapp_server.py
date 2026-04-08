from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

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
from bot.config import (
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
    WEBAPP_PUBLIC_URL,
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


@dataclass
class InitContext:
    vault_id: int
    user_id: int
    mode: str  # "private" | "group"


def _download_file_response(data: bytes, media: str, fname: str) -> Response:
    """Attachment + CORS for Telegram WebApp downloadFile (Bot API 8.0+)."""
    cd = "attachment; filename*=UTF-8''" + quote(fname, safe="")
    return Response(
        content=data,
        media_type=media,
        headers={
            "Content-Disposition": cd,
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


async def _init_context_from_raw(init_data: str) -> InitContext:
    try:
        vals = validate_init_data(init_data.strip(), BOT_TOKEN)
        vault_id, mode = await vault_id_from_init_vals(vals)
        if not vals.get("user"):
            raise ValueError("no user")
        user = json.loads(vals["user"])
        uid = int(user["id"])
        return InitContext(vault_id=vault_id, user_id=uid, mode=mode)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid init data") from None


def _png_data_uri(path: Path) -> str | None:
    if not path.is_file():
        return None
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def build_spa_html(webapp_dir: Path) -> str:
    """
    Inline CSS/JS (and logos) into index.html.

    Telegram WebViews + ngrok often fail to load separate /static/* assets
    (interstitials / extra hops), which yields unstyled HTML and no JS — so
    categories never appear. One HTML response fixes that.

    The Telegram Web App bridge script is inlined too: if /static/telegram-web-app.js
    never loads, window.Telegram.WebApp has no initData and the app shows "Open from Telegram…".
    """
    html = (webapp_dir / "index.html").read_text(encoding="utf-8")
    twa_path = webapp_dir / "telegram-web-app.js"
    if twa_path.is_file():
        twa = twa_path.read_text(encoding="utf-8")

        def _inline_twa(_m: re.Match[str]) -> str:
            return "<script>\n" + twa + "\n</script>"

        html2 = re.sub(
            r'<script\s+src="/static/telegram-web-app\.js"\s*>\s*</script>',
            _inline_twa,
            html,
            count=1,
        )
        if html2 == html:
            raise RuntimeError(
                "index.html must include "
                '<script src="/static/telegram-web-app.js"></script> for Mini App inlining'
            )
        html = html2
    css = (webapp_dir / "styles.css").read_text(encoding="utf-8")
    i18n_path = webapp_dir / "i18n.js"
    i18n_block = (
        i18n_path.read_text(encoding="utf-8") + "\n"
        if i18n_path.is_file()
        else ""
    )
    js = (webapp_dir / "app.js").read_text(encoding="utf-8")
    html = html.replace(
        '<link rel="stylesheet" href="/static/styles.css" />',
        f"<style>\n{css}\n</style>",
    )
    html = html.replace(
        '<script src="/static/app.js"></script>',
        f"<script>\n{i18n_block}{js}\n</script>",
    )
    transparent = _png_data_uri(LOGO_TRANSPARENT_PATH) or _png_data_uri(LOGO_SOLID_PATH)
    solid = _png_data_uri(LOGO_SOLID_PATH) or _png_data_uri(LOGO_TRANSPARENT_PATH)
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
        yield

    app = FastAPI(
        title="FamDoc Mini App API",
        version="2.0",
        lifespan=_lifespan,
    )

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

    @app.get("/api/bootstrap")
    async def api_bootstrap(ctx: InitContext = Init) -> dict:
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
        if BILLING_MODE in ("payme", "click"):
            pl = format_paytech_price_label()
            uses_stars = False
            paytech_ok = paytech_configured()
        else:
            pl = upgrade_price_label()
            uses_stars = billing_uses_stars()
            paytech_ok = True
        return {
            "categories": cats,
            "total": total,
            "context_mode": ctx.mode,
            "document_limit": doc_limit,
            "free_document_limit": FREE_DOCUMENT_LIMIT,
            "purchased_extra_slots": extra,
            "billing": {
                "upgrade_enabled": FREE_DOCUMENT_LIMIT > 0
                and (BILLING_MODE == "telegram" or paytech_ok),
                "slots_per_purchase": UPGRADE_EXTRA_SLOTS,
                "price_label": pl,
                "uses_stars": uses_stars,
                "mode": BILLING_MODE,
                "paytech_ready": paytech_ok,
            },
        }

    @app.get("/api/documents")
    async def api_list_documents(
        category: str | None = None,
        q: str | None = None,
        ctx: InitContext = Init,
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
                }
                for r in rows
            ]
        }

    @app.patch("/api/documents/{doc_id}")
    async def api_patch_document(
        doc_id: int,
        body: DocPatch,
        ctx: InitContext = Init,
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
    async def api_delete_document(doc_id: int, ctx: InitContext = Init):
        meta = await db.get_document(ctx.vault_id, doc_id)
        if not meta:
            raise HTTPException(status_code=404, detail="Not found")
        await db.delete_document_row(ctx.vault_id, doc_id)
        await _remove_doc_blobs(ctx.vault_id, meta)
        return {"ok": True}

    @app.post("/api/documents/bulk-delete")
    async def api_bulk_delete(payload: BulkIds, ctx: InitContext = Init):
        metas = await db.get_documents_meta(ctx.vault_id, payload.ids)
        found = {m["id"] for m in metas}
        if found != set(payload.ids):
            raise HTTPException(status_code=400, detail="Invalid or foreign ids")
        await db.delete_documents(ctx.vault_id, payload.ids)
        for m in metas:
            await _remove_doc_blobs(ctx.vault_id, m)
        return {"ok": True, "deleted": len(payload.ids)}

    @app.post("/api/documents/bulk-move")
    async def api_bulk_move(payload: BulkMove, ctx: InitContext = Init):
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
    async def api_bulk_zip(payload: BulkIds, ctx: InitContext = Init):
        return await _bulk_zip_response(ctx, payload.ids)

    @app.get("/api/documents/bulk-zip")
    async def api_bulk_zip_get(
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
        meta = await db.get_document(ctx.vault_id, doc_id)
        if not meta:
            raise HTTPException(status_code=404, detail="Not found")
        data = await read_stored_file(ctx.vault_id, meta["stored_filename"])
        if not data:
            raise HTTPException(status_code=404, detail="Missing file")
        media = meta.get("mime_type") or "application/octet-stream"
        fname = meta["original_filename"]
        return _download_file_response(data, media, fname)

    @app.get("/api/shared/documents/{doc_id}")
    async def api_shared_document(
        doc_id: int,
        vault_id: int,
        exp: int,
        sig: str,
    ):
        if not verify_file_share(doc_id, vault_id, exp, sig):
            raise HTTPException(status_code=404, detail="Invalid or expired link")
        meta = await db.get_document(vault_id, doc_id)
        if not meta:
            raise HTTPException(status_code=404, detail="Not found")
        data = await read_stored_file(vault_id, meta["stored_filename"])
        if not data:
            raise HTTPException(status_code=404, detail="Missing file")
        media = meta.get("mime_type") or "application/octet-stream"
        fname = meta["original_filename"]
        return _download_file_response(data, media, fname)

    @app.get("/api/documents/{doc_id}/share-link")
    async def api_document_share_link(doc_id: int, ctx: InitContext = Init):
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
    async def api_doc_preview(doc_id: int, ctx: InitContext = Init):
        meta = await db.get_document(ctx.vault_id, doc_id)
        if not meta:
            raise HTTPException(status_code=404, detail="Not found")
        prev = (meta.get("preview_stored_filename") or "").strip()
        if prev:
            data = await read_stored_file(ctx.vault_id, prev)
            if data:
                return Response(content=data, media_type="image/jpeg")
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
    async def api_family(ctx: InitContext = Init):
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
        ctx: InitContext = Init,
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
    async def api_family_accept(body: InviteAcceptBody, ctx: InitContext = Init):
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

    @app.post("/api/billing/invoice")
    async def api_billing_invoice(ctx: InitContext = Init):
        if FREE_DOCUMENT_LIMIT <= 0:
            raise HTTPException(status_code=400, detail="billing_disabled")
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
        ctx: InitContext = Init,
        category: str = Form(...),
        file: UploadFile = File(...),
        display_name: str = Form(""),
        tags: str = Form(""),
        notes: str = Form(""),
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
        orig = resolve_display_filename(display_name, file.filename)
        stored = await save_upload(ctx.vault_id, orig, raw)
        suggested = suggest_category(orig, file.content_type)
        new_id = await db.add_document(
            family_chat_id=ctx.vault_id,
            category=category,
            stored_filename=stored,
            original_filename=orig,
            mime_type=file.content_type,
            file_size=len(raw),
            tags=tags.strip(),
            notes=notes.strip(),
        )
        jpeg = await asyncio.to_thread(build_preview_jpeg, raw, file.content_type)
        if jpeg:
            pstored = await save_upload(ctx.vault_id, "preview.jpg", jpeg)
            await db.set_document_preview(ctx.vault_id, new_id, pstored)
        return {
            "id": new_id,
            "category": category,
            "suggested_category": suggested,
            "suggested_match": suggested == category,
        }

    @app.get("/brand/logo-transparent.png")
    async def brand_logo_transparent():
        if LOGO_TRANSPARENT_PATH.is_file():
            return FileResponse(LOGO_TRANSPARENT_PATH, media_type="image/png")
        if LOGO_SOLID_PATH.is_file():
            return FileResponse(LOGO_SOLID_PATH, media_type="image/png")
        raise HTTPException(
            status_code=404,
            detail="Add Logo_transparent.png or Logo.png to the project root",
        )

    @app.get("/brand/logo.png")
    async def brand_logo_solid():
        if LOGO_SOLID_PATH.is_file():
            return FileResponse(LOGO_SOLID_PATH, media_type="image/png")
        if LOGO_TRANSPARENT_PATH.is_file():
            return FileResponse(LOGO_TRANSPARENT_PATH, media_type="image/png")
        raise HTTPException(
            status_code=404,
            detail="Add Logo.png or Logo_transparent.png to the project root",
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
        return HTMLResponse(
            content=build_spa_html(webapp_dir),
            media_type="text/html",
        )

    return app
