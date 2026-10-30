"""Agent 3 (Escrow & NDA) settings, read from environment / .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class EscrowSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Agent 2 — source of the un-anonymised match data (needs MATCHER_API_KEY on both sides).
    matcher_url: str = "http://localhost:8001"
    matcher_api_key: str = ""

    escrow_database_path: str = "data/escrow.sqlite3"
    # Shared secret the (future) orchestrator sends as `X-API-Key`. Empty = auth disabled (dev only).
    escrow_api_key: str = ""

    # --- Masumi: each developer buys the "reveal" service from this agent (one payment per side) ---
    escrow_bypass_payment: bool = True  # true = deposits are treated as locked immediately (dev/testing)
    escrow_agent_identifier: str = ""  # identifier of THIS agent after registering it in Masumi
    masumi_network: str = "Preprod"
    masumi_payment_service_url: str = "http://localhost:3001"
    masumi_api_key: str = ""
    deposit_tada: float = 10.0  # informational; the price lives in the agent's registry entry

    # --- process deadlines (hours) ---
    consent_window_h: int = 168
    deposit_window_h: int = 48
    signature_window_h: int = 168

    # --- NDA template parameters ---
    platform_name: str = "Flächenpool-Plattform"
    nda_jurisdiction_city: str = "Berlin"
    nda_term_years: int = 2
    nda_aftereffect_years: int = 5
    nda_non_circumvention_months: int = 24
    nda_max_penalty_eur: int = 50_000


@lru_cache
def get_escrow_settings() -> EscrowSettings:
    return EscrowSettings()
