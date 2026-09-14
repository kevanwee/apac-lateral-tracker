"""Article body text — the fix for headlines that do not name anyone.

## Why this exists

Measured over 698 gate-passed Australasian Lawyer headlines, the rules produced
20 records. The dominant reason was not a template gap: **the headline usually
does not contain the person's name.** The outlet's house style is
"Holding Redlich welcomes IP partner" or "Bartier Perry brings in first chief
transformation officer, appoints four partners". No name, so nothing to extract,
however good the templates are. That is an information ceiling, not a parsing
one.

The names are one level down. The same Bartier Perry article's body reads:

    "...brought in its first chief transformation officer in Roger Habib...
     The firm further strengthened its leadership with Alison Cui, Kate Ralph,
     Raffael Maestri and Mario Rashid-Ring becoming the firm's newest partners.
     Cui is a lateral hire from HNT Legal..."

Five named partners from a headline that named none.

## What this module does and does not do

It converts one already-fetched HTML page into plain text. It does **not** fetch
anything — fetching stays in `PoliteClient`, under the same robots checks and
the same 10s floor.

Article text remains transient. It is held for one extraction call and
discarded; nothing here writes to the database, and `raw_items` still has no
column for it (Phase 0, section 2).

Reading article pages is gated on `sources.html_access_reviewed_at` being set,
which is a dated human decision, not something an adapter turns on for itself.
"""

from __future__ import annotations

import re

# Whole elements whose text is never article content.
_STRIP_ELEMENTS = re.compile(
    r"<(script|style|nav|header|footer|aside|form|noscript|svg|iframe)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

# Most outlets put a byline immediately before the body. Anything above it is
# navigation, section menus and the site's own chrome.
_BYLINE = re.compile(
    r"By\s+[A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){0,2}\s+\d{1,2}\s+\w{3,9}\s+\d{4}",
)

# Trailing site furniture that survives tag stripping. Everything below the
# first of these is other articles' text, and the names in it belong to other
# articles — the observed failure was one person appearing on five unrelated
# headlines because the related-article rail was being read as article body.
_TAIL_MARKERS = [
    "related stories", "related articles", "most read", "most popular",
    "subscribe", "newsletter", "share this article", "copyright",
    "terms of use", "privacy policy", "follow us", "read next",
    "recommended", "deal highlights", "you might also like",
    "more from", "latest news", "latest articles", "editor's picks",
    "sign up", "all rights reserved", "leave a reply", "comments",
]

# Enough for extraction; a whole page of boilerplate is not more evidence.
BODY_CHAR_LIMIT = 4000
MIN_USEFUL_LENGTH = 120


def _unescape(text: str) -> str:
    import html

    return html.unescape(text)


def to_text(html_source: str) -> str:
    """Strip a page down to readable text. Layout-agnostic on purpose."""
    cleaned = _COMMENTS.sub(" ", html_source)
    cleaned = _STRIP_ELEMENTS.sub(" ", cleaned)
    # Preserve block boundaries so sentences do not run together.
    cleaned = re.sub(r"</(p|div|li|h[1-6]|br)\s*>", " \n ", cleaned, flags=re.IGNORECASE)
    text = _TAGS.sub(" ", cleaned)
    return _WS.sub(" ", _unescape(text)).strip()


def body_of(html_source: str) -> str | None:
    """The article body, or None if the page yielded nothing usable.

    Deliberately simple: find the byline and take what follows, cut at the
    first piece of trailing furniture. Site-specific selectors would break the
    moment an outlet redesigns, and a wrong body is only a confidence penalty,
    not a wrong record — span verification still has to find every value in
    whatever text this returns.
    """
    text = to_text(html_source)
    if not text:
        return None

    # Site chrome sits above the article and repeats the headline, so cutting
    # at the *last* occurrence of the headline drops the navigation without
    # needing a per-site selector.
    headline = title_of(html_source)
    if headline:
        at = text.rfind(headline)
        if at > 0:
            text = text[at + len(headline):]

    match = _BYLINE.search(text)
    body = text[match.end():] if match else text

    body = trim_tail(body)

    if len(body) < MIN_USEFUL_LENGTH:
        return None
    return body[:BODY_CHAR_LIMIT]


def trim_tail(body: str) -> str:
    """Cut trailing page furniture: related-article rails, footers, promos.

    Separate from `body_of` because it has to be re-appliable to text that has
    already been through it. The article cache stores the *parsed* body, so a
    cache hit skips `body_of` entirely and text parsed by an older version of
    this file is replayed forever. That is not hypothetical: 129 of 1,586
    cached bodies still carried a "RELATED ARTICLES / MORE FROM AUTHOR" rail
    after these markers were added, 286,664 characters of other articles'
    headlines that the extractor read as body prose. Eleven stored moves named
    a partner who appears nowhere but in that rail -- including one person
    attached to four unrelated articles.

    Cutting is idempotent: running it on already-cut text finds no marker and
    changes nothing, so the cache can be repaired on read without refetching
    1,586 pages at the 10 s-per-origin floor.
    """
    lowered = body.lower()
    cut = len(body)
    for marker in _TAIL_MARKERS:
        found = lowered.find(marker)
        # Only trust a marker that appears after some real content.
        if found > MIN_USEFUL_LENGTH:
            cut = min(cut, found)
    return body[:cut].strip()


# ---------------------------------------------------------------------------
# The real headline
# ---------------------------------------------------------------------------
# A slug-derived headline is a reconstruction, and for some outlets a poor one.
# Asia Business Law Journal slugs entities rather than the headline, so
#
#     law.asia/kennedys-hong-kong-andrew-carpenter/
#
# rebuilds as "Kennedys hong kong andrew carpenter" — no verb, nothing a
# template can match. The page itself carries what the outlet actually
# published:
#
#     "Kennedys lands corporate partner from RPC in Hong Kong"
#
# which names the verb, both firms and the market. Once the article has been
# fetched, that is strictly better evidence than the slug, and the headline is
# something `raw_items` stores anyway.

_H1 = re.compile(r"<h1\b[^>]*>(?P<text>.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TITLE_TAG = re.compile(r"<title\b[^>]*>(?P<text>.*?)</title>", re.IGNORECASE | re.DOTALL)
# Outlets append their own name: "Headline | Law.asia", "Headline - The Lawyer".
_SITE_SUFFIX = re.compile(r"\s*[|\u2013\u2014-]\s*[^|\u2013\u2014-]{1,40}$")

MAX_HEADLINE_WORDS = 30


def title_of(html_source: str) -> str | None:
    """The headline the outlet actually published, or None.

    Prefers <h1> over <title>: <title> carries the site name and sometimes a
    section, while <h1> is usually the headline alone.
    """
    for pattern, strip_suffix in ((_H1, False), (_TITLE_TAG, True)):
        match = pattern.search(html_source)
        if not match:
            continue
        text = _WS.sub(" ", _unescape(_TAGS.sub(" ", match.group("text")))).strip()
        text = text.lstrip("\ufeff").strip()
        if strip_suffix:
            text = _SITE_SUFFIX.sub("", text).strip()
        # A nav-only <h1> or an empty title is worse than the slug we have.
        words = text.split()
        if 3 <= len(words) <= MAX_HEADLINE_WORDS:
            return text
    return None
