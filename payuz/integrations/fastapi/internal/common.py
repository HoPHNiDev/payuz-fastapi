"""Shared helpers for the async webhook handlers."""
import json
from typing import Any, Dict

from fastapi import Response


def json_response(payload: Dict[str, Any], status_code: int = 200) -> Response:
    """Serialize a dict to a JSON :class:`~fastapi.Response`."""
    return Response(
        content=json.dumps(payload), media_type="application/json", status_code=status_code
    )
