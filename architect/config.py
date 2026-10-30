"""Agent 4 (Pool Architect) settings, read from environment / .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class ArchitectSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Agent 3 — source of the revealed parcels/companies (needs ESCROW_API_KEY on both sides).
    escrow_url: str = "http://localhost:8002"
    escrow_api_key: str = ""

    architect_database_path: str = "data/architect.sqlite3"
    # Shared secret the (future) orchestrator sends as `X-API-Key`. Empty = auth disabled (dev only).
    architect_api_key: str = ""

    # --- document parameters ---
    platform_name: str = "Flächenpool-Plattform"
    pool_jurisdiction_city: str = "Berlin"
    pool_exclusivity_months: int = 24
    pool_term_years: int = 30


@lru_cache
def get_architect_settings() -> ArchitectSettings:
    return ArchitectSettings()
