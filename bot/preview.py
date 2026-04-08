"""Generate small JPEG previews (PDF first page, downscaled images)."""

from __future__ import annotations

import io
# Max edge for grid thumbnails
_MAX_EDGE = 400


def build_preview_jpeg(file_bytes: bytes, mime_type: str | None) -> bytes | None:
    """
    Return JPEG bytes for thumbnail, or None if unsupported / failure.
    """
    mt = (mime_type or "").lower()
    try:
        if mt.startswith("image/"):
            return _image_preview(file_bytes)
        if "pdf" in mt or file_bytes[:4] == b"%PDF":
            return _pdf_first_page_preview(file_bytes)
    except Exception:
        return None
    return None


def _image_preview(data: bytes) -> bytes | None:
    from PIL import Image  # noqa: PLC0415

    im = Image.open(io.BytesIO(data))
    im = im.convert("RGB")
    im.thumbnail((_MAX_EDGE, _MAX_EDGE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=82, optimize=True)
    return buf.getvalue()


def _pdf_first_page_preview(data: bytes) -> bytes | None:
    import fitz  # PyMuPDF  # noqa: PLC0415

    doc = fitz.open(stream=data, filetype="pdf")
    if doc.page_count < 1:
        return None
    page = doc.load_page(0)
    pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
    img_bytes = pix.tobytes("png")
    from PIL import Image  # noqa: PLC0415

    im = Image.open(io.BytesIO(img_bytes))
    im = im.convert("RGB")
    im.thumbnail((_MAX_EDGE, _MAX_EDGE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=82, optimize=True)
    return buf.getvalue()
