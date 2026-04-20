"""
WebSocket connection manager for PV updates.

Manages WebSocket connections and routes PV updates to connected clients.
Write operations (set/stop) are routed through the DeviceControl protocol
so they inherit coordination (A4) checks.
"""

import asyncio
import uuid
from typing import Callable, Dict, Optional, Set, TYPE_CHECKING

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from ..models import (
    DeviceCommandRequest,
    DeviceLockedError,
    PVSetRequest,
    PVUpdate,
    WebSocketAction,
)
from ..registry_client import RegistryClient, RegistryValidationError
from ._envelopes import send_error, send_event

if TYPE_CHECKING:
    from ..protocols import DeviceControl, PVMonitor


logger = structlog.get_logger(__name__)


class WebSocketManager:
    """
    Manages WebSocket connections and PV update routing.

    Uses PVMonitor protocol for subscription management and DeviceControl
    protocol for coordination-checked write operations.
    """

    def __init__(
        self,
        pv_monitor: "PVMonitor",
        device_controller: "DeviceControl",
        registry_client: Optional[RegistryClient] = None,
    ):
        self.pv_monitor = pv_monitor
        self.device_controller = device_controller
        self.registry_client = registry_client
        self._connections: Dict[str, WebSocket] = {}
        self._subscriptions: Dict[str, Set[str]] = {}
        self._pv_clients: Dict[str, Set[str]] = {}
        self._pv_callbacks: Dict[str, Callable[[PVUpdate], None]] = {}
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def connect(self, websocket: WebSocket) -> str:
        await websocket.accept()
        client_id = str(uuid.uuid4())

        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        async with self._lock:
            self._connections[client_id] = websocket
            self._subscriptions[client_id] = set()

        logger.info("websocket_connected", client_id=client_id)
        return client_id

    async def disconnect(self, client_id: str):
        async with self._lock:
            self._connections.pop(client_id, None)
            pv_names = self._subscriptions.pop(client_id, set())

            for pv_name in pv_names:
                if pv_name in self._pv_clients:
                    self._pv_clients[pv_name].discard(client_id)
                    if not self._pv_clients[pv_name]:
                        self._pv_clients.pop(pv_name)
                        callback = self._pv_callbacks.pop(pv_name, None)
                        self.pv_monitor.unsubscribe(pv_name, callback)

        logger.info("websocket_disconnected", client_id=client_id, pv_count=len(pv_names))

    async def close_all(self):
        """Close every open client connection (invoked on service shutdown)."""
        async with self._lock:
            sockets = list(self._connections.values())
            self._connections.clear()
        for ws in sockets:
            try:
                await ws.close(code=1001, reason="Service shutting down")
            except Exception:  # noqa: BLE001
                pass

    async def subscribe_pvs(self, client_id: str, pv_names: list[str]):
        """Subscribe a client to PVs; runs blocking EPICS subscribes off-loop."""
        async with self._lock:
            if client_id not in self._connections:
                logger.warning("subscribe_unknown_client", client_id=client_id)
                return

            new_pvs: list[tuple[str, Callable[[PVUpdate], None]]] = []
            for pv_name in pv_names:
                self._subscriptions[client_id].add(pv_name)
                if pv_name not in self._pv_clients:
                    self._pv_clients[pv_name] = set()
                    callback = self._make_pv_callback(pv_name)
                    self._pv_callbacks[pv_name] = callback
                    new_pvs.append((pv_name, callback))
                self._pv_clients[pv_name].add(client_id)

        # Run blocking EPICS subscribes outside the asyncio lock.
        for pv_name, callback in new_pvs:
            try:
                await asyncio.to_thread(self.pv_monitor.subscribe, pv_name, callback)
                logger.info("subscribed_to_pv", pv_name=pv_name, client_id=client_id)
            except Exception as e:  # noqa: BLE001
                logger.error("pv_subscription_failed", pv_name=pv_name, error=str(e))
                async with self._lock:
                    self._pv_callbacks.pop(pv_name, None)
                    self._pv_clients.pop(pv_name, None)

        # Send current values in parallel.
        values = await asyncio.gather(
            *(asyncio.to_thread(self.pv_monitor.get_value, pv_name) for pv_name in pv_names),
            return_exceptions=True,
        )
        for value in values:
            if isinstance(value, BaseException) or value is None:
                continue
            await self._send_to_client(
                client_id,
                PVUpdate(
                    pv_name=value.pv_name,
                    value=value.value,
                    timestamp=value.timestamp,
                    status=value.status,
                    severity=value.severity,
                    connected=value.connected,
                ),
            )

        logger.info("client_subscribed", client_id=client_id, pv_count=len(pv_names))

    async def unsubscribe_pvs(self, client_id: str, pv_names: list[str]):
        async with self._lock:
            if client_id not in self._subscriptions:
                return

            for pv_name in pv_names:
                self._subscriptions[client_id].discard(pv_name)
                if pv_name in self._pv_clients:
                    self._pv_clients[pv_name].discard(client_id)
                    if not self._pv_clients[pv_name]:
                        self._pv_clients.pop(pv_name)
                        callback = self._pv_callbacks.pop(pv_name, None)
                        self.pv_monitor.unsubscribe(pv_name, callback)
                        logger.info("unsubscribed_from_pv", pv_name=pv_name)

        logger.info("client_unsubscribed", client_id=client_id, pv_count=len(pv_names))

    def _make_pv_callback(self, pv_name: str) -> Callable[[PVUpdate], None]:
        def callback(update: PVUpdate) -> None:
            if self._loop is None:
                logger.warning("callback_before_loop_initialized", pv_name=pv_name)
                return
            asyncio.run_coroutine_threadsafe(
                self._broadcast_update(pv_name, update), self._loop
            )

        return callback

    async def _broadcast_update(self, pv_name: str, update: PVUpdate):
        async with self._lock:
            client_ids = self._pv_clients.get(pv_name, set()).copy()
        for client_id in client_ids:
            await self._send_to_client(client_id, update)

    async def _send_to_client(
        self, client_id: str, update: PVUpdate, websocket: Optional[WebSocket] = None
    ):
        if websocket is None:
            async with self._lock:
                websocket = self._connections.get(client_id)

        if not websocket:
            return

        try:
            await websocket.send_json(update.model_dump(mode="json"))
        except Exception as e:  # noqa: BLE001
            logger.error(
                "websocket_send_error",
                client_id=client_id,
                pv_name=update.pv_name,
                error=str(e),
            )

    async def handle_client(self, websocket: WebSocket):
        client_id = await self.connect(websocket)

        try:
            while True:
                data = await websocket.receive_json()
                action = data.get("action") or data.get("type")

                if action in ("subscribe", WebSocketAction.SUBSCRIBE.value):
                    await self._handle_subscribe(client_id, websocket, data)
                elif action in ("unsubscribe", WebSocketAction.UNSUBSCRIBE.value):
                    await self._handle_unsubscribe(client_id, websocket, data)
                elif action == WebSocketAction.SUBSCRIBE_SAFELY.value:
                    await self._handle_subscribe_safely(client_id, websocket, data)
                elif action == WebSocketAction.SUBSCRIBE_READ_ONLY.value:
                    await self._handle_subscribe_read_only(client_id, websocket, data)
                elif action == WebSocketAction.REFRESH.value:
                    await self._handle_refresh(client_id, websocket, data)
                elif action == WebSocketAction.SET.value:
                    await self._handle_set(client_id, websocket, data)
                elif action in ("stop", WebSocketAction.STOP.value):
                    await self._handle_stop(websocket, data)
                elif action == "ping":
                    await send_event(websocket, "pong")
                else:
                    logger.warning("unknown_message_type", type=action, client_id=client_id)
                    await send_error(websocket, f"Unknown action: {action}")

        except WebSocketDisconnect:
            logger.info("websocket_disconnect", client_id=client_id)
        except Exception as e:  # noqa: BLE001
            logger.error("websocket_error", client_id=client_id, error=str(e), exc_info=True)
        finally:
            await self.disconnect(client_id)

    async def _handle_subscribe(self, client_id: str, websocket: WebSocket, data: dict):
        pv_names = [data["pv"]] if data.get("pv") else data.get("pv_names", [])

        valid_pvs = await self._validate_pvs(websocket, pv_names)
        if valid_pvs:
            await send_event(websocket, "subscribed", pv_names=valid_pvs)
            await self.subscribe_pvs(client_id, valid_pvs)

    async def _handle_unsubscribe(self, client_id: str, websocket: WebSocket, data: dict):
        pv_names = [data["pv"]] if data.get("pv") else data.get("pv_names", [])

        await self.unsubscribe_pvs(client_id, pv_names)
        await send_event(websocket, "unsubscribed", pv_names=pv_names)

    async def _handle_subscribe_safely(self, client_id: str, websocket: WebSocket, data: dict):
        pv = data.get("pv")
        if not pv:
            await send_error(websocket, "pv field required for subscribeSafely")
            return

        if not await self._validate_single_pv(websocket, pv):
            return

        try:
            if not await asyncio.to_thread(self.pv_monitor.is_connected, pv):
                await asyncio.to_thread(self.pv_monitor.subscribe, pv)

            value = await asyncio.to_thread(self.pv_monitor.get_value, pv)
            if value is None or not value.connected:
                await send_error(
                    websocket, f"PV {pv} not connected", pv=pv, connected=False
                )
                return

            await self.subscribe_pvs(client_id, [pv])
            await send_event(websocket, "subscribed", pv_names=[pv], connected=True)

        except Exception as e:  # noqa: BLE001
            await send_error(websocket, str(e), pv=pv)

    async def _handle_subscribe_read_only(self, client_id: str, websocket: WebSocket, data: dict):
        pv_names = [data["pv"]] if data.get("pv") else data.get("pv_names", [])

        valid_pvs = await self._validate_pvs(websocket, pv_names)
        if valid_pvs:
            await self.subscribe_pvs(client_id, valid_pvs)
            await send_event(websocket, "subscribed", pv_names=valid_pvs, read_only=True)

    async def _handle_refresh(self, client_id: str, websocket: WebSocket, data: dict):
        pv = data.get("pv")

        async with self._lock:
            if pv:
                pv_names = [pv] if pv in self._subscriptions.get(client_id, set()) else []
            else:
                pv_names = list(self._subscriptions.get(client_id, set()))

        values = await asyncio.gather(
            *(asyncio.to_thread(self.pv_monitor.get_value, pv_name) for pv_name in pv_names),
            return_exceptions=True,
        )
        for value in values:
            if isinstance(value, BaseException) or value is None:
                continue
            await self._send_to_client(
                client_id,
                PVUpdate(
                    pv_name=value.pv_name,
                    value=value.value,
                    timestamp=value.timestamp,
                    status=value.status,
                    severity=value.severity,
                    connected=value.connected,
                    read_access=True,
                    write_access=True,
                ),
            )

        await send_event(websocket, "refreshed", pv_names=pv_names)

    async def _handle_set(self, client_id: str, websocket: WebSocket, data: dict):
        """Set PV value via DeviceControl (inherits coordination check)."""
        pv = data.get("pv")
        value = data.get("value")
        timeout = data.get("timeout")
        use_put = bool(data.get("use_put", False))

        if not pv or value is None:
            await send_error(websocket, "pv and value fields required for set")
            return

        if not await self._validate_single_pv(websocket, pv):
            return

        try:
            response = await self.device_controller.set_pv(
                PVSetRequest(pv_name=pv, value=value, wait=not use_put, timeout=timeout)
            )
            await send_event(
                websocket,
                "set_complete",
                pv=pv,
                value=value,
                success=response.success,
                message=response.message,
                use_put=use_put,
            )
        except DeviceLockedError as e:
            await send_error(websocket, str(e), pv=pv, locked=True)
        except Exception as e:  # noqa: BLE001
            logger.error("pv_set_error", pv=pv, value=value, error=str(e))
            await send_error(websocket, str(e), pv=pv)

    async def _handle_stop(self, websocket: WebSocket, data: dict):
        """Stop a device via DeviceControl (inherits coordination check)."""
        device = data.get("device")

        if not device:
            await send_error(websocket, "device field required for stop")
            return

        try:
            response = await self.device_controller.execute_device_method(
                DeviceCommandRequest(device_name=device, method="stop", args=[], kwargs={})
            )
            await send_event(
                websocket,
                "stop_complete",
                device=device,
                success=response.success,
                message=response.message or "Device stopped",
            )
        except DeviceLockedError as e:
            await send_error(websocket, str(e), device=device, locked=True)
        except Exception as e:  # noqa: BLE001
            logger.error("pv_stop_error", device=device, error=str(e))
            await send_error(websocket, str(e), device=device)

    async def _validate_pvs(self, websocket: WebSocket, pv_names: list[str]) -> list[str]:
        if not self.registry_client:
            return list(pv_names)

        results = await asyncio.gather(
            *(self.registry_client.validate_pv(p) for p in pv_names),
            return_exceptions=True,
        )
        valid: list[str] = []
        for pv_name, result in zip(pv_names, results):
            if isinstance(result, (RegistryValidationError, RuntimeError)):
                await send_error(websocket, str(result), pv=pv_name)
            elif isinstance(result, Exception):
                raise result
            else:
                valid.append(pv_name)
        return valid

    async def _validate_single_pv(self, websocket: WebSocket, pv: str) -> bool:
        if not self.registry_client:
            return True
        try:
            await self.registry_client.validate_pv(pv)
            return True
        except (RegistryValidationError, RuntimeError) as e:
            await send_error(websocket, str(e), pv=pv)
            return False

    def get_stats(self) -> dict:
        return {
            "active_connections": len(self._connections),
            "total_pvs": len(self._pv_clients),
            "connected_pvs": len(self.pv_monitor.get_connected_pvs()),
        }
