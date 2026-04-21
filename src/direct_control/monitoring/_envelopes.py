"""
Shared WebSocket message envelope helpers.

All outbound WS messages in this service share the shape
``{"type": <str>, "timestamp": <iso>, **fields}``; these helpers build and
send that envelope so the two managers don't repeat it ~60 times.
"""

import asyncio
from datetime import datetime
from typing import Any, Optional

from fastapi import WebSocket


class LockedWS:
    """
    Per-connection WebSocket wrapper that serializes outbound sends.

    Starlette's ``WebSocket.send_json`` is not concurrency-safe across
    coroutines. In this service a single client has three concurrent
    senders: the handler's request/response loop, fan-out broadcasts
    triggered by CA callbacks, and the heartbeat task. Without
    serialization these can interleave at the ASGI layer and produce
    protocol errors on busy connections.
    """

    def __init__(self, ws: WebSocket):
        self._ws = ws
        self._send_lock = asyncio.Lock()

    async def accept(self) -> None:
        await self._ws.accept()

    async def close(self, code: int = 1000, reason: Optional[str] = None) -> None:
        await self._ws.close(code=code, reason=reason)

    async def send_json(self, data: Any) -> None:
        async with self._send_lock:
            await self._ws.send_json(data)

    async def send_text(self, data: str) -> None:
        async with self._send_lock:
            await self._ws.send_text(data)

    async def receive_json(self) -> Any:
        return await self._ws.receive_json()

    async def receive_text(self) -> str:
        return await self._ws.receive_text()

    @property
    def query_params(self):
        return self._ws.query_params

    @property
    def headers(self):
        return self._ws.headers

    @property
    def client(self):
        return self._ws.client


async def send_event(ws, type_: str, **fields: Any) -> None:
    """Send a typed WS event with an ISO timestamp and arbitrary fields."""
    await ws.send_json({"type": type_, "timestamp": datetime.now().isoformat(), **fields})


async def send_error(ws: WebSocket, message: str, **fields: Any) -> None:
    """Send a WS error envelope with the given message."""
    await send_event(ws, "error", message=message, **fields)


async def heartbeat_loop(ws: WebSocket, interval: float) -> None:
    """
    Server-initiated WS heartbeat.

    Fires `{"type": "heartbeat", ...}` every `interval` seconds. Intended
    to keep NAT/proxy idle timers from reaping the TCP connection and to
    surface dead peers early (the next send will fail and we close).
    """
    if interval <= 0:
        return
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await send_event(ws, "heartbeat")
            except Exception:  # noqa: BLE001
                try:
                    await ws.close(code=1001, reason="Heartbeat send failed")
                except Exception:  # noqa: BLE001
                    pass
                return
    except asyncio.CancelledError:
        return
