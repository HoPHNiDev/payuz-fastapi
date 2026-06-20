"""
Async FastAPI integration for payuz.

Exposes the Base-agnostic transaction model (mixin + standalone) and the async webhook
handlers for every supported gateway.
"""

from .models import (  # noqa: F401
    Base,
    PaymentTransaction,
    PaymentTransactionMixin,
    run_migrations,
)
from .schemas import (  # noqa: F401
    ClickWebhookRequest,
    ClickWebhookResponse,
    PaymentTransactionBase,
    PaymentTransactionCreate,
    PaymentTransaction as PaymentTransactionSchema,
    PaymentTransactionList,
    PaymeWebhookErrorResponse,
    PaymeWebhookRequest,
    PaymeWebhookResponse,
)
from .internal import (  # noqa: F401
    ClickWebhookHandler,
    OctoWebhookHandler,
    PaymeWebhookHandler,
    PaynetWebhookHandler,
    UzumWebhookHandler,
)

__all__ = [
    # model
    "Base",
    "PaymentTransaction",
    "PaymentTransactionMixin",
    "run_migrations",
    # handlers
    "PaymeWebhookHandler",
    "ClickWebhookHandler",
    "UzumWebhookHandler",
    "PaynetWebhookHandler",
    "OctoWebhookHandler",
    # schemas
    "PaymentTransactionBase",
    "PaymentTransactionCreate",
    "PaymentTransactionSchema",
    "PaymentTransactionList",
    "PaymeWebhookRequest",
    "PaymeWebhookResponse",
    "PaymeWebhookErrorResponse",
    "ClickWebhookRequest",
    "ClickWebhookResponse",
]
