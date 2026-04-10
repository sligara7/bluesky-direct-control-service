"""
Configuration settings for Direct Device Control Service.
"""

from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Configuration settings for the Direct Device Control Service.
    
    Settings can be overridden via environment variables with the
    DIRECT_CONTROL_ prefix.
    """
    
    model_config = SettingsConfigDict(
        env_prefix="DIRECT_CONTROL_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )
    
    # Service configuration
    host: str = "0.0.0.0"
    port: int = 8003
    log_level: str = "info"
    
    # Service dependencies
    experiment_execution_url: str = "http://localhost:8001"
    configuration_service_url: str = "http://localhost:8004"
    auth_service_url: str = "http://localhost:8010"
    
    # EPICS configuration
    epics_ca_addr_list: Optional[str] = None
    epics_ca_auto_addr_list: bool = True
    epics_ca_max_array_bytes: int = 1000000
    
    # Coordination settings
    coordination_check_enabled: bool = True  # Can disable for testing
    coordination_timeout: float = 5.0  # Seconds to wait for coordination check
    
    # Authorization settings
    require_auth: bool = True
    allowed_roles: list[str] = ["staff", "scientist"]  # Roles that can command devices
    
    # Command timeout
    command_timeout: float = 30.0  # Seconds to wait for command completion
    
    # Observability
    enable_metrics: bool = True
    metrics_port: int = 9003
    enable_tracing: bool = False
