"""
Async Payme (Merchant API / JSON-RPC) webhook handler.

``PaymeWebhookHandlerInternal`` holds the core logic; ``PaymeWebhookHandler`` is the thin
public subclass consumers extend to override the async event hooks.
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# pylint: disable=E0401,E0611
from payuz.core.base import BasePaymentProcessor
from payuz.core.exceptions import (
    AccountNotFound,
    InvalidAccount,
    InvalidAmount,
    MethodNotFound,
    PermissionDenied,
    TransactionCompleted,
    TransactionNotFound,
    UnsupportedMethod,
)

from ..models import PaymentTransaction
from .common import coerce_account_value, json_response

logger = logging.getLogger(__name__)


class PaymeWebhookHandlerInternal(BasePaymentProcessor):
    """Async Payme (Merchant API / JSON-RPC) webhook handler — core logic."""

    def __init__(
        self,
        db: AsyncSession,
        payme_id: str,
        payme_key: str,
        account_model: Any,
        account_field: str = "id",
        amount_field: str = "amount",
        one_time_payment: bool = True,
        transaction_model: Any = PaymentTransaction,
    ):
        """
        Args:
            db: Async database session.
            payme_id: Payme merchant ID.
            payme_key: Payme merchant key (Basic-auth password).
            account_model: The host project's account/order model class.
            account_field: ``account[...]`` field name Payme sends; also the host model column
                to match it against. The special value ``"order_id"`` resolves the lookup to the
                model's ``id`` primary key (the value is coerced to int/UUID as needed).
            amount_field: Attribute on the account holding the expected amount (in som).
            one_time_payment: Validate the amount strictly (single-payment accounts).
            transaction_model: Payment-transaction model (defaults to the standalone model).
        """
        self.db = db
        self.payme_id = payme_id
        self.payme_key = payme_key
        self.account_model = account_model
        self.account_field = account_field
        self.amount_field = amount_field
        self.one_time_payment = one_time_payment
        self.transaction_model = transaction_model

    # ── dispatch ───────────────────────────────────────────────────────────--
    async def handle_webhook(self, request: Request) -> Response:
        """Handle a Payme JSON-RPC webhook request."""
        request_id = 0
        try:
            self._check_auth(request.headers.get("Authorization"))

            data = await request.json()
            method = data.get("method")
            params = data.get("params", {})
            request_id = data.get("id", 0)

            handlers = {
                "CheckPerformTransaction": self._check_perform_transaction,
                "CreateTransaction": self._create_transaction,
                "PerformTransaction": self._perform_transaction,
                "CheckTransaction": self._check_transaction,
                "CancelTransaction": self._cancel_transaction,
                "GetStatement": self._get_statement,
            }
            handler = handlers.get(method)
            if handler is None:
                return self._rpc_error(request_id, -32601, f"Method not supported: {method}")

            result = await handler(params)
            return json_response({"jsonrpc": "2.0", "id": request_id, "result": result})

        except PermissionDenied:
            return self._rpc_error(request_id, -32504, "permission denied")
        except (MethodNotFound, UnsupportedMethod) as e:
            return self._rpc_error(request_id, -32601, str(e))
        except (AccountNotFound, InvalidAccount) as e:
            return self._rpc_error(request_id, -31050, str(e))
        except (InvalidAmount, TransactionNotFound) as e:
            return self._rpc_error(request_id, -31001, str(e))
        except TransactionCompleted as e:
            # service already delivered / tokens spent → cannot cancel a performed txn
            return self._rpc_error(request_id, -31007, str(e))
        except HTTPException as e:
            return self._rpc_error(request_id, -31003, e.detail)
        except Exception as e:  # noqa: BLE001
            logger.exception("Unexpected error in Payme webhook: %s", e)
            return self._rpc_error(request_id, -32400, "Internal error")

    @staticmethod
    def _rpc_error(request_id: Any, code: int, message: str) -> Response:
        return json_response(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )

    # ── helpers ────────────────────────────────────────────────────────────--
    def _check_auth(self, auth_header: Optional[str]) -> None:
        try:
            self.check_basic_auth(auth_header, expected_password=self.payme_key)
        except PermissionDenied:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Authentication error: %s", e)
            raise PermissionDenied("Authentication error")

    async def _find_account(self, params: Dict[str, Any]) -> Any:
        account_value = params.get("account", {}).get(self.account_field)
        if not account_value:
            raise AccountNotFound("Account not found in parameters")

        lookup_field = "id" if self.account_field == "order_id" else self.account_field
        account_value = coerce_account_value(lookup_field, account_value)

        res = await self.db.execute(
            select(self.account_model).filter_by(**{lookup_field: account_value})
        )
        account = res.scalar_one_or_none()
        if not account:
            raise AccountNotFound(
                f"Account with {self.account_field}={account_value} not found"
            )
        return account

    def _validate_amount(self, account: Any, amount: Any) -> bool:
        # str() round-trip: a Float account column would otherwise inherit binary
        # representation error (Decimal(1234.56) != 1234.56) → false InvalidAmount.
        expected_amount = Decimal(str(getattr(account, self.amount_field))) * 100
        received_amount = Decimal(amount)
        if self.one_time_payment and expected_amount != received_amount:
            raise InvalidAmount(
                f"Invalid amount. Expected: {expected_amount}, received: {received_amount}"
            )
        if not self.one_time_payment and received_amount <= 0:
            raise InvalidAmount(
                f"Invalid amount. Amount must be positive, received: {received_amount}"
            )
        return True

    async def _get_txn(self, transaction_id: str):
        m = self.transaction_model
        res = await self.db.execute(
            select(m).where(m.gateway == m.PAYME, m.transaction_id == transaction_id)
        )
        return res.scalar_one_or_none()

    # ── JSON-RPC methods ──────────────────────────────────────────────────---
    async def _check_perform_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        account = await self._find_account(params)
        self._validate_amount(account, params.get("amount"))
        await self.before_check_perform_transaction(params, account)
        return {"allow": True}

    async def _create_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        transaction_id = params.get("id")
        account = await self._find_account(params)
        amount = params.get("amount")
        self._validate_amount(account, amount)
        m = self.transaction_model

        if self.one_time_payment:
            res = await self.db.execute(
                select(m).where(
                    m.gateway == m.PAYME,
                    m.account_id == str(account.id),
                    m.transaction_id != transaction_id,
                )
            )
            pending = [
                t for t in res.scalars().all()
                if t.state not in (m.SUCCESSFULLY, m.CANCELLED, m.CANCELLED_DURING_INIT)
            ]
            if pending:
                raise InvalidAccount(
                    f"Account with {self.account_field}={account.id} "
                    f"already has a pending transaction"
                )

        transaction = await self._get_txn(transaction_id)
        if transaction:
            await self.transaction_already_exists(params, transaction)
            create_time = (transaction.extra_data or {}).get("create_time", params.get("time"))
            return {
                "transaction": transaction.transaction_id,
                "state": transaction.state,
                "create_time": create_time,
            }

        transaction = m(
            gateway=m.PAYME,
            transaction_id=transaction_id,
            account_id=str(account.id),
            amount=Decimal(amount) / 100,
            state=m.INITIATING,
            extra_data={
                "account_field": self.account_field,
                "account_value": params.get("account", {}).get(self.account_field),
                "create_time": params.get("time"),
                "raw_params": params,
            },
        )
        self.db.add(transaction)
        await self.db.commit()
        await self.db.refresh(transaction)

        await self.transaction_created(params, transaction, account)
        return {
            "transaction": transaction.transaction_id,
            "state": transaction.state,
            "create_time": params.get("time"),
        }

    async def _perform_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        transaction = await self._get_txn(params.get("id"))
        if not transaction:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Transaction {params.get('id')} not found",
            )
        await transaction.mark_as_paid(self.db)
        await self.successfully_payment(params, transaction)
        return {
            "transaction": transaction.transaction_id,
            "state": transaction.state,
            "perform_time": (
                int(transaction.performed_at.timestamp() * 1000)
                if transaction.performed_at else 0
            ),
        }

    async def _check_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        transaction = await self._get_txn(params.get("id"))
        if not transaction:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Transaction {params.get('id')} not found",
            )
        await self.check_transaction(params, transaction)
        create_time = (transaction.extra_data or {}).get(
            "create_time", int(transaction.created_at.timestamp() * 1000)
        )
        return {
            "transaction": transaction.transaction_id,
            "state": transaction.state,
            "create_time": create_time,
            "perform_time": (
                int(transaction.performed_at.timestamp() * 1000)
                if transaction.performed_at else 0
            ),
            "cancel_time": (
                int(transaction.cancelled_at.timestamp() * 1000)
                if transaction.cancelled_at else 0
            ),
            "reason": transaction.reason,
        }

    async def _cancel_response(self, transaction) -> Dict[str, Any]:
        reason = transaction.reason
        if reason is None:
            from payuz.gateways.payme.constants import PaymeCancelReason

            reason = PaymeCancelReason.REASON_FUND_RETURNED
            transaction.reason = reason
            await self.db.commit()
            await self.db.refresh(transaction)
        return {
            "transaction": transaction.transaction_id,
            "state": transaction.state,
            "cancel_time": (
                int(transaction.cancelled_at.timestamp() * 1000)
                if transaction.cancelled_at else 0
            ),
            "reason": reason,
        }

    async def _cancel_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        transaction = await self._get_txn(params.get("id"))
        if not transaction:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Transaction {params.get('id')} not found",
            )
        m = self.transaction_model

        # already cancelled → idempotent: refresh reason if provided, return as-is
        if transaction.state in (m.CANCELLED, m.CANCELLED_DURING_INIT):
            if "reason" in params:
                reason = params.get("reason")
                if reason is None:
                    from payuz.gateways.payme.constants import PaymeCancelReason

                    reason = PaymeCancelReason.REASON_FUND_RETURNED
                if isinstance(reason, str) and reason.isdigit():
                    reason = int(reason)
                transaction.reason = reason
                extra = dict(transaction.extra_data or {})
                extra["cancel_reason"] = reason
                transaction.extra_data = extra
                await self.db.commit()
                await self.db.refresh(transaction)
            return await self._cancel_response(transaction)

        # hook may raise TransactionCompleted to refuse cancelling a delivered order (→ -31007)
        await self.before_cancel_transaction(params, transaction)

        reason = params.get("reason")
        await transaction.mark_as_cancelled(self.db, reason=reason)

        extra = dict(transaction.extra_data or {})
        if "cancel_reason" not in extra:
            extra["cancel_reason"] = reason if reason is not None else 5
            transaction.extra_data = extra
            await self.db.commit()
            await self.db.refresh(transaction)

        await self.cancelled_payment(params, transaction)
        return await self._cancel_response(transaction)

    async def _get_statement(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from_date = params.get("from")
        to_date = params.get("to")
        from_dt = datetime.fromtimestamp(from_date / 1000) if from_date else datetime.fromtimestamp(0)
        to_dt = datetime.fromtimestamp(to_date / 1000) if to_date else datetime.now()
        m = self.transaction_model

        res = await self.db.execute(
            select(m).where(
                m.gateway == m.PAYME, m.created_at >= from_dt, m.created_at <= to_dt
            )
        )
        result = []
        for t in res.scalars().all():
            result.append({
                "id": t.transaction_id,
                "time": int(t.created_at.timestamp() * 1000),
                "amount": int(t.amount * 100),
                "account": {self.account_field: t.account_id},
                "state": t.state,
                "create_time": (t.extra_data or {}).get(
                    "create_time", int(t.created_at.timestamp() * 1000)
                ),
                "perform_time": (
                    int(t.performed_at.timestamp() * 1000) if t.performed_at else 0
                ),
                "cancel_time": (
                    int(t.cancelled_at.timestamp() * 1000) if t.cancelled_at else 0
                ),
                "reason": t.reason,
            })
        await self.get_statement(params, result)
        return {"transactions": result}

    # ── overridable async event hooks (no-op defaults) ────────────────────---
    async def before_check_perform_transaction(self, params, account) -> None:
        """Before allowing a transaction (raise to deny)."""

    async def before_cancel_transaction(self, params, transaction) -> None:
        """Before cancelling a *performed* transaction. Raise ``TransactionCompleted`` to
        refuse (→ Payme -31007) when the order is already delivered / tokens spent."""

    async def transaction_already_exists(self, params, transaction) -> None:
        """A transaction with this id already exists."""

    async def transaction_created(self, params, transaction, account) -> None:
        """A new transaction was created."""

    async def successfully_payment(self, params, transaction) -> None:
        """Payment confirmed (PerformTransaction)."""

    async def check_transaction(self, params, transaction) -> None:
        """CheckTransaction was called."""

    async def cancelled_payment(self, params, transaction) -> None:
        """Transaction cancelled/refunded."""

    async def get_statement(self, params, transactions) -> None:
        """GetStatement was called."""


class PaymeWebhookHandler(PaymeWebhookHandlerInternal):
    """Payme (Merchant API / JSON-RPC) webhook handler. Override the async event hooks."""
