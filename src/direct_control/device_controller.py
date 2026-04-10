"""
Device controller for executing EPICS commands and Ophyd device methods.

Implements the DeviceControl protocol for commanding devices with
coordination checks (A4 requirement).
"""

import asyncio
from typing import Any, Optional, Dict, TYPE_CHECKING
import structlog
from epics import caget, caput, PV
from datetime import datetime

from .models import (
    PVSetRequest,
    PVSetResponse,
    DeviceCommandRequest,
    DeviceCommandResponse,
    CommandMode,
    ControlError,
    DeviceLockedError,
)
from .config import Settings

if TYPE_CHECKING:
    from .protocols import CoordinationService


logger = structlog.get_logger(__name__)


class DeviceController:
    """
    Handles device commanding with coordination checks.

    Executes EPICS PV sets and Ophyd device methods, ensuring proper
    coordination with active plan execution (A4 requirement).

    Implements: DeviceControl protocol
    """

    def __init__(self, settings: Settings, coordination: "CoordinationService"):
        """
        Initialize device controller.

        Args:
            settings: Service configuration
            coordination: Coordination service client (implements CoordinationService protocol)
        """
        self.settings = settings
        self.coordination = coordination

        # Set EPICS environment if configured
        if settings.epics_ca_addr_list:
            import os
            os.environ['EPICS_CA_ADDR_LIST'] = settings.epics_ca_addr_list
            os.environ['EPICS_CA_AUTO_ADDR_LIST'] = (
                'YES' if settings.epics_ca_auto_addr_list else 'NO'
            )
    
    async def set_pv(self, request: PVSetRequest) -> PVSetResponse:
        """
        Set EPICS PV value with coordination check (Low Fidelity Channel).

        Two execution modes based on request.wait:
        - wait=True (put-completion): Waits for EPICS put-completion callback,
          returns confirmed result. Use when confirmation is required.
        - wait=False (fire-and-forget): Issues write immediately without waiting.
          Ideal for motor jogging where user monitors PV readback updates.

        Args:
            request: PV set request

        Returns:
            PV set response with mode indication

        Raises:
            DeviceLockedError: If PV/device is locked by active plan
            ControlError: If set operation fails
        """
        pv_name = request.pv_name
        mode = CommandMode.PUT_COMPLETION if request.wait else CommandMode.FIRE_AND_FORGET

        # Perform coordination check
        # Note: For PV-level control, we use the PV name as device name
        # A more sophisticated implementation might map PVs to devices
        coord_status = await self.coordination.check_device_available(pv_name)

        if not coord_status.device_available:
            logger.warning(
                "device_locked",
                pv_name=pv_name,
                locked_by=coord_status.locked_by,
            )
            raise DeviceLockedError(
                f"PV {pv_name} is locked by plan {coord_status.locked_by}"
            )

        # Execute PV set operation
        try:
            logger.info(
                "setting_pv",
                pv_name=pv_name,
                value=request.value,
                mode=mode.value,
                wait=request.wait,
            )

            # Run caput in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            timeout = request.timeout or self.settings.command_timeout

            success = await loop.run_in_executor(
                None,
                lambda: caput(
                    pv_name,
                    request.value,
                    wait=request.wait,
                    timeout=timeout,
                )
            )

            if success:
                if mode == CommandMode.FIRE_AND_FORGET:
                    # Fire-and-forget: write issued, client should monitor PV updates
                    logger.info(
                        "pv_write_issued",
                        pv_name=pv_name,
                        value=request.value,
                        mode="fire-and-forget",
                    )
                    return PVSetResponse(
                        pv_name=pv_name,
                        success=True,
                        value_set=request.value,
                        timestamp=datetime.now(),
                        coordination_checked=True,
                        mode=mode,
                        message="Write issued (fire-and-forget). Monitor PV readback for confirmation.",
                    )
                else:
                    # Put-completion: write confirmed
                    logger.info(
                        "pv_set_confirmed",
                        pv_name=pv_name,
                        value=request.value,
                        mode="put-completion",
                    )
                    return PVSetResponse(
                        pv_name=pv_name,
                        success=True,
                        value_set=request.value,
                        timestamp=datetime.now(),
                        coordination_checked=True,
                        mode=mode,
                        message="PV set confirmed (put-completion)",
                    )
            else:
                logger.error("pv_set_failed", pv_name=pv_name, value=request.value, mode=mode.value)
                raise ControlError(f"Failed to set PV {pv_name}")

        except Exception as e:
            logger.error(
                "pv_set_error",
                pv_name=pv_name,
                error=str(e),
                mode=mode.value,
                exc_info=True,
            )
            return PVSetResponse(
                pv_name=pv_name,
                success=False,
                value_set=request.value,
                timestamp=datetime.now(),
                coordination_checked=True,
                mode=mode,
                message=f"Error: {str(e)}",
            )
    
    async def execute_device_method(
        self,
        request: DeviceCommandRequest
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
        device_name = request.device_name
        
        # Perform coordination check
        coord_status = await self.coordination.check_device_available(device_name)
        
        if not coord_status.device_available:
            logger.warning(
                "device_locked",
                device_name=device_name,
                locked_by=coord_status.locked_by,
            )
            raise DeviceLockedError(
                f"Device {device_name} is locked by plan {coord_status.locked_by}"
            )
        
        # Execute device method
        # Note: Full Ophyd device loading would require Configuration Service integration
        # This is a simplified implementation for the pip-installable pattern
        try:
            logger.info(
                "executing_device_method",
                device_name=device_name,
                method=request.method,
                args=request.args,
                kwargs=request.kwargs,
                use_put=request.use_put,
            )

            # In a full implementation, we would:
            # 1. Query Configuration Service for device definition
            # 2. Instantiate Ophyd device
            # 3. Execute method using put() or set() based on use_put flag:
            #    - use_put=True: device.put(value) - returns immediately
            #    - use_put=False: device.set(value) - waits for Status.done
            # For now, return a placeholder

            mode_desc = "put() (no wait)" if request.use_put else "set() (wait for completion)"
            logger.warning(
                "device_method_not_implemented",
                device_name=device_name,
                method=request.method,
                use_put=request.use_put,
                note=f"Full Ophyd device execution requires Configuration Service integration. Would use {mode_desc}",
            )

            return DeviceCommandResponse(
                device_name=device_name,
                method=request.method,
                success=False,
                result=None,
                timestamp=datetime.now(),
                coordination_checked=True,
                message=f"Device method execution requires Configuration Service integration. Mode: {mode_desc}",
                use_put=request.use_put,
            )

        except Exception as e:
            logger.error(
                "device_method_error",
                device_name=device_name,
                method=request.method,
                error=str(e),
                exc_info=True,
            )
            return DeviceCommandResponse(
                device_name=device_name,
                method=request.method,
                success=False,
                result=None,
                timestamp=datetime.now(),
                coordination_checked=True,
                message=f"Error: {str(e)}",
                use_put=request.use_put,
            )
    
    async def get_pv_value(self, pv_name: str) -> Optional[Any]:
        """
        Get current PV value (read-only, no coordination check needed).

        Args:
            pv_name: EPICS PV name

        Returns:
            Current PV value or None if not available
        """
        try:
            loop = asyncio.get_event_loop()
            value = await loop.run_in_executor(
                None,
                lambda: caget(pv_name, timeout=5.0)
            )
            return value
        except Exception as e:
            logger.error("get_pv_error", pv_name=pv_name, error=str(e))
            return None

    async def access_nested_device(
        self,
        device_path: str,
        method: str = "read",
        value: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Access nested device component (ophyd-websocket compatible).

        Supports dot-separated paths like:
        - motor1
        - motor1.user_readback
        - detector.image.array_size

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
        # Parse device path
        parts = device_path.split(".")
        device_name = parts[0]
        component_path = parts[1:] if len(parts) > 1 else []

        logger.info(
            "accessing_nested_device",
            device_path=device_path,
            device_name=device_name,
            component_path=component_path,
            method=method,
        )

        # For write operations, perform coordination check
        if method in ("set", "put", "write", "trigger", "stop"):
            coord_status = await self.coordination.check_device_available(device_name)

            if not coord_status.device_available:
                logger.warning(
                    "device_locked",
                    device_path=device_path,
                    locked_by=coord_status.locked_by,
                )
                raise DeviceLockedError(
                    f"Device {device_name} is locked by plan {coord_status.locked_by}"
                )

        # In a full implementation, we would:
        # 1. Query Configuration Service for device definition
        # 2. Instantiate Ophyd device
        # 3. Navigate to the nested component
        # 4. Execute method
        #
        # For now, we implement a simplified version that works with EPICS PVs
        # where the component path maps to PV suffixes
        #
        # Example: motor1.user_readback -> IOC:motor1.RBV or IOC:motor1:RBV

        # Placeholder implementation - returns mock data or attempts PV access
        try:
            if method in ("read", "get"):
                # Try to interpret as PV pattern
                # This is a simplified mapping - real implementation would query config service
                logger.warning(
                    "nested_device_read_placeholder",
                    device_path=device_path,
                    note="Full Ophyd device access requires Configuration Service integration",
                )
                return {
                    "device_path": device_path,
                    "method": method,
                    "status": "placeholder",
                    "note": "Full Ophyd device access requires Configuration Service integration",
                }

            elif method in ("set", "put", "write"):
                logger.warning(
                    "nested_device_set_placeholder",
                    device_path=device_path,
                    value=value,
                    note="Full Ophyd device access requires Configuration Service integration",
                )
                return {
                    "device_path": device_path,
                    "method": method,
                    "value": value,
                    "status": "placeholder",
                    "note": "Full Ophyd device access requires Configuration Service integration",
                }

            else:
                logger.warning(
                    "nested_device_method_placeholder",
                    device_path=device_path,
                    method=method,
                    note="Full Ophyd device access requires Configuration Service integration",
                )
                return {
                    "device_path": device_path,
                    "method": method,
                    "status": "placeholder",
                    "note": "Full Ophyd device access requires Configuration Service integration",
                }

        except Exception as e:
            logger.error(
                "nested_device_error",
                device_path=device_path,
                method=method,
                error=str(e),
                exc_info=True,
            )
            raise ControlError(f"Failed to access {device_path}: {str(e)}")
