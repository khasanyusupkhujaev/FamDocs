"""PNG QR codes for manual bank transfer instructions (card + amount + FAMDOC comment)."""

from __future__ import annotations

import io

import qrcode


def build_payment_qr_png(*, card: str, amount_uzs: int, comment: str) -> bytes:
    """
    Encode a clear multi-line payload readable by any QR scanner.
    (Not bank-proprietary EMV; works everywhere for copy/paste verification.)
    """
    card = " ".join(card.split())
    text = (
        "FamDoc payment\n"
        f"Card: {card}\n"
        f"Amount: {amount_uzs} UZS\n"
        f"Comment: {comment}\n"
    )
    qr = qrcode.QRCode(version=None, box_size=5, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#111", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
