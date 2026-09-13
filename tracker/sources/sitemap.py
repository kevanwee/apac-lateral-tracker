"""Sitemap adapter — historical enumeration without touching an article page.

A sitemap is the index a site publishes *for* machines. It gives a URL and a
date per article, and for most news sites the URL slug is the headline with the
punctuation stripped:

    .../full-federal-court-rejects-ex-directors-bid-for-total-tools-shares
 -> "Full federal court rejects ex directors bid for Total Tools shares"

That is enough to run the relevance gate over years of archive for the cost of
a few dozen requests, instead of one request per article. On the sampled
sources it reaches 2019.

## What this adapter does not pretend

A de-slugified headline is lossy: no capitalisation, no apostrophes, no
punctuation, and some sites append an ID. Items from here are therefore marked
`headline_is_derived` and their access level is `headline_only`, which carries
the confidence penalty and never auto-accepts. That is the correct handling —
it is a real signal that a move happened, and thin evidence for what happened.

To turn one into a full record, the article page has to be read, and that needs
a dated terms review on the source first (`html_access_reviewed_at`). This
adapter deliberately does not fetch article pages.

## lastmod is not a publication date

Some sitemaps regenerate `lastmod` in bulk — Global Legal Post stamps thousands
of URLs with one timestamp. Where that is detected, the date is marked
estimated rather than being quietly believed. Year-partitioned sitemaps
(Australasian Lawyer) carry the year in the filename, which is trustworthy.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

from tracker.net.client import PoliteClient
from tracker.sources.base import RawItem, SourceConfig

log = logging.getLogger(__name__)

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Trailing numeric ids many CMSs append to a slug.
_TRAILING_ID = re.compile(r"-\d{4,}$")
# Words that should keep their capitalisation when a slug is rebuilt.
_SLUG_SPLIT = re.compile(r"[-_]+")

# If more than this share of a sitemap's URLs carry one timestamp, lastmod is a
# regeneration artefact rather than a publication date.
_BULK_LASTMOD_SHARE = 0.5


def headline_from_slug(url: str) -> str:
    """Rebuild a readable headline from a URL slug.

    Deliberately conservative: it restores spaces and sentence case and nothing
    else. Guessing at apostrophes or proper nouns would produce a headline the
    outlet never wrote.
    """
    # Parse properly rather than splitting the raw string: otherwise the
    # hostname becomes a candidate slug for a URL that is all ids.
    path = urlparse(url).path.rstrip("/")
    segments = [s for s in path.split("/") if s]

    # Some CMSs put the article id in its own trailing segment
    # (.../total-tools-shares/589190) and others append it to the slug
    # (...-gibraltar-office-381116139). Walk back past the bare-id form.
    while segments and segments[-1].isdigit():
        segments.pop()
    if not segments:
        return ""

    slug = _TRAILING_ID.sub("", segments[-1])
    words = [w for w in _SLUG_SPLIT.split(slug) if w]
    if not words:
        return ""
    text = " ".join(words)
    return text[:1].upper() + text[1:]


@dataclass
class SitemapAdapter:
    """Enumerates a site's archive from its sitemap index.

    options:
      sitemap_url        index or a single sitemap (defaults to /sitemap.xml)
      year_url_template  e.g. "https://host/au/sitemaps/articles/{year}"
      include_pattern    only URLs matching this regex are treated as articles
      max_sitemaps       ceiling on child sitemaps fetched in one run
    """

    config: SourceConfig
    client: PoliteClient

    def fetch(self, *, since: datetime | None = None) -> Iterable[RawItem]:
        opts = self.config.options
        include = re.compile(opts["include_pattern"]) if opts.get("include_pattern") else None
        exclude = re.compile(opts["exclude_pattern"]) if opts.get("exclude_pattern") else None
        items: list[RawItem] = []

        for sitemap_url, known_year in self._sitemaps_to_read(since):
            try:
                result = self.client.fetch(sitemap_url)
            except Exception as exc:  # noqa: BLE001 - a missing year is not a failure
                log.info("%s: %s unavailable (%s)", self.config.slug, sitemap_url, exc)
                continue

            entries = self._parse_urlset(result.text)
            if not entries:
                continue

            bulk = self._lastmod_is_bulk(entries)
            for url, lastmod in entries:
                if include and not include.search(url):
                    continue
                if exclude and exclude.search(url):
                    continue
                published, estimated = self._published(lastmod, known_year, bulk)
                if since is not None and published < since and not estimated:
                    continue
                headline = headline_from_slug(url)
                if not headline:
                    continue
                items.append(
                    RawItem(
                        source_slug=self.config.slug,
                        url=url,
                        headline=headline,
                        published_at=published,
                        published_at_is_estimated=estimated,
                        # A slug is a lead, not an article.
                        access_level="headline_only",
                        headline_is_derived=True,
                        body_text=None,
                    )
                )
        return items

    # -- internals ---------------------------------------------------------

    def _sitemaps_to_read(self, since: datetime | None) -> list[tuple[str, int | None]]:
        opts = self.config.options
        max_sitemaps = int(opts.get("max_sitemaps", 25))

        # Year-partitioned archives are the cleanest case: the year is in the
        # filename, so the window can be honoured without reading anything.
        template = opts.get("year_url_template")
        if template:
            end = datetime.now(UTC).year
            start = since.year if since else end
            years = range(min(start, end), end + 1)
            return [(template.format(year=y), y) for y in years][:max_sitemaps]

        index_url = opts.get("sitemap_url") or f"{self.config.base_url.rstrip('/')}/sitemap.xml"
        try:
            result = self.client.fetch(index_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: sitemap index unreadable (%s)", self.config.slug, exc)
            return []

        children = self._parse_sitemapindex(result.text)
        if not children:
            return [(index_url, None)]  # a flat sitemap, not an index

        pattern = opts.get("child_pattern")
        if pattern:
            children = [c for c in children if re.search(pattern, c)]
        return [(c, None) for c in children[:max_sitemaps]]

    @staticmethod
    def _parse_urlset(xml: str) -> list[tuple[str, str | None]]:
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            return []
        out: list[tuple[str, str | None]] = []
        for url in root.findall("sm:url", NS):
            loc = url.findtext("sm:loc", namespaces=NS)
            if loc:
                out.append((loc.strip(), url.findtext("sm:lastmod", namespaces=NS)))
        return out

    @staticmethod
    def _parse_sitemapindex(xml: str) -> list[str]:
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            return []
        return [
            loc.strip()
            for sm in root.findall("sm:sitemap", NS)
            if (loc := sm.findtext("sm:loc", namespaces=NS))
        ]

    @staticmethod
    def _lastmod_is_bulk(entries: list[tuple[str, str | None]]) -> bool:
        stamps = [lm for _, lm in entries if lm]
        if len(stamps) < 10:
            return False
        most_common = max(stamps.count(s) for s in set(stamps))
        return most_common / len(stamps) > _BULK_LASTMOD_SHARE

    @staticmethod
    def _published(
        lastmod: str | None, known_year: int | None, bulk: bool
    ) -> tuple[datetime, bool]:
        if lastmod and not bulk:
            try:
                parsed = datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed, False
            except ValueError:
                pass
        if known_year:
            # Mid-year, flagged estimated. Good enough to bucket by year, and
            # honest that the day is unknown.
            return datetime(known_year, 7, 1, tzinfo=UTC), True
        return datetime.now(UTC), True
