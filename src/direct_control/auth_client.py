"""
Auth client for validating tokens and checking permissions via Auth Service.

Provides both REST (require_permission) and WebSocket (require_permission_ws)
auth helpers. When require_auth=false, returns anonymous user without calling
Auth Service.
"""

from typing import Dict, Any, Optional

import httpx
import structlog

from .config import Settings


logger = structlog.get_logger(__name__)


class AuthClient:
    """
    HTTP client for token validation and permission checking via Auth Service.

    When require_auth=false in settings, all checks return an anonymous user
    without making any HTTP calls.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.auth_service_url = settings.auth_service_url
        self.require_auth = settings.require_auth
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.auth_service_url,
                timeout=5.0,
            )
        return self._client

    async def require_permission(
        self,
        authorization: Optional[str],
        permission: str,
        resource: str = "",
    ) -> Dict[str, Any]:
        """
        Validate token and check permission for REST endpoints.

        Args:
            authorization: Authorization header value (Bearer token)
            permission: Permission to check (e.g., COMMAND_DEVICE, MONITOR_DEVICES)
            resource: Optional resource identifier

        Returns:
            Dict with user_id and token

        Raises:
            AuthError: With appropriate status code (401, 403, 503)
        """
        if not self.require_auth:
            return {"user_id": "anonymous", "token": ""}

        if not authorization:
            raise AuthError(401, "Authorization header required")

        token = authorization[7:] if authorization.startswith("Bearer ") else authorization

        try:
            client = await self._get_client()

            # Validate token
            response = await client.post(
                "/api/v1/auth/validate",
                json={"token": token},
            )
            if response.status_code != 200:
                raise AuthError(401, "Invalid token")

            user_info = response.json()
            user_id = user_info.get("user_id")

            # Check permission
            perm_response = await client.post(
                "/api/v1/auth/check-permission",
                json={
                    "user_id": user_id,
                    "permission": permission,
                    "resource": resource,
                },
            )
            if perm_response.status_code != 200:
                raise AuthError(503, "Failed to check permission")

            if not perm_response.json().get("granted"):
                raise AuthError(403, f"Permission '{permission}' required")

            return {"user_id": user_id, "token": token}

        except AuthError:
            raise
        except httpx.RequestError as e:
            logger.error("auth_service_unavailable", error=str(e))
            raise AuthError(503, "Auth service unavailable") from e

    async def require_permission_ws(
        self,
        token: Optional[str],
        permission: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Validate token and check permission for WebSocket connections.

        Args:
            token: JWT token (from query param or first message)
            permission: Permission to check

        Returns:
            User dict with user_id, or None if auth fails
        """
        if not self.require_auth:
            return {"user_id": "anonymous"}

        if not token:
            return None

        try:
            client = await self._get_client()

            response = await client.post(
                "/api/v1/auth/validate",
                json={"token": token},
            )
            if response.status_code != 200:
                return None

            user_info = response.json()
            user_id = user_info.get("user_id")

            perm_response = await client.post(
                "/api/v1/auth/check-permission",
                json={
                    "user_id": user_id,
                    "permission": permission,
                    "resource": "",
                },
            )
            if perm_response.status_code != 200:
                return None

            if not perm_response.json().get("granted"):
                return None

            return {"user_id": user_id}

        except httpx.RequestError as e:
            logger.error("auth_service_unavailable_ws", error=str(e))
            return None

    async def cleanup(self):
        """Cleanup HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None


class AuthError(Exception):
    """Auth validation error with HTTP status code."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)
