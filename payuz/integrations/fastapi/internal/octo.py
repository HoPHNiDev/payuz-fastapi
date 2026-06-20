"""
Internal async FastAPI webhook handler for Octo.

Async-native port of the Django ``OctoWebhook`` view. Octo is a *single-callback* webhook:
Octo POSTs a JSON body to the ``notify_url`` given during ``prepare_payment`` and expects a
simple JSON reply. This handler mirrors the Click handler's shape (returns a JSON ``Response``)
and uses an :class:`~sqlalchemy.ext.asyncio.AsyncSession` for all DB access.

The core logic lives in :class:`OctoWebhookHandlerInternal`; the public
:class:`OctoWebhookHandler` subclass is the one consumers instantiate / subclass to override
the async event hooks.

Signature verification (production only)::

    sha1(unique_key + octo_payment_UUID + status).hexdigest().upper()

compared (case-insensitively) against the ``signature`` field of the callback.
"""

import hashlib
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict

from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# pylint: disable=E0401,E0611
from payuz.core.exceptions import AccountNotFound, InvalidAmount, PermissionDenied
from payuz.gateways.octo.constants import OctoStatus

from ..models import PaymentTransaction
from .common import json_response as _json_response

logger = logging.getLogger(__name__)



class OctoWebhookHandlerInternal:
    """Async Octo callback webhook handler — core logic."""

    REQUIRED_FIELDS = ("octo_payment_UUID", "shop_transaction_id", "status", "total_sum")
    VALID_STATUSES = {
        OctoStatus.CREATED,
        OctoStatus.WAITING_PAY,
        OctoStatus.SUCCEEDED,
        OctoStatus.CANCELED,
        OctoStatus.FAILED,
        OctoStatus.REFUNDED,
    }

    def __init__(
        self,
        db: AsyncSession,
        octo_shop_id: Any,
        octo_secret: str,
        account_model: Any,
        unique_key: str = "",
        account_field: str = "id",
        amount_field: str = "amount",
        one_time_payment: bool = True,
        is_test_mode: bool = False,
        transaction_model: Any = PaymentTransaction,
    ):
        """
        Args:
            db: Async database session.
            octo_shop_id: Octo merchant shop ID (matched against the callback's ``octo_shop_id``).
            octo_secret: Octo secret key (kept for parity with the gateway / future API calls).
            account_model: The host project's account/order model class.
            unique_key: Octo signing key (required in production for signature verification).
            account_field: Model field used to look up the account by ``shop_transaction_id``.
            amount_field: Attribute on the account holding the expected amount (in som).
            one_time_payment: Validate the amount strictly and reject a second paid txn.
            is_test_mode: When ``True``, signature verification is DISABLED (testing only).
            transaction_model: Payment-transaction model (defaults to the standalone model).
        """
        if not octo_shop_id:
            raise ValueError("octo_shop_id is required")
        if not octo_secret:
            raise ValueError("octo_secret is required")
        if not is_test_mode and not unique_key:
            raise ValueError(
                "unique_key is required in production. Get this key from the Octo team, "
                "or set is_test_mode=True for testing."
            )

        self.db = db
        self.octo_shop_id = octo_shop_id
        self.octo_secret = octo_secret
        self.account_model = account_model
        self.unique_key = unique_key
        self.account_field = account_field
        self.amount_field = amount_field
        self.one_time_payment = one_time_payment
        self.is_test_mode = is_test_mode
        self.transaction_model = transaction_model

        if self.is_test_mode:
            logger.warning(
                "Octo webhook is running in TEST MODE. Signature verification is DISABLED. "
                "For production: set is_test_mode=False and provide the Octo unique_key."
            )

    # ── dispatch ───────────────────────────────────────────────────────────--
    async def handle_webhook(self, request: Request) -> Response:
        """Handle an incoming Octo callback POST request."""
        try:
            data = await self._parse_request(request)
            self._validate_provider_context(data)
            self._check_signature(data)
            self._validate_payload(data)
            return await self._handle_callback(data)
        except PermissionDenied as exc:
            return self._error_response(str(exc), status_code=403, code="permission_denied")
        except AccountNotFound as exc:
            return self._error_response(str(exc), status_code=404, code="account_not_found")
        except InvalidAmount as exc:
            return self._error_response(str(exc), status_code=400, code="invalid_amount")
        except ValueError as exc:
            return self._error_response(str(exc), status_code=400, code="invalid_payload")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in Octo webhook: %s", exc)
            return self._error_response("Internal error", status_code=500, code="internal_error")

    # ── response helpers ───────────────────────────────────────────────────--
    @staticmethod
    def _error_response(message: str, status_code: int = 400, code: str = "") -> Response:
        payload: Dict[str, Any] = {"error": message}
        if code:
            payload["code"] = code
        return _json_response(payload, status_code=status_code)

    @staticmethod
    def _ok_response(transaction: Any) -> Response:
        return _json_response({
            "status": "ok",
            "transaction_status": transaction.state,
        })

    # ── parsing / validation ───────────────────────────────────────────────--
    @staticmethod
    async def _parse_request(request: Request) -> Dict[str, Any]:
        try:
            body = await request.body()
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error("Octo webhook: invalid JSON body - %s", exc)
            raise ValueError("Invalid JSON") from exc

        if not isinstance(data, dict):
            raise ValueError("Invalid payload: JSON object expected")

        logger.info("Octo webhook received: %s", data)
        return data

    def _validate_provider_context(self, data: Dict[str, Any]) -> None:
        callback_shop_id = data.get("octo_shop_id")
        if callback_shop_id in (None, ""):
            return

        if str(callback_shop_id) != str(self.octo_shop_id):
            raise PermissionDenied("Invalid shop id")

    def _check_signature(self, data: Dict[str, Any]) -> None:
        if self.is_test_mode:
            logger.warning("Octo: Signature verification SKIPPED (test mode)")
            return

        signature = str(data.get("signature") or "").strip()
        uuid = str(data.get("octo_payment_UUID") or "").strip()
        status = str(data.get("status") or "").strip()

        if not signature:
            raise PermissionDenied("Missing signature")

        is_valid = self._verify_signature(
            unique_key=self.unique_key,
            uuid=uuid,
            status=status,
            signature=signature,
        )
        if not is_valid:
            logger.warning("Octo webhook: invalid signature")
            raise PermissionDenied("Invalid signature")

    @staticmethod
    def _verify_signature(unique_key: str, uuid: str, status: str, signature: str) -> bool:
        """Verify callback signature using SHA-1: sha1(unique_key + uuid + status)."""
        raw = f"{unique_key}{uuid}{status}"
        computed = hashlib.sha1(raw.encode("utf-8")).hexdigest().upper()  # noqa: S324
        return computed == signature.upper()

    def _validate_payload(self, data: Dict[str, Any]) -> None:
        missing_fields = [
            field for field in self.REQUIRED_FIELDS if data.get(field) in (None, "")
        ]
        if missing_fields:
            raise ValueError(f"Missing required fields: {', '.join(missing_fields)}")

        data["octo_payment_UUID"] = str(data["octo_payment_UUID"]).strip()
        data["shop_transaction_id"] = str(data["shop_transaction_id"]).strip()
        data["status"] = str(data["status"]).strip().lower()

        if data["status"] not in self.VALID_STATUSES:
            raise ValueError(f"Invalid status: {data['status']}")

        amount = self._parse_decimal(data["total_sum"], field_name="total_sum")
        if amount < Decimal("0"):
            raise InvalidAmount("total_sum must be non-negative")

        if (
            data["status"] in {OctoStatus.CREATED, OctoStatus.WAITING_PAY, OctoStatus.SUCCEEDED}
            and amount <= Decimal("0")
        ):
            raise InvalidAmount("total_sum must be positive")

        data["total_sum"] = amount

    @staticmethod
    def _parse_decimal(value: Any, field_name: str) -> Decimal:
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(f"Invalid decimal value for {field_name}") from None

    def _validate_amount(self, received_amount: Any, expected_amount: Any) -> None:
        """Validate that the received amount matches the expected amount."""
        received = self._parse_decimal(received_amount, "total_sum")
        expected = self._parse_decimal(expected_amount, self.amount_field)

        if self.one_time_payment:
            if abs(received - expected) > Decimal("0.01"):
                logger.warning(
                    "Octo amount mismatch: received=%s, expected=%s", received, expected
                )
                raise InvalidAmount(
                    f"Amount mismatch: received={received}, expected={expected}"
                )
            return

        if received <= Decimal("0"):
            logger.warning(
                "Octo invalid amount for non one-time flow: received=%s", received
            )
            raise InvalidAmount("Amount must be positive")

    # ── account lookup (async) ─────────────────────────────────────────────--
    async def _find_account(self, account_id: Any) -> Any:
        """Find the account (order) from ``account_model`` by ``account_field``."""
        lookup_value: Any = account_id
        if self.account_field == "id" and isinstance(account_id, str) and account_id.isdigit():
            lookup_value = int(account_id)

        res = await self.db.execute(
            select(self.account_model).filter_by(**{self.account_field: lookup_value})
        )
        account = res.scalar_one_or_none()
        if account is None:
            raise AccountNotFound(
                f"Account not found for {self.account_field}={account_id}"
            )
        return account

    # ── callback handling ──────────────────────────────────────────────────--
    async def _handle_callback(self, data: Dict[str, Any]) -> Response:
        """Process the callback data and update the transaction."""
        shop_transaction_id = data["shop_transaction_id"]
        octo_payment_uuid = data["octo_payment_UUID"]
        status = data["status"]
        total_sum = data["total_sum"]
        m = self.transaction_model

        extra_data = {
            "shop_transaction_id": shop_transaction_id,
            "transfer_sum": data.get("transfer_sum"),
            "refunded_sum": data.get("refunded_sum"),
            "card_country": data.get("card_country"),
            "maskedPan": data.get("maskedPan"),
            "rrn": data.get("rrn"),
            "riskLevel": data.get("riskLevel"),
            "payed_time": data.get("payed_time"),
            "card_type": data.get("card_type"),
            "currency": data.get("currency"),
            "card_vendor": data.get("card_vendor"),
            "status": status,
        }

        # 1. Check duplicate: if this octo_payment_UUID was already processed.
        transaction = await m.get_by_transaction_id(self.db, m.OCTO, octo_payment_uuid)

        if transaction is not None:
            # Already in a final state — return immediately, unless this is an explicit
            # refund callback that should move SUCCESSFULLY -> CANCELLED.
            if transaction.state in (m.SUCCESSFULLY, m.CANCELLED, m.CANCELLED_DURING_INIT):
                if status == OctoStatus.REFUNDED and transaction.state == m.SUCCESSFULLY:
                    pass
                else:
                    logger.info(
                        "Octo duplicate callback for %s, state=%s — skipping",
                        octo_payment_uuid,
                        transaction.state,
                    )
                    return self._ok_response(transaction)

            if transaction.amount is not None:
                self._validate_amount(total_sum, transaction.amount)

            # Update extra_data on existing non-final transaction.
            current = dict(transaction.extra_data or {})
            current.update(extra_data)
            transaction.extra_data = current
            await self.db.commit()
            await self.db.refresh(transaction)

        else:
            # 2. Find account (order) from account_model.
            account = await self._find_account(shop_transaction_id)

            # 3. Validate amount against account model.
            expected_amount = getattr(account, self.amount_field, None)
            if expected_amount is None:
                raise InvalidAmount(f"Account is missing '{self.amount_field}' field")
            self._validate_amount(total_sum, expected_amount)

            # 4. Check one_time_payment: reject if account already has a paid transaction.
            if self.one_time_payment:
                res = await self.db.execute(
                    select(m).where(
                        m.gateway == m.OCTO,
                        m.account_id == str(account.id),
                        m.state == m.SUCCESSFULLY,
                    )
                )
                if res.scalar_one_or_none() is not None:
                    logger.warning(
                        "Octo: account %s already has a successful payment — rejecting",
                        account.id,
                    )
                    return self._error_response(
                        "Payment already completed for this account",
                        status_code=409,
                        code="already_paid",
                    )

            # 5. Create new transaction.
            #    transaction_id = octo_payment_UUID (Octo's unique ID)
            #    account_id     = account.id        (merchant's order ID)
            transaction = await m.create_transaction(
                self.db,
                gateway=m.OCTO,
                transaction_id=octo_payment_uuid,
                account_id=str(account.id),
                amount=total_sum,
                extra_data=extra_data,
            )
            await self.transaction_created(data, transaction, account)

        if status == OctoStatus.SUCCEEDED:
            await transaction.mark_as_paid(self.db)
            await self.successfully_payment(data, transaction)

        elif status in (OctoStatus.CANCELED, OctoStatus.FAILED, OctoStatus.REFUNDED):
            await transaction.mark_as_cancelled(self.db)
            await self.cancelled_payment(data, transaction)

        return self._ok_response(transaction)

    # ── overridable async event hooks (no-op defaults) ────────────────────---
    async def transaction_created(self, params, transaction, account) -> None:
        """A new transaction was created."""

    async def successfully_payment(self, params, transaction) -> None:
        """Payment succeeded (Octo status ``succeeded``)."""

    async def cancelled_payment(self, params, transaction) -> None:
        """Payment cancelled/failed/refunded (Octo status ``canceled``/``failed``/``refunded``)."""


class OctoWebhookHandler(OctoWebhookHandlerInternal):
    """Public async Octo webhook handler. Subclass and override the async event hooks."""
