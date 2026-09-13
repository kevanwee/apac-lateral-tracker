"""Source register: YAML in, adapters out.

config/sources.yaml is the only place an outlet or a region is declared.
`tracker sources sync` reconciles it into the `sources` table; nothing creates
a source row by hand.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from tracker.net.client import PoliteClient
from tracker.sources.base import SourceAdapter, SourceConfig
from tracker.sources.feed import FeedAdapter
from tracker.sources.imap_adapter import ImapAdapter
from tracker.sources.paginated_feed import PaginatedFeedAdapter
from tracker.sources.sitemap import SitemapAdapter

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "sources.yaml"

AdapterFactory = Callable[[SourceConfig, PoliteClient], SourceAdapter]

def _imap(config: SourceConfig, _client: PoliteClient) -> SourceAdapter:
    """IMAP takes credentials rather than the HTTP client."""
    import os

    username = os.environ.get("IMAP_USERNAME", "").strip()
    password = os.environ.get("IMAP_PASSWORD", "").strip()
    if not username or not password:
        raise RegistryError(
            f"{config.slug}: IMAP_USERNAME and IMAP_PASSWORD must be set. "
            f"Use an app-specific password; see .env.example."
        )
    return ImapAdapter(config=config, username=username, password=password)


# An outlet gets a bespoke adapter only when the generic one cannot read it.
#   feed            a live RSS/Atom window — the daily path
#   paginated_feed  the same feed walked back through its archive — backfill
#   sitemap         URL-level enumeration of an archive, headline_only
#   imap            newsletters delivered to a mailbox we control
ADAPTERS: dict[str, AdapterFactory] = {
    "feed": lambda config, client: FeedAdapter(config=config, client=client),
    "paginated_feed": lambda config, client: PaginatedFeedAdapter(
        config=config, client=client
    ),
    "sitemap": lambda config, client: SitemapAdapter(config=config, client=client),
    "imap": _imap,
}

# Adapters that read an archive rather than a live window. Only these are worth
# running during a backfill; the rest just re-read the same recent items.
BACKFILL_ADAPTERS = {"paginated_feed", "sitemap", "imap"}


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class RegisteredSource:
    config: SourceConfig
    blocked_reason: str | None
    notes: str | None

    @property
    def collectable(self) -> bool:
        return self.config.active and self.blocked_reason is None


def load(path: Path = CONFIG_PATH) -> list[RegisteredSource]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "sources" not in raw:
        raise RegistryError(f"{path} has no `sources` key")

    seen: set[str] = set()
    registered: list[RegisteredSource] = []

    for entry in raw["sources"]:
        slug = entry.get("slug")
        if not slug:
            raise RegistryError("every source needs a slug")
        if slug in seen:
            raise RegistryError(f"duplicate source slug: {slug}")
        seen.add(slug)

        adapter = entry.get("adapter", "feed")
        if adapter not in ADAPTERS:
            raise RegistryError(f"{slug}: unknown adapter {adapter!r}")

        tier = entry.get("reliability_tier")
        if tier not in (1, 2, 3):
            raise RegistryError(f"{slug}: reliability_tier must be 1, 2 or 3")
        # Mirrors the database constraint, so a bad register fails at load
        # rather than at INSERT.
        if tier == 1 and not entry.get("firm_name"):
            raise RegistryError(f"{slug}: tier 1 is reserved for a firm's own newsroom")

        access_type = entry.get("access_type", "feed")
        # Only the adapters that actually read a feed need a feed URL. A
        # sitemap or mailbox source is still access_type feed — it reads what
        # the outlet publishes for machines — but has no feed of its own.
        if adapter in {"feed", "paginated_feed"} and not entry.get("feed_url"):
            raise RegistryError(f"{slug}: the {adapter} adapter needs a feed_url")
        # Phase 0: HTML collection needs a dated terms review before it runs.
        if (
            access_type == "html"
            and entry.get("active", True)
            and not entry.get("html_access_reviewed_at")
        ):
            raise RegistryError(
                f"{slug}: an active html source needs html_access_reviewed_at "
                f"and html_access_reviewed_by"
            )

        registered.append(
            RegisteredSource(
                config=SourceConfig(
                    slug=slug,
                    name=entry["name"],
                    base_url=entry["base_url"],
                    feed_url=entry.get("feed_url"),
                    access_type=access_type,
                    reliability_tier=tier,
                    adapter=adapter,
                    jurisdiction_focus=tuple(entry.get("jurisdiction_focus", [])),
                    default_access_level=entry.get("access_level", "summary"),
                    firm_name=entry.get("firm_name"),
                    poll_interval_hours=entry.get("poll_interval_hours", 6),
                    active=entry.get("active", True),
                    terms_url=entry.get("terms_url"),
                    terms_note=entry.get("terms_note"),
                    html_access_reviewed_at=entry.get("html_access_reviewed_at"),
                    html_access_reviewed_by=entry.get("html_access_reviewed_by"),
                    options=entry.get("options", {}),
                ),
                blocked_reason=entry.get("blocked_reason"),
                notes=entry.get("notes"),
            )
        )

    return registered


def build_adapter(source: RegisteredSource, client: PoliteClient) -> SourceAdapter:
    return ADAPTERS[source.config.adapter](source.config, client)
