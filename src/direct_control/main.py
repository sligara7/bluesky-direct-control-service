"""
FastAPI application for Direct Device Control Service.

Note: Uses lifespan pattern to defer Settings() creation until after
CLI sets environment variables.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Header, Query
from fastapi.middleware.cors import CORSMiddleware
import structlog

from .config import Settings
from .models import (
    HealthResponse,
    PVSetRequest,
    PVSetResponse,
    DeviceCommandRequest,
    DeviceCommandResponse,
    DeviceLockedError,
    CoordinationCheckError,
    AuthorizationError,
    WebSocketAction,
    WebSocketSetRequest,
    WebSocketSetResponse,
    NestedDeviceRequest,
    NestedDeviceResponse,
    ValueLimitError,
)
from .protocols import CoordinationService, DeviceControl
from .coordination_client import CoordinationClient
from .device_controller import DeviceController
from .registry_client import RegistryClient, RegistryValidationError
from .auth_client import AuthClient, AuthError


logger = structlog.get_logger(__name__)


# ===== Application Lifecycle =====

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifecycle manager.

    Initialize settings and clients on startup, clean up on shutdown.
    """
    logger.info("Starting Direct Device Control Service")

    # Get settings (reads env vars set by CLI)
    settings = Settings()

    # Initialize clients
    coordination_client = CoordinationClient(settings)
    device_controller = DeviceController(settings, coordination_client)
    registry_client = RegistryClient(settings)
    auth_client = AuthClient(settings)

    # Store in app state
    app.state.settings = settings
    app.state.coordination_client = coordination_client
    app.state.device_controller = device_controller
    app.state.registry_client = registry_client
    app.state.auth_client = auth_client

    logger.info(
        "Service initialized successfully",
        port=settings.port,
        coordination_url=settings.experiment_execution_url,
        coordination_enabled=settings.coordination_check_enabled,
        require_auth=settings.require_auth,
    )

    try:
        yield
    finally:
        # Shutdown
        logger.info("Shutting down Direct Device Control Service")
        await coordination_client.cleanup()
        await registry_client.cleanup()
        await auth_client.cleanup()
        logger.info("Service shut down successfully")


