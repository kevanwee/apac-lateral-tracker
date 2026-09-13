"""Runtime configuration, read from the environment.

Nothing here reads a secret from disk outside of a developer's local .env.
In deployment these come from GitHub Actions secrets and Vercel env vars.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv(override=False)

# Phase 0: hard floor between requests to the same source. A source may be
# polled more slowly than this, never faster.
MIN_REQUEST_INTERVAL_SECONDS = 10


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in, "
            f"or set it in the deployment environment."
        )
    return value


@dataclass(frozen=True)
class Config:
    database_url: str
    database_url_direct: str
    user_agent: str
    min_request_interval_seconds: int
    llm_cost_ceiling_usd_per_run: float
    extraction_model: str

    @classmethod
    def load(cls) -> Config:
        database_url = _require("DATABASE_URL")
        user_agent = _require("TRACKER_USER_AGENT")

        # Phase 0: the User-Agent must carry a way to reach us. A UA without a
        # contact address is not a descriptive UA, so refuse to start.
        if "contact:" not in user_agent and "@" not in user_agent:
            raise ConfigError(
                "TRACKER_USER_AGENT must include a contact address "
                "(see .env.example for the expected form)."
            )

        interval = int(
            os.environ.get(
                "TRACKER_MIN_REQUEST_INTERVAL_SECONDS", MIN_REQUEST_INTERVAL_SECONDS
            )
        )
        if interval < MIN_REQUEST_INTERVAL_SECONDS:
            raise ConfigError(
                f"TRACKER_MIN_REQUEST_INTERVAL_SECONDS may not be set below the "
                f"{MIN_REQUEST_INTERVAL_SECONDS}s floor."
            )

        return cls(
            database_url=database_url,
            database_url_direct=os.environ.get("DATABASE_URL_DIRECT") or database_url,
            user_agent=user_agent,
            min_request_interval_seconds=interval,
            llm_cost_ceiling_usd_per_run=float(
                os.environ.get("TRACKER_LLM_COST_CEILING_USD_PER_RUN", "5.00")
            ),
            extraction_model=os.environ.get(
                "TRACKER_EXTRACTION_MODEL", "claude-sonnet-5"
            ),
        )
