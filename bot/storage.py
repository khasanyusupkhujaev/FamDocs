"""
Family document blobs: local disk (dev) or S3-compatible object storage (production).

Optional AES-256-GCM encryption (FAMDOC_FILE_MASTER_KEY) protects data at rest: a stolen
disk or bucket export is useless without the key. The *application* still holds the key
in production — true "only on user device" privacy requires client-side encryption.
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

import aiofiles

from bot.config import (
    FILE_ENCRYPTION_KEY,
    S3_ACCESS_KEY_ID,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_PREFIX,
    S3_REGION,
    S3_SECRET_ACCESS_KEY,
    STORAGE_BACKEND,
    STORAGE_DIR,
)


class StorageAccessDeniedError(Exception):
    """S3/R2 AccessDenied — grant Object Read & Write on the bucket for this API token."""


def resolve_display_filename(display_name: str, file_filename: str | None) -> str:
    """
    User-provided title for a file. If empty, use the upload's file name.
    If the user omits an extension but the file has one, append it so the
    stored record keeps a sensible name.
    """
    raw = (display_name or "").strip()
    fallback = Path(file_filename or "upload.bin").name
    if not raw:
        return fallback or "upload.bin"
    p = Path(raw)
    fext = Path(fallback).suffix
    if fext and not p.suffix:
        return raw + fext
    return raw


def _safe_segment(name: str) -> str:
    base = Path(name).name
    base = re.sub(r"[^\w.\-]", "_", base, flags=re.UNICODE)
    return base[:180] if base else "file"


_MAGIC = b"FAMDOC\x01"


def _encrypt_if_configured(plaintext: bytes) -> bytes:
    key = FILE_ENCRYPTION_KEY
    if key is None:
        return plaintext
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aes = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aes.encrypt(nonce, plaintext, None)
    return _MAGIC + nonce + ciphertext


def decrypt_stored_blob(blob: bytes) -> bytes:
    """
    Decrypt if encrypted; return legacy plaintext bytes unchanged.
    Used when reading stored objects.
    """
    key = FILE_ENCRYPTION_KEY
    if key is None or not blob.startswith(_MAGIC):
        return blob
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    rest = blob[len(_MAGIC) :]
    if len(rest) < 12 + 16:
        return blob
    nonce = rest[:12]
    ciphertext = rest[12:]
    aes = AESGCM(key)
    return aes.decrypt(nonce, ciphertext, None)


class BlobBackend(ABC):
    @abstractmethod
    async def save(
        self, family_chat_id: int, stored_filename: str, data: bytes
    ) -> None:
        ...

    @abstractmethod
    async def read(
        self, family_chat_id: int, stored_filename: str
    ) -> bytes | None:
        ...

    @abstractmethod
    async def delete(self, family_chat_id: int, stored_filename: str) -> None:
        ...


class LocalBlobBackend(BlobBackend):
    def _path(self, family_chat_id: int, stored_filename: str) -> Path:
        return STORAGE_DIR / str(family_chat_id) / stored_filename

    async def save(
        self, family_chat_id: int, stored_filename: str, data: bytes
    ) -> None:
        folder = STORAGE_DIR / str(family_chat_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / stored_filename
        async with aiofiles.open(path, "wb") as f:
            await f.write(data)

    async def read(
        self, family_chat_id: int, stored_filename: str
    ) -> bytes | None:
        path = self._path(family_chat_id, stored_filename)
        if not path.is_file():
            return None
        async with aiofiles.open(path, "rb") as f:
            return await f.read()

    async def delete(self, family_chat_id: int, stored_filename: str) -> None:
        p = self._path(family_chat_id, stored_filename)
        if p.is_file():
            p.unlink()


class S3BlobBackend(BlobBackend):
    """AWS S3, Cloudflare R2, MinIO, etc. (boto3, sync calls in thread pool)."""

    def __init__(self) -> None:
        import boto3  # noqa: PLC0415

        if not S3_BUCKET or not S3_ACCESS_KEY_ID or not S3_SECRET_ACCESS_KEY:
            raise RuntimeError(
                "S3 storage requires FAMDOC_S3_BUCKET, FAMDOC_S3_ACCESS_KEY_ID, "
                "and FAMDOC_S3_SECRET_ACCESS_KEY."
            )
        self._bucket = S3_BUCKET
        session = boto3.session.Session(
            aws_access_key_id=S3_ACCESS_KEY_ID,
            aws_secret_access_key=S3_SECRET_ACCESS_KEY,
            region_name=S3_REGION,
        )
        self._client = session.client(
            "s3",
            endpoint_url=S3_ENDPOINT_URL or None,
        )

    def _key(self, family_chat_id: int, stored_filename: str) -> str:
        return f"{S3_PREFIX}/{family_chat_id}/{stored_filename}"

    async def save(
        self, family_chat_id: int, stored_filename: str, data: bytes
    ) -> None:
        key = self._key(family_chat_id, stored_filename)

        def _put() -> None:
            from botocore.exceptions import ClientError  # noqa: PLC0415

            try:
                self._client.put_object(Bucket=self._bucket, Key=key, Body=data)
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "AccessDenied":
                    raise StorageAccessDeniedError(
                        "R2/S3 denied upload (PutObject). "
                        "Use an API token with Object Read & Write on this bucket."
                    ) from e
                raise

        await asyncio.to_thread(_put)

    async def read(
        self, family_chat_id: int, stored_filename: str
    ) -> bytes | None:
        key = self._key(family_chat_id, stored_filename)

        def _get() -> bytes | None:
            from botocore.exceptions import ClientError  # noqa: PLC0415

            try:
                o = self._client.get_object(Bucket=self._bucket, Key=key)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey", "NotFound"):
                    return None
                if code == "AccessDenied":
                    raise StorageAccessDeniedError(
                        "R2/S3 denied read (GetObject). "
                        "Use an API token with Object Read on this bucket."
                    ) from e
                raise
            return o["Body"].read()

        return await asyncio.to_thread(_get)

    async def delete(self, family_chat_id: int, stored_filename: str) -> None:
        key = self._key(family_chat_id, stored_filename)

        def _del() -> None:
            from botocore.exceptions import ClientError  # noqa: PLC0415

            try:
                self._client.delete_object(Bucket=self._bucket, Key=key)
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "AccessDenied":
                    raise StorageAccessDeniedError(
                        "R2/S3 denied delete (DeleteObject)."
                    ) from e
                raise

        await asyncio.to_thread(_del)

    async def read_raw_key(self, key: str) -> bytes | None:
        """GetObject by full key (e.g. branding assets under a fixed path)."""

        def _get() -> bytes | None:
            from botocore.exceptions import ClientError  # noqa: PLC0415

            try:
                o = self._client.get_object(Bucket=self._bucket, Key=key)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey", "NotFound"):
                    return None
                if code == "AccessDenied":
                    raise StorageAccessDeniedError(
                        "R2/S3 denied read (GetObject) for branding or static key. "
                        "Use an API token with Object Read on this bucket."
                    ) from e
                raise
            return o["Body"].read()

        return await asyncio.to_thread(_get)


_backend: BlobBackend | None = None


def _make_backend() -> BlobBackend:
    b = (STORAGE_BACKEND or "local").lower().strip()
    if b == "local":
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        return LocalBlobBackend()
    if b in ("s3", "r2", "minio"):
        return S3BlobBackend()
    raise RuntimeError(
        f"Unknown FAMDOC_STORAGE={STORAGE_BACKEND!r}. Use 'local' or 's3'."
    )


def get_backend() -> BlobBackend:
    global _backend
    if _backend is None:
        _backend = _make_backend()
    return _backend


async def read_bucket_key(key: str) -> bytes | None:
    """
    Read one object by its full bucket key (not family_chat_id layout).
    Returns None for local storage or missing object.
    """
    if STORAGE_BACKEND != "s3":
        return None
    backend = get_backend()
    if not isinstance(backend, S3BlobBackend):
        return None
    return await backend.read_raw_key(key)


async def save_upload(
    family_chat_id: int, original_filename: str, data: bytes
) -> str:
    """
    Store blob (encrypted on write if FAMDOC_FILE_MASTER_KEY is set).
    Returns stored_filename (UUID-based key in DB).
    """
    safe = _safe_segment(original_filename)
    stored = f"{uuid.uuid4().hex}_{safe}"
    payload = _encrypt_if_configured(data)
    await get_backend().save(family_chat_id, stored, payload)
    return stored


async def read_stored_file(
    family_chat_id: int, stored_filename: str
) -> bytes | None:
    """Plaintext file bytes (decrypts if the object was encrypted)."""
    raw = await get_backend().read(family_chat_id, stored_filename)
    if raw is None:
        return None
    return decrypt_stored_blob(raw)


async def remove_stored_file(family_chat_id: int, stored_filename: str) -> None:
    await get_backend().delete(family_chat_id, stored_filename)
