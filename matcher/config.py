"""Agent 2 (Matcher) settings, read from environment / .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class MatcherSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Agent 1 (validator) — called over HTTP so it can later move behind the orchestrator.
    validator_url: str = "http://localhost:8000"
    validator_poll_timeout_s: float = 120.0
    # Agent 1's INTERNAL_API_KEY. Set = unpaid POST /internal/audit (works with BYPASS_PAYMENT=false);
    # empty = public /start_job, which only completes while Agent 1 runs with BYPASS_PAYMENT=true.
    validator_api_key: str = ""

    matcher_database_path: str = "data/matcher.sqlite3"
    match_radius_m: float = 1500.0
    # Parcels below this verdict never enter the pool.
    rejected_verdicts: tuple[str, ...] = ("NO_GO",)
    max_parcels_per_developer: int = 50  # limits "probe parcels" used to scan for neighbours
    min_area_ha: float = 0.5
    max_area_ha: float = 1000.0
    duplicate_overlap_ratio: float = 0.9

    # Shared secret the (future) orchestrator sends as `X-API-Key`. Empty = auth disabled (dev only).
    matcher_api_key: str = ""


@lru_cache
def get_matcher_settings() -> MatcherSettings:
    return MatcherSettings()
