"""
Async Click (SHOP-API: Prepare/Complete) webhook handler.

``ClickWebhookHandlerInternal`` holds the core logic; ``ClickWebhookHandler`` is the thin
public subclass consumers extend to override the async event hooks.
"""

import hashlib
import logging
from typing import Any, Dict

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PaymentTransaction
from .common import coerce_account_value

logger = logging.getLogger(__name__)


class ClickWebhookHandlerInternal:
    """Async Click (SHOP-API: Prepare/Complete) webhook handler — core logic."""

    def __init__(
        self,
        db: AsyncSession,
        service_id: str,
        secret_key: str,
        account_model: Any,
        commission_percent: float = 0.0,
        account_field: str = "id",
        amount_field: str = "amount",
        one_time_payment: bool = True,
        transaction_model: Any = PaymentTransaction,
    ):
        self.db = db
        self.service_id = service_id
        self.secret_key = secret_key
        self.account_model = account_model
        self.commission_percent = commission_percent
        self.account_field = account_field
        self.amount_field = amount_field
        self.one_time_payment = one_time_payment
        self.transaction_model = transaction_model

    async def handle_webhook(self, request: Request) -> Dict[str, Any]:
        """Handle a Click Prepare/Complete webhook (form-encoded)."""
        try:
            form_data = await request.form()
            params = {key: form_data.get(key) for key in form_data}

            self._check_auth(params)

            click_trans_id = params.get("click_trans_id")
            merchant_trans_id = params.get("merchant_trans_id")
            amount = float(params.get("amount", 0))
            action = int(params.get("action", -1))
            error = int(params.get("error", 0))
            m = self.transaction_model

            try:
                account = await self._find_account(merchant_trans_id)
            except Exception:  # noqa: BLE001
                logger.error("Account not found: %s", merchant_trans_id)
                return {
                    "click_trans_id": click_trans_id,
                    "merchant_trans_id": merchant_trans_id,
                    "error": -5,
                    "error_note": "User not found",
                }

            try:
                expected = float(getattr(account, self.amount_field, 0))
                self._validate_amount(amount, expected)
            except Exception as e:  # noqa: BLE001
                logger.error("Invalid amount: %s", e)
                return {
                    "click_trans_id": click_trans_id,
                    "merchant_trans_id": merchant_trans_id,
                    "error": -2,
                    "error_note": str(e),
                }

            res = await self.db.execute(
                select(m).where(m.gateway == m.CLICK, m.transaction_id == click_trans_id)
            )
            transaction = res.scalar_one_or_none()

            if transaction:
                if transaction.state == m.SUCCESSFULLY:
                    await self.transaction_already_exists(params, transaction)
                    return {
                        "click_trans_id": click_trans_id,
                        "merchant_trans_id": merchant_trans_id,
                        "merchant_prepare_id": transaction.id,
                        "error": 0,
                        "error_note": "Success",
                    }
                if transaction.state == m.CANCELLED:
                    return {
                        "click_trans_id": click_trans_id,
                        "merchant_trans_id": merchant_trans_id,
                        "merchant_prepare_id": transaction.id,
                        "error": -9,
                        "error_note": "Transaction cancelled",
                    }

            if action == 0:  # Prepare
                transaction = m(
                    gateway=m.CLICK,
                    transaction_id=click_trans_id,
                    account_id=str(account.id),
                    amount=amount,
                    state=m.INITIATING,
                    extra_data={"raw_params": params, "merchant_trans_id": merchant_trans_id},
                )
                self.db.add(transaction)
                await self.db.commit()
                await self.db.refresh(transaction)
                await self.transaction_created(params, transaction, account)
                return {
                    "click_trans_id": click_trans_id,
                    "merchant_trans_id": merchant_trans_id,
                    "merchant_prepare_id": transaction.id,
                    "error": 0,
                    "error_note": "Success",
                }

            if action == 1:  # Complete
                if not transaction:
                    transaction = m(
                        gateway=m.CLICK,
                        transaction_id=click_trans_id,
                        account_id=str(account.id),
                        amount=amount,
                        state=m.INITIATING,
                        extra_data={
                            "raw_params": params,
                            "merchant_trans_id": merchant_trans_id,
                        },
                    )
                    self.db.add(transaction)
                    await self.db.commit()
                    await self.db.refresh(transaction)

                if error >= 0:
                    await transaction.mark_as_paid(self.db)
                    await self.successfully_payment(params, transaction)
                else:
                    # reason is the Click error code (int) — the `reason` column is Integer;
                    # a formatted string would raise on commit. Note text stays in extra_data.
                    await transaction.mark_as_cancelled(self.db, reason=error)
                    await self.cancelled_payment(params, transaction)

                return {
                    "click_trans_id": click_trans_id,
                    "merchant_trans_id": merchant_trans_id,
                    "merchant_prepare_id": transaction.id,
                    "error": 0,
                    "error_note": "Success",
                }

            logger.error("Unsupported action: %s", action)
            return {
                "click_trans_id": click_trans_id,
                "merchant_trans_id": merchant_trans_id,
                "error": -3,
                "error_note": "Action not found",
            }

        except Exception as e:  # noqa: BLE001
            logger.exception("Unexpected error in Click webhook: %s", e)
            return {"error": -7, "error_note": "Internal error"}

    def _check_auth(self, params: Dict[str, Any]) -> None:
        if not all([self.service_id, self.secret_key]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing required settings: service_id or secret_key",
            )
        if str(params.get("service_id")) != str(self.service_id):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid service ID"
            )

        sign_string = params.get("sign_string")
        sign_time = params.get("sign_time")
        if not sign_string or not sign_time:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing signature parameters"
            )

        text_parts = [
            str(params.get("click_trans_id") or ""),
            str(params.get("service_id") or ""),
            str(self.secret_key or ""),
            str(params.get("merchant_trans_id") or ""),
            str(params.get("merchant_prepare_id") or ""),
            str(params.get("amount") or ""),
            str(params.get("action") or ""),
            str(sign_time),
        ]
        calculated = hashlib.md5("".join(text_parts).encode("utf-8")).hexdigest()  # noqa: S324
        if calculated != sign_string:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature"
            )

    async def _find_account(self, merchant_trans_id: str) -> Any:
        # account_field="order_id" resolves to the host model's `id` column (mirrors Payme).
        lookup_field = "id" if self.account_field == "order_id" else self.account_field
        account_value = coerce_account_value(lookup_field, merchant_trans_id)

        res = await self.db.execute(
            select(self.account_model).filter_by(**{lookup_field: account_value})
        )
        account = res.scalar_one_or_none()
        if not account:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Account with {self.account_field}={merchant_trans_id} not found",
            )
        return account

    def _validate_amount(self, received_amount: float, expected_amount: float) -> None:
        if self.one_time_payment:
            if self.commission_percent > 0:
                expected_amount = round(
                    expected_amount * (1 + self.commission_percent / 100), 2
                )
            if abs(received_amount - expected_amount) > 0.01:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Incorrect amount. Expected: {expected_amount}, "
                        f"received: {received_amount}"
                    ),
                )
        elif received_amount <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Amount must be positive"
            )

    # ── overridable async event hooks (no-op defaults) ────────────────────---
    async def transaction_already_exists(self, params, transaction) -> None:
        """A transaction with this id already exists."""

    async def transaction_created(self, params, transaction, account) -> None:
        """A new transaction was created (Prepare)."""

    async def successfully_payment(self, params, transaction) -> None:
        """Payment confirmed (Complete, error >= 0)."""

    async def cancelled_payment(self, params, transaction) -> None:
        """Payment cancelled (Complete, error < 0)."""


class ClickWebhookHandler(ClickWebhookHandlerInternal):
    """Click (SHOP-API: Prepare/Complete) webhook handler. Override the async event hooks."""
