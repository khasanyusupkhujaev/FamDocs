"""Heuristic category suggestion from filename / MIME (no OCR in v1)."""

from __future__ import annotations

from bot.keyboards import (
    CAT_FAMILY,
    CAT_LEGAL,
    CAT_MEDICAL,
    CAT_PROPERTY,
    CAT_SCHOOL,
)


def suggest_category(filename: str, mime: str | None) -> str:
    fn = (filename or "").lower()
    mt = (mime or "").lower()

    medical_kw = (
        "medical",
        "health",
        "doctor",
        "hospital",
        "lab",
        "prescription",
        "rx",
        "vaccin",
        "insurance",
        "clinic",
        "diagnos",
        "xray",
        "mri",
    )
    if any(k in fn for k in medical_kw) or "health" in mt:
        return CAT_MEDICAL

    school_kw = (
        "school",
        "diploma",
        "degree",
        "transcript",
        "certificate",
        "university",
        "grade",
        "report card",
        "education",
    )
    if any(k in fn for k in school_kw):
        return CAT_SCHOOL

    property_kw = (
        "deed",
        "property",
        "mortgage",
        "title",
        "lease",
        "hoa",
        "survey",
        "closing",
    )
    if any(k in fn for k in property_kw):
        return CAT_PROPERTY

    legal_kw = (
        "legal",
        "contract",
        "court",
        "lawyer",
        "attorney",
        "will",
        "trust",
        "passport",
        "notary",
        "agreement",
        "power of attorney",
    )
    if any(k in fn for k in legal_kw):
        return CAT_LEGAL

    if mt.startswith("image/"):
        return CAT_FAMILY

    if "pdf" in mt or fn.endswith(".pdf"):
        return CAT_LEGAL

    return CAT_FAMILY
