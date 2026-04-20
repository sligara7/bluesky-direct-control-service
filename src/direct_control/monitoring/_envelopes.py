"""
Shared WebSocket message envelope helpers.

All outbound WS messages in this service share the shape
``{"type": <str>, "timestamp": <iso>, **fields}``; these helpers build and
send that envelope so the two managers don't repeat it ~60 times.
"""

from datetime import datetime
from typing import Any

from fastapi import WebSocket


async def send_event(ws: WebSocket, type_: str, **fields: Any) -> None:
    """Send a typed WS event with an ISO timestamp and arbitrary fields."""
    await ws.send_json({"type": type_, "timestamp": datetime.now().isoformat(), **fields})


async def send_error(ws: WebSocket, message: str, **fields: Any) -> None:
    """Send a WS error envelope with the given message."""
    await send_event(ws, "error", message=message, **fields)
