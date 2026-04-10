"""
Direct Device Control Service (SVC-003).

Device commanding with coordination checks against active plan execution.

Note: To allow CLI to set environment variables before Settings() is created,
we use lazy imports. Modules are imported on first access, not at module load time.
"""

__version__ = "1.0.0"


def __getattr__(name):
    """Lazy import of module-level attributes."""
    # Settings
    if name == "Settings":
        from .config import Settings
        return Settings

    # Clients
    if name == "CoordinationClient":
        from .coordination_client import CoordinationClient
        return CoordinationClient

    if name == "DeviceController":
        from .device_controller import DeviceController
        return DeviceController

    # Models
    if name in (
        "PVSetRequest", "PVSetResponse", "DeviceCommandRequest",
        "DeviceCommandResponse", "CoordinationStatus", "DeviceLockStatus",
        "ControlError", "DeviceLockedError", "CoordinationCheckError",
        "AuthorizationError", "HealthResponse",
        # Phase 2: WebSocket and nested device models
        "WebSocketAction", "WebSocketSetRequest", "WebSocketSetResponse",
        "NestedDeviceRequest", "NestedDeviceResponse", "PVLimits", "ValueLimitError",
    ):
        from . import models
        return getattr(models, name)

    # App
    if name == "app":
        from .main import app
        return app

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "PVSetRequest",
    "PVSetResponse",
    "DeviceCommandRequest",
    "DeviceCommandResponse",
    "CoordinationStatus",
    "DeviceLockStatus",
    "ControlError",
    "DeviceLockedError",
    "CoordinationCheckError",
    "AuthorizationError",
    "HealthResponse",
    # Phase 2: WebSocket and nested device models
    "WebSocketAction",
    "WebSocketSetRequest",
    "WebSocketSetResponse",
    "NestedDeviceRequest",
    "NestedDeviceResponse",
    "PVLimits",
    "ValueLimitError",
    "Settings",
    "CoordinationClient",
    "DeviceController",
    "app",
]
