"""Source adapter interface.

One adapter per outlet behind a common interface. An adapter's only job is to
turn whatever the outlet publishes into `RawItem`s; it does no filtering, no
extraction and no database work.

On article text: `RawItem.body_text` is transient. It is never written to the
database (there is no column for it — see docs/constraints.md section 2) and it
exists only so the extract stage can read it in the same process that fetched
it. When extract runs standalone against items ingested earlier, the text is
gone and it re-fetches, politely, or works from the headline alone.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

AccessLevel = Literal["headline_only", "summary", "full_public"]


@dataclass(frozen=True)
class RawItem:
    source_slug: str
    url: str
    headline: str
    published_at: datetime
    access_level: AccessLevel
    published_at_is_estimated: bool = False
    # True when the headline was rebuilt from a URL slug rather than published
    # by the outlet. Lossy: no punctuation, no capitalisation. See sitemap.py.
    headline_is_derived: bool = False
    # Transient. Not persisted, not logged, discarded after extraction.
    body_text: str | None = field(default=None, repr=False)

    @property
    def content_hash(self) -> bytes:
        """Change detection over everything the outlet gave us."""
        payload = f"{self.headline}\x00{self.body_text or ''}"
        return hashlib.sha256(payload.encode("utf-8")).digest()

    @property
    def extraction_text(self) -> str:
        """Headline plus available summary — the only text extraction ever sees.

        Character spans recorded as provenance point into this string, so it
        must be built the same way every time.
        """
        if not self.body_text:
            return self.headline
        return f"{self.headline}\n\n{self.body_text}"

    def without_text(self) -> RawItem:
        """A copy safe to hold past the extraction call."""
        return RawItem(
            source_slug=self.source_slug,
            url=self.url,
            headline=self.headline,
            published_at=self.published_at,
            access_level=self.access_level,
            published_at_is_estimated=self.published_at_is_estimated,
            headline_is_derived=self.headline_is_derived,
            body_text=None,
        )


@dataclass(frozen=True)
class SourceConfig:
    """One row of sources/registry.yaml, as loaded."""

    slug: str
    name: str
    base_url: str
    access_type: Literal["feed", "html", "api"]
    reliability_tier: int
    adapter: str
    feed_url: str | None = None
    jurisdiction_focus: tuple[str, ...] = ()
    default_access_level: AccessLevel = "summary"
    firm_name: str | None = None
    poll_interval_hours: int = 6
    active: bool = True
    terms_url: str | None = None
    terms_note: str | None = None
    html_access_reviewed_at: str | None = None
    html_access_reviewed_by: str | None = None
    # Adapter-specific settings, e.g. a firm newsroom's item selector.
    options: dict = field(default_factory=dict)


class SourceAdapter(Protocol):
    """fetch() -> list[RawItem]. That is the whole contract."""

    config: SourceConfig

    def fetch(self, *, since: datetime | None = None) -> Iterable[RawItem]: ...
