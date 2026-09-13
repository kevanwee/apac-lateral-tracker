"""Archive adapters: sitemap enumeration and feed pagination.

No network. The XML and URL shapes here are copied from what the live sources
actually served on 2026-09-13, including the one that broke the first
implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tracker.gate import evaluate
from tracker.sources.base import SourceConfig
from tracker.sources.paginated_feed import PaginatedFeedAdapter
from tracker.sources.sitemap import SitemapAdapter, headline_from_slug

# ---------------------------------------------------------------------------
# Headline recovery from URL slugs
# ---------------------------------------------------------------------------


def test_a_slug_becomes_a_readable_headline():
    url = (
        "https://www.globallegalpost.com/news/signature-litigation-hires-seasoned"
        "-litigator-from-triay-lawyers-to-head-up-gibraltar-office-381116139"
    )
    assert headline_from_slug(url) == (
        "Signature litigation hires seasoned litigator from triay lawyers to "
        "head up gibraltar office"
    )


def test_an_id_in_its_own_path_segment_is_not_mistaken_for_the_slug():
    """This broke the first implementation: every headline came out as a number."""
    url = (
        "https://www.thelawyermag.com/au/practice-areas/litigation-dispute-resolution"
        "/full-federal-court-rejects-ex-directors-bid-for-total-tools-shares/589190"
    )
    assert headline_from_slug(url).startswith("Full federal court rejects")


def test_a_url_that_is_only_ids_yields_no_headline():
    assert headline_from_slug("https://example.test/12345/67890") == ""


def test_recovered_headlines_still_clear_the_relevance_gate():
    """De-slugged text loses case and punctuation; the gate must survive that."""
    urls = [
        "https://x.test/au/news/shane-bilardi-steps-up-as-dla-pipers-new-country"
        "-managing-partner-for-australia/1",
        "https://x.test/au/news/seven-new-partners-join-minterellison/2",
        "https://x.test/au/news/herbert-smith-freehills-appoints-nick-baker-as"
        "-managing-partner/3",
    ]
    for url in urls:
        assert evaluate(headline_from_slug(url), reliability_tier=2).passed, url


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

URLSET = """<?xml version="1.0" encoding="utf-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://x.test/au/news/firm-a-hires-partner-jane-doe/101</loc>
       <lastmod>2024-03-04</lastmod></url>
  <url><loc>https://x.test/au/news/firm-b-welcomes-partner-john-roe/102</loc>
       <lastmod>2024-05-21</lastmod></url>
  <url><loc>https://x.test/nz/news/unrelated-court-ruling/103</loc>
       <lastmod>2024-06-01</lastmod></url>
</urlset>
"""


class FakeClient:
    """Records what was requested, so politeness can be asserted."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requested: list[str] = []

    def fetch(self, url: str):
        self.requested.append(url)
        if url not in self.pages:
            raise RuntimeError(f"404 {url}")
        text = self.pages[url]

        class Result:
            pass

        result = Result()
        result.text = text
        result.fetched_at = datetime.now(UTC)
        return result


def make_adapter(pages, **options):
    config = SourceConfig(
        slug="test-archive",
        name="Test archive",
        base_url="https://x.test",
        access_type="feed",
        reliability_tier=2,
        adapter="sitemap",
        default_access_level="headline_only",
        options=options,
    )
    return SitemapAdapter(config=config, client=FakeClient(pages))


# ---------------------------------------------------------------------------
# Sitemap enumeration
# ---------------------------------------------------------------------------


def test_year_partitioned_sitemaps_honour_the_window_without_reading_them():
    adapter = make_adapter(
        {"https://x.test/sitemaps/2026": URLSET},
        year_url_template="https://x.test/sitemaps/{year}",
    )
    adapter.fetch(since=datetime(2026, 1, 1, tzinfo=UTC))
    assert adapter.client.requested == ["https://x.test/sitemaps/2026"]


def test_include_pattern_filters_out_other_sections():
    adapter = make_adapter(
        {"https://x.test/sitemaps/2026": URLSET},
        year_url_template="https://x.test/sitemaps/{year}",
        include_pattern="/au/",
    )
    items = list(adapter.fetch(since=datetime(2024, 1, 1, tzinfo=UTC)))
    assert len(items) == 2
    assert all("/au/" in i.url for i in items)


def test_sitemap_items_are_marked_derived_and_headline_only():
    """They are leads, not evidence, and migration 0009 enforces the pairing."""
    adapter = make_adapter(
        {"https://x.test/sitemaps/2026": URLSET},
        year_url_template="https://x.test/sitemaps/{year}",
    )
    items = list(adapter.fetch(since=datetime(2024, 1, 1, tzinfo=UTC)))
    assert items
    for item in items:
        assert item.headline_is_derived is True
        assert item.access_level == "headline_only"
        assert item.body_text is None


def test_a_missing_year_is_skipped_rather_than_failing_the_run():
    adapter = make_adapter(
        {"https://x.test/sitemaps/2026": URLSET},
        year_url_template="https://x.test/sitemaps/{year}",
    )
    assert list(adapter.fetch(since=datetime(2024, 1, 1, tzinfo=UTC)))


