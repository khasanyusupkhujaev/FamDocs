"""
PayTech.uz (paytechuz): Payme / Click checkout + webhooks on the same SQLite DB as FamDoc.

Requires PAYTECH_LICENSE_API_KEY from https://pay-tech.uz/console (see paytechuz docs).
Install: pip install paytechuz sqlalchemy (do not use paytechuz[fastapi] — pydantic pin conflicts).
"""

from __future__ import annotations

import logging

from sqlalchemy import Column, Float, Integer, create_engine
from sqlalchemy.orm import Session, sessionmaker

from bot.config import (
    BILLING_MODE,
    CLICK_MERCHANT_ID,
    CLICK_MERCHANT_USER_ID,
    CLICK_SECRET_KEY,
    CLICK_SERVICE_ID,
    CLICK_TEST_MODE,
    DB_PATH,
    PAYME_MERCHANT_ID,
    PAYME_MERCHANT_KEY,
    PAYME_TEST_MODE,
    PAYTECH_PRICE_UZS,
    UPGRADE_EXTRA_SLOTS,
    UPGRADE_INVOICE_DESCRIPTION,
    WEBAPP_PUBLIC_URL,
)
from bot.paytech_grant import grant_paytech_payment

log = logging.getLogger(__name__)

_engine = None
SessionLocal: sessionmaker | None = None

# PayTech FastAPI models Base (includes PaymentTransaction + migrations)
from paytechuz.integrations.fastapi.models import Base, PaymentTransaction, run_migrations  # noqa: E402
from paytechuz.integrations.fastapi.routes import (  # noqa: E402
    ClickWebhookHandler,
    PaymeWebhookHandler,
)
from paytechuz.gateways.click import ClickGateway  # noqa: E402
from paytechuz.gateways.payme import PaymeGateway  # noqa: E402


class FamdocPaytechOrder(Base):
    """Order row for Payme/Click; amount is in UZS (som), same units as paytechuz create_payment."""

    __tablename__ = "famdoc_paytech_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    vault_id = Column(Integer, nullable=False, index=True)
    slots = Column(Integer, nullable=False)
    amount = Column(Float, nullable=False)


def init_paytech_db() -> None:
    global _engine, SessionLocal
    if BILLING_MODE not in ("payme", "click"):
        return
    path = DB_PATH.resolve()
    _engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )
    run_migrations(_engine)
    Base.metadata.create_all(_engine)
    SessionLocal = sessionmaker(bind=_engine)
    log.info("PayTech DB tables ready (mode=%s)", BILLING_MODE)


def paytech_configured() -> bool:
    if BILLING_MODE == "payme":
        return bool(PAYME_MERCHANT_ID and PAYME_MERCHANT_KEY)
    if BILLING_MODE == "click":
        return bool(CLICK_SERVICE_ID and CLICK_MERCHANT_ID and CLICK_SECRET_KEY)
    return False


def format_paytech_price_label() -> str:
    n = PAYTECH_PRICE_UZS
    return f"{n:,}".replace(",", " ") + " UZS"


def _grant_from_transaction(db: Session, transaction: PaymentTransaction, prefix: str) -> None:
    try:
        oid = int(transaction.account_id)
    except (TypeError, ValueError):
        log.warning("PayTech grant: bad account_id %r", transaction.account_id)
        return
    order = db.query(FamdocPaytechOrder).filter_by(id=oid).first()
    if not order:
        log.warning("PayTech grant: no order id=%s", oid)
        return
    charge_id = f"{prefix}:{transaction.transaction_id}"
    grant_paytech_payment(charge_id, order.vault_id, order.slots)


class FamDocPaymeHandler(PaymeWebhookHandler):
    def successfully_payment(self, params, transaction) -> None:
        _grant_from_transaction(self.db, transaction, "payme")


class FamDocClickHandler(ClickWebhookHandler):
    def successfully_payment(self, params, transaction) -> None:
        _grant_from_transaction(self.db, transaction, "click")


def create_paytech_checkout(vault_id: int) -> tuple[str, str]:
    """
    Create pending order + payment URL.
    Returns (checkout_url, open_with) where open_with is ``paytech_link``.
    """
    if SessionLocal is None:
        raise RuntimeError("PayTech not initialized")
    if not paytech_configured():
        raise ValueError("paytech_missing_credentials")

    return_url = (WEBAPP_PUBLIC_URL or "").strip().rstrip("/") + "/"
    db = SessionLocal()
    try:
        order = FamdocPaytechOrder(
            vault_id=vault_id,
            slots=UPGRADE_EXTRA_SLOTS,
            amount=float(PAYTECH_PRICE_UZS),
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        if BILLING_MODE == "payme":
            gw = PaymeGateway(
                payme_id=PAYME_MERCHANT_ID,
                payme_key=PAYME_MERCHANT_KEY or None,
                is_test_mode=PAYME_TEST_MODE,
            )
            url = gw.create_payment(
                id=order.id,
                amount=order.amount,
                return_url=return_url,
                account_field_name="order_id",
            )
            return url, "paytech_link"

        if BILLING_MODE == "click":
            gw = ClickGateway(
                service_id=CLICK_SERVICE_ID,
                merchant_id=CLICK_MERCHANT_ID,
                merchant_user_id=CLICK_MERCHANT_USER_ID or None,
                secret_key=CLICK_SECRET_KEY,
                is_test_mode=CLICK_TEST_MODE,
            )
            url = gw.create_payment(
                id=str(order.id),
                amount=order.amount,
                return_url=return_url,
                description=UPGRADE_INVOICE_DESCRIPTION[:200],
            )
            return url, "paytech_link"
    finally:
        db.close()

    raise ValueError("unsupported_paytech_mode")
