import hashlib
import secrets
import aiosqlite
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.config import DB_PATH


DOCUMENTS_DDL = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_chat_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    mime_type TEXT,
    file_size INTEGER,
    uploaded_at TEXT NOT NULL,
    tags TEXT DEFAULT '',
    notes TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_docs_family ON documents(family_chat_id);
CREATE INDEX IF NOT EXISTS idx_docs_family_cat ON documents(family_chat_id, category);
"""

VAULT_INVITES_DDL = """
CREATE TABLE IF NOT EXISTS vault_members (
    user_id INTEGER PRIMARY KEY NOT NULL,
    vault_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    joined_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vault_members_vault ON vault_members(vault_id);

CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    vault_id INTEGER NOT NULL,
    created_by_user_id INTEGER NOT NULL,
    phone_digits TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    accepted_at TEXT,
    accepted_user_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_invites_vault ON invites(vault_id);
"""

SCHEMA = DOCUMENTS_DDL + "\n" + VAULT_INVITES_DDL


async def _ensure_family_tables(db: aiosqlite.Connection) -> None:
    """
    Older famdoc.db files may predate vault_members / invites. A failed or partial
    init can also leave documents without family tables. Apply DDL if missing.
    """
    cur = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        ("vault_members",),
    )
    has_vault = await cur.fetchone() is not None
    cur = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        ("invites",),
    )
    has_invites = await cur.fetchone() is not None
    if not has_vault or not has_invites:
        await db.executescript(VAULT_INVITES_DDL)

# Migrate legacy category keys from earlier FamDoc versions
LEGACY_CATEGORY_MAP: dict[str, str] = {
    "passport": "legal_documents",
    "birth_certificate": "legal_documents",
    "diploma": "school_certificates",
    "agreement": "legal_documents",
    "other": "family_photos_other",
}


async def _migrate_schema(db: aiosqlite.Connection) -> None:
    cur = await db.execute("PRAGMA table_info(documents)")
    cols = {r[1] for r in await cur.fetchall()}
    if "tags" not in cols:
        await db.execute("ALTER TABLE documents ADD COLUMN tags TEXT DEFAULT ''")
    if "notes" not in cols:
        await db.execute("ALTER TABLE documents ADD COLUMN notes TEXT DEFAULT ''")
    if "preview_stored_filename" not in cols:
        await db.execute(
            "ALTER TABLE documents ADD COLUMN preview_stored_filename TEXT DEFAULT ''"
        )
    for old, new in LEGACY_CATEGORY_MAP.items():
        await db.execute(
            "UPDATE documents SET category = ? WHERE category = ?",
            (new, old),
        )
    # Private-chat vault rows: one membership per Telegram user (positive ids only).
    await db.execute(
        """
        INSERT OR IGNORE INTO vault_members (user_id, vault_id, role, joined_at)
        SELECT DISTINCT family_chat_id, family_chat_id, 'owner', datetime('now')
        FROM documents
        WHERE family_chat_id > 0
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS vault_entitlements (
            vault_id INTEGER PRIMARY KEY NOT NULL,
            extra_slots INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_ledger (
            telegram_payment_charge_id TEXT PRIMARY KEY NOT NULL,
            vault_id INTEGER NOT NULL,
            slots_granted INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await _ensure_family_tables(db)
        await _migrate_schema(db)
        await db.commit()


async def add_document(
    *,
    family_chat_id: int,
    category: str,
    stored_filename: str,
    original_filename: str,
    mime_type: str | None,
    file_size: int | None,
    tags: str = "",
    notes: str = "",
    preview_stored_filename: str = "",
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO documents
            (family_chat_id, category, stored_filename, original_filename,
             mime_type, file_size, uploaded_at, tags, notes, preview_stored_filename)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                family_chat_id,
                category,
                stored_filename,
                original_filename,
                mime_type,
                file_size,
                now,
                tags,
                notes,
                preview_stored_filename or "",
            ),
        )
        await db.commit()
        return int(cur.lastrowid)


def _row_to_dict(r: aiosqlite.Row) -> dict[str, Any]:
    d = dict(r)
    d.setdefault("tags", "")
    d.setdefault("notes", "")
    d.setdefault("preview_stored_filename", "")
    return d


async def list_documents(
    family_chat_id: int,
    category: str | None = None,
    *,
    search: str | None = None,
) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        q = (search or "").strip()
        like = f"%{q}%" if q else None
        if category:
            if like:
                cur = await db.execute(
                    """
                    SELECT id, category, stored_filename, original_filename,
                           mime_type, file_size, uploaded_at, tags, notes,
                           IFNULL(preview_stored_filename,'') AS preview_stored_filename
                    FROM documents
                    WHERE family_chat_id = ? AND category = ?
                      AND (
                        original_filename LIKE ? COLLATE NOCASE
                        OR IFNULL(tags,'') LIKE ? COLLATE NOCASE
                        OR IFNULL(notes,'') LIKE ? COLLATE NOCASE
                      )
                    ORDER BY uploaded_at DESC
                    """,
                    (family_chat_id, category, like, like, like),
                )
            else:
                cur = await db.execute(
                    """
                    SELECT id, category, stored_filename, original_filename,
                           mime_type, file_size, uploaded_at, tags, notes,
                           IFNULL(preview_stored_filename,'') AS preview_stored_filename
                    FROM documents
                    WHERE family_chat_id = ? AND category = ?
                    ORDER BY uploaded_at DESC
                    """,
                    (family_chat_id, category),
                )
        else:
            if like:
                cur = await db.execute(
                    """
                    SELECT id, category, stored_filename, original_filename,
                           mime_type, file_size, uploaded_at, tags, notes,
                           IFNULL(preview_stored_filename,'') AS preview_stored_filename
                    FROM documents
                    WHERE family_chat_id = ?
                      AND (
                        original_filename LIKE ? COLLATE NOCASE
                        OR IFNULL(tags,'') LIKE ? COLLATE NOCASE
                        OR IFNULL(notes,'') LIKE ? COLLATE NOCASE
                      )
                    ORDER BY uploaded_at DESC
                    """,
                    (family_chat_id, like, like, like),
                )
            else:
                cur = await db.execute(
                    """
                    SELECT id, category, stored_filename, original_filename,
                           mime_type, file_size, uploaded_at, tags, notes,
                           IFNULL(preview_stored_filename,'') AS preview_stored_filename
                    FROM documents
                    WHERE family_chat_id = ?
                    ORDER BY uploaded_at DESC
                    """,
                    (family_chat_id,),
                )
        rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]


async def get_document(
    family_chat_id: int, doc_id: int
) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, category, stored_filename, original_filename,
                   mime_type, file_size, uploaded_at, tags, notes,
                   IFNULL(preview_stored_filename,'') AS preview_stored_filename
            FROM documents
            WHERE family_chat_id = ? AND id = ?
            """,
            (family_chat_id, doc_id),
        )
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def count_by_category(family_chat_id: int) -> dict[str, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT category, COUNT(*) as c
            FROM documents
            WHERE family_chat_id = ?
            GROUP BY category
            """,
            (family_chat_id,),
        )
        rows = await cur.fetchall()
        return {r[0]: r[1] for r in rows}


