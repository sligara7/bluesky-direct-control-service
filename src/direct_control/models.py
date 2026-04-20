"""
Pydantic models for Direct Device Control + Monitoring Service.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ===== Device Control Enums =====

class DeviceLockStatus(str, Enum):
    """Status of a device lock."""
    AVAILABLE = "available"
    LOCKED = "locked"
    UNKNOWN = "unknown"


class CommandMode(str, Enum):
    """Command execution mode for PV writes."""
    PUT_COMPLETION = "put-completion"
    FIRE_AND_FORGET = "fire-and-forget"


class SubscriptionStatus(str, Enum):
    """Status of a PV subscription."""
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class AlarmSeverity(str, Enum):
    """EPICS alarm severity levels."""
    NO_ALARM = "NO_ALARM"
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    INVALID = "INVALID"


ALARM_SEVERITY_NAMES = {
    0: "NO_ALARM",
    1: "MINOR",
    2: "MAJOR",
    3: "INVALID",
}


# ===== Device Control Request/Response =====

class PVSetRequest(BaseModel):
    """
    Request to set a PV value (Low Fidelity Channel).

    Two modes available:
    - wait=True (put-completion): Waits for write confirmation.
    - wait=False (fire-and-forget): Issues write immediately, client monitors
      PV readback for feedback.
    """
    model_config = ConfigDict(extra="forbid")

    pv_name: str = Field(..., description="EPICS PV name")
    value: Any = Field(..., description="Value to set")
    wait: bool = Field(False, description="Wait for put completion")
    timeout: Optional[float] = Field(None, description="Timeout (only used when wait=True)", ge=0.0)


class PVSetResponse(BaseModel):
    """Response from PV set operation."""
    model_config = ConfigDict(extra="forbid")

    pv_name: str
    success: bool
    value_set: Any
    timestamp: datetime
    coordination_checked: bool
    mode: CommandMode
    message: Optional[str] = None


class DeviceCommandRequest(BaseModel):
    """
    Request to execute a device method (High Fidelity Channel).

    use_put=False (default): ophyd set() waits for completion.
    use_put=True: ophyd put() returns immediately.
    """
    model_config = ConfigDict(extra="forbid")

    device_name: str
    method: str
    args: List[Any] = Field(default_factory=list)
    kwargs: Dict[str, Any] = Field(default_factory=dict)
    timeout: Optional[float] = Field(None, ge=0.0)
    use_put: bool = False


class DeviceCommandResponse(BaseModel):
    """Response from device command execution."""
    model_config = ConfigDict(extra="forbid")

    device_name: str
    method: str
    success: bool
    result: Any = None
    timestamp: datetime
    coordination_checked: bool
    message: Optional[str] = None
    use_put: bool = False


class CoordinationStatus(BaseModel):
    """Coordination status from Experiment Execution Service."""
    model_config = ConfigDict(extra="forbid")

    device_available: bool
    locked_by: Optional[str] = None
    status: DeviceLockStatus
    timestamp: datetime


# ===== PV Metadata / Value Models =====

class PVValue(BaseModel):
    """
    Current value of a PV (as-ophyd-api compatible).

    Returned by PVMonitor.get_value(). Includes EPICS metadata for richer
    client display (units, precision, limits, alarm status).
    """
    model_config = ConfigDict(extra="allow")

    pv_name: str
    value: Any
    timestamp: datetime
    status: int = 0
    severity: int = 0
    connected: bool = True

    units: Optional[str] = None
    precision: Optional[int] = None
    enum_strs: Optional[List[str]] = None

    lower_ctrl_limit: Optional[float] = None
    upper_ctrl_limit: Optional[float] = None
    lower_disp_limit: Optional[float] = None
    upper_disp_limit: Optional[float] = None

    read_access: bool = True
    write_access: bool = True


class PVUpdate(BaseModel):
    """PV update notification sent via WebSocket (ophyd-websocket compatible)."""
    model_config = ConfigDict(extra="forbid")

    event_type: str = "pv_update"
    pv_name: str
    value: Any
    timestamp: datetime
    status: int = 0
    severity: int = 0
    connected: bool = True
    read_access: bool = True
    write_access: bool = False
    alarm_status: Optional[str] = None
    alarm_severity: Optional[int] = None
    alarm_severity_name: Optional[str] = None
    lower_ctrl_limit: Optional[float] = None
    upper_ctrl_limit: Optional[float] = None
    lower_disp_limit: Optional[float] = None
    upper_disp_limit: Optional[float] = None
    units: Optional[str] = None
    precision: Optional[int] = None


class PVInfo(BaseModel):
    """Detailed PV information including metadata."""
    model_config = ConfigDict(extra="forbid")

    pv_name: str
    value: Any = None
    connected: bool
    read_access: bool = True
    write_access: bool = True
    timestamp: datetime

    lower_ctrl_limit: Optional[float] = None
    upper_ctrl_limit: Optional[float] = None
    lower_disp_limit: Optional[float] = None
    upper_disp_limit: Optional[float] = None

    units: Optional[str] = None
    precision: Optional[int] = None
    enum_strs: Optional[List[str]] = None

    alarm_status: Optional[str] = None
    alarm_severity: Optional[AlarmSeverity] = None


class PVValueResponse(BaseModel):
    """PV value response with connection and access info."""
    model_config = ConfigDict(extra="forbid")

    pv_name: str
    value: Any
    timestamp: datetime
    connected: bool = True
    read_access: bool = True
    write_access: bool = True


class PVLimits(BaseModel):
    """PV value limits for validation."""
    model_config = ConfigDict(extra="forbid")

    pv_name: str
    lower_limit: Optional[float] = None
    upper_limit: Optional[float] = None
    has_limits: bool = False


# ===== Monitoring Subscription Models =====

class PVMonitorRequest(BaseModel):
    """Request to monitor one or more PVs."""
    model_config = ConfigDict(extra="forbid")

    pv_names: List[str]
    update_rate: Optional[float] = Field(None, ge=0.0)
    buffer_size: Optional[int] = Field(None, ge=1, le=1000)


class PVSubscription(BaseModel):
    """Information about an active PV subscription."""
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    pv_names: List[str]
    status: SubscriptionStatus
    created_at: datetime
    last_update: Optional[datetime] = None
    update_count: int = 0
    client_id: Optional[str] = None


# ===== WebSocket Models (ophyd-websocket compatible) =====

class WebSocketAction(str, Enum):
    """WebSocket control actions (ophyd-websocket compatible)."""
    SET = "set"
    GET = "get"
    PING = "ping"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    SUBSCRIBE_SAFELY = "subscribeSafely"
    SUBSCRIBE_READ_ONLY = "subscribeReadOnly"
    REFRESH = "refresh"
    STOP = "stop"


class WebSocketMessage(BaseModel):
    """Incoming WebSocket message."""
    model_config = ConfigDict(extra="allow")

    action: WebSocketAction
    pv: Optional[str] = None
    pv_names: Optional[List[str]] = None
    device: Optional[str] = None
    component: Optional[str] = None
    value: Optional[Any] = None
    timeout: Optional[float] = None


class WebSocketSetRequest(BaseModel):
    """WebSocket set request."""
    model_config = ConfigDict(extra="forbid")

    action: WebSocketAction
    pv: Optional[str] = None
    device: Optional[str] = None
    component: Optional[str] = None
    value: Optional[Any] = None
    timeout: Optional[float] = None


class WebSocketSetResponse(BaseModel):
    """WebSocket set response."""
    model_config = ConfigDict(extra="forbid")

    type: str
    pv: Optional[str] = None
    device: Optional[str] = None
    component: Optional[str] = None
    value: Optional[Any] = None
    success: bool
    message: Optional[str] = None
    timestamp: str


# ===== Nested Component Models =====

class NestedDeviceRequest(BaseModel):
    """Request to access nested device component."""
    model_config = ConfigDict(extra="forbid")

    method: str = "read"
    value: Optional[Any] = None
    timeout: Optional[float] = None


class NestedDeviceResponse(BaseModel):
    """Response from nested device access."""
    model_config = ConfigDict(extra="forbid")

    device_path: str
    method: str
    success: bool
    result: Any = None
    timestamp: datetime
    message: Optional[str] = None


# ===== Device-Socket Models =====

class DeviceUpdate(BaseModel):
    """Device value update notification (ophyd-websocket compatible)."""
    model_config = ConfigDict(extra="forbid")

    event_type: str = "device_update"
    device: str
    signal: Optional[str] = None
    value: Any
    timestamp: datetime
    connected: bool = True
    read_access: Optional[bool] = True
    write_access: Optional[bool] = None


class DeviceInfo(BaseModel):
    """Device information from configuration service."""
    model_config = ConfigDict(extra="allow")

    name: str
    device_type: str
    ophyd_class: Optional[str] = None
    pvs: Dict[str, str] = Field(default_factory=dict)
    is_movable: bool = False
    is_readable: bool = True


# ===== Stop Operation Models =====

class StopRequest(BaseModel):
    """Request to stop a device/motor."""
    model_config = ConfigDict(extra="forbid")

    timeout: Optional[float] = None


class StopResponse(BaseModel):
    """Response from stop operation."""
    model_config = ConfigDict(extra="forbid")

    pv_name: str
    success: bool
    timestamp: datetime
    message: Optional[str] = None


# ===== Health Response =====

class HealthResponse(BaseModel):
    """Health check response for the merged service."""
    model_config = ConfigDict(extra="forbid")

    status: str = "healthy"
    timestamp: datetime
    coordination_service_available: bool
    active_subscriptions: int = 0
    connected_pvs: int = 0
    websocket_connections: int = 0


# ===== Exceptions =====

class ControlError(Exception):
    """Base exception for control errors."""


class DeviceLockedError(ControlError):
    """Raised when device is locked by active plan."""


class CoordinationCheckError(ControlError):
    """Raised when coordination check fails."""


class ValueLimitError(ControlError):
    """Raised when value is outside PV limits."""


class MonitoringError(Exception):
    """Base exception for monitoring errors."""


class PVNotFoundError(MonitoringError):
    """Raised when a requested PV cannot be found."""


class SubscriptionError(MonitoringError):
    """Raised when subscription management fails."""