# Create FastAPI app
app = FastAPI(
    title="Bluesky Direct Device Control Service",
    description="Device commanding with A4 coordination checks",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== Dependencies =====

def get_settings() -> Settings:
    """Dependency injection for settings."""
    return app.state.settings


def get_coordination_client() -> CoordinationService:
    """Dependency injection for coordination client (implements CoordinationService protocol)."""
    return app.state.coordination_client


def get_device_controller() -> DeviceControl:
    """Dependency injection for device controller (implements DeviceControl protocol)."""
    return app.state.device_controller


def get_registry_client() -> RegistryClient:
    """Dependency injection for registry client."""
    return app.state.registry_client


def get_auth_client() -> AuthClient:
    """Dependency injection for auth client."""
    return app.state.auth_client


async def require_command_device(
    authorization: Optional[str] = Header(None),
    auth_client: AuthClient = Depends(get_auth_client),
) -> dict:
    """Auth dependency: requires COMMAND_DEVICE permission for write operations."""
    try:
        return await auth_client.require_permission(authorization, "COMMAND_DEVICE")
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


async def require_stop_device(
    authorization: Optional[str] = Header(None),
    auth_client: AuthClient = Depends(get_auth_client),
) -> dict:
    """Auth dependency: requires STOP_DEVICE permission for stop operations."""
    try:
        return await auth_client.require_permission(authorization, "STOP_DEVICE")
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


async def require_monitor_devices(
    authorization: Optional[str] = Header(None),
    auth_client: AuthClient = Depends(get_auth_client),
) -> dict:
    """Auth dependency: requires MONITOR_DEVICES permission for read operations."""
    try:
        return await auth_client.require_permission(authorization, "MONITOR_DEVICES")
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# ===== Health & Readiness =====

@app.get("/health", response_model=HealthResponse)
async def health_check(
    coordination_client: CoordinationService = Depends(get_coordination_client),
):
    """
    Health check endpoint.

    Returns:
        Service health status
    """
    coord_available = await coordination_client.is_service_available()

    # In full implementation, also check auth service
    auth_available = True  # Placeholder

    return HealthResponse(
        status="healthy" if coord_available else "degraded",
        timestamp=datetime.now(),
        coordination_service_available=coord_available,
        auth_service_available=auth_available,
    )


# ===== PV Control Endpoints =====

@app.post("/api/v1/pv/set", response_model=PVSetResponse)
async def set_pv(
    request: PVSetRequest,
    auth: dict = Depends(require_command_device),
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
):
    """
    Set EPICS PV value with coordination check (Low Fidelity Channel).

    This is the "low fidelity" command channel with two modes:

    1. **Fire-and-forget (wait=false, default)**: Issues PV write and returns
       immediately. Client should monitor PV readback for feedback. Ideal for
       motor jogging and simple signal adjustments.

    2. **Put-completion (wait=true)**: Waits for EPICS put-completion callback
       and returns confirmed result. Use when confirmation is required.

    This endpoint implements the A4 coordination requirement by checking
    with SVC-001 (Experiment Execution Service) before allowing the command.

    Args:
        request: PV set request with optional wait parameter
        auth: Validated user info (requires COMMAND_DEVICE permission)

    Returns:
        PV set response with mode indication

    Raises:
        HTTPException 404: If PV not in registry
        HTTPException 423: If device is locked by active plan
        HTTPException 503: If coordination check fails
        HTTPException 500: If operation fails
    """
    try:
        await registry_client.validate_pv(request.pv_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        response = await device_controller.set_pv(request)
        return response

    except DeviceLockedError as e:
        logger.warning("pv_locked", pv_name=request.pv_name, error=str(e))
        raise HTTPException(status_code=423, detail=str(e))  # 423 Locked

    except CoordinationCheckError as e:
        logger.error("coordination_check_failed", pv_name=request.pv_name, error=str(e))
        raise HTTPException(status_code=503, detail=f"Coordination check failed: {e}")

    except Exception as e:
        logger.error("set_pv_error", pv_name=request.pv_name, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/pv/{pv_name}/value")
async def get_pv_value(
    pv_name: str,
    auth: dict = Depends(require_monitor_devices),
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
):
    """
    Get current PV value (read-only, no coordination check).

    Args:
        pv_name: EPICS PV name

    Returns:
        Current PV value
    """
    try:
        await registry_client.validate_pv(pv_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    value = await device_controller.get_pv_value(pv_name)

    if value is None:
        raise HTTPException(status_code=404, detail=f"PV {pv_name} not found or not available")

    return {
        "pv_name": pv_name,
        "value": value,
        "timestamp": datetime.now().isoformat(),
    }


# ===== Device Control Endpoints =====

@app.post("/api/v1/device/execute", response_model=DeviceCommandResponse)
async def execute_device_method(
    request: DeviceCommandRequest,
    auth: dict = Depends(require_command_device),
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
):
    """
    Execute Ophyd device method with coordination check (High Fidelity Channel).

    This is the "high fidelity" command channel - consistent single path that
    ALWAYS returns a confirmed result. Unlike the PV channel, there is no
    fire-and-forget mode.

    Use this channel when:
    - Confirmation of operation completion is required
    - Invoking Ophyd device methods (set, move, trigger, etc.)

    This endpoint implements the A4 coordination requirement by checking
    with SVC-001 before allowing device commands.

    Args:
        request: Device command request
        auth: Validated user info (requires COMMAND_DEVICE permission)

    Returns:
        Device command response (always confirmed)

    Raises:
        HTTPException 404: If device not in registry
        HTTPException 423: If device is locked by active plan
        HTTPException 503: If coordination check fails
        HTTPException 500: If operation fails
    """
    try:
        await registry_client.validate_device(request.device_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        response = await device_controller.execute_device_method(request)
        return response

    except DeviceLockedError as e:
        logger.warning("device_locked", device_name=request.device_name, error=str(e))
        raise HTTPException(status_code=423, detail=str(e))

    except CoordinationCheckError as e:
        logger.error(
            "coordination_check_failed",
            device_name=request.device_name,
            error=str(e)
        )
        raise HTTPException(status_code=503, detail=f"Coordination check failed: {e}")

    except Exception as e:
        logger.error(
            "device_command_error",
            device_name=request.device_name,
            error=str(e),
            exc_info=True
        )
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/device/{device_name}/stop", response_model=DeviceCommandResponse)
async def stop_device(
    device_name: str,
    auth: dict = Depends(require_stop_device),
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
):
    """
    Stop a device (as-ophyd-api compatible).

    Calls the stop() method on the device if available. This is typically
    used to abort a motor move or other ongoing operation.

    Args:
        device_name: Name of the device to stop

    Returns:
        Device command response with stop result

    Raises:
        HTTPException 404: If device not in registry
        HTTPException 423: If device is locked by active plan
        HTTPException 500: If stop operation fails
    """
    try:
        await registry_client.validate_device(device_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        # Create a command request for the stop method
        request = DeviceCommandRequest(
            device_name=device_name,
            method="stop",
            args=[],
            kwargs={},
        )
        response = await device_controller.execute_device_method(request)
        return response

    except DeviceLockedError as e:
        logger.warning("device_stop_locked", device_name=device_name, error=str(e))
        raise HTTPException(status_code=423, detail=str(e))

    except CoordinationCheckError as e:
        logger.error("coordination_check_failed", device_name=device_name, error=str(e))
        raise HTTPException(status_code=503, detail=f"Coordination check failed: {e}")

    except Exception as e:
        logger.error("device_stop_error", device_name=device_name, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ===== Nested Device Endpoints (ophyd-websocket compatible) =====

@app.post("/api/v1/device/{device_path:path}", response_model=NestedDeviceResponse)
async def access_nested_device(
    device_path: str,
    request: Optional[NestedDeviceRequest] = None,
    auth: dict = Depends(require_command_device),
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
):
    """
    Access nested device component (ophyd-websocket compatible).

    Supports paths like:
    - motor1
    - motor1.user_readback
    - detector.image.array_size

    Args:
        device_path: Dot-separated device component path
        request: Optional request body for set operations

    Returns:
        Nested device response with value or operation result
    """
    # Validate the top-level device name
    device_name = device_path.split(".")[0]
    try:
        await registry_client.validate_device(device_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    method = request.method if request else "read"
    value = request.value if request else None
    timeout = request.timeout if request else None

    try:
        # Parse device path and resolve component
        result = await device_controller.access_nested_device(
            device_path=device_path,
            method=method,
            value=value,
            timeout=timeout,
        )

        return NestedDeviceResponse(
            device_path=device_path,
            method=method,
            success=True,
            result=result,
            timestamp=datetime.now(),
            message=None,
        )

    except DeviceLockedError as e:
        logger.warning("nested_device_locked", device_path=device_path, error=str(e))
        raise HTTPException(status_code=423, detail=str(e))

    except CoordinationCheckError as e:
        logger.error("coordination_check_failed", device_path=device_path, error=str(e))
        raise HTTPException(status_code=503, detail=f"Coordination check failed: {e}")

    except Exception as e:
        logger.error("nested_device_error", device_path=device_path, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/device/{device_path:path}/value")
async def get_nested_device_value(
    device_path: str,
    auth: dict = Depends(require_monitor_devices),
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
):
    """
    Get nested device component value (read-only, no coordination check).

    Supports paths like motor1.user_readback.

    Args:
        device_path: Dot-separated device component path

    Returns:
        Current device component value
    """
    device_name = device_path.split(".")[0]
    try:
        await registry_client.validate_device(device_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        value = await device_controller.access_nested_device(
            device_path=device_path,
            method="read",
            value=None,
            timeout=None,
        )

        return {
            "device_path": device_path,
            "value": value,
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error("nested_device_read_error", device_path=device_path, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ===== WebSocket Control Endpoint (ophyd-websocket compatible) =====

@app.websocket("/api/v1/control-socket")
async def websocket_control(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
    device_controller: DeviceControl = Depends(get_device_controller),
):
    """
    WebSocket endpoint for device control (ophyd-websocket compatible).

    Authentication: Pass token via query param ?token=<jwt> or send
    {"action": "auth", "token": "..."} as first message within 5 seconds.

    Protocol:
        Client -> Server:
            {"action": "auth", "token": "..."}  (if no query param token)
            {"action": "set", "pv": "IOC:m1", "value": 10}
            {"action": "set", "device": "motor1", "component": "user_readback", "value": 10}
            {"action": "get", "pv": "IOC:m1"}
            {"action": "ping"}

        Server -> Client:
            {"type": "set_complete", "pv": "...", "success": true, ...}
            {"type": "value", "pv": "...", "value": ..., ...}
            {"type": "pong", "timestamp": "..."}
            {"type": "error", "message": "...", ...}
    """
    auth_client: AuthClient = app.state.auth_client
    registry_client: RegistryClient = app.state.registry_client

    await websocket.accept()

    # Authenticate the WebSocket connection
    ws_user = None
    if token:
        ws_user = await auth_client.require_permission_ws(token, "MONITOR_DEVICES")
    elif auth_client.require_auth:
        # Wait for auth message within 5 seconds
        try:
            data = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
            if data.get("action") == "auth" and data.get("token"):
                ws_user = await auth_client.require_permission_ws(
                    data["token"], "MONITOR_DEVICES"
                )
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": "First message must be auth with token",
                    "timestamp": datetime.now().isoformat(),
                })
                await websocket.close(code=4001, reason="Authentication required")
                return
        except asyncio.TimeoutError:
            await websocket.close(code=4001, reason="Authentication timeout")
            return
    else:
        ws_user = {"user_id": "anonymous"}

    if ws_user is None:
        await websocket.close(code=4001, reason="Invalid token")
        return

    logger.info("control_websocket_connected", user=ws_user.get("user_id"))

    # Store the token for per-action permission escalation checks
    ws_token = token

    try:
        while True:
            data = await websocket.receive_json()

            action = data.get("action", "").lower()

            if action == "auth":
                # Allow re-auth / token upgrade mid-session
                new_token = data.get("token")
                if new_token:
                    ws_token = new_token
                    ws_user = await auth_client.require_permission_ws(new_token, "MONITOR_DEVICES")
                    if ws_user:
                        await websocket.send_json({
                            "type": "auth_ok",
                            "user_id": ws_user.get("user_id"),
                            "timestamp": datetime.now().isoformat(),
                        })
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "message": "Invalid token",
                            "timestamp": datetime.now().isoformat(),
                        })

            elif action == "ping":
                await websocket.send_json({
                    "type": "pong",
                    "timestamp": datetime.now().isoformat(),
                })

            elif action == "set":
                # Requires COMMAND_DEVICE permission
                if auth_client.require_auth:
                    user = await auth_client.require_permission_ws(ws_token, "COMMAND_DEVICE")
                    if not user:
                        await websocket.send_json({
                            "type": "error",
                            "message": "Permission 'COMMAND_DEVICE' required",
                            "timestamp": datetime.now().isoformat(),
                        })
                        continue

                await _handle_websocket_set(websocket, data, device_controller, registry_client)

            elif action == "get":
                # MONITOR_DEVICES already checked at connection level
                await _handle_websocket_get(websocket, data, device_controller, registry_client)

            elif action == "stop":
                # Requires STOP_DEVICE permission
                if auth_client.require_auth:
                    user = await auth_client.require_permission_ws(ws_token, "STOP_DEVICE")
                    if not user:
                        await websocket.send_json({
                            "type": "error",
                            "message": "Permission 'STOP_DEVICE' required",
                            "timestamp": datetime.now().isoformat(),
                        })
                        continue

                await _handle_websocket_stop(websocket, data, device_controller, registry_client)

            else:
                await websocket.send_json({
                    "type": "error",
                    "message": f"Unknown action: {action}",
                    "timestamp": datetime.now().isoformat(),
                })

    except WebSocketDisconnect:
        logger.info("control_websocket_disconnected")

    except Exception as e:
        logger.error("control_websocket_error", error=str(e), exc_info=True)
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass


async def _handle_websocket_set(
    websocket: WebSocket,
    data: dict,
    device_controller: DeviceControl,
    registry_client: RegistryClient,
):
    """Handle WebSocket set action (as-ophyd-api compatible with use_put option)."""
    pv = data.get("pv")
    device = data.get("device")
    component = data.get("component")
    value = data.get("value")
    timeout = data.get("timeout")
    use_put = data.get("use_put", False)  # as-ophyd-api compatible

    if value is None:
        await websocket.send_json({
            "type": "error",
            "message": "value field required for set action",
            "timestamp": datetime.now().isoformat(),
        })
        return

    # Registry validation
    try:
        if pv:
            await registry_client.validate_pv(pv)
        elif device:
            await registry_client.validate_device(device)
        else:
            await websocket.send_json({
                "type": "error",
                "message": "pv or device field required for set action",
                "timestamp": datetime.now().isoformat(),
            })
            return
    except RegistryValidationError as e:
        await websocket.send_json({
            "type": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat(),
        })
        return
    except RuntimeError as e:
        await websocket.send_json({
            "type": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat(),
        })
        return

    try:
        if pv:
            # PV set - use_put controls wait behavior
            # use_put=True -> wait=False (fire-and-forget)
            # use_put=False -> wait=True (wait for completion)
            request = PVSetRequest(pv_name=pv, value=value, wait=not use_put, timeout=timeout)
            response = await device_controller.set_pv(request)

            await websocket.send_json({
                "type": "set_complete",
                "pv": pv,
                "value": value,
                "success": response.success,
                "message": response.message,
                "use_put": use_put,
                "timestamp": datetime.now().isoformat(),
            })

        elif device:
            # Device set (with optional nested component)
            # For device commands, use_put would affect ophyd set() vs put()
            device_path = f"{device}.{component}" if component else device
            method = "put" if use_put else "set"
            result = await device_controller.access_nested_device(
                device_path=device_path,
                method=method,
                value=value,
                timeout=timeout,
            )

            await websocket.send_json({
                "type": "set_complete",
                "device": device,
                "component": component,
                "value": value,
                "success": True,
                "result": result,
                "use_put": use_put,
                "timestamp": datetime.now().isoformat(),
            })

    except DeviceLockedError as e:
        await websocket.send_json({
            "type": "error",
            "message": str(e),
            "pv": pv,
            "device": device,
            "locked": True,
            "timestamp": datetime.now().isoformat(),
        })

    except Exception as e:
        await websocket.send_json({
            "type": "error",
            "message": str(e),
            "pv": pv,
            "device": device,
            "timestamp": datetime.now().isoformat(),
        })


async def _handle_websocket_get(
    websocket: WebSocket,
    data: dict,
    device_controller: DeviceControl,
    registry_client: RegistryClient,
):
    """Handle WebSocket get action."""
    pv = data.get("pv")
    device = data.get("device")
    component = data.get("component")

    # Registry validation
    try:
        if pv:
            await registry_client.validate_pv(pv)
        elif device:
            await registry_client.validate_device(device)
        else:
            await websocket.send_json({
                "type": "error",
                "message": "pv or device field required for get action",
                "timestamp": datetime.now().isoformat(),
            })
            return
    except RegistryValidationError as e:
        await websocket.send_json({
            "type": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat(),
        })
        return
    except RuntimeError as e:
        await websocket.send_json({
            "type": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat(),
        })
        return

    try:
        if pv:
            # PV get
            value = await device_controller.get_pv_value(pv)

            await websocket.send_json({
                "type": "value",
                "pv": pv,
                "value": value,
                "timestamp": datetime.now().isoformat(),
            })

        elif device:
            # Device get (with optional nested component)
            device_path = f"{device}.{component}" if component else device
            value = await device_controller.access_nested_device(
                device_path=device_path,
                method="read",
                value=None,
                timeout=None,
            )

            await websocket.send_json({
                "type": "value",
                "device": device,
                "component": component,
                "value": value,
                "timestamp": datetime.now().isoformat(),
            })

    except Exception as e:
        await websocket.send_json({
            "type": "error",
            "message": str(e),
            "pv": pv,
            "device": device,
            "timestamp": datetime.now().isoformat(),
        })


async def _handle_websocket_stop(
    websocket: WebSocket,
    data: dict,
    device_controller: DeviceControl,
    registry_client: RegistryClient,
):
    """Handle WebSocket stop action (as-ophyd-api compatible)."""
    device = data.get("device")

    if not device:
        await websocket.send_json({
            "type": "error",
            "message": "device field required for stop action",
            "timestamp": datetime.now().isoformat(),
        })
        return

    # Registry validation
    try:
        await registry_client.validate_device(device)
    except RegistryValidationError as e:
        await websocket.send_json({
            "type": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat(),
        })
        return
    except RuntimeError as e:
        await websocket.send_json({
            "type": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat(),
        })
        return

    try:
        # Create a command request for the stop method
        request = DeviceCommandRequest(
            device_name=device,
            method="stop",
            args=[],
            kwargs={},
        )
        response = await device_controller.execute_device_method(request)

        await websocket.send_json({
            "type": "stop_complete",
            "device": device,
            "success": response.success,
            "message": response.message or "Device stopped",
            "timestamp": datetime.now().isoformat(),
        })

    except DeviceLockedError as e:
        await websocket.send_json({
            "type": "error",
            "message": str(e),
            "device": device,
            "locked": True,
            "timestamp": datetime.now().isoformat(),
        })

    except Exception as e:
        await websocket.send_json({
            "type": "error",
            "message": str(e),
            "device": device,
            "timestamp": datetime.now().isoformat(),
        })


# ===== Statistics Endpoint =====

@app.get("/api/v1/stats")
async def get_stats(
    settings: Settings = Depends(get_settings),
    coordination_client: CoordinationService = Depends(get_coordination_client),
):
    """
    Get service statistics.

    Returns:
        Service statistics
    """
    coord_available = await coordination_client.is_service_available()

    return {
        "service": "direct_control",
        "timestamp": datetime.now().isoformat(),
        "coordination_enabled": settings.coordination_check_enabled,
        "coordination_service_available": coord_available,
        "command_timeout": settings.command_timeout,
    }


# ===== Factory Function =====

def create_app() -> FastAPI:
    """
    Factory function for creating the FastAPI app.

    Used by CLI with uvicorn's factory=True parameter to ensure
    environment variables are set before Settings() is created.
    """
    return app
