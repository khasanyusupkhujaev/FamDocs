import base64
import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv

# Project root (folder that contains `bot/`). Load .env from here so it works
# even when you run `python -m bot.main` from another cwd.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError(
        f"Set TELEGRAM_BOT_TOKEN in {BASE_DIR / '.env'} "
        "(copy from .env.example) or export it in your shell. "
        "Get a token from @BotFather on Telegram."
    )

# One root for all files; categories are metadata in the DB, not separate OS trees.
DATA_DIR = Path(os.getenv("FAMDOC_DATA_DIR", str(BASE_DIR / "data")))
STORAGE_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "famdoc.db"

def _normalize_s3_endpoint(raw: str | None) -> str | None:
    """
    Cloudflare R2 / S3 API URL must be the account endpoint only, e.g.
    https://<accountid>.r2.cloudflarestorage.com — not .../bucket-name.
    """
    if not raw:
        return None
    raw = raw.strip()
    p = urlparse(raw)
    if not p.scheme or not p.netloc:
        return raw or None
    # Strip accidental path (often users paste bucket in the URL)
    if p.path not in ("", "/"):
        return urlunparse((p.scheme, p.netloc, "", "", "", ""))
    return raw


# --- Blob storage: local (dev) or S3-compatible (production) ---
# If FAMDOC_STORAGE is unset but S3 bucket + keys are set, we default to s3 (no silent local uploads).
_explicit_storage = os.getenv("FAMDOC_STORAGE", "").strip().lower()
S3_ENDPOINT_URL = _normalize_s3_endpoint(os.getenv("FAMDOC_S3_ENDPOINT", "").strip())
S3_BUCKET = os.getenv("FAMDOC_S3_BUCKET", "").strip()
S3_ACCESS_KEY_ID = os.getenv("FAMDOC_S3_ACCESS_KEY_ID", "").strip()
S3_SECRET_ACCESS_KEY = os.getenv("FAMDOC_S3_SECRET_ACCESS_KEY", "").strip()
S3_REGION = os.getenv("FAMDOC_S3_REGION", "auto").strip() or "auto"
S3_PREFIX = (os.getenv("FAMDOC_S3_PREFIX", "famdoc").strip().strip("/") or "famdoc")

_s3_credentials_present = bool(S3_BUCKET and S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY)

if _explicit_storage and _explicit_storage not in ("local", "s3", "r2", "minio"):
    raise RuntimeError(
        f"Unknown FAMDOC_STORAGE={_explicit_storage!r}. Use local, s3, r2, or minio."
    )
if _explicit_storage in ("local", "s3", "r2", "minio"):
    STORAGE_BACKEND = "s3" if _explicit_storage in ("s3", "r2", "minio") else "local"
elif _s3_credentials_present:
    STORAGE_BACKEND = "s3"
else:
    STORAGE_BACKEND = "local"

if STORAGE_BACKEND == "s3" and not _s3_credentials_present:
    raise RuntimeError(
        "FAMDOC_STORAGE is s3 (or auto-selected) but FAMDOC_S3_BUCKET, "
        "FAMDOC_S3_ACCESS_KEY_ID, and FAMDOC_S3_SECRET_ACCESS_KEY must all be set in .env"
    )


def _parse_file_master_key(raw: str) -> bytes:
    raw = raw.strip()
    try:
        pad = "=" * (-len(raw) % 4)
        decoded = base64.b64decode(raw + pad)
        if len(decoded) == 32:
            return decoded
    except Exception:
        pass
    if len(raw) == 64 and all(c in "0123456789abcdefABCDEF" for c in raw):
        return bytes.fromhex(raw)
    raise RuntimeError(
        "FAMDOC_FILE_MASTER_KEY must decode to 32 bytes (AES-256). "
        "Use base64 or 64 hex chars. Generate: python -c "
        '"import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"'
    )


# Optional AES-256-GCM for blobs at rest (recommended in production).
_FILE_MASTER_RAW = os.getenv("FAMDOC_FILE_MASTER_KEY", "").strip()
FILE_ENCRYPTION_KEY: bytes | None
if _FILE_MASTER_RAW:
    FILE_ENCRYPTION_KEY = _parse_file_master_key(_FILE_MASTER_RAW)
