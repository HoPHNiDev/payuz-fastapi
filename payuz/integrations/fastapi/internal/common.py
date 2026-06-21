"""Shared helpers for the async webhook handlers."""
import json
import uuid
from typing import Any, Dict

from fastapi import Response


def json_response(payload: Dict[str, Any], status_code: int = 200) -> Response:
    """Serialize a dict to a JSON :class:`~fastapi.Response`."""
    return Response(
        content=json.dumps(payload), media_type="application/json", status_code=status_code
    )


def coerce_account_value(lookup_field: str, value: Any) -> Any:
    """Coerce a string account value to the type of the ``id`` primary key.

    Providers send account values as strings. When ``account_field="order_id"`` resolves the
    lookup to the host model's ``id`` column, the value must match that column's Python type:
    an integer PK needs ``int``, a UUID PK needs :class:`uuid.UUID` (SQLAlchemy will not bind a
    bare string against a UUID column). Anything else is returned unchanged.
    """
    if lookup_field == "id" and isinstance(value, str):
        if value.isdigit():
            return int(value)
        try:
            return uuid.UUID(value)
        except ValueError:
            return value
    return value
