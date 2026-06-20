"""
Async SQLAlchemy model for the payuz FastAPI integration.

The transaction schema is exposed as a **Base-agnostic declarative mixin**
(:class:`PaymentTransactionMixin`) so a host project can attach it to its *own* declarative
``Base`` and manage migrations with its *own* Alembic — without revolving around the library's
``Base``. A ready-made standalone model (:class:`PaymentTransaction`, bound to an internal
``Base``) is also provided for small projects that don't have their own Base.

Usage with your own Base (recommended for projects with Alembic)::

    from yourapp.db import Base                       # your declarative Base
    from payuz.integrations.fastapi import PaymentTransactionMixin

    class PaymentTransaction(Base, PaymentTransactionMixin):
        __tablename__ = "payments"

All write helpers are **async** and take an :class:`~sqlalchemy.ext.asyncio.AsyncSession`.
They commit before returning so the event hooks in the webhook handlers observe a persisted
row (the same "commit then notify" ordering the handlers rely on).
"""
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import JSON, Column, DateTime, Float, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import declarative_base


class PaymentTransactionMixin:
    """Declarative mixin: payment-transaction columns + async helpers (no Base binding)."""

    # Payment gateways
    PAYME = "payme"
    CLICK = "click"
    UZUM = "uzum"
    PAYNET = "paynet"
    OCTO = "octo"

    # Transaction states
    CREATED = 0
    INITIATING = 1
    SUCCESSFULLY = 2
    CANCELLED = -2
    CANCELLED_DURING_INIT = -1

    # NOTE: plain ``Column`` on a declarative mixin is copied per-subclass by SQLAlchemy,
    # so the same mixin can back both the standalone model and a host project's model.
    id = Column(Integer, primary_key=True, index=True)
    gateway = Column(String(16), index=True)
    transaction_id = Column(String(255), index=True)
    account_id = Column(String(255), index=True)
    amount = Column(Float)
    state = Column(Integer, default=CREATED, index=True)
    reason = Column(Integer, nullable=True)  # provider cancel reason code
    extra_data = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True
    )
    performed_at = Column(DateTime, nullable=True, index=True)
    cancelled_at = Column(DateTime, nullable=True, index=True)

    # ── async helpers ────────────────────────────────────────────────────────
    @classmethod
    async def get_by_transaction_id(
        cls, db: AsyncSession, gateway: str, transaction_id: str
    ) -> Optional["PaymentTransactionMixin"]:
        """Return the transaction for ``(gateway, transaction_id)`` or ``None``."""
        res = await db.execute(
            select(cls).where(
                cls.gateway == gateway, cls.transaction_id == transaction_id
            )
        )
        return res.scalar_one_or_none()

    @classmethod
    async def create_transaction(
        cls,
        db: AsyncSession,
        gateway: str,
        transaction_id: str,
        account_id: str,
        amount: float,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> "PaymentTransactionMixin":
        """Create a new transaction or return the existing one (idempotent by txn id)."""
        existing = await cls.get_by_transaction_id(db, gateway, transaction_id)
        if existing is not None:
            return existing

        transaction = cls(
            gateway=gateway,
            transaction_id=transaction_id,
            account_id=str(account_id),
            amount=amount,
            state=cls.CREATED,
            extra_data=extra_data or {},
        )
        db.add(transaction)
        await db.commit()
        await db.refresh(transaction)
        return transaction

    async def mark_as_paid(self, db: AsyncSession) -> "PaymentTransactionMixin":
        """Mark the transaction as successfully paid (idempotent)."""
        if self.state != self.SUCCESSFULLY:
            self.state = self.SUCCESSFULLY
            self.performed_at = datetime.utcnow()
            await db.commit()
            await db.refresh(self)
        return self

    async def mark_as_cancelled(
        self, db: AsyncSession, reason: Optional[Any] = None
    ) -> "PaymentTransactionMixin":
        """Mark the transaction as cancelled/refunded, storing the reason code."""
        if reason is None:
            reason_code: Any = 5  # REASON_FUND_RETURNED
        elif isinstance(reason, str) and reason.isdigit():
            reason_code = int(reason)
        else:
            reason_code = reason

        if self.state not in (self.CANCELLED, self.CANCELLED_DURING_INIT):
            # cancelled while only initiated (or Payme reason 3) → -1, otherwise -2
            if self.state == self.INITIATING or reason_code == 3:
                self.state = self.CANCELLED_DURING_INIT
            else:
                self.state = self.CANCELLED
            self.cancelled_at = datetime.utcnow()

        self.reason = reason_code
        extra = dict(self.extra_data or {})
        extra["cancel_reason"] = reason_code
        self.extra_data = extra

        await db.commit()
        await db.refresh(self)
        return self


# Standalone Base + concrete model for projects without their own declarative Base.
Base = declarative_base()


class PaymentTransaction(Base, PaymentTransactionMixin):
    """Ready-made standalone transaction model. Prefer the mixin if you have your own Base."""

    __tablename__ = "payments"


async def run_migrations(engine) -> None:
    """Create tables for the **standalone** model only.

    Projects that bind :class:`PaymentTransactionMixin` to their own Base must NOT call this —
    they manage the schema through their own migrations (Alembic). ``engine`` must be an
    :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
