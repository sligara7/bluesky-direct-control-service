"""
Device WebSocket manager for ophyd-websocket compatible device monitoring.

Manages WebSocket connections for device-level subscriptions, recursively
subscribing to all PVs associated with a device from the configuration service.
Write/stop operations are routed through DeviceControl for coordination checks.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Dict, Optional, Set, TYPE_CHECKING

import httpx
import structlog
from fastapi import WebSocket, WebSocketDisconnect

from ..config import Settings
from ..models import (
    DeviceCommandRequest,
    DeviceInfo,
    DeviceLockedError,
    DeviceUpdate,
    PVUpdate,
    WebSocketAction,
)

if TYPE_CHECKING:
    from ..protocols import DeviceControl, PVMonitor


logger = structlog.get_logger(__name__)


class DeviceWebSocketManager:
    """
    Manages WebSocket connections for device-level subscriptions.

    Implements ophyd-websocket compatible device-socket protocol. Writes/stops
    are routed through the DeviceControl protocol so they inherit A4
    coordination checks.
    """

    def __init__(
        self,
        pv_monitor: "PVMonitor",
        device_controller: "DeviceControl",
        settings: Settings,
    ):
        self.pv_monitor = pv_monitor
        self.device_controller = device_controller
        self.settings = settings
        self._connections: Dict[str, WebSocket] = {}
        self._device_subscriptions: Dict[str, Set[str]] = {}
        self._device_pvs: Dict[str, Dict[str, str]] = {}
        self._pv_callbacks: Dict[str, callable] = {}
        self._device_clients: Dict[str, Set[str]] = {}
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def _fetch_device_info(self, device_name: str) -> Optional[DeviceInfo]:
        config_url = self.settings.configuration_service_url
        try:
            client = await self._get_http_client()
            response = await client.get(f"{config_url}/api/v1/devices/{device_name}")
            if response.status_code == 200:
                data = response.json()
                return DeviceInfo(
                    name=data.get("name", device_name),
                    device_type=data.get("device_type", "unknown"),
                    ophyd_class=data.get("ophyd_class"),
                    pvs=data.get("pvs", {}),
                    is_movable=data.get("is_movable", False),
                    is_readable=data.get("is_readable", True),
                )
            logger.warning(
                "device_info_fetch_failed",
                device_name=device_name,
                status=response.status_code,
            )
            return None
        except Exception as e:
            logger.error("device_info_fetch_error", device_name=device_name, error=str(e))
            return None

    async def connect(self, websocket: WebSocket) -> str:
        await websocket.accept()
        client_id = str(uuid.uuid4())

        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        async with self._lock:
            self._connections[client_id] = websocket
            self._device_subscriptions[client_id] = set()

        logger.info("device_websocket_connected", client_id=client_id)
        return client_id

    async def disconnect(self, client_id: str):
        async with self._lock:
            self._connections.pop(client_id, None)
            device_names = self._device_subscriptions.pop(client_id, set())

            for device_name in device_names:
                if device_name in self._device_clients:
                    self._device_clients[device_name].discard(client_id)
                    if not self._device_clients[device_name]:
                        self._device_clients.pop(device_name)
                        await self._unsubscribe_device_pvs(device_name)

        logger.info("device_websocket_disconnected", client_id=client_id)

    async def _unsubscribe_device_pvs(self, device_name: str):
        pvs = self._device_pvs.pop(device_name, {})
        for pv_name in pvs.values():
            callback = self._pv_callbacks.pop(pv_name, None)
            if callback:
                self.pv_monitor.unsubscribe(pv_name, callback)

    async def subscribe_device(
        self, client_id: str, device_name: str, require_connection: bool = False
    ):
        async with self._lock:
            if client_id not in self._connections:
                logger.warning("subscribe_unknown_client", client_id=client_id)
                return False
            if device_name in self._device_subscriptions.get(client_id, set()):
                return True

        device_info = await self._fetch_device_info(device_name)
        if device_info is None:
            return False

        async with self._lock:
            self._device_subscriptions[client_id].add(device_name)

            if device_name not in self._device_clients:
                self._device_clients[device_name] = set()
                self._device_pvs[device_name] = device_info.pvs

                for component, pv_name in device_info.pvs.items():
                    callback = self._make_device_callback(device_name, component, pv_name)
                    self._pv_callbacks[pv_name] = callback
                    try:
                        self.pv_monitor.subscribe(pv_name, callback)
                        logger.debug(
                            "subscribed_device_pv",
                            device=device_name,
                            component=component,
                            pv=pv_name,
                        )
                    except Exception as e:
                        logger.error(
                            "device_pv_subscribe_failed", pv=pv_name, error=str(e)
                        )
                        if require_connection:
                            return False

            self._device_clients[device_name].add(client_id)

        await self._send_current_values(client_id, device_name)

        logger.info(
            "device_subscribed",
            client_id=client_id,
            device=device_name,
            pvs=len(device_info.pvs),
        )
        return True

    async def unsubscribe_device(self, client_id: str, device_name: str):
        async with self._lock:
            if client_id not in self._device_subscriptions:
                return

            self._device_subscriptions[client_id].discard(device_name)

            if device_name in self._device_clients:
                self._device_clients[device_name].discard(client_id)
                if not self._device_clients[device_name]:
                    self._device_clients.pop(device_name)
                    await self._unsubscribe_device_pvs(device_name)

        logger.info("device_unsubscribed", client_id=client_id, device=device_name)

    def _make_device_callback(self, device_name: str, component: str, pv_name: str):
        def callback(update: PVUpdate):
            if self._loop is None:
                return
            device_update = DeviceUpdate(
                device=device_name,
                signal=component,
                value=update.value,
                timestamp=update.timestamp,
                connected=update.connected,
                read_access=update.read_access,
                write_access=update.write_access,
            )
            asyncio.run_coroutine_threadsafe(
                self._broadcast_device_update(device_name, device_update), self._loop
            )

        return callback

    async def _broadcast_device_update(self, device_name: str, update: DeviceUpdate):
        async with self._lock:
            client_ids = self._device_clients.get(device_name, set()).copy()

        for client_id in client_ids:
            await self._send_to_client(client_id, update)

    async def _send_to_client(self, client_id: str, update: DeviceUpdate):
        async with self._lock:
            websocket = self._connections.get(client_id)

        if not websocket:
            return

        try:
            await websocket.send_json(update.model_dump(mode="json"))
        except Exception as e:
            logger.error("device_websocket_send_error", client_id=client_id, error=str(e))

    async def _send_current_values(self, client_id: str, device_name: str):
        async with self._lock:
            pvs = self._device_pvs.get(device_name, {})
            websocket = self._connections.get(client_id)

        if not websocket:
            return

        for component, pv_name in pvs.items():
            current_value = await asyncio.to_thread(self.pv_monitor.get_value, pv_name)
            if current_value:
                update = DeviceUpdate(
                    device=device_name,
                    signal=component,
                    value=current_value.value,
                    timestamp=current_value.timestamp,
                    connected=current_value.connected,
                    read_access=True,
                    write_access=True,
                )
                try:
                    await websocket.send_json(update.model_dump(mode="json"))
                except Exception as e:
                    logger.error("send_current_value_error", error=str(e))

    async def handle_client(self, websocket: WebSocket):
        client_id = await self.connect(websocket)

        try:
            while True:
                data = await websocket.receive_json()
                action = data.get("action")

                if action == "subscribe":
                    await self._handle_subscribe(client_id, websocket, data)
                elif action == "unsubscribe":
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
                    await self._handle_stop(client_id, websocket, data)
                elif action == "ping":
                    await websocket.send_json(
                        {"type": "pong", "timestamp": datetime.now().isoformat()}
                    )
                else:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": (
                                f"Unknown action: {action}. Expected: subscribe, "
                                "unsubscribe, subscribeSafely, subscribeReadOnly, "
                                "refresh, set, stop, ping"
                            ),
                            "timestamp": datetime.now().isoformat(),
                        }
                    )

        except WebSocketDisconnect:
            logger.info("device_websocket_disconnect", client_id=client_id)
        except Exception as e:
            logger.error("device_websocket_error", client_id=client_id, error=str(e))
        finally:
            await self.disconnect(client_id)

    async def _handle_subscribe(self, client_id: str, websocket: WebSocket, data: dict):
        device_name = data.get("device")
        if not device_name:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "device field required",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            return

        if await self.subscribe_device(client_id, device_name):
            await websocket.send_json(
                {
                    "type": "subscribed",
                    "device": device_name,
                    "message": f"Subscribed to device {device_name}",
                    "timestamp": datetime.now().isoformat(),
                }
            )
        else:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": f"Device '{device_name}' not found in configuration service",
                    "device": device_name,
                    "timestamp": datetime.now().isoformat(),
                }
            )

    async def _handle_unsubscribe(self, client_id: str, websocket: WebSocket, data: dict):
        device_name = data.get("device")
        if not device_name:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "device field required",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            return

        await self.unsubscribe_device(client_id, device_name)
        await websocket.send_json(
            {
                "type": "unsubscribed",
                "device": device_name,
                "message": f"Unsubscribed from {device_name}",
                "timestamp": datetime.now().isoformat(),
            }
        )

    async def _handle_subscribe_safely(self, client_id: str, websocket: WebSocket, data: dict):
        device_name = data.get("device")
        if not device_name:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "device field required",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            return

        if await self.subscribe_device(client_id, device_name, require_connection=True):
            await websocket.send_json(
                {
                    "type": "subscribed",
                    "device": device_name,
                    "connected": True,
                    "timestamp": datetime.now().isoformat(),
                }
            )
        else:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": f"Device {device_name} not connected or not found",
                    "device": device_name,
                    "connected": False,
                    "timestamp": datetime.now().isoformat(),
                }
            )

    async def _handle_subscribe_read_only(self, client_id: str, websocket: WebSocket, data: dict):
        device_name = data.get("device")
        if not device_name:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "device field required",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            return

        if await self.subscribe_device(client_id, device_name):
            await websocket.send_json(
                {
                    "type": "subscribed",
                    "device": device_name,
                    "read_only": True,
                    "timestamp": datetime.now().isoformat(),
                }
            )
        else:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": f"Device {device_name} not found",
                    "device": device_name,
                    "timestamp": datetime.now().isoformat(),
                }
            )

    async def _handle_refresh(self, client_id: str, websocket: WebSocket, data: dict):
        device_name = data.get("device")

        async with self._lock:
            if device_name:
                devices = (
                    [device_name]
                    if device_name in self._device_subscriptions.get(client_id, set())
                    else []
                )
            else:
                devices = list(self._device_subscriptions.get(client_id, set()))

        for dev in devices:
            await self._send_current_values(client_id, dev)

        await websocket.send_json(
            {
                "type": "refreshed",
                "devices": devices,
                "timestamp": datetime.now().isoformat(),
            }
        )

    async def _handle_set(self, client_id: str, websocket: WebSocket, data: dict):
        """Set device component via DeviceControl (inherits coordination check)."""
        device_name = data.get("device")
        value = data.get("value")
        component = data.get("component")
        timeout = data.get("timeout")
        use_put = bool(data.get("use_put", False))

        if not device_name or value is None:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "device and value fields required",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            return

        async with self._lock:
            if device_name not in self._device_subscriptions.get(client_id, set()):
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": (
                            f"Device {device_name} not subscribed. Subscribe before setting."
                        ),
                        "device": device_name,
                        "timestamp": datetime.now().isoformat(),
                    }
                )
                return

        try:
            device_path = f"{device_name}.{component}" if component else device_name
            method = "put" if use_put else "set"
            result = await self.device_controller.access_nested_device(
                device_path=device_path,
                method=method,
                value=value,
                timeout=timeout,
            )
            await websocket.send_json(
                {
                    "type": "set_complete",
                    "device": device_name,
                    "component": component,
                    "value": value,
                    "success": True,
                    "result": result,
                    "use_put": use_put,
                    "timestamp": datetime.now().isoformat(),
                }
            )
        except DeviceLockedError as e:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": str(e),
                    "device": device_name,
                    "locked": True,
                    "timestamp": datetime.now().isoformat(),
                }
            )
        except Exception as e:
            logger.error("device_set_error", device=device_name, value=value, error=str(e))
            await websocket.send_json(
                {
                    "type": "error",
                    "message": str(e),
                    "device": device_name,
                    "timestamp": datetime.now().isoformat(),
                }
            )

    async def _handle_stop(self, client_id: str, websocket: WebSocket, data: dict):
        """Stop a device via DeviceControl (inherits coordination check)."""
        device_name = data.get("device")

        if not device_name:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "device field required for stop",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            return

        async with self._lock:
            if device_name not in self._device_subscriptions.get(client_id, set()):
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": (
                            f"Device {device_name} not subscribed. Subscribe before stopping."
                        ),
                        "device": device_name,
                        "timestamp": datetime.now().isoformat(),
                    }
                )
                return

        try:
            response = await self.device_controller.execute_device_method(
                DeviceCommandRequest(
                    device_name=device_name, method="stop", args=[], kwargs={}
                )
            )
            await websocket.send_json(
                {
                    "type": "stop_complete",
                    "device": device_name,
                    "success": response.success,
                    "message": response.message or "Device stopped",
                    "timestamp": datetime.now().isoformat(),
                }
            )
        except DeviceLockedError as e:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": str(e),
                    "device": device_name,
                    "locked": True,
                    "timestamp": datetime.now().isoformat(),
                }
            )
        except Exception as e:
            logger.error("device_stop_error", device=device_name, error=str(e))
            await websocket.send_json(
                {
                    "type": "error",
                    "message": str(e),
                    "device": device_name,
                    "timestamp": datetime.now().isoformat(),
                }
            )

    def get_stats(self) -> dict:
        return {
            "active_connections": len(self._connections),
            "subscribed_devices": len(self._device_clients),
            "total_device_pvs": sum(len(pvs) for pvs in self._device_pvs.values()),
        }
