"""
payuz — async FastAPI payment-gateway integration for Uzbekistan.

Unified interface for Payme, Click, Uzum, Paynet and Octo. FastAPI-only (async).
"""

__version__ = '0.1.0'

try:
    import fastapi  # noqa: F401
    HAS_FASTAPI = True
except ImportError:  # pragma: no cover
    HAS_FASTAPI = False

# Core gateway clients (framework-agnostic, used to build pay links / call provider APIs)
from payuz.core.base import BasePaymentGateway  # noqa: E402
from payuz.core.constants import PaymentGateway  # noqa: E402
from payuz.gateways.payme.client import PaymeGateway  # noqa: E402
from payuz.gateways.click.client import ClickGateway  # noqa: E402
from payuz.gateways.uzum.client import UzumGateway  # noqa: E402
from payuz.gateways.paynet.client import PaynetGateway  # noqa: E402
from payuz.gateways.octo.client import OctoGateway  # noqa: E402
from payuz.factory import create_gateway  # noqa: E402

__all__ = [
    '__version__',
    'HAS_FASTAPI',
    # Core
    'BasePaymentGateway',
    'PaymentGateway',
    # Gateways
    'PaymeGateway',
    'ClickGateway',
    'UzumGateway',
    'PaynetGateway',
    'OctoGateway',
    # Factory
    'create_gateway',
]
