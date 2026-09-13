"""Paginated feed adapter — the same feed, walked backwards through history.

A live RSS feed shows a rolling window: ten items on a firm newsroom, a few
weeks of coverage. That is fine for a daily run and useless for a backfill.

Many feeds (every WordPress site, which is most firm newsrooms) accept a page
parameter and will serve the same feed shifted back in time. Rajah & Tann's
reaches April 2020 at page 15 — about six years of tier-1 announcements, with
the summaries intact rather than reconstructed.

Paging stops at the first of:
  - an empty page, meaning the archive has run out
  - a page whose newest item predates `since`
  - `max_pages`, so a misconfigured source cannot walk a site forever

Every page is one request, so a 15-page walk costs 150 seconds at the 10s
floor. That is the intended cost of being polite during a backfill.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import feedparser

from tracker.net.client import PoliteClient
from tracker.sources.base import RawItem, SourceConfig
from tracker.sources.feed import FeedAdapter

log = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 20


@dataclass
class PaginatedFeedAdapter(FeedAdapter):
    """A FeedAdapter that can walk back through a feed's archive.

    Without `since` it behaves exactly like its parent: one request, page one.
    Pagination is a backfill path, not the daily path.
    """

    config: SourceConfig
    client: PoliteClient

    @property
    def _page_param(self) -> str:
        return self.config.options.get("page_param", "paged")

    @property
    def _max_pages(self) -> int:
        return int(self.config.options.get("max_pages", DEFAULT_MAX_PAGES))

    def fetch(self, *, since: datetime | None = None) -> Iterable[RawItem]:
        if since is None:
            return super().fetch()
        return list(self._walk_back(since))

    def _walk_back(self, since: datetime) -> Iterable[RawItem]:
        seen: set[str] = set()

        for page in range(1, self._max_pages + 1):
            url = self._page_url(page)
            try:
                result = self.client.fetch(url)
            except Exception as exc:  # noqa: BLE001 - a short archive is not a failure
                log.info("%s: stopping at page %d (%s)", self.config.slug, page, exc)
                return

            parsed = feedparser.parse(result.text)
            if not parsed.entries:
                log.info("%s: archive ends at page %d", self.config.slug, page)
                return

            newest_on_page: datetime | None = None
            page_items: list[RawItem] = []

            for entry in parsed.entries:
                item = self._to_raw_item(entry, fetched_at=result.fetched_at)
                if item is None or item.url in seen:
                    continue
                seen.add(item.url)
                if newest_on_page is None or item.published_at > newest_on_page:
                    newest_on_page = item.published_at
                if item.published_at >= since:
                    page_items.append(item)

            yield from page_items

            # Everything on this page predates the window, so everything on the
            # next page does too. Feeds are ordered; trust that and stop.
            if newest_on_page is not None and newest_on_page < since:
                log.info(
                    "%s: reached %s at page %d, before the %s window",
                    self.config.slug,
                    newest_on_page.date(),
                    page,
                    since.date(),
                )
                return

    def _page_url(self, page: int) -> str:
        base = self.config.feed_url
        if page == 1:
            return base
        joiner = "&" if "?" in base else "?"
        return f"{base}{joiner}{self._page_param}={page}"
