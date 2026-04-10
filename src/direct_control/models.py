"""
Pydantic models for Direct Device Control Service.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional, Dict, List
from pydantic import BaseModel, Field, ConfigDict


class DeviceLockStatus(str, Enum):
    """Status of a device lock."""
    AVAILABLE = "available"
    LOCKED = "locked"
    UNKNOWN = "unknown"


class CommandMode(str, Enum):
    """Command execution mode for PV writes."""
    PUT_COMPLETION = "put-completion"  # High fidelity: wait for confirmation
    FIRE_AND_FORGET = "fire-and-forget"  # Low fidelity: issue write, don't wait


class PVSetRequest(BaseModel):
    """
    Request to set a PV value (Low Fidelity Channel).

    Two modes available:
    - wait=True (put-completion): Waits for write confirmation, returns result via F-012
    - wait=False (fire-and-forget): Issues write immediately, ideal for motor jogging
      where client monitors PV readback updates instead of waiting for completion
    """
    model_config = ConfigDict(extra="forbid")

    pv_name: str = Field(..., description="EPICS PV name")
    value: Any = Field(..., description="Value to set")
    wait: bool = Field(
        False,  # Default to fire-and-forget for low-fidelity channel
        description="Wait for put completion. False=fire-and-forget (monitor PV updates), True=wait for confirmation"
    )
    timeout: Optional[float] = Field(None, description="Timeout in seconds (only used when wait=True)", ge=0.0)


class PVSetResponse(BaseModel):
    """
    Response from PV set operation (Low Fidelity Channel).

    The `mode` field indicates which channel was used:
    - "put-completion": Write confirmed, `success` reflects actual result
    - "fire-and-forget": Write issued, `success` indicates write was sent (not confirmed)
    """
    model_config = ConfigDict(extra="forbid")

    pv_name: str = Field(..., description="EPICS PV name")
    success: bool = Field(..., description="Whether set operation succeeded (or was issued for fire-and-forget)")
    value_set: Any = Field(..., description="Value that was set")
    timestamp: datetime = Field(..., description="Timestamp of operation")
    coordination_checked: bool = Field(..., description="Whether coordination was checked")
    mode: CommandMode = Field(..., description="Execution mode: put-completion or fire-and-forget")
    message: Optional[str] = Field(None, description="Status message or error")


class DeviceCommandRequest(BaseModel):
    """
    Request to execute a device method (High Fidelity Channel).

    This is the high-fidelity channel that always returns a result.

    Use when:
    - Confirmation of operation completion is required
    - Invoking Ophyd device methods (set, move, trigger, etc.)

    The `use_put` option (as-ophyd-api compatible):
    - use_put=False (default): Uses ophyd's set() method which returns a Status
      object and waits for the operation to complete (e.g., motor done moving)
    - use_put=True: Uses ophyd's put() method which writes the value and returns
      immediately without waiting for completion. Faster for rapid adjustments.
    """
    model_config = ConfigDict(extra="forbid")

    device_name: str = Field(..., description="Ophyd device name")
    method: str = Field(..., description="Method to execute (set, read, trigger, etc.)")
    args: List[Any] = Field(default_factory=list, description="Positional arguments")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="Keyword arguments")
    timeout: Optional[float] = Field(None, description="Timeout in seconds", ge=0.0)
    use_put: bool = Field(
        False,
        description="Use put() instead of set(). put() returns immediately without "
                    "waiting for completion (e.g., motor done moving). Useful for "
                    "rapid jogging where you don't need confirmation."
    )


class DeviceCommandResponse(BaseModel):
    """
    Response from device command execution (High Fidelity Channel).

    This response indicates the result of the operation. When use_put=True,
    the response returns immediately after issuing the command. When
    use_put=False (default), the response waits for operation completion.
    """
    model_config = ConfigDict(extra="forbid")

    device_name: str = Field(..., description="Ophyd device name")
    method: str = Field(..., description="Method executed")
    success: bool = Field(..., description="Whether command succeeded")
    result: Any = Field(None, description="Command result")
    timestamp: datetime = Field(..., description="Timestamp of operation")
    coordination_checked: bool = Field(..., description="Whether coordination was checked")
    message: Optional[str] = Field(None, description="Status message or error")
    use_put: bool = Field(
        False,
        description="Whether put() was used instead of set(). "
                    "True means command returned without waiting for completion."
    )


class CoordinationStatus(BaseModel):
    """Coordination status from Experiment Execution Service."""
    model_config = ConfigDict(extra="forbid")
    
    device_available: bool = Field(..., description="Whether device is available")
    locked_by: Optional[str] = Field(None, description="Plan ID holding the lock")
    status: DeviceLockStatus = Field(..., description="Lock status")
    timestamp: datetime = Field(..., description="Status timestamp")


class ControlError(Exception):
    """Base exception for control errors."""
    pass


class DeviceLockedError(ControlError):
    """Raised when device is locked by active plan."""
    pass


class CoordinationCheckError(ControlError):
    """Raised when coordination check fails."""
    pass


class AuthorizationError(ControlError):
    """Raised when user is not authorized to command device."""
    pass


class HealthResponse(BaseModel):
    """Health check response."""
    model_config = ConfigDict(extra="forbid")

    status: str = Field("healthy", description="Service health status")
    timestamp: datetime = Field(..., description="Health check timestamp")
    coordination_service_available: bool = Field(..., description="Coordination service reachable")
    auth_service_available: bool = Field(..., description="Auth service reachable")


# ===== PV Metadata Models (as-ophyd-api / ophyd-websocket compatible) =====

class AlarmSeverity(str, Enum):
    """EPICS alarm severity levels."""
    NO_ALARM = "NO_ALARM"
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    INVALID = "INVALID"


class PVInfo(BaseModel):
    """
    Detailed PV information including metadata (as-ophyd-api compatible).

    Equivalent to as-ophyd-api's describe endpoint and ophyd-websocket's
    meta subscription event.
    """
    model_config = ConfigDict(extra="forbid")

    pv_name: str = Field(..., description="EPICS PV name")
    value: Any = Field(None, description="Current value")
    connected: bool = Field(..., description="Whether PV is connected")
    read_access: bool = Field(True, description="Whether read access is available")
    write_access: bool = Field(True, description="Whether write access is available")
    timestamp: datetime = Field(..., description="Timestamp of last update")

    # Limits (from as-ophyd-api)
    lower_ctrl_limit: Optional[float] = Field(None, description="Lower control limit")
    upper_ctrl_limit: Optional[float] = Field(None, description="Upper control limit")
    lower_disp_limit: Optional[float] = Field(None, description="Lower display limit")
    upper_disp_limit: Optional[float] = Field(None, description="Upper display limit")

    # Metadata (from ophyd-websocket)
    units: Optional[str] = Field(None, description="Engineering units")
    precision: Optional[int] = Field(None, description="Display precision")
    enum_strs: Optional[List[str]] = Field(None, description="Enum string values")

    # Alarm status (from as-ophyd-api)
    alarm_status: Optional[str] = Field(None, description="Alarm status")
    alarm_severity: Optional[AlarmSeverity] = Field(None, description="Alarm severity")


class PVValueResponse(BaseModel):
    """
    PV value response with connection and access info (ophyd-websocket compatible).

    This extended response includes connection status and access permissions,
    matching the ophyd-websocket value update format.
    """
    model_config = ConfigDict(extra="forbid")

    pv_name: str = Field(..., description="EPICS PV name")
    value: Any = Field(..., description="Current value")
    timestamp: datetime = Field(..., description="Timestamp")
    connected: bool = Field(True, description="Whether PV is connected")
    read_access: bool = Field(True, description="Whether read access is available")
    write_access: bool = Field(True, description="Whether write access is available")


class StopRequest(BaseModel):
    """Request to stop a device/motor (as-ophyd-api compatible)."""
    model_config = ConfigDict(extra="forbid")

    timeout: Optional[float] = Field(None, description="Timeout in seconds")


class StopResponse(BaseModel):
    """Response from stop operation."""
    model_config = ConfigDict(extra="forbid")

    pv_name: str = Field(..., description="PV name that was stopped")
    success: bool = Field(..., description="Whether stop succeeded")
    timestamp: datetime = Field(..., description="Timestamp")
    message: Optional[str] = Field(None, description="Status message")


# ===== WebSocket Models (ophyd-websocket compatible) =====

class WebSocketAction(str, Enum):
    """WebSocket control actions (ophyd-websocket compatible)."""
    SET = "set"  # Set PV or device value
    GET = "get"  # Get current value
    PING = "ping"  # Keepalive ping
    SUBSCRIBE = "subscribe"  # Subscribe to value updates
    UNSUBSCRIBE = "unsubscribe"  # Unsubscribe from updates
    SUBSCRIBE_SAFELY = "subscribeSafely"  # Subscribe, fail if not connected
    SUBSCRIBE_READ_ONLY = "subscribeReadOnly"  # Subscribe with read-only access
    REFRESH = "refresh"  # Refresh all subscriptions
    STOP = "stop"  # Stop device movement


class WebSocketSetRequest(BaseModel):
    """WebSocket set request (ophyd-websocket compatible)."""
    model_config = ConfigDict(extra="forbid")

    action: WebSocketAction = Field(..., description="Action to perform")
    pv: Optional[str] = Field(None, description="PV name (for PV operations)")
    device: Optional[str] = Field(None, description="Device name (for device operations)")
    component: Optional[str] = Field(None, description="Nested component path (e.g., 'user_readback')")
    value: Optional[Any] = Field(None, description="Value to set (for set action)")
    timeout: Optional[float] = Field(None, description="Timeout in seconds")


class WebSocketSetResponse(BaseModel):
    """WebSocket set response (ophyd-websocket compatible)."""
    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., description="Response type")
    pv: Optional[str] = Field(None, description="PV name")
    device: Optional[str] = Field(None, description="Device name")
    component: Optional[str] = Field(None, description="Nested component path")
    value: Optional[Any] = Field(None, description="Value (current or set)")
    success: bool = Field(..., description="Whether operation succeeded")
    message: Optional[str] = Field(None, description="Status message or error")
    timestamp: str = Field(..., description="ISO timestamp")


# ===== Nested Component Models =====

class NestedDeviceRequest(BaseModel):
    """Request to access nested device component (device_path comes from URL)."""
    model_config = ConfigDict(extra="forbid")

    method: str = Field("read", description="Method to execute (read, set, trigger, etc.)")
    value: Optional[Any] = Field(None, description="Value to set (for set method)")
    timeout: Optional[float] = Field(None, description="Timeout in seconds")


class NestedDeviceResponse(BaseModel):
    """Response from nested device access."""
    model_config = ConfigDict(extra="forbid")

    device_path: str = Field(..., description="Full device path")
    method: str = Field(..., description="Method executed")
    success: bool = Field(..., description="Whether operation succeeded")
    result: Any = Field(None, description="Result value")
    timestamp: datetime = Field(..., description="Timestamp")
    message: Optional[str] = Field(None, description="Status message or error")


# ===== Value Limit Validation =====

class PVLimits(BaseModel):
    """PV value limits for validation."""
    model_config = ConfigDict(extra="forbid")

    pv_name: str = Field(..., description="PV name")
    lower_limit: Optional[float] = Field(None, description="Lower control limit")
    upper_limit: Optional[float] = Field(None, description="Upper control limit")
    has_limits: bool = Field(False, description="Whether limits are defined")


class ValueLimitError(ControlError):
    """Raised when value is outside PV limits."""
    pass
