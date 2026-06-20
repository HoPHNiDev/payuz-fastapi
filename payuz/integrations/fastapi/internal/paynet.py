"""
Internal async FastAPI webhook handler for Paynet (JSON-RPC).

This is an async-native port of the Django Paynet webhook
(:mod:`payuz.integrations.django.internal_webhooks.paynet`). It mirrors the
established async pattern used by the Payme handler in
:mod:`payuz.integrations.fastapi.internal`: the DB is an
:class:`~sqlalchemy.ext.asyncio.AsyncSession`, all queries go through
``select(...)`` + ``await self.db.execute(...)``, and every lifecycle event is
an overridable ``async def`` hook.

:class:`PaynetWebhookHandlerInternal` holds the core logic; the thin public
:class:`PaynetWebhookHandler` subclass is what consumers instantiate /
subclass to override the async event hooks.
"""

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# pylint: disable=E0401,E0611
from payuz.core.base import BasePaymentProcessor
from payuz.core.exceptions import (
    AccountNotFound,
    AlreadyPaid,
    InvalidAmount,
    MethodNotFound,
    PermissionDenied,
    ServiceNotFound,
    TransactionAlreadyExists,
    TransactionNotFound,
)
from payuz.gateways.paynet.constants import PaynetErrors

from ..models import PaymentTransaction
from .common import json_response as _json_response

logger = logging.getLogger(__name__)



