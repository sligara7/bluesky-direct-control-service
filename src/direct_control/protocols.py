"""
Protocol interfaces for Direct Device Control Service (SVC-003).

Defines type-safe contracts for service components following design principles:
- Python typing protocols for interface contracts
- Dependency injection support
- Separation of concerns

These protocols enable:
- Multiple coordination client implementations (HTTP, mock)
- Multiple device controller implementations
- Testing with mock implementations
- Clear interface boundaries between components
"""

from datetime import datetime
from typing import Any, Optional, Protocol, runtime_checkable

from .models import (
    CoordinationStatus,
    PVSetRequest,
    PVSetResponse,
    DeviceCommandRequest,
    DeviceCommandResponse,
)


@runtime_checkable
class CoordinationService(Protocol):
    """
    Protocol for coordination service clients.

    Implements the A4 coordination requirement: check if a device is
    available for direct control (not locked by an active plan).

    Implementations:
    - CoordinationClient: HTTP client to Experiment Execution Service
    - MockCoordinationClient: Always returns available (for testing)
    """

    async def check_device_available(self, device_name: str) -> CoordinationStatus:
        """
        Check if device is available for direct control.

        This is the CRITICAL A4 coordination check. It queries SVC-001
        (Experiment Execution Service) to determine if the device is
        currently locked by an executing plan.

        Args:
            device_name: Name of the device to check

        Returns:
            CoordinationStatus with device availability

        Raises:
            CoordinationCheckError: If coordination check fails
        """
        ...

    async def is_service_available(self) -> bool:
        """
        Check if coordination service is reachable.

        Returns:
            True if service is available
        """
        ...

    async def cleanup(self) -> None:
        """Cleanup resources (HTTP client, etc.)."""
        ...


@runtime_checkable
class DeviceControl(Protocol):
    """
    Protocol for device control operations.

    Defines the interface for commanding EPICS PVs and Ophyd devices.

    Implementations:
    - DeviceController: Full implementation with EPICS/Ophyd
    - MockDeviceController: Returns mock responses (for testing)
    """

    async def set_pv(self, request: PVSetRequest) -> PVSetResponse:
        """
        Set EPICS PV value with coordination check.

        Two execution modes based on request.wait:
        - wait=True: Put-completion, waits for confirmation
        - wait=False: Fire-and-forget, returns immediately

        Args:
            request: PV set request

        Returns:
            PV set response with mode indication

        Raises:
            DeviceLockedError: If PV/device is locked by active plan
            ControlError: If set operation fails
        """
        ...

    async def execute_device_method(
        self, request: DeviceCommandRequest
    ) -> DeviceCommandResponse:
        """
        Execute Ophyd device method with coordination check.

        Args:
            request: Device command request

        Returns:
            Device command response

        Raises:
            DeviceLockedError: If device is locked by active plan
            ControlError: If command execution fails
        """
        ...

    async def get_pv_value(self, pv_name: str) -> Optional[Any]:
        """
        Get current PV value (read-only, no coordination check).

        Args:
            pv_name: EPICS PV name

        Returns:
            Current PV value or None if not available
        """
        ...

    async def access_nested_device(
        self,
        device_path: str,
        method: str = "read",
        value: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Access nested device component (ophyd-websocket compatible).

        Args:
            device_path: Dot-separated device component path
            method: Method to execute (read, set, trigger, etc.)
            value: Value to set (for set method)
            timeout: Timeout in seconds

        Returns:
            Result of the operation

        Raises:
            DeviceLockedError: If device is locked by active plan
            ControlError: If operation fails
        """
        ...


class MockCoordinationClient:
    """
    Mock coordination client for testing.

    Always returns devices as available (no coordination check).
    """

    def __init__(self, always_available: bool = True):
        """
        Initialize mock client.

        Args:
            always_available: If True, devices are always available.
                              If False, devices are always locked.
        """
        self.always_available = always_available
        self.check_count = 0

    async def check_device_available(self, device_name: str) -> CoordinationStatus:
        """Return mock coordination status."""
        from .models import DeviceLockStatus

        self.check_count += 1

        if self.always_available:
            return CoordinationStatus(
                device_available=True,
                locked_by=None,
                status=DeviceLockStatus.AVAILABLE,
                timestamp=datetime.now(),
            )
        else:
            return CoordinationStatus(
                device_available=False,
                locked_by="mock_plan",
                status=DeviceLockStatus.LOCKED,
                timestamp=datetime.now(),
            )

    async def is_service_available(self) -> bool:
        """Always available for testing."""
        return True

    async def cleanup(self) -> None:
        """No cleanup needed for mock."""
        pass
