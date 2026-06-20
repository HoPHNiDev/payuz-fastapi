"""
Internal async FastAPI webhook handler for payuz (Uzum Biller API).

This is an async-native port of the Django ``UzumWebhook`` handler
(:mod:`payuz.integrations.django.internal_webhooks.uzum`). It preserves the Uzum
Biller protocol exactly: the same actions (check / create / confirm / reverse /
status), the same Basic-auth + serviceId checks, the same response shapes and the
same error codes — only the runtime is async (an
:class:`~sqlalchemy.ext.asyncio.AsyncSession` instead of the Django ORM, and
``async def`` event hooks).

The split mirrors ``internal.py`` + ``routes.py``:
:class:`UzumWebhookHandlerInternal` holds the core logic and
:class:`UzumWebhookHandler` is the thin public subclass consumers extend to
override the async event hooks.

Unlike the Django handler — which received the action from the URL route
(``/check``, ``/create``, …) — :meth:`handle_webhook` derives the action from the
last segment of the request URL path. Consumers therefore mount one route per
action (or a single ``/{action}`` route) and forward the ``Request`` here.
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
    InvalidServiceId,
    PaymentAlreadyMade,
    PermissionDenied,
    TransactionCancelled,
    TransactionNotFound,
)
from payuz.gateways.uzum.constants import UzumStatus

from ..models import PaymentTransaction
from .common import json_response as _json_response

logger = logging.getLogger(__name__)



class UzumWebhookHandlerInternal(BasePaymentProcessor):
    """Async Uzum (Biller API) webhook handler — core logic."""

    def __init__(
        self,
        db: AsyncSession,
        username: str,
        password: str,
        account_model: Any,
        service_id: Optional[str] = None,
        account_field: str = "id",
        amount_field: str = "amount",
        one_time_payment: bool = True,
        transaction_model: Any = PaymentTransaction,
    ):
        """
        Args:
            db: Async database session.
            username: Biller API Basic-auth username.
            password: Biller API Basic-auth password.
            account_model: The host project's account/order model class.
            service_id: Uzum serviceId; when set, requests must match it.
            account_field: Model field to match the account identifier on.
            amount_field: Attribute on the account holding the expected amount.
            one_time_payment: Reject ``create`` when the account already has a
                successful payment.
            transaction_model: Payment-transaction model (defaults to the
                standalone model).
        """
        self.db = db
        self.username = username
        self.password = password
        self.account_model = account_model
        self.service_id = service_id
        self.account_field = account_field
        self.amount_field = amount_field
        self.one_time_payment = one_time_payment
        self.transaction_model = transaction_model

    # ── error helper ─────────────────────────────────────────────────────────
    def _error_response(
        self, error_code: str, request_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Generate an error response in Uzum format."""
        timestamp = int(datetime.now().timestamp() * 1000)

        service_id = self.service_id
        if request_data and "serviceId" in request_data:
            service_id = request_data["serviceId"]

        return {
            "serviceId": service_id,
            "timestamp": timestamp,
            "status": UzumStatus.FAILED,
            "errorCode": str(error_code),
        }

    # ── dispatch ─────────────────────────────────────────────────────────────
    async def handle_webhook(self, request: Request) -> Response:
        """Handle an Uzum Biller webhook request.

        The action (``check`` / ``create`` / ``confirm`` / ``reverse`` /
        ``status``) is taken from the last segment of the request URL path,
        mirroring the Django URL routing (``/check``, ``/create``, …).
        """
        action = request.url.path.rstrip("/").rsplit("/", 1)[-1].lower()

        data: Dict[str, Any] = {}
        try:
            # Parse request data first so it can be used in the error response.
            try:
                raw = await request.body()
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response(
                    self._error_response("10002"),  # JSON Parsing Error
                    status_code=400,
                )

            # Check authorization.
            self._check_auth(request.headers.get("Authorization"))

            # Validate service ID.
            self._check_service_id(data)

            if action == "check":
                result = await self._handle_check(data)
            elif action == "create":
                result = await self._handle_create(data)
            elif action == "confirm":
                result = await self._handle_confirm(data)
            elif action == "reverse":
                result = await self._handle_reverse(data)
            elif action == "status":
                result = await self._handle_status(data)
            else:
                return _json_response(
                    self._error_response("10003", data),  # Invalid Operation
                    status_code=400,
                )

            return _json_response(result)

        except PermissionDenied:
            return _json_response(
                self._error_response("10001", data),  # Access Denied
                status_code=400,
            )
        except InvalidServiceId:
            return _json_response(
                self._error_response("10006", data),  # Invalid Service ID
                status_code=400,
            )
        except AccountNotFound:
            return _json_response(
                self._error_response("10007", data),  # Account/Attribute Not Found
                status_code=400,
            )
        except PaymentAlreadyMade:
            return _json_response(
                self._error_response("10008", data),  # Payment Already Made
                status_code=400,
            )
        except TransactionCancelled:
            return _json_response(
                self._error_response("10009", data),  # Payment Cancelled
                status_code=400,
            )
        except TransactionNotFound:
            return _json_response(
                self._error_response("10009", data),  # Transaction not found
                status_code=400,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Uzum webhook error: %s", e)
            return _json_response(
                self._error_response("99999", data),  # Internal Error
                status_code=400,
            )

    # ── helpers ──────────────────────────────────────────────────────────────
    def _check_auth(self, auth_header: Optional[str]) -> None:
        try:
            self.check_basic_auth(
                auth_header,
                expected_username=self.username,
                expected_password=self.password,
            )
        except PermissionDenied:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Uzum auth failed for username: %s", self.username)
            raise PermissionDenied("Authentication error") from e

    def _check_service_id(self, data: Dict[str, Any]) -> None:
        """Validate service ID from request matches configured service ID."""
        request_service_id = data.get("serviceId")

        if request_service_id is None:
            logger.error("Uzum webhook: Missing serviceId in request")
            raise InvalidServiceId("Missing service ID")

        if self.service_id and int(request_service_id) != int(self.service_id):
            logger.error(
                "Uzum webhook: Invalid serviceId. Expected %s, got %s",
                self.service_id,
                request_service_id,
            )
            raise InvalidServiceId("Invalid service ID")

    async def _find_account(self, params: Dict[str, Any]) -> Any:
        """Find account by parameters."""
        # Try the configured field first, then common field names.
        account_value = params.get(self.account_field)

        if not account_value:
            account_value = params.get("account")

        if not account_value:
            account_value = params.get("orderId")

        if not account_value:
            account_value = params.get("order_id")

        if not account_value:
            raise AccountNotFound("Account identifier not found")

        # Handle lookup similar to Payme.
        lookup_field = "id" if self.account_field == "order_id" else self.account_field

        if (
            lookup_field == "id"
            and isinstance(account_value, str)
            and account_value.isdigit()
        ):
            account_value = int(account_value)

        res = await self.db.execute(
            select(self.account_model).filter_by(**{lookup_field: account_value})
        )
        account = res.scalar_one_or_none()
        if not account:
            raise AccountNotFound("Account not found")
        return account

    async def _get_txn(self, trans_id: str):
        m = self.transaction_model
        res = await self.db.execute(
            select(m).where(m.gateway == m.UZUM, m.transaction_id == trans_id)
        )
        return res.scalar_one_or_none()

    # ── actions ──────────────────────────────────────────────────────────────
    async def _handle_check(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Request: { "serviceId": ..., "params": { "orderId": ... } }
        Response: { "serviceId": ..., "timestamp": ..., "status": "OK", "data": { ... } }
        """
        params = data.get("params", {})
        if not params:
            raise AccountNotFound("Missing params")

        account = await self._find_account(params)

        extra_data = await self.get_check_data(params, account) or {}

        response_data = {"account": {"value": str(account.id)}}
        response_data.update(extra_data)

        timestamp = int(datetime.now().timestamp() * 1000)
        service_id = data.get("serviceId", self.service_id)

        await self.check_transaction(params, account)

        return {
            "serviceId": service_id,
            "timestamp": timestamp,
            "status": UzumStatus.OK,
            "data": response_data,
        }

    async def _handle_create(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Request: { "transId": "...", "amount": 1000, "params": ... }
        """
        trans_id = data.get("transId")
        service_id = data.get("serviceId", self.service_id)
        amount = data.get("amount")  # in tiyins
        params = data.get("params", {})
        m = self.transaction_model

        account = await self._find_account(params)

        # One-time payment: reject if the account already has a successful txn.
        if self.one_time_payment:
            res = await self.db.execute(
                select(m).where(
                    m.gateway == m.UZUM,
                    m.account_id == str(account.id),
                    m.state == m.SUCCESSFULLY,
                    m.transaction_id != trans_id,
                )
            )
            if res.scalars().first():
                raise PaymentAlreadyMade(
                    f"Account {account.id} already has a successful payment"
                )

        transaction = await self._get_txn(trans_id)
        created = transaction is None
        if created:
            transaction = m(
                gateway=m.UZUM,
                transaction_id=trans_id,
                account_id=str(account.id),
                amount=Decimal(amount) / 100,
                state=m.CREATED,
                extra_data={"raw_params": data},
            )
            self.db.add(transaction)
            await self.db.commit()
            await self.db.refresh(transaction)
        else:
            if transaction.state == m.SUCCESSFULLY:
                raise PaymentAlreadyMade("Payment has already been made")
            if transaction.state == m.CANCELLED:
                raise TransactionCancelled("Transaction has been cancelled")

        extra_data = await self.get_check_data(params, account) or {}
        response_data = {"account": {"value": str(account.id)}}
        response_data.update(extra_data)

        # Use transaction created_at for transTime.
        trans_time = int(transaction.created_at.timestamp() * 1000)

        if created:
            await self.transaction_created(data, transaction, account)

        return {
            "serviceId": service_id,
            "transId": trans_id,
            "status": UzumStatus.CREATED,
            "transTime": trans_time,
            "data": response_data,
            "amount": amount,
        }

    async def _handle_confirm(self, data: Dict[str, Any]) -> Dict[str, Any]:
        trans_id = data.get("transId")
        service_id = data.get("serviceId", self.service_id)
        m = self.transaction_model

        transaction = await self._get_txn(trans_id)
        if transaction is None:
            raise TransactionNotFound("Transaction not found")

        if transaction.state != m.SUCCESSFULLY:
            await transaction.mark_as_paid(self.db)
            await self.successfully_payment(data, transaction)

        # Prepare data for response.
        account = await self._get_account_for_transaction(transaction)

        params = data.get("params", {})
        if not params and transaction.extra_data:
            params = transaction.extra_data.get("raw_params", {}).get("params", {})

        response_data: Dict[str, Any] = {}
        if account:
            response_data["account"] = {"value": str(account.id)}
            extra_data = await self.get_check_data(params, account) or {}
            response_data.update(extra_data)

        # Use transaction updated_at for confirmTime (when it was confirmed).
        confirm_time = int(transaction.updated_at.timestamp() * 1000)
        # Use transaction created_at for transTime.
        trans_time = int(transaction.created_at.timestamp() * 1000)

        return {
            "serviceId": service_id,
            "transId": trans_id,
            "status": UzumStatus.CONFIRMED,
            "confirmTime": confirm_time,
            "transTime": trans_time,
            "data": response_data,
            "amount": int(transaction.amount * 100),
        }

    async def _handle_reverse(self, data: Dict[str, Any]) -> Dict[str, Any]:
        trans_id = data.get("transId")
        service_id = data.get("serviceId", self.service_id)
        m = self.transaction_model

        transaction = await self._get_txn(trans_id)
        if transaction is None:
            raise TransactionNotFound("Transaction not found")

        # Check if transaction is already cancelled.
        if transaction.state == m.CANCELLED:
            raise TransactionCancelled("Transaction has already been cancelled")

        await transaction.mark_as_cancelled(self.db)
        await self.cancelled_payment(data, transaction)

        # Prepare data for response.
        account = await self._get_account_for_transaction(transaction)

        params = data.get("params", {})
        if not params and transaction.extra_data:
            params = transaction.extra_data.get("raw_params", {}).get("params", {})

        response_data: Dict[str, Any] = {}
        if account:
            response_data["account"] = {"value": str(account.id)}
            extra_data = await self.get_check_data(params, account) or {}
            response_data.update(extra_data)

        return {
            "serviceId": service_id,
            "transId": trans_id,
            "status": UzumStatus.REVERSED,
            "reverseTime": int(datetime.now().timestamp() * 1000),
            "data": response_data,
            "amount": int(transaction.amount * 100),
        }

    async def _handle_status(self, data: Dict[str, Any]) -> Dict[str, Any]:
        trans_id = data.get("transId")
        service_id = data.get("serviceId", self.service_id)
        m = self.transaction_model

        transaction = await self._get_txn(trans_id)
        if transaction is None:
            raise TransactionNotFound("Transaction not found")

        status_value = UzumStatus.CREATED
        confirm_time = None
        reverse_time = None

        # Set confirmTime if transaction was ever confirmed (performed_at is set).
        if transaction.performed_at:
            confirm_time = int(transaction.performed_at.timestamp() * 1000)

        if transaction.state == m.SUCCESSFULLY:
            status_value = UzumStatus.CONFIRMED
        elif transaction.state == m.CANCELLED:
            status_value = UzumStatus.REVERSED
            if transaction.cancelled_at:
                reverse_time = int(transaction.cancelled_at.timestamp() * 1000)

        # Prepare data for response.
        account = await self._get_account_for_transaction(transaction)

        params = data.get("params", {})
        if not params and transaction.extra_data:
            params = transaction.extra_data.get("raw_params", {}).get("params", {})

        response_data: Dict[str, Any] = {}
        if account:
            response_data["account"] = {"value": str(account.id)}
            extra_data = await self.get_check_data(params, account) or {}
            response_data.update(extra_data)

        await self.get_statement(data, transaction)

        return {
            "serviceId": service_id,
            "transId": trans_id,
            "status": status_value,
            "transTime": int(transaction.created_at.timestamp() * 1000),
            "confirmTime": confirm_time,
            "reverseTime": reverse_time,
            "data": response_data,
            "amount": int(transaction.amount * 100),
        }

    async def _get_account_for_transaction(self, transaction: Any) -> Any:
        """Look up the account a transaction belongs to (``None`` if missing)."""
        if not transaction.account_id:
            return None

        account_value: Any = transaction.account_id
        if isinstance(account_value, str) and account_value.isdigit():
            account_value = int(account_value)

        res = await self.db.execute(
            select(self.account_model).filter_by(id=account_value)
        )
        return res.scalar_one_or_none()

    # ── overridable async event hooks (no-op defaults) ───────────────────────-
    async def transaction_created(self, params, transaction, account) -> None:
        """A new transaction was created (``create``)."""

    async def successfully_payment(self, params, transaction) -> None:
        """Payment confirmed (``confirm``)."""

    async def cancelled_payment(self, params, transaction) -> None:
        """Transaction reversed/refunded (``reverse``)."""

    async def check_transaction(self, params, account) -> None:
        """``check`` was called for an account."""

    async def get_statement(self, params, transaction) -> None:
        """``status`` was called for a transaction."""

    async def get_check_data(self, params, account):
        """
        Override to return extra data for the ``check`` action.

        Args:
            params: Request parameters.
            account: Account object.

        Returns:
            Dict of extra fields merged into the ``data`` field of the response,
            e.g. ``{"fio": {"value": "Ivanov Ivan"}}``.
        """
        return None


class UzumWebhookHandler(UzumWebhookHandlerInternal):
    """Uzum (Biller API) webhook handler. Override the async event hooks."""
