"""Trailing page furniture, and the cache that preserved it.

`body_of` has cut related-article rails since the markers were added. What it
could not do is reach text that was already in `article_cache.json`: the cache
stores the parsed body, so a cache hit skips parsing altogether and replays
whatever the parser did on the day the entry was written.

Measured on the stored corpus before this was fixed: 129 of 1,586 cached
bodies still carried a rail, 286,664 characters of other articles' headlines
read as body prose, and 11 of 480 canonical moves named a partner who appears
nowhere in the article except in that rail.
"""

from __future__ import annotations

from tracker.sources.article import MIN_USEFUL_LENGTH, trim_tail

# The real rail, from https://law.asia/han-kun-law-offices-cherry-jin-china/.
# Everything after "RELATED ARTICLES" is a list of other articles' headlines;
# the extractor read the last of them as a movement sentence and attached
# Li Laixiang to four unrelated articles.
LEDE = (
    "Han Kun Law Offices has hired Cherry Jin as a partner in its Shanghai "
    "office, strengthening the firm's capital markets practice. Jin advises "
    "issuers and underwriters on domestic and cross-border offerings, and "
    "joins from a leading PRC firm where she spent nine years advising on "
    "listings and regulatory matters for technology and healthcare clients. "
)
RAIL = (
    "RELATED ARTICLES MORE FROM AUTHOR Market pulse Han Kun adds dispute "
    "resolution partner in Shanghai Market pulse Han Kun officially opens "
    "Hangzhou office, cements presence Market pulse Commercial technology "
    "partner Li Laixiang rejoins Han Kun MOST POPULAR In-house Counsel "
    "Awards 2026"
)


def test_a_related_article_rail_is_cut_from_the_body():
    got = trim_tail(LEDE + RAIL)
    assert "Cherry Jin" in got
    assert "Li Laixiang" not in got
    assert "RELATED ARTICLES" not in got


def test_trimming_is_idempotent():
    """The cache repair re-applies this to text that may already be cut.

    If it were not idempotent, repairing the cache would keep eating the body
    a little more on every extraction run.
    """
    once = trim_tail(LEDE + RAIL)
    assert trim_tail(once) == once
    assert trim_tail(once) == LEDE.strip()


def test_a_marker_inside_the_opening_words_is_not_trusted():
    """A body that merely mentions 'comments' early on is not all furniture."""
    body = "Comments on the deal were sought. " + LEDE
    assert trim_tail(body) == body.strip()


def test_a_marker_is_only_trusted_after_real_content():
    body = "Related articles " + LEDE
    assert len(body) > MIN_USEFUL_LENGTH
    assert trim_tail(body) == body.strip()


def test_text_with_no_furniture_is_returned_unchanged():
    assert trim_tail(LEDE) == LEDE.strip()