def test_bulk_regenerated_lastmod_is_not_believed_as_a_publication_date():
    """Global Legal Post stamps thousands of URLs with one timestamp."""
    stamped = "".join(
        f"<url><loc>https://x.test/news/firm-hires-partner-number-{n}/{n}</loc>"
        f"<lastmod>2026-09-11T17:50:02+00:00</lastmod></url>"
        for n in range(20)
    )
    xml = (
        '<?xml version="1.0"?><urlset '
        'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{stamped}</urlset>"
    )
    adapter = make_adapter(
        {"https://x.test/sitemap.xml": xml}, sitemap_url="https://x.test/sitemap.xml"
    )
    items = list(adapter.fetch())
    assert items
    assert all(i.published_at_is_estimated for i in items)


def test_varied_lastmod_is_trusted():
    adapter = make_adapter(
        {"https://x.test/sitemap.xml": URLSET}, sitemap_url="https://x.test/sitemap.xml"
    )
    items = list(adapter.fetch())
    assert not any(i.published_at_is_estimated for i in items)
    assert "2024-03-04" in {i.published_at.date().isoformat() for i in items}


def test_a_sitemap_index_is_walked_to_its_matching_children_only():
    index = (
        '<?xml version="1.0"?><sitemapindex '
        'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>https://x.test/sitemap-posts-1.xml</loc></sitemap>"
        "<sitemap><loc>https://x.test/sitemap-pages.xml</loc></sitemap>"
        "</sitemapindex>"
    )
    adapter = make_adapter(
        {
            "https://x.test/sitemap.xml": index,
            "https://x.test/sitemap-posts-1.xml": URLSET,
        },
        sitemap_url="https://x.test/sitemap.xml",
        child_pattern="sitemap-posts",
    )
    assert list(adapter.fetch())
    assert "https://x.test/sitemap-pages.xml" not in adapter.client.requested


def test_malformed_xml_yields_nothing_rather_than_raising():
    adapter = make_adapter(
        {"https://x.test/sitemap.xml": "<not-xml"},
        sitemap_url="https://x.test/sitemap.xml",
    )
    assert list(adapter.fetch()) == []


# ---------------------------------------------------------------------------
# Feed pagination
# ---------------------------------------------------------------------------


def rss(items: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link><pubDate>{d}</pubDate></item>"
        for t, u, d in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'


def make_paginated(pages, **options):
    config = SourceConfig(
        slug="test-feed",
        name="Test feed",
        base_url="https://x.test",
        feed_url="https://x.test/feed",
        access_type="feed",
        reliability_tier=1,
        adapter="paginated_feed",
        default_access_level="full_public",
        options=options,
    )
    return PaginatedFeedAdapter(config=config, client=FakeClient(pages))


PAGES = {
    "https://x.test/feed": rss(
        [("Firm hires partner A", "https://x.test/a", "Mon, 01 Sep 2026 00:00:00 +0000")]
    ),
    "https://x.test/feed?paged=2": rss(
        [("Firm hires partner B", "https://x.test/b", "Sat, 01 Mar 2025 00:00:00 +0000")]
    ),
    "https://x.test/feed?paged=3": rss(
        [("Firm hires partner C", "https://x.test/c", "Wed, 01 Mar 2023 00:00:00 +0000")]
    ),
}


def test_without_a_window_pagination_never_happens():
    """The recurring path must stay one request per source."""
    adapter = make_paginated(PAGES)
    list(adapter.fetch())
    assert adapter.client.requested == ["https://x.test/feed"]


def test_a_window_walks_back_through_the_archive():
    adapter = make_paginated(PAGES)
    items = list(adapter.fetch(since=datetime(2024, 1, 1, tzinfo=UTC)))
    assert {i.url for i in items} == {"https://x.test/a", "https://x.test/b"}


def test_paging_stops_once_a_page_predates_the_window():
    adapter = make_paginated(PAGES)
    list(adapter.fetch(since=datetime(2024, 1, 1, tzinfo=UTC)))
    assert adapter.client.requested == [
        "https://x.test/feed",
        "https://x.test/feed?paged=2",
        "https://x.test/feed?paged=3",
    ]


def test_paging_stops_at_the_end_of_the_archive():
    adapter = make_paginated({"https://x.test/feed": PAGES["https://x.test/feed"]})
    assert len(list(adapter.fetch(since=datetime(2020, 1, 1, tzinfo=UTC)))) == 1


def test_max_pages_bounds_the_walk():
    """A misconfigured source must not be able to walk a site forever."""
    endless = {"https://x.test/feed": PAGES["https://x.test/feed"]}
    for n in range(2, 40):
        endless[f"https://x.test/feed?paged={n}"] = rss(
            [
                (
                    f"Firm hires partner {n}",
                    f"https://x.test/{n}",
                    "Tue, 01 Sep 2026 00:00:00 +0000",
                )
            ]
        )
    adapter = make_paginated(endless, max_pages=5)
    list(adapter.fetch(since=datetime(2020, 1, 1, tzinfo=UTC)))
    assert len(adapter.client.requested) == 5


@pytest.mark.parametrize("page_param", ["paged", "page"])
def test_the_page_parameter_is_configurable(page_param):
    pages = {
        "https://x.test/feed": PAGES["https://x.test/feed"],
        f"https://x.test/feed?{page_param}=2": PAGES["https://x.test/feed?paged=2"],
    }
    adapter = make_paginated(pages, page_param=page_param)
    list(adapter.fetch(since=datetime(2024, 1, 1, tzinfo=UTC)))
    assert f"https://x.test/feed?{page_param}=2" in adapter.client.requested
