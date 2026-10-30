"""Runtime configuration, read from environment / .env."""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Masumi -----------------------------------------------------------
    bypass_payment: bool = True
    masumi_network: str = "Preprod"
    masumi_payment_service_url: str = "http://localhost:3001"
    masumi_api_key: str = ""
    # The agent identifier you get after registering the agent (>= 57 chars).
    agent_identifier: str = ""
    price_tada: float = 10.0
    # Optional knobs for newer payment-service versions (leave empty to omit).
    masumi_payment_source_type: str = ""
    masumi_supported_payment_source_index: str = ""
    # If set, sent as RequestedFunds (lovelace). Empty = price taken from the registry entry.
    masumi_requested_funds_lovelace: str = ""

    # --- Storage ------------------------------------------------------------
    database_path: str = "data/jobs.sqlite3"

    # --- GIS ------------------------------------------------------------------
    http_timeout_s: float = 45.0
    http_retries: int = 2
    max_area_ha: float = 1000.0
    overpass_endpoints: list[str] = Field(
        default_factory=lambda: [
            "https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
        ]
    )
    user_agent: str = "land-audit-worker/0.1 (Masumi agent; contact via repository)"


@lru_cache
def get_settings() -> Settings:
    return Settings()