else:
    FILE_ENCRYPTION_KEY = None

# Telegram Mini App — must be HTTPS in production (e.g. ngrok https URL → WEBAPP_PORT).
_webapp = os.getenv("WEBAPP_PUBLIC_URL", "").strip().rstrip("/")
WEBAPP_PUBLIC_URL: str | None = _webapp if _webapp else None
WEBAPP_MENU_BUTTON_TEXT = (os.getenv("WEBAPP_MENU_BUTTON_TEXT", "FamDocs") or "FamDocs").strip()
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0").strip()
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8080"))

# Free tier: max documents per vault (web + Telegram). Set to 0 to disable the cap.
FREE_DOCUMENT_LIMIT = int(os.getenv("FAMDOC_FREE_DOCUMENT_LIMIT", "10"))

# Telegram Payments: leave empty to use Telegram Stars (XTR). Otherwise use the token from @BotFather (e.g. Stripe).
PAYMENT_PROVIDER_TOKEN = os.getenv("FAMDOC_PAYMENT_PROVIDER_TOKEN", "").strip()
# Fiat: ISO currency + amount in smallest units (e.g. USD cents). Ignored when using Stars.
UPGRADE_CURRENCY = (os.getenv("FAMDOC_UPGRADE_CURRENCY", "USD") or "USD").strip().upper()
UPGRADE_AMOUNT_MINOR = int(os.getenv("FAMDOC_UPGRADE_AMOUNT_MINOR", "299"))
# Stars: whole stars per purchase (used when provider token is empty).
UPGRADE_STARS = int(os.getenv("FAMDOC_UPGRADE_STARS", "150"))
# How many extra document slots one purchase adds (on top of FAMDOC_FREE_DOCUMENT_LIMIT).
UPGRADE_EXTRA_SLOTS = int(os.getenv("FAMDOC_UPGRADE_EXTRA_SLOTS", "50"))
UPGRADE_INVOICE_TITLE = (os.getenv("FAMDOC_UPGRADE_TITLE", "More document slots") or "More slots").strip()[
    :32
]
UPGRADE_INVOICE_DESCRIPTION = (
    os.getenv(
        "FAMDOC_UPGRADE_DESCRIPTION",
        "Add more space for family documents in your vault.",
    )
    or "Extra document slots"
).strip()[:255]

# Billing: telegram (Stars / provider) | payme | click (PayTech) | manual (bank card + admin grants).
_billing_mode = (os.getenv("FAMDOC_BILLING_MODE", "telegram") or "telegram").strip().lower()
BILLING_MODE = (
    _billing_mode
    if _billing_mode in ("telegram", "payme", "click", "manual")
    else "telegram"
)


def _parse_admin_ids(raw: str) -> frozenset[int]:
    ids: list[int] = []
    for part in (raw or "").replace(" ", "").split(","):
        if part.isdigit() and int(part) > 0:
            ids.append(int(part))
    return frozenset(ids)


# Comma-separated Telegram user ids who may use /stats, /grant, /admin.
ADMIN_TELEGRAM_IDS: frozenset[int] = _parse_admin_ids(
    os.getenv("FAMDOC_ADMIN_TELEGRAM_IDS", "")
)
# Comma-separated @usernames (without @) — resolved to numeric ids at bot startup.
ADMIN_USERNAMES: tuple[str, ...] = tuple(
    p.strip().lstrip("@")
    for p in (os.getenv("FAMDOC_ADMIN_USERNAMES", "") or "").split(",")
    if p.strip()
)


def is_miniapp_admin(telegram_user_id: int, telegram_username: str | None) -> bool:
    """
    True if this Telegram account may use Mini App admin UI and /api/admin/*.
    Matches FAMDOC_ADMIN_TELEGRAM_IDS or FAMDOC_ADMIN_USERNAMES (case-insensitive).
    If the user hides their username in Telegram, use numeric id in FAMDOC_ADMIN_TELEGRAM_IDS.
    """
    if telegram_user_id in ADMIN_TELEGRAM_IDS:
        return True
    un = (telegram_username or "").strip().lstrip("@").lower()
    if not un:
        return False
    allowed = {a.strip().lstrip("@").lower() for a in ADMIN_USERNAMES if a.strip()}
    return un in allowed

