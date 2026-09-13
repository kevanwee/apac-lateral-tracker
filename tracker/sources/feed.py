"""Generic RSS/Atom adapter.

Covers every seeded source. An outlet needs its own adapter only when its feed
is malformed in a way feedparser cannot absorb, or when it publishes no feed at
all — and in that second case it needs a terms review before it gets one.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import feedparser

from tracker.net.client import PoliteClient
from tracker.sources.base import RawItem, SourceConfig

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Feed summaries are frequently a full article dump. We only need enough to
# extract from, and holding more than necessary in memory is pointless.
SUMMARY_CHAR_LIMIT = 4000


def strip_html(raw: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", raw)).strip()


@dataclass
class FeedAdapter:
    config: SourceConfig
    client: PoliteClient

    def fetch(self, *, since: datetime | None = None) -> Iterable[RawItem]:
        if not self.config.feed_url:
            raise ValueError(f"{self.config.slug}: feed adapter needs a feed_url")

        result = self.client.fetch(self.config.feed_url)
        parsed = feedparser.parse(result.text)

        if parsed.bozo and not parsed.entries:
            raise ValueError(
                f"{self.config.slug}: feed did not parse ({parsed.bozo_exception})"
            )

        items: list[RawItem] = []
        for entry in parsed.entries:
            item = self._to_raw_item(entry, fetched_at=result.fetched_at)
            if item is None:
                continue
            if since is not None and item.published_at < since:
                continue
            items.append(item)
        return items

    # -- internals ---------------------------------------------------------

    def _to_raw_item(self, entry, *, fetched_at: datetime) -> RawItem | None:
        url = (entry.get("link") or "").strip()
        headline = strip_html(entry.get("title") or "")
        if not url or not headline:
            log.debug("%s: skipping entry without link or title", self.config.slug)
            return None

        published_at, estimated = self._published_at(entry, fetched_at)

        # Paywalled outlets expose a headline and nothing usable. Taking the
        # feed's teaser as if it were the article would overstate what we know.
        if self.config.default_access_level == "headline_only":  # noqa: SIM108
            body = None
        else:
            body = self._summary(entry)

        return RawItem(
            source_slug=self.config.slug,
            url=url,
            headline=headline,
            published_at=published_at,
            published_at_is_estimated=estimated,
            access_level=self.config.default_access_level,
            body_text=body,
        )

    @staticmethod
    def _published_at(entry, fetched_at: datetime) -> tuple[datetime, bool]:
        for key in ("published_parsed", "updated_parsed"):
            parsed = entry.get(key)
            if parsed:
                return datetime(*parsed[:6], tzinfo=UTC), False
        # announced_date is NOT NULL downstream, so a feed without a date gets
        # the fetch time and a flag saying so, rather than being dropped.
        return fetched_at, True

    @staticmethod
    def _summary(entry) -> str | None:
        candidates: list[str] = []
        for content in entry.get("content") or []:
            if content.get("value"):
                candidates.append(content["value"])
        for key in ("summary", "description"):
            if entry.get(key):
                candidates.append(entry[key])

        for raw in candidates:
            text = strip_html(raw)
            if text:
                return text[:SUMMARY_CHAR_LIMIT]
        return None
