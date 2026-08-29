"""
config.py — Central configuration via pydantic-settings.

All values are overridable through environment variables / a .env file so
the judge harness can point the service at whichever LLM provider it wants
without touching code.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    HOST: str = "0.0.0.0"
    PORT: int = 8080
    APP_START_TIME: float = Field(default_factory=lambda: __import__("time").time())

    LLM_PROVIDER: str = "mock"
    LLM_MODEL: str = "llama-3.3-70b-versatile"
    LLM_API_KEY: str = ""
    LLM_TEMPERATURE: float = 0.5
    LLM_TIMEOUT_SECONDS: float = 8.0

    HEALTHZ_BUDGET_S: float = 2.0
    METADATA_BUDGET_S: float = 2.0
    CONTEXT_BUDGET_S: float = 5.0
    TICK_BUDGET_S: float = 10.0
    REPLY_BUDGET_S: float = 10.0

    TEAM_NAME: str = "Team Pratham"
    TEAM_MEMBERS: List[str] = ["Lead Engineer"]
    CONTACT_EMAIL: str = "prathamarora25fbfan@gmail.com"
    APP_VERSION: str = "1.0.0"
    APPROACH_SUMMARY: str = (
        "Dual-scope dispatcher with deterministic unit-economics grounding and category-voice synthesis."
    )

    OPT_OUT_SUPPRESSION_DAYS: int = 30
    CANNED_WAIT_SECONDS: int = 86400


settings = Settings()


CATEGORY_IDS = {"dentists", "salons", "restaurants", "gyms", "pharmacies"}