async def count_documents(family_chat_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM documents WHERE family_chat_id = ?",
            (family_chat_id,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def get_purchased_extra_slots(vault_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT extra_slots FROM vault_entitlements WHERE vault_id = ?",
            (vault_id,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def grant_extra_slots_for_payment(
    telegram_payment_charge_id: str,
    vault_id: int,
    slots: int,
) -> bool:
    """
    Record a Telegram payment and add slots. Idempotent per charge id.
    Returns True if this charge was new and slots were applied.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                """
                INSERT INTO payment_ledger
                (telegram_payment_charge_id, vault_id, slots_granted, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (telegram_payment_charge_id, vault_id, slots, now),
            )
        except aiosqlite.IntegrityError:
            await db.rollback()
            return False
        await db.execute(
            """
            INSERT INTO vault_entitlements (vault_id, extra_slots, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(vault_id) DO UPDATE SET
                extra_slots = vault_entitlements.extra_slots + excluded.extra_slots,
                updated_at = excluded.updated_at
            """,
            (vault_id, slots, now),
        )
        await db.commit()
        return True


async def add_extra_slots(vault_id: int, slots: int) -> None:
    """Increase purchased extra slots (admin grant or external reconciliation)."""
    if slots <= 0:
        return
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO vault_entitlements (vault_id, extra_slots, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(vault_id) DO UPDATE SET
                extra_slots = vault_entitlements.extra_slots + excluded.extra_slots,
                updated_at = excluded.updated_at
            """,
            (vault_id, slots, now),
        )
        await db.commit()


async def admin_statistics() -> dict[str, int]:
    """Dashboard counts: registered users, uploads activity, totals."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM vault_members")
        row = await cur.fetchone()
        users_registered = int(row[0]) if row else 0

        cur = await db.execute("SELECT COUNT(*) FROM documents")
        row = await cur.fetchone()
        total_documents = int(row[0]) if row else 0

        cur = await db.execute(
            "SELECT COUNT(DISTINCT family_chat_id) FROM documents"
        )
        row = await cur.fetchone()
        vaults_with_uploads = int(row[0]) if row else 0

    return {
        "users_registered": users_registered,
        "vaults_with_uploads": vaults_with_uploads,
        "total_documents": total_documents,
    }


async def update_document(
    family_chat_id: int,
    doc_id: int,
    *,
    original_filename: str | None = None,
    category: str | None = None,
    tags: str | None = None,
    notes: str | None = None,
) -> bool:
    fields: list[str] = []
    vals: list[Any] = []
    if original_filename is not None:
        fields.append("original_filename = ?")
        vals.append(original_filename)
    if category is not None:
        fields.append("category = ?")
        vals.append(category)
    if tags is not None:
        fields.append("tags = ?")
        vals.append(tags)
    if notes is not None:
        fields.append("notes = ?")
        vals.append(notes)
    if not fields:
        return True
    vals.extend([family_chat_id, doc_id])
    sql = f"UPDATE documents SET {', '.join(fields)} WHERE family_chat_id = ? AND id = ?"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, vals)
        await db.commit()
        return cur.rowcount > 0


async def delete_document_row(family_chat_id: int, doc_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM documents WHERE family_chat_id = ? AND id = ?",
            (family_chat_id, doc_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_documents(family_chat_id: int, doc_ids: list[int]) -> int:
    if not doc_ids:
        return 0
    placeholders = ",".join("?" * len(doc_ids))
    sql = f"DELETE FROM documents WHERE family_chat_id = ? AND id IN ({placeholders})"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, (family_chat_id, *doc_ids))
        await db.commit()
        return cur.rowcount


async def get_documents_meta(
    family_chat_id: int, doc_ids: list[int]
) -> list[dict[str, Any]]:
    if not doc_ids:
        return []
    placeholders = ",".join("?" * len(doc_ids))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""
            SELECT id, category, stored_filename, original_filename,
                   mime_type, file_size, uploaded_at, tags, notes,
                   IFNULL(preview_stored_filename,'') AS preview_stored_filename
            FROM documents
            WHERE family_chat_id = ? AND id IN ({placeholders})
            """,
            (family_chat_id, *doc_ids),
        )
        rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]


def _digits_only(phone: str) -> str:
    return "".join(c for c in (phone or "") if c.isdigit())


async def get_vault_for_user(user_id: int) -> int:
    """Resolve shared family vault for a private-chat user (positive Telegram id)."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT vault_id FROM vault_members WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if row:
            return int(row[0])
        await db.execute(
            """
            INSERT OR IGNORE INTO vault_members (user_id, vault_id, role, joined_at)
            VALUES (?, ?, 'owner', ?)
            """,
            (user_id, user_id, now),
        )
        await db.commit()
        return user_id


async def get_vault_membership(user_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, vault_id, role, joined_at FROM vault_members WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_vault_members(vault_id: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT user_id, vault_id, role, joined_at
            FROM vault_members
            WHERE vault_id = ?
            ORDER BY joined_at
            """,
            (vault_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def user_belongs_to_vault(user_id: int, vault_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM vault_members WHERE user_id = ? AND vault_id = ?",
            (user_id, vault_id),
        )
        return await cur.fetchone() is not None


async def create_invite(
    *,
    vault_id: int,
    created_by_user_id: int,
    phone: str = "",
) -> tuple[str, datetime]:
    """Returns (plaintext_token, expires_at)."""
    if not await user_belongs_to_vault(created_by_user_id, vault_id):
        raise PermissionError("not a member of this vault")
    token = secrets.token_urlsafe(24)
    th = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=7)
    now_s = now.isoformat()
    exp_s = exp.isoformat()
    digits = _digits_only(phone)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO invites (token_hash, vault_id, created_by_user_id, phone_digits, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (th, vault_id, created_by_user_id, digits, now_s, exp_s),
        )
        await db.commit()
    return token, exp


async def accept_invite(token: str, user_id: int) -> tuple[bool, str]:
    """
    Join a family vault via invite token.
    Returns (success, reason) where reason is ok|already_member|invalid|expired|already_in_family
    """
    raw = (token or "").strip()
    if not raw:
        return False, "invalid"
    th = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, vault_id, expires_at, accepted_at
            FROM invites
            WHERE token_hash = ?
            """,
            (th,),
        )
        inv = await cur.fetchone()
        if not inv:
            return False, "invalid"
        if inv["accepted_at"]:
            return False, "invalid"
        es = str(inv["expires_at"])
        if " " in es and "T" not in es:
            es = es.replace(" ", "T", 1)
        exp = datetime.fromisoformat(es.replace("Z", "+00:00"))
        exp_aware = exp.replace(tzinfo=timezone.utc) if exp.tzinfo is None else exp
        if now > exp_aware:
            return False, "expired"
        target_vault = int(inv["vault_id"])

        cur = await db.execute(
            "SELECT vault_id FROM vault_members WHERE user_id = ?",
            (user_id,),
        )
        m = await cur.fetchone()
        if not m:
            await db.execute(
                """
                INSERT OR IGNORE INTO vault_members (user_id, vault_id, role, joined_at)
                VALUES (?, ?, 'owner', ?)
                """,
                (user_id, user_id, now.isoformat()),
            )
            await db.commit()
            cur = await db.execute(
                "SELECT vault_id FROM vault_members WHERE user_id = ?",
                (user_id,),
            )
            m = await cur.fetchone()
        if not m:
            return False, "invalid"
        old_vault = int(m[0])
        if old_vault == target_vault:
            await db.execute(
                "UPDATE invites SET accepted_at = ?, accepted_user_id = ? WHERE id = ?",
                (now.isoformat(), user_id, inv["id"]),
            )
            await db.commit()
            return True, "already_member"
        if old_vault != user_id:
            return False, "already_in_family"
        await db.execute(
            """
            UPDATE documents SET family_chat_id = ? WHERE family_chat_id = ?
            """,
            (target_vault, old_vault),
        )
        await db.execute("DELETE FROM vault_members WHERE user_id = ?", (user_id,))
        await db.execute(
            """
            INSERT INTO vault_members (user_id, vault_id, role, joined_at)
            VALUES (?, ?, 'member', ?)
            """,
            (user_id, target_vault, now.isoformat()),
        )
        await db.execute(
            "UPDATE invites SET accepted_at = ?, accepted_user_id = ? WHERE id = ?",
            (now.isoformat(), user_id, inv["id"]),
        )
        await db.commit()
    return True, "ok"


async def set_document_preview(
    family_chat_id: int, doc_id: int, preview_stored_filename: str
) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE documents SET preview_stored_filename = ?
            WHERE family_chat_id = ? AND id = ?
            """,
            (preview_stored_filename, family_chat_id, doc_id),
        )
        await db.commit()
        return cur.rowcount > 0
