"""Preflight checks — what is required, what is missing, and what is optional.

Answers "can this actually run right now", without running it. Every check is
cheap and offline: no network, no database writes, no model calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Level = Literal["ok", "optional", "blocker"]


@dataclass(frozen=True)
class Check:
    level: Level
    message: str
    remedy: str | None = None


def run_checks() -> list[Check]:
    checks: list[Check] = []

    # --- configuration ----------------------------------------------------
    try:
        from tracker.config import Config

        Config.load()
        checks.append(Check("ok", "Configuration loads; User-Agent carries a contact address"))
    except Exception as exc:  # noqa: BLE001
        checks.append(
            Check(
                "blocker",
                f"Configuration will not load: {exc}",
                "Set TRACKER_USER_AGENT; see docs/SETUP.md step 2.",
            )
        )

    checks.append(
        Check("ok", ".env present")
        if Path(".env").exists()
        else Check(
            "optional",
            ".env absent, so environment variables must be exported each run",
            "cp .env.example .env and fill it in.",
        )
    )

    # --- database ---------------------------------------------------------
    if os.environ.get("DATABASE_URL"):
        checks.append(Check("ok", "DATABASE_URL is set"))
    else:
        checks.append(
            Check(
                "blocker",
                "DATABASE_URL is not set, so db migrate / backfill / extract cannot run",
                "Create a free Neon Postgres and set it; see docs/SETUP.md step 1. "
                "Until then, `tracker collect` runs the same pipeline into a JSON file.",
            )
        )

    # --- schema -----------------------------------------------------------
    try:
        from tracker import migrate

        found = migrate.discover()
        versions = [m.version for m in found]
        if versions != list(range(1, len(found) + 1)):
            checks.append(
                Check("blocker", f"Migration versions are not contiguous: {versions}")
            )
        else:
            checks.append(
                Check("ok", f"{len(found)} migrations, contiguous to {versions[-1]:04d}")
            )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("blocker", f"Migrations unreadable: {exc}"))

    # --- sources and firms ------------------------------------------------
    try:
        from tracker.firms import FirmGazetteer
        from tracker.sources import registry

        sources = registry.load()
        gazetteer = FirmGazetteer.load()
        live = [s for s in sources if s.collectable]
        blocked = [s for s in sources if not s.collectable]

        if live:
            checks.append(
                Check("ok", f"{len(live)} collectable sources, {len(blocked)} registered but off")
            )
        else:
            checks.append(Check("blocker", "No collectable sources in config/sources.yaml"))

        if len(gazetteer) >= 50:
            checks.append(Check("ok", f"{len(gazetteer)} firms in the gazetteer"))
        else:
            checks.append(
                Check(
                    "optional",
                    f"Only {len(gazetteer)} firms in the gazetteer; yield will suffer",
                )
            )

        reviewed = [s.config.slug for s in sources if s.config.html_access_reviewed_at]
        if reviewed:
            checks.append(Check("ok", f"Article bodies enabled for: {', '.join(reviewed)}"))
        else:
            checks.append(
                Check(
                    "optional",
                    "No source has a dated terms review, so extraction sees headlines only",
                    "Most trade press headlines name nobody. Review an outlet's terms, "
                    "then set html_access_reviewed_at in config/sources.yaml.",
                )
            )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("blocker", f"Source register or gazetteer unreadable: {exc}"))

    # --- extractors -------------------------------------------------------
    try:
        from tracker.extract.rules import RuleExtractor
        from tracker.firms import FirmGazetteer

        RuleExtractor(gazetteer=FirmGazetteer.load())
        checks.append(Check("ok", "Rule extractor ready — free, no API key required"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("blocker", f"Rule extractor will not build: {exc}"))

    checks.append(
        Check("ok", "ANTHROPIC_API_KEY is set; --extractor llm and auto are available")
        if os.environ.get("ANTHROPIC_API_KEY")
        else Check(
            "optional",
            "ANTHROPIC_API_KEY unset, so only --extractor rules works",
            "That is the free path and it is the default. Nothing to do unless you "
            "want the model to handle what the rules abstain on.",
        )
    )

    checks.append(
        Check("ok", "IMAP credentials set")
        if os.environ.get("IMAP_USERNAME") and os.environ.get("IMAP_PASSWORD")
        else Check(
            "optional",
            "IMAP credentials unset, so the ALB newsletter source stays off",
            "See docs/SETUP.md step 6.",
        )
    )

    return checks


def blockers(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.level == "blocker"]
