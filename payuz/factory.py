from payuz.core.base import BasePaymentGateway
from payuz.core.constants import PaymentGateway

from payuz.gateways.payme.client import PaymeGateway
from payuz.gateways.click.client import ClickGateway
from payuz.gateways.uzum.client import UzumGateway
from payuz.gateways.paynet.client import PaynetGateway
from payuz.gateways.octo.client import OctoGateway


def create_gateway(gateway_type: str, **kwargs) -> BasePaymentGateway:
    """
    Create a payment gateway instance.

    Args:
        gateway_type: Type of gateway ('payme', 'click', 'uzum', 'paynet', or 'octo')
        **kwargs: Gateway-specific configuration

    Returns:
        Payment gateway instance

    Raises:
        ValueError: If the gateway type is not supported
        ImportError: If the required gateway module is not available
    """
    if gateway_type.lower() == PaymentGateway.PAYME.value:
        return PaymeGateway(**kwargs)
    if gateway_type.lower() == PaymentGateway.CLICK.value:
        return ClickGateway(**kwargs)
    if gateway_type.lower() == PaymentGateway.UZUM.value:
        return UzumGateway(**kwargs)
    if gateway_type.lower() == PaymentGateway.PAYNET.value:
        return PaynetGateway(**kwargs)
    if gateway_type.lower() == PaymentGateway.OCTO.value:
        return OctoGateway(**kwargs)

    raise ValueError(f"Unsupported gateway type: {gateway_type}")