class PaynetWebhookHandlerInternal(BasePaymentProcessor):
    """Async Paynet (JSON-RPC) webhook handler — core logic."""

    def __init__(
        self,
        db: AsyncSession,
        paynet_username: str,
        paynet_password: str,
        account_model: Any,
        paynet_service_id: str = "",
        account_field: str = "id",
        amount_field: str = "amount",
        account_info_fields: Any = ("id",),
        one_time_payment: bool = True,
        transaction_model: Any = PaymentTransaction,
    ):
        """
        Args:
            db: Async database session.
            paynet_username: Paynet Basic-auth username.
            paynet_password: Paynet Basic-auth password.
            account_model: The host project's account/order model class.
            paynet_service_id: Expected Paynet ``serviceId`` (empty disables the check).
            account_field: ``fields[...]`` field name Paynet sends (and model field to match).
            amount_field: Attribute on the account holding the expected amount (in som).
            account_info_fields: Account attributes returned by ``GetInformation``.
            one_time_payment: Reject accounts that already have a successful payment.
            transaction_model: Payment-transaction model (defaults to the standalone model).
        """
        self.db = db
        self.paynet_username = paynet_username
        self.paynet_password = paynet_password
        self.account_model = account_model
        self.paynet_service_id = paynet_service_id
        self.account_field = account_field
        self.amount_field = amount_field
        self.account_info_fields = account_info_fields
        self.one_time_payment = one_time_payment
        self.transaction_model = transaction_model

    # ── dispatch ───────────────────────────────────────────────────────────--
    async def handle_webhook(self, request: Request) -> Response:
        """Handle a Paynet JSON-RPC webhook request."""
        rpc_id = None
        try:
            # Check authorization
            self._check_auth(request.headers.get("Authorization"))

            # Parse request data
            try:
                body = await request.body()
                data = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._error_response(
                    None, PaynetErrors.JSON_PARSING_ERROR, "Error parsing JSON."
                )

            if not isinstance(data, dict):
                return self._error_response(
                    None, PaynetErrors.INVALID_RPC_REQUEST, "Invalid RPC Request"
                )

            rpc_id = data.get("id")
            method = data.get("method")
            params = data.get("params", {})

            if not all(k in data for k in ("jsonrpc", "method", "id", "params")):
                return self._error_response(
                    rpc_id, PaynetErrors.INVALID_RPC_REQUEST, "Missing required fields"
                )

            # Process the request based on the method
            if method == "PerformTransaction":
                result = await self._perform_transaction(params, rpc_id)
            elif method == "CheckTransaction":
                result = await self._check_transaction(params)
            elif method == "CancelTransaction":
                result = await self._cancel_transaction(params, rpc_id)
            elif method == "GetStatement":
                result = await self._get_statement(params)
            elif method == "ChangePassword":
                result = "success"
            elif method == "GetInformation":
                result = await self._get_information(params)
            else:
                raise MethodNotFound(f"method {method} is not supported")

            # Return the result
            return _json_response({"jsonrpc": "2.0", "id": rpc_id, "result": result})

        except PermissionDenied:
            return self._error_response(
                rpc_id,
                PaynetErrors.INVALID_LOGIN_OR_PASSWORD,
                "Invalid login or password",
                status_code=401,
            )

        except MethodNotFound as e:
            return self._error_response(rpc_id, PaynetErrors.METHOD_NOT_FOUND, str(e))

        except ServiceNotFound as e:
            return self._error_response(rpc_id, PaynetErrors.SERVICE_NOT_FOUND, str(e))

        except AccountNotFound:
            return self._error_response(rpc_id, PaynetErrors.CLIENT_NOT_FOUND, "Клиент не найден")

        except InvalidAmount as e:
            return self._error_response(rpc_id, PaynetErrors.INVALID_AMOUNT, str(e))

        except TransactionNotFound as e:
            return self._error_response(rpc_id, PaynetErrors.TRANSACTION_NOT_FOUND, str(e))

        except TransactionAlreadyExists as e:
            return self._error_response(
                rpc_id, PaynetErrors.TRANSACTION_ALREADY_EXISTS, str(e)
            )

        except AlreadyPaid:
            return self._error_response(rpc_id, PaynetErrors.ALREADY_PAID, "Клиент не найден")

        except Exception as e:  # noqa: BLE001
            logger.exception("Unexpected error in Paynet webhook: %s", e)
            return self._error_response(rpc_id, PaynetErrors.SYSTEM_ERROR, "System error")

    # ── helpers ────────────────────────────────────────────────────────────--
    def _check_auth(self, auth_header: Optional[str]) -> None:
        try:
            self.check_basic_auth(
                auth_header,
                expected_username=self.paynet_username,
                expected_password=self.paynet_password,
            )
        except PermissionDenied:
            # Re-raise to be caught by handle_webhook which maps it to error response
            raise
        except Exception as e:  # noqa: BLE001
            raise PermissionDenied("Invalid authentication format") from e

    def _validate_service_id(self, params: Dict[str, Any]) -> None:
        service_id = params.get("serviceId")
        if (
            service_id
            and self.paynet_service_id
            and str(service_id) != str(self.paynet_service_id)
        ):
            raise ServiceNotFound(f"Service {service_id} not found")

    def _error_response(
        self, rpc_id: Any, code: int, message: str, status_code: int = 200
    ) -> Response:
        return _json_response(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": code, "message": message},
            },
            status_code=status_code,
        )

    async def _find_account(self, params: Dict[str, Any]) -> Any:
        """Find account by parameters (Paynet passes them in ``params['fields']``)."""
        fields = params.get("fields", {})
        account_value = fields.get(self.account_field)

        if not account_value:
            # Try looking in top level just in case, but standard is fields
            account_value = params.get("account", {}).get(self.account_field)

        if not account_value:
            raise AccountNotFound("Account not found in parameters")

        lookup_field = "id" if self.account_field == "order_id" else self.account_field

        if lookup_field == "id" and isinstance(account_value, str) and account_value.isdigit():
            account_value = int(account_value)

        res = await self.db.execute(
            select(self.account_model).filter_by(**{lookup_field: account_value})
        )
        account = res.scalar_one_or_none()
        if not account:
            raise AccountNotFound(
                f"Account with {self.account_field}={account_value} not found"
            )
        return account

    async def _get_txn(self, transaction_id: Any):
        m = self.transaction_model
        res = await self.db.execute(
            select(m).where(m.gateway == m.PAYNET, m.transaction_id == transaction_id)
        )
        return res.scalar_one_or_none()

    # ── JSON-RPC methods ──────────────────────────────────────────────────---
    async def _perform_transaction(
        self, params: Dict[str, Any], rpc_id: Any
    ) -> Dict[str, Any]:
        self._validate_service_id(params)
        transaction_id = params.get("transactionId")
        amount = params.get("amount")
        service_id = params.get("serviceId")
        m = self.transaction_model

        account = await self._find_account(params)

        # Check for one-time payment - if account already has a successful transaction
        if self.one_time_payment:
            res = await self.db.execute(
                select(m).where(
                    m.gateway == m.PAYNET,
                    m.account_id == str(account.id),
                    m.state == m.SUCCESSFULLY,
                    m.transaction_id != transaction_id,
                )
            )
            existing_transaction = res.scalars().first()
            if existing_transaction:
                raise AlreadyPaid(f"Account {account.id} already has a successful payment")

        # Amount check
        # Convert account amount to tiyin (if stored in soum) and verify strict match.
        if hasattr(account, self.amount_field):
            account_amount_tiyin = int(getattr(account, self.amount_field) * 100)
            request_amount_tiyin = int(amount)
            if account_amount_tiyin != request_amount_tiyin:
                raise InvalidAmount("Incorrect amount")

        # Check if transaction exists
        existing = await self._get_txn(transaction_id)
        if existing is not None:
            raise TransactionAlreadyExists("Transaction already exists")

        # Create transaction
        transaction = m(
            gateway=m.PAYNET,
            transaction_id=transaction_id,
            account_id=str(account.id),
            amount=Decimal(amount) / 100,
            state=m.CREATED,
            extra_data={
                "service_id": service_id,
                "rpc_id": rpc_id,
                "time": params.get("time"),
            },
        )
        self.db.add(transaction)
        await self.db.commit()
        await self.db.refresh(transaction)

        await self.transaction_created(params, transaction, account)

        if transaction.state != m.SUCCESSFULLY:
            transaction.amount = Decimal(amount) / 100
            # Paynet PerformTransaction IS the payment execution.
            await transaction.mark_as_paid(self.db)
            await self.successfully_payment(params, transaction)

        return {
            "providerTrnId": transaction.id,
            "timestamp": transaction.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "fields": {self.account_field: transaction.account_id},
        }

    async def _check_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._validate_service_id(params)
        transaction_id = params.get("transactionId")
        m = self.transaction_model

        transaction = await self._get_txn(transaction_id)
        if transaction is None:
            return {
                "transactionState": 3,  # Transaction not found
                "providerTrnId": 0,
                "timestamp": self._format_timestamp(datetime.now()),
            }

        await self.check_transaction(params, transaction)

        # Map transaction states: 1 - Successful, 2 - Cancelled
        status = 1 if transaction.state == m.SUCCESSFULLY else 2

        return {
            "transactionState": status,
            "providerTrnId": transaction.id,
            "timestamp": self._format_timestamp(transaction.updated_at),
        }

    async def _cancel_transaction(
        self, params: Dict[str, Any], rpc_id: Any
    ) -> Dict[str, Any]:
        self._validate_service_id(params)
        transaction_id = params.get("transactionId")
        m = self.transaction_model

        transaction = await self._get_txn(transaction_id)
        if transaction is None:
            raise TransactionNotFound("Transaction not found")

        if transaction.state in (m.CANCELLED, m.CANCELLED_DURING_INIT):
            # Idempotent: already cancelled (PaynetErrors.TRANSACTION_ALREADY_CANCELLED = 202)
            return {
                "providerTrnId": transaction.id,
                "timestamp": transaction.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
                "transactionState": 2,  # Cancelled
            }

        await transaction.mark_as_cancelled(self.db)
        await self.cancelled_payment(params, transaction)

        return {
            "providerTrnId": transaction.id,
            "timestamp": transaction.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
            "transactionState": 2,  # Cancelled
        }

    async def _get_statement(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._validate_service_id(params)
        date_from = params.get("dateFrom")
        date_to = params.get("dateTo")
        m = self.transaction_model

        res = await self.db.execute(
            select(m).where(
                m.gateway == m.PAYNET,
                m.created_at >= date_from,
                m.created_at <= date_to,
                m.state != m.CANCELLED,
                m.state != m.CANCELLED_DURING_INIT,
            )
        )
        transactions = res.scalars().all()

        statements = [
            {
                "amount": int(tx.amount * 100),  # Return to Tiyin
                "providerTrnId": tx.id,
                "transactionId": tx.transaction_id,
                "timestamp": tx.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
            for tx in transactions
        ]

        await self.get_statement(params, statements)
        return {"statements": statements}

    async def _get_information(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._validate_service_id(params)
        account = await self._find_account(params)
        m = self.transaction_model

        # Check for one-time payment - if account already has a successful transaction
        if self.one_time_payment:
            res = await self.db.execute(
                select(m).where(
                    m.gateway == m.PAYNET,
                    m.account_id == str(account.id),
                    m.state == m.SUCCESSFULLY,
                )
            )
            existing_transaction = res.scalars().first()
            if existing_transaction:
                raise AlreadyPaid(f"Account {account.id} already has a successful payment")

        # Construct fields to return
        fields: Dict[str, Any] = {}
        for field in self.account_info_fields:
            if hasattr(account, field):
                fields[field] = getattr(account, field)

        response: Dict[str, Any] = {
            "status": "0",  # Active
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "fields": fields,
        }

        # Get additional check data from user implementation
        check_data = await self.get_check_data(params, account)
        if check_data:
            # If user provides fields, merge them carefully
            if "fields" in check_data:
                response["fields"].update(check_data.pop("fields"))

            # Merge other top-level keys (e.g. balance, custom status keys if any)
            response.update(check_data)

        if "balance" in response:
            try:
                response["balance"] = int(float(response["balance"]))
            except (ValueError, TypeError):
                pass

        return response

    def _format_timestamp(self, dt: Any) -> str:
        """Format timestamp to "Fri Jan 30 10:21:18 UZT 2026"."""
        if isinstance(dt, (int, float)):
            if dt == 0:
                dt = datetime.now()
            else:
                dt = datetime.fromtimestamp(dt)
        return dt.strftime("%a %b %d %H:%M:%S UZT %Y")

    # ── overridable async event hooks (no-op defaults) ────────────────────---
    async def transaction_created(self, params, transaction, account) -> None:
        """A new transaction was created (PerformTransaction)."""

    async def successfully_payment(self, params, transaction) -> None:
        """Payment confirmed (PerformTransaction)."""

    async def cancelled_payment(self, params, transaction) -> None:
        """Transaction cancelled/refunded (CancelTransaction)."""

    async def check_transaction(self, params, transaction) -> None:
        """CheckTransaction was called."""

    async def get_statement(self, params, transactions) -> None:
        """GetStatement was called."""

    async def get_check_data(self, params, account) -> Optional[Dict[str, Any]]:
        """Return extra ``GetInformation`` data (e.g. ``{"fields": {...}, "balance": ...}``)."""
        return None


class PaynetWebhookHandler(PaynetWebhookHandlerInternal):
    """Public async Paynet webhook handler.

    Subclass this and override the async event hooks
    (:meth:`transaction_created`, :meth:`successfully_payment`,
    :meth:`cancelled_payment`, :meth:`check_transaction`,
    :meth:`get_statement`, :meth:`get_check_data`) to customize behavior.
    """