# Manual transfer: show card number (spaces ok) and optional bank name line.
TRANSFER_CARD_DISPLAY = os.getenv("FAMDOC_TRANSFER_CARD", "").strip()
TRANSFER_INSTRUCTIONS = (os.getenv("FAMDOC_TRANSFER_INSTRUCTIONS", "") or "").strip()

# Monthly tiers (UZS): price → extra document slots per month (admin grants after payment).
MANUAL_TIER_1_UZS = int(os.getenv("FAMDOC_TIER1_UZS", "10000"))
MANUAL_TIER_1_SLOTS = int(os.getenv("FAMDOC_TIER1_SLOTS", "10"))
MANUAL_TIER_2_UZS = int(os.getenv("FAMDOC_TIER2_UZS", "25000"))
MANUAL_TIER_2_SLOTS = int(os.getenv("FAMDOC_TIER2_SLOTS", "20"))
MANUAL_TIER_3_UZS = int(os.getenv("FAMDOC_TIER3_UZS", "50000"))
MANUAL_TIER_3_SLOTS = int(os.getenv("FAMDOC_TIER3_SLOTS", "40"))


def manual_billing_tiers() -> list[dict[str, int | str]]:
    """Three subscription tiers for FAMDOC_BILLING_MODE=manual (UZS / month)."""
    return [
        {"id": "tier1", "price_uzs": MANUAL_TIER_1_UZS, "slots": MANUAL_TIER_1_SLOTS},
        {"id": "tier2", "price_uzs": MANUAL_TIER_2_UZS, "slots": MANUAL_TIER_2_SLOTS},
        {"id": "tier3", "price_uzs": MANUAL_TIER_3_UZS, "slots": MANUAL_TIER_3_SLOTS},
    ]


def manual_tier_allowed_amounts() -> frozenset[int]:
    return frozenset(int(t["price_uzs"]) for t in manual_billing_tiers())

# PayTech.uz license (https://pay-tech.uz/console) — required by paytechuz when creating Payme/Click links.
# Loaded via dotenv above; paytechuz reads PAYTECH_LICENSE_API_KEY from the environment.

# Uzbek som price for one extra-slot pack (Payme/Click).
PAYTECH_PRICE_UZS = int(os.getenv("FAMDOC_PAYTECH_PRICE_UZS", "49990"))

# Payme merchant credentials
PAYME_MERCHANT_ID = os.getenv("FAMDOC_PAYME_MERCHANT_ID", "").strip()
PAYME_MERCHANT_KEY = os.getenv("FAMDOC_PAYME_KEY", "").strip()
PAYME_TEST_MODE = os.getenv("FAMDOC_PAYME_TEST", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Click merchant credentials
CLICK_SERVICE_ID = os.getenv("FAMDOC_CLICK_SERVICE_ID", "").strip()
CLICK_MERCHANT_ID = os.getenv("FAMDOC_CLICK_MERCHANT_ID", "").strip()
CLICK_MERCHANT_USER_ID = os.getenv("FAMDOC_CLICK_MERCHANT_USER_ID", "").strip()
CLICK_SECRET_KEY = os.getenv("FAMDOC_CLICK_SECRET_KEY", "").strip()
CLICK_TEST_MODE = os.getenv("FAMDOC_CLICK_TEST", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Optional branding: local files and/or objects in the same R2/S3 bucket as document blobs.
LOGO_TRANSPARENT_PATH = BASE_DIR / "Logo_transparent.png"
LOGO_SOLID_PATH = BASE_DIR / "Logo.png"
# Full object keys in the bucket (not relative to family id). If unset with S3, defaults to
# "{S3_PREFIX}/brand/Logo_transparent.png" and "{S3_PREFIX}/brand/Logo.png".
LOGO_TRANSPARENT_S3_KEY = os.getenv("FAMDOC_LOGO_TRANSPARENT_KEY", "").strip()
LOGO_SOLID_S3_KEY = os.getenv("FAMDOC_LOGO_SOLID_KEY", "").strip()

# For invite deep links: https://t.me/<username>?start=join_TOKEN
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
