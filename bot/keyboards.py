from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

CAT_MEDICAL = "medical_records"
CAT_SCHOOL = "school_certificates"
CAT_PROPERTY = "property_deeds"
CAT_LEGAL = "legal_documents"
CAT_FAMILY = "family_photos_other"

JOIN_FAMILY_TEXT = "🔗 Join family"

CATEGORY_ORDER = (
    CAT_MEDICAL,
    CAT_SCHOOL,
    CAT_PROPERTY,
    CAT_LEGAL,
    CAT_FAMILY,
)

CATEGORY_LABELS: dict[str, str] = {
    CAT_MEDICAL: "Medical records",
    CAT_SCHOOL: "School certificates",
    CAT_PROPERTY: "Property deeds",
    CAT_LEGAL: "Legal documents",
    CAT_FAMILY: "Family photos / other",
}

CATEGORY_EMOJI: dict[str, str] = {
    CAT_MEDICAL: "🏥",
    CAT_SCHOOL: "🎓",
    CAT_PROPERTY: "🏠",
    CAT_LEGAL: "⚖️",
    CAT_FAMILY: "👨‍👩‍👧",
}


def main_reply_keyboard() -> ReplyKeyboardRemove:
    """
    No custom reply rows — keeps the standard message field.
    The Mini App is opened from the blue menu button (set in bot startup via
    WEBAPP_MENU_BUTTON_TEXT, e.g. FamDocs).
    """
    return ReplyKeyboardRemove()


def category_sidebar_inline(
    current_category: str | None = None,
    *,
    include_all: bool = True,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key in CATEGORY_ORDER:
        label = f"{CATEGORY_EMOJI[key]} {CATEGORY_LABELS[key]}"
        if current_category == key:
            label = f"▸ {label}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"cat:{key}",
                )
            ]
        )
    if include_all:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📚 All categories",
                    callback_data="cat:__all__",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def document_actions_inline(doc_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬇️ Download",
                    callback_data=f"dl:{doc_id}",
                ),
            ],
        ]
    )


def reply_text_to_category(text: str) -> str | None:
    mapping = {
        f"{CATEGORY_EMOJI[CAT_MEDICAL]} Medical records": CAT_MEDICAL,
        f"{CATEGORY_EMOJI[CAT_SCHOOL]} School certificates": CAT_SCHOOL,
        f"{CATEGORY_EMOJI[CAT_PROPERTY]} Property deeds": CAT_PROPERTY,
        f"{CATEGORY_EMOJI[CAT_LEGAL]} Legal documents": CAT_LEGAL,
        f"{CATEGORY_EMOJI[CAT_FAMILY]} Family photos / other": CAT_FAMILY,
    }
    return mapping.get(text.strip())


def category_reply_button_texts() -> tuple[str, ...]:
    return tuple(f"{CATEGORY_EMOJI[k]} {CATEGORY_LABELS[k]}" for k in CATEGORY_ORDER)


def invite_wait_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Cancel")]],
        resize_keyboard=True,
    )
