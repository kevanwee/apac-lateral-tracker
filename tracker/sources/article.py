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

# Trailing site furniture that survives tag stripping.
_TAIL_MARKERS = [
    "related stories", "most read", "subscribe", "newsletter",
    "share this article", "copyright", "terms of use", "privacy policy",
    "follow us", "read next", "recommended",
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

    match = _BYLINE.search(text)
    body = text[match.end():] if match else text

    lowered = body.lower()
    cut = len(body)
    for marker in _TAIL_MARKERS:
        found = lowered.find(marker)
        # Only trust a marker that appears after some real content.
        if found > MIN_USEFUL_LENGTH:
            cut = min(cut, found)
    body = body[:cut].strip()

    if len(body) < MIN_USEFUL_LENGTH:
        return None
    return body[:BODY_CHAR_LIMIT]
